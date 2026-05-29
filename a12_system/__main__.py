#!/usr/bin/env python3
"""
A12 System v2 — ESP32 Camera Surveillance System
Entry point: python -m A12_System_v2
"""

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

import cv2
import requests

from .config import load_config
from .logging_setup import setup_logging
from .runtime_config import RuntimeConfig
from .camera import Camera
from .detection import Detector
from .notifier import Notifier
from .mqtt_client import MQTTClient
from .database import EventDB
from .stats import Statistics
from .audio_monitor import AudioMonitor
from .ha_monitor import HAMonitor
from .status_monitor import StatusMonitor
from .pipeline import DetectionPipeline

# Script directory
try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()

DATA_DIR = os.environ.get("A12_DATA_DIR", SCRIPT_DIR)
CONFIG_DIR = os.environ.get("A12_CONFIG_DIR", SCRIPT_DIR)


def _configure_compute_threads() -> int:
    """Keep native libraries from taking all CPU cores on the host."""
    requested = os.environ.get("A12_CV_THREADS", "1")
    try:
        thread_count = max(1, int(requested))
    except ValueError:
        thread_count = 1

    cv2.setNumThreads(thread_count)
    return thread_count


def _startup_message(config: dict, detector: Detector) -> str:
    runtime = os.environ.get("A12_RUNTIME", "local")
    resource_limit = os.environ.get("A12_RESOURCE_LIMIT", "host defaults")
    mode = config.get("security", {}).get("mode", "MONITOR")
    known_faces = len(getattr(detector, "known_face_names", []))
    camera_id = config.get("camera_id", "esp32_cam")
    camera_name = config.get("camera_name", camera_id)
    camera_url = config.get("camera_url", "unknown")
    mqtt_base_topic = config.get("mqtt", {}).get("base_topic", "esp32_camera")
    data_dir = os.environ.get("A12_DATA_DIR", SCRIPT_DIR)

    return (
        "A12 System v2 started\n"
        f"Runtime: {runtime}\n"
        f"Mode: {mode}\n"
        f"Camera: {camera_name} ({camera_id})\n"
        f"Camera URL: {camera_url}\n"
        f"MQTT base topic: {mqtt_base_topic}\n"
        f"Known faces: {known_faces}\n"
        f"Limits: {resource_limit}\n"
        f"Data: {data_dir}"
    )


