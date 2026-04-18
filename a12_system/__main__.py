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
        # Load config & setup logging
        initial_config = load_config(SCRIPT_DIR)
        setup_logging(initial_config, SCRIPT_DIR)
        self.runtime_config = RuntimeConfig(initial_config)

        logging.info("=" * 50)
        logging.info("A12 System v2 - Detection Pipeline")
        logging.info("=" * 50)

        # Initialize components
        config = self.runtime_config.get_all()
        camera = Camera(config)
        detector = Detector(config, SCRIPT_DIR)
        notifier = Notifier(config)
        self.stats = Statistics(save_path=os.path.join(SCRIPT_DIR, "stats.json"))
        self.db = EventDB(os.path.join(SCRIPT_DIR, "events.db"))

        # Shared state
        force_yolo_event = threading.Event()
        shared_state = {"last_frame": time.time()}

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
            shared_state, self.http_session
        )
        self.status_monitor.start()

        # Audio monitor
        def audio_callback(rms, msg):
            logging.info(msg)
            notifier.send_telegram(msg)
            self.mqtt_client.publish("audio/alert", "ON")
            self.db.log_event("audio", "LOUD_NOISE", rms)

        if self.runtime_config.get("audio_enabled", False):
            self.audio_monitor = AudioMonitor(config, callback=audio_callback)
            self.audio_monitor.start()
            logging.info("Audio monitor started")
        else:
            logging.info("Audio monitor disabled")

        # HA monitor
        def ha_sensor_callback(entity_id, sensor_name, old_state, new_state, attributes):
            if new_state == "on":
                logging.info(f"HA Sensor: {sensor_name} -> Forcing YOLO check!")
                self.db.log_event("ha_sensor", sensor_name, 1.0, entity_id)
                force_yolo_event.set()

        if self.runtime_config.get("home_assistant_token"):
            self.ha_monitor = HAMonitor(config, callback=ha_sensor_callback)
            self.ha_monitor.start()
            logging.info("HA Monitor started")
        else:
            logging.info("HA Monitor disabled (no token)")

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
            script_dir=SCRIPT_DIR,
        )

        # Initial camera config
        init_settings = self.runtime_config.get("camera_init_settings", {})
        if init_settings:
            camera.set_camera_settings(init_settings)
            time.sleep(2)

        notifier.send_telegram("A12 System v2 started")

        # Main reconnection loop
        consecutive_failures = 0
        stuck_notified = False
        STUCK_THRESHOLD = 2

        while self.running:
            try:
                stream_response = camera.get_stream()
                if stream_response:
                    if stuck_notified:
                        logging.info("ESP32 recovered!")
                        notifier.send_telegram("ESP32 is back online!")
                        stuck_notified = False

                    consecutive_failures = 0
                    camera.process_stream(stream_response, self.pipeline.process_frame)
                else:
                    consecutive_failures += 1
                    logging.error(f"Failed to get stream (Attempt {consecutive_failures})")

                    if consecutive_failures >= STUCK_THRESHOLD and not stuck_notified:
                        notifier.send_telegram("ESP32 not responding (STUCK)!")
                        stuck_notified = True

                    time.sleep(5)
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
