#!/usr/bin/env python3
"""
A12 System v2 — ESP32 Camera Surveillance System
Entry point: python -m A12_System_v2
"""

import logging
import os
import signal
import threading
import time
from datetime import datetime

import cv2
import requests

from .config import load_config
from .logging_setup import setup_logging
from .runtime_config import RuntimeConfig
from .camera import Camera, stall_event_detail
from .flat_episode import prune_files
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
        exposure_reset_settings = self.runtime_config.get("camera_exposure_reset_settings") or {
            "aec": 1,
            "aec2": 1,
            "ae_level": 5,
            "agc": 1,
            "gainceiling": 6,
            "brightness": 3,
            "contrast": 1,
            "denoise": 4,
        }

        def _camera_exposure_reset_silent():
            camera.set_camera_settings(exposure_reset_settings)

        def _camera_exposure_reset():
            _camera_exposure_reset_silent()
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
            camera_reset_fn=_camera_exposure_reset_silent,
        )

        # Initial camera config
        init_settings = self.runtime_config.get("camera_init_settings", {})
        if init_settings:
            camera.set_camera_settings(init_settings)
            time.sleep(2)

        # Every A12 (re)start is announced — an A12 crash-loop must stay visible
        # on the primary monitoring channel, and a redeploy mid-episode needs its
        # "came up" confirmation. An ongoing flat episode is noted, not used to
        # suppress the message.
        startup_msg = _startup_message(config, detector)
        if self.pipeline.flat_state.episode_active():
            startup_msg += "\nNote: flat-frame episode still active (camera may be wedged)."
        notifier.send_telegram(startup_msg, bypass_cooldown=True)

        idle_decode_fps = max(1, int(self.runtime_config.get("stream_idle_decode_fps", 2)))
        active_decode_fps = max(
            idle_decode_fps,
            int(self.runtime_config.get("stream_active_decode_fps", 10)),
        )

        def target_decode_fps():
            now = time.time()
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

        def _stream_reconnect_requested() -> bool:
            if not shared_state.pop("force_stream_reconnect", False):
                return False
            # Forensics: dump the last raw JPEG the stream delivered before the
            # teardown, so a hung-gray episode leaves evidence of the actual
            # bytes A12 received (a second stream client starves, so this is
            # the only way to capture them).
            try:
                raw = camera.last_raw_jpg
                if raw:
                    prune_files(self.pipeline.screenshot_folder, "flat_debug_raw_", keep=20)
                    path = os.path.join(
                        self.pipeline.screenshot_folder,
                        f"flat_debug_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg",
                    )
                    with open(path, "wb") as fh:
                        fh.write(raw)
                    logging.warning(f"Flat-frame evidence saved: {path}")
            except Exception as e:
                logging.warning(f"Flat-frame evidence dump failed: {e}")
            return True

        # Main reconnection loop
        consecutive_failures = 0
        stuck_notified = False
        reboot_grace_until = 0.0
        STUCK_THRESHOLD = 2
        BASE_RETRY_SLEEP = 5
        MAX_RETRY_SLEEP = 60
        REBOOT_GRACE_SECONDS = 60.0

        while self.running:
            try:
                stream_response = camera.get_stream()
                if stream_response:
                    if stuck_notified:
                        logging.info("ESP32 recovered!")
                        notifier.send_telegram("ESP32 is back online!", bypass_cooldown=True)
                        stuck_notified = False
                        shared_state["send_recovery_snapshot"] = True

                    # Kick AEC shortly after every (re)connect so the OV3660 doesn't
                    # stay frozen at near-zero exposure. The pipeline brightness
                    # watchdog also covers this, but it is rate-limited to one reset
                    # per 300s — up to 5 minutes of near-black frames (blind
                    # detection) if its window was just consumed.
                    threading.Timer(5.0, _camera_exposure_reset_silent).start()

                    consecutive_failures = 0
                    stream_end_reason = camera.process_stream(
                        stream_response,
                        self.pipeline.process_frame,
                        target_fps_getter=target_decode_fps,
                        reconnect_requested=_stream_reconnect_requested,
                    )

                    if stream_end_reason in ("frozen", "stream_ended"):
                        # Camera-side view of the stall we just hit: did the
                        # camera drop us (send_fail/errno) and how starved is it?
                        health = camera.log_health_snapshot(stream_end_reason)
                        # Persist the stall: docker logs rotate (and have been
                        # lost to corruption), the events DB keeps statistics.
                        self.pipeline.db.log_event(
                            "stream_stall",
                            stream_end_reason,
                            0.0,
                            stall_event_detail(camera.last_freeze_summary, health),
                        )
                        self.pipeline.note_stream_freeze(stream_end_reason)

                    if shared_state.pop("reboot_camera", False):
                        # Camera-side OV3660 wedge: reconnects can't fix gray pixels
                        # at the source. Reboot the camera over the LAN — a soft
                        # ESP.restart clears it (verified 2026-07-11). The next
                        # get_stream() reconnects to the fresh boot.
                        camera.reboot()
                        # The camera drops offline for ~15-25s now. That outage is
                        # self-inflicted: don't let the loop below count it toward
                        # STUCK alerts ("not responding!" / "back online!" spam for
                        # a reboot we requested ourselves).
                        reboot_grace_until = time.time() + REBOOT_GRACE_SECONDS
                elif time.time() < reboot_grace_until:
                    logging.info(
                        "Stream still down after commanded camera reboot — expected, retrying"
                    )
                    time.sleep(BASE_RETRY_SLEEP)
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
        # Idempotent: run() calls this on the normal path and main() calls it again
        # from its finally, so that a crash cannot skip teardown.
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True

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
    retry = 0
    while retry < 5:
        # A fresh Application per attempt. Reusing one instance meant run() built a
        # new MQTTClient, StatusMonitor, AudioMonitor, HAMonitor, Statistics and
        # EventDB on every retry while the previous set kept running: two paho
        # threads with duplicate subscriptions (every zigbee message handled and
        # logged twice), two status monitors sending the same sabotage alert, and
        # stale callbacks holding a database handle that _cleanup may have closed.
        # After five crashes there were five copies of everything.
        app = Application()
        try:
            app.run()
            if not app.running:
                break
        except KeyboardInterrupt:
            # Delivered before run() enters its main loop (the signal handler
            # raises), it used to escape past `except Exception` and kill the
            # process with a traceback and no cleanup — no offline status, no
            # database close.
            logging.info("Interrupted during startup; shutting down")
            break
        except Exception as e:
            logging.error(f"Fatal crash: {e}")
            retry += 1
            time.sleep(10)
        finally:
            # Always tear the old instance down before building another one.
            try:
                app._cleanup()
            except Exception as e:
                logging.error(f"Cleanup after crash failed: {e}")


if __name__ == "__main__":
    main()