class Application:
    """Main application orchestrator."""

    def __init__(self):
        self.running = True
        self.runtime_config = None
        self.stats = None
        self.audio_monitor = None
        self.ha_monitor = None
        self.db = None
        self.mqtt_client = None
        self.status_monitor = None
        self.pipeline = None
        self.http_session = requests.Session()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, sig, frame):
        logging.info("Shutdown signal received...")
        self.running = False
        raise KeyboardInterrupt

    def run(self) -> None:
        """Main application loop with auto-restart."""
        os.makedirs(DATA_DIR, exist_ok=True)

        # Load config & setup logging
        initial_config = load_config(CONFIG_DIR)
        setup_logging(initial_config, DATA_DIR)
        self.runtime_config = RuntimeConfig(initial_config)

        logging.info("=" * 50)
        logging.info("A12 System v2 - Detection Pipeline")
        logging.info("=" * 50)
        cv_threads = _configure_compute_threads()
        logging.info(f"OpenCV thread limit: {cv_threads}")

        # Initialize components
        config = self.runtime_config.get_all()
        camera_id = str(config.get("camera_id", "esp32_cam")).strip() or "esp32_cam"
        camera_name = str(config.get("camera_name", camera_id)).strip() or camera_id
        camera_label = str(config.get("telegram", {}).get("camera_label", camera_name)).strip()
        log_prefix = f"[{camera_id}:{camera_name}]"
        logging.info(f"{log_prefix} Camera identity loaded")
        _user = config.get("camera_http_user", "admin")
        _pass = config.get("camera_http_pass", "admin")
        if _user:
            self.http_session.auth = (_user, _pass)
        camera = Camera(config)
        detector = Detector(config, SCRIPT_DIR)
        notifier = Notifier(config)
        self.stats = Statistics(save_path=os.path.join(DATA_DIR, "stats.json"))
        self.db = EventDB(os.path.join(DATA_DIR, "events.db"))

        # Shared state
        force_yolo_event = threading.Event()
        shared_state = {
            "last_frame": time.time(),
            "last_person_activity": 0.0,
            "last_sensor_activity": 0.0,
            "last_event_activity": 0.0,
            "external_yolo_until": 0.0,
            "external_yolo_source": "",
            "recording_buffer_until": 0.0,
            "stream_decode_fps": 0,
            "pipeline_state": "starting",
        }

        # MQTT with callbacks
        def mqtt_callback(event_type, payload):
            if event_type == "zigbee":
                sensor_name = payload.get("name", "unknown")
                data = payload.get("data", {})

                if "occupancy" in data and data["occupancy"]:
                    logging.info(f"Zigbee Motion: {sensor_name} -> Forcing YOLO check!")
                    self.db.log_event("sensor", "motion", 1.0, sensor_name)
                    force_yolo_event.set()
                elif "contact" in data:
                    state = "CLOSED" if data["contact"] else "OPEN"
                    logging.info(f"Zigbee Door: {sensor_name} is {state}")
                    self.db.log_event("sensor", "door", 0.0 if data["contact"] else 1.0, sensor_name)
                    if not data["contact"]:
                        force_yolo_event.set()

            elif event_type == "esp32_motion":
                detected = payload.get("detected", False)
                if detected:
                    now = time.time()
                    shared_state["last_esp32_motion"] = now
                    # last_sensor_activity intentionally NOT set — prevents PIR recording
                    # without YOLO confirmation. ESP32 motion gates YOLO only; physical
                    # PIR/HA sensors remain the sole source for unconditional clip saves.
                    window = float(self.runtime_config.get("external_trigger_yolo_window_seconds", 15.0))
                    was_active = now < float(shared_state.get("external_yolo_until", 0.0))
                    shared_state["external_yolo_until"] = max(
                        float(shared_state.get("external_yolo_until", 0.0)), now + window
                    )
                    shared_state["external_yolo_source"] = "esp32_motion"
                    if not was_active:
                        force_yolo_event.set()
                        logging.info("ESP32 motion: ON")
                    else:
                        logging.debug("ESP32 motion: ON (window active)")

            elif event_type == "esp32_person_uncertain":
                confidence = payload.get("confidence", 0.0)
                now = time.time()
                shared_state["last_esp32_motion"] = now
                window = float(self.runtime_config.get("external_trigger_yolo_window_seconds", 15.0))
                shared_state["external_yolo_until"] = max(
                    float(shared_state.get("external_yolo_until", 0.0)), now + window
                )
                shared_state["external_yolo_source"] = "esp32_uncertain"
                force_yolo_event.set()
                logging.info(f"ESP32 person_uncertain: conf={confidence:.2f} → forcing YOLO")

            elif event_type == "config":
                topic = payload.get("topic", "")
                value = payload.get("payload", "")
                if self.runtime_config.update_from_mqtt(topic, value):
                    mode = self.runtime_config.get("security.mode", "MONITOR")
                    self.mqtt_client.publish("camera/config/status/mode", mode)
                    self.mqtt_client.publish(
                        "camera/config/status/last_update", datetime.now().isoformat()
                    )
                    logging.info(f"Config updated via MQTT: {topic} = {value}")

        self.mqtt_client = MQTTClient(config, callback=mqtt_callback)

        # Status monitor
        self.status_monitor = StatusMonitor(
            self.runtime_config, self.mqtt_client, notifier, self.db,
            shared_state, self.http_session, stats=self.stats
        )
        self.status_monitor.start()

        # Audio monitor
        def audio_callback(rms, msg):
            logging.info(f"{log_prefix} {msg}")
            notifier.send_telegram(f"{camera_label}: {msg}" if camera_label else msg)
            self.mqtt_client.publish("audio/alert", "ON")
            self.db.log_event("audio", "LOUD_NOISE", rms)

        if self.runtime_config.get("audio_enabled", False):
            self.audio_monitor = AudioMonitor(config, callback=audio_callback)
            self.audio_monitor.start()
            logging.info(f"{log_prefix} Audio monitor started")
        else:
            logging.info(f"{log_prefix} Audio monitor disabled")

        # HA monitor
        def ha_sensor_callback(entity_id, sensor_name, old_state, new_state, attributes):
            if new_state == "on":
                window = float(self.runtime_config.get("external_trigger_yolo_window_seconds", 15.0))
                until = time.time() + window
                shared_state["external_yolo_until"] = max(
                    float(shared_state.get("external_yolo_until", 0.0)),
                    until,
                )
                shared_state["external_yolo_source"] = entity_id
                shared_state["last_sensor_activity"] = time.time()
                shared_state["last_event_activity"] = time.time()
                logging.info(
                    f"HA Sensor: {sensor_name} -> YOLO window opened for {window:.1f}s"
                )
                self.db.log_event("ha_sensor", sensor_name, 1.0, entity_id)
                force_yolo_event.set()

        if self.runtime_config.get("home_assistant_token"):
            self.ha_monitor = HAMonitor(config, callback=ha_sensor_callback)
            self.ha_monitor.start()
            logging.info(f"{log_prefix} HA Monitor started")
        else:
            logging.info(f"{log_prefix} HA Monitor disabled (no token)")

        # Exposure reset callback — triggered by brightness watchdog when AEC freezes
        exposure_reset_settings = {
            "aec": 1, "ae_level": 0, "agc": 1, "gainceiling": 4,
        }

        def _camera_exposure_reset():
            camera.set_camera_settings(exposure_reset_settings)
            notifier.send_telegram(
                f"{camera_label}: Camera exposure auto-reset (AEC freeze detected)",
                bypass_cooldown=True,
            )

        # Detection pipeline
        self.pipeline = DetectionPipeline(
            runtime_config=self.runtime_config,
            detector=detector,
            notifier=notifier,
            mqtt_client=self.mqtt_client,
            db=self.db,
            stats=self.stats,
            audio_monitor=self.audio_monitor,
            ha_monitor=self.ha_monitor,
            force_yolo_event=force_yolo_event,
            shared_state=shared_state,
            status_monitor=self.status_monitor,
            script_dir=DATA_DIR,
            camera_reset_fn=_camera_exposure_reset,
        )

        # Initial camera config
        init_settings = self.runtime_config.get("camera_init_settings", {})
        if init_settings:
            camera.set_camera_settings(init_settings)
            time.sleep(2)

        notifier.send_telegram(_startup_message(config, detector), bypass_cooldown=True)

        idle_decode_fps = max(1, int(self.runtime_config.get("stream_idle_decode_fps", 2)))
        active_decode_fps = max(
            idle_decode_fps,
            int(self.runtime_config.get("stream_active_decode_fps", 10)),
        )

        def target_decode_fps():
            now = time.time()
            active_until = max(
                float(shared_state.get("external_yolo_until", 0.0)),
                float(shared_state.get("recording_buffer_until", 0.0)),
            )
            if now < float(shared_state.get("recording_buffer_until", 0.0)):
                shared_state["pipeline_state"] = "recording_buffer"
                shared_state["stream_decode_fps"] = active_decode_fps
                return active_decode_fps
            if force_yolo_event.is_set() or now < float(shared_state.get("external_yolo_until", 0.0)):
                shared_state["pipeline_state"] = "pir_window"
                shared_state["stream_decode_fps"] = active_decode_fps
                return active_decode_fps
            if self.ha_monitor and self.ha_monitor.is_any_sensor_active():
                shared_state["pipeline_state"] = "pir_active"
                shared_state["stream_decode_fps"] = active_decode_fps
                return active_decode_fps
            if time.time() - float(shared_state.get("last_esp32_motion", 0.0)) < 3.0:
                shared_state["pipeline_state"] = "motion"
                shared_state["stream_decode_fps"] = active_decode_fps
                return active_decode_fps
            shared_state["pipeline_state"] = "idle"
            shared_state["stream_decode_fps"] = idle_decode_fps
            return idle_decode_fps

        # Main reconnection loop
        consecutive_failures = 0
        stuck_notified = False
        STUCK_THRESHOLD = 2
        BASE_RETRY_SLEEP = 5
        MAX_RETRY_SLEEP = 60

        while self.running:
            try:
                stream_response = camera.get_stream()
                if stream_response:
                    if stuck_notified:
                        logging.info("ESP32 recovered!")
                        notifier.send_telegram("ESP32 is back online!", bypass_cooldown=True)
                        stuck_notified = False
                        shared_state["send_recovery_snapshot"] = True

                    consecutive_failures = 0
                    camera.process_stream(
                        stream_response,
                        self.pipeline.process_frame,
                        target_fps_getter=target_decode_fps,
                    )
                else:
                    consecutive_failures += 1
                    retry_sleep = min(
                        BASE_RETRY_SLEEP * (2 ** min(consecutive_failures // 5, 4)),
                        MAX_RETRY_SLEEP,
                    )
                    logging.error(
                        f"Stream unavailable ({consecutive_failures} consecutive failures),"
                        f" retrying in {retry_sleep}s"
                    )

                    if consecutive_failures >= STUCK_THRESHOLD and not stuck_notified:
                        notifier.send_telegram("ESP32 not responding (STUCK)!", bypass_cooldown=True)
                        stuck_notified = True

                    time.sleep(retry_sleep)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logging.error(f"Error in main loop: {e}")
                time.sleep(5)

        self._cleanup()

    def _cleanup(self) -> None:
        logging.info("Shutting down...")
        if self.pipeline:
            self.pipeline.stop()
        if self.status_monitor:
            self.status_monitor.stop()
        if self.mqtt_client:
            self.mqtt_client.publish("status", "offline")
        if self.audio_monitor:
            self.audio_monitor.stop()
        if self.ha_monitor:
            self.ha_monitor.stop()
        if self.stats:
            self.stats.stop()
        if self.mqtt_client:
            self.mqtt_client.stop()
        if self.db:
            self.db.close()
        logging.info("Cleanup complete")


def main():
    app = Application()
    retry = 0
    while retry < 5:
        try:
            app.run()
            if not app.running:
                break
        except Exception as e:
            logging.error(f"Fatal crash: {e}")
            retry += 1
            time.sleep(10)


if __name__ == "__main__":
    main()
