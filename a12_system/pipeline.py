"""Detection pipeline: per-frame processing with sensor fusion and notifications."""

import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import cv2


class DetectionPipeline:
    """Orchestrates motion detection, YOLO inference, sensor fusion, and notifications."""

    def __init__(
        self,
        runtime_config,
        detector,
        notifier,
        mqtt_client,
        db,
        stats,
        audio_monitor,
        ha_monitor,
        force_yolo_event: threading.Event,
        shared_state: dict,
        status_monitor,
        script_dir: str,
    ):
        self.runtime_config = runtime_config
        self.detector = detector
        self.notifier = notifier
        self.mqtt_client = mqtt_client
        self.db = db
        self.stats = stats
        self.audio_monitor = audio_monitor
        self.ha_monitor = ha_monitor
        self.force_yolo_event = force_yolo_event
        self.shared_state = shared_state
        self.status_monitor = status_monitor
        self.running = True

        # Frame buffer for GIF/MP4 creation
        gif_fps = runtime_config.get("gif.fps", 10)
        gif_duration = runtime_config.get("gif.duration_seconds", 2)
        self.gif_buffer_size = gif_fps * gif_duration
        self.frame_buffer: deque = deque(maxlen=self.gif_buffer_size)

        # Screenshot folder
        self.screenshot_folder = os.path.join(script_dir, "screenshots")
        os.makedirs(self.screenshot_folder, exist_ok=True)

        # Cooldowns
        self.last_save_time: dict[str, float] = {}
        self.cooldown_seconds = runtime_config.get("detection_cooldown_seconds", 5)

        # Counters (thread-safe)
        self._counter_lock = threading.Lock()
        self.motion_frame_counter = 0
        self.yolo_check_interval = runtime_config.get("yolo.check_interval", 5)

        # FPS tracking
        self.last_heartbeat = 0
        self.heartbeat_interval = 30
        self.frame_count = 0

        # Periodic YOLO
        self.last_periodic_yolo = 0
        self.periodic_yolo_interval = runtime_config.get("periodic_yolo_interval", 30)

        # Async notification worker
        self.notification_queue: queue.Queue = queue.Queue()
        self._notify_thread = threading.Thread(target=self._notification_worker, daemon=True)
        self._notify_thread.start()

    def process_frame(self, frame) -> None:
        """Main per-frame processing callback."""
        if not self.running:
            return

        self.frame_count += 1
        self.shared_state["last_frame"] = time.time()

        current_time = time.time()

        # Heartbeat logging
        if current_time - self.last_heartbeat > self.heartbeat_interval:
            elapsed = current_time - self.last_heartbeat
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            rssi = self.status_monitor.current_rssi if self.status_monitor else 0
            logging.info(
                f"Stream active | FPS: {fps:.1f} | Frames: {self.frame_count} | RSSI: {rssi}dBm"
            )
            self.last_heartbeat = current_time
            self.frame_count = 0

        # Motion detection
        motion_enabled = self.runtime_config.get("motion.threshold", 50) > 0
        motion_detected = self.detector.detect_motion(frame) if motion_enabled else False

        if motion_detected:
            self.mqtt_client.publish("motion", "ON")
            with self._counter_lock:
                self.motion_frame_counter += 1
                counter_val = self.motion_frame_counter
        else:
            self.mqtt_client.publish("motion", "OFF")
            counter_val = 0

        # Decide whether to run YOLO
        run_yolo = False
        yolo_reason = ""

        if self.force_yolo_event.is_set():
            logging.info("Forced YOLO check (External trigger: HA/Zigbee sensor)!")
            run_yolo = True
            yolo_reason = "external_trigger"
            self.force_yolo_event.clear()
        elif motion_detected and counter_val % self.yolo_check_interval == 0:
            run_yolo = True
            yolo_reason = "motion_detected"
        elif (current_time - self.last_periodic_yolo) > self.periodic_yolo_interval:
            logging.info(f"Periodic YOLO check (no motion for {self.periodic_yolo_interval}s)")
            run_yolo = True
            yolo_reason = "periodic"
            self.last_periodic_yolo = current_time

        if not run_yolo:
            return

        # YOLO inference
        self.frame_buffer.append(frame.copy())
        all_detections = self.detector.detect_objects(frame)

        person_detections = [(l, c) for l, c in all_detections if l == "person"]
        animal_detections = [(l, c) for l, c in all_detections if l != "person"]

        person_found = len(person_detections) > 0
        self.stats.record_motion_event(False, motion_detected, person_found)
        self.mqtt_client.publish("person", "ON" if person_found else "OFF")

        if self.runtime_config.get("debug_detection", False):
            logging.debug(f"YOLO: person={person_found}, detections={len(person_detections)}")

        if person_detections:
            self._handle_person_detections(frame, person_detections)

        if animal_detections:
            self._handle_animal_detections(frame, animal_detections)

    def _handle_person_detections(self, frame, detections: list) -> None:
        """Process person detections with sensor fusion logic."""
        for label, confidence in detections:
            logging.info(f"Detected: {label} ({confidence:.2f})")

            # Sensor fusion
            security_mode = self.runtime_config.get("security.mode", "MONITOR")
            require_confirmation = self.runtime_config.get("security.require_sensor_confirmation", False)
            sensor_window = self.runtime_config.get("security.sensor_fusion_window_seconds", 10.0)

            sensor_confirmed = False
            active_sensors = []

            if self.ha_monitor:
                sensor_confirmed = self.ha_monitor.is_any_sensor_recently_active(sensor_window)
                active_sensors = self.ha_monitor.get_recently_active_sensors(sensor_window)

            # Event classification
            event_classification = "CAMERA_ONLY"
            event_priority = "normal"
            should_notify = True
            alarm_triggered = False

            if security_mode == "SECURITY":
                if sensor_confirmed:
                    event_classification = "CONFIRMED_ALARM"
                    event_priority = "critical"
                    alarm_triggered = True
                    logging.warning(
                        f"CONFIRMED ALARM: Person + {len(active_sensors)} sensor(s)"
                    )
                    self.mqtt_client.publish("camera/alarm/trigger", "ON")
                else:
                    event_classification = "UNCONFIRMED_CAMERA"
                    event_priority = "low"
                    if require_confirmation:
                        logging.info(f"UNCONFIRMED: Person detected, no sensor ({sensor_window}s)")
                        should_notify = False
                        self.db.log_event("unconfirmed_detection", label, float(confidence))
                    else:
                        logging.info("Person detected (no sensor confirmation - allowed)")

            elif security_mode == "MONITOR":
                event_classification = "MONITOR_MODE"
                if sensor_confirmed:
                    logging.info("Person detected + Sensor active (MONITOR mode)")

            else:  # TEST mode
                event_classification = "TEST_MODE"
                event_priority = "debug"
                logging.info(f"TEST MODE: Person={label}, Sensors={sensor_confirmed}")

            self.mqtt_client.publish("camera/detection/classification", event_classification)
            self.mqtt_client.publish("camera/detection/priority", event_priority)
            self.mqtt_client.publish(
                "camera/detection/sensor_confirmed",
                "true" if sensor_confirmed else "false",
            )

            if not should_notify:
                continue

            # Cooldown check
            if label in self.last_save_time:
                if (time.time() - self.last_save_time[label]) < self.cooldown_seconds:
                    continue

            self.stats.record_detection(label)
            self.db.log_event("detection", label, float(confidence))

            # Face recognition
            person_name = ""
            skip_telegram = False

            if self.runtime_config.get("face_recognition", {}).get("enabled", False):
                is_known, name = self.detector.identify_person(frame)
                person_name = name
                self.stats.record_face_attempt(is_known, name)
                self.db.log_event("face", name if is_known else "unknown", 1.0 if is_known else 0.0)
                self.mqtt_client.publish("face", name if is_known else "unknown")

                whitelist = self.runtime_config.get(
                    "face_recognition.whitelisted_names", []
                )
                if is_known and name in whitelist:
                    skip_telegram = True
                    logging.info(f"Known person: {name} - Telegram skipped")

            # Save & notify
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label_folder = os.path.join(self.screenshot_folder, label)
            os.makedirs(label_folder, exist_ok=True)
            self.last_save_time[label] = time.time()

            # Always save JPG locally
            jpg_path = os.path.join(label_folder, f"{label}_{timestamp}.jpg")
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)

            if not skip_telegram:
                display_label = label
                if alarm_triggered:
                    display_label = "ALARM! " + label.upper()

                self._queue_notification(
                    display_label, timestamp, person_name, label_folder
                )

    def _handle_animal_detections(self, frame, detections: list) -> None:
        """Process animal detections with security mode filtering."""
        security_mode = self.runtime_config.get("security.mode", "MONITOR")
        detect_animals = self.runtime_config.get("security.animal_detection", True)

        if not detect_animals:
            return

        for label, confidence in detections:
            if security_mode == "SECURITY":
                logging.info(f"Animal ignored in SECURITY mode: {label} ({confidence:.2f})")
                continue

            if label in self.last_save_time:
                if (time.time() - self.last_save_time[label]) < self.cooldown_seconds:
                    continue

            logging.info(f"Animal Detected: {label} ({confidence:.2f})")
            self.mqtt_client.publish("camera/detection/animal", label)
            self.db.log_event("detection", label, float(confidence))

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label_folder = os.path.join(self.screenshot_folder, label)
            os.makedirs(label_folder, exist_ok=True)
            self.last_save_time[label] = time.time()

            jpg_path = os.path.join(label_folder, f"{label}_{timestamp}.jpg")
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)

    def _queue_notification(self, label: str, timestamp: str, person_name: str, label_folder: str) -> None:
        """Queue async notification task for GIF/MP4 creation."""
        audio_data = None
        if self.audio_monitor and self.audio_monitor.running:
            audio_data = self.audio_monitor.get_audio_data()

        task = {
            "label": label,
            "frames": list(self.frame_buffer)[-self.gif_buffer_size:],
            "audio_data": audio_data,
            "timestamp": timestamp,
            "person_name": person_name,
            "label_folder": label_folder,
        }
        self.notification_queue.put(task)

    def _notification_worker(self) -> None:
        """Background thread to handle media creation and Telegram notifications."""
        logging.info("Notification worker started")
        while self.running:
            try:
                task = self.notification_queue.get(timeout=1)
                label = task["label"]
                frames = task["frames"]
                audio_data = task.get("audio_data")
                timestamp = task["timestamp"]
                person_name = task.get("person_name", "")
                label_folder = task["label_folder"]

                # Try MP4 with audio first
                mp4_created = False
                if audio_data is not None:
                    mp4_path = os.path.join(label_folder, f"{label}_{timestamp}.mp4")
                    if self.notifier.create_mp4(frames, audio_data, mp4_path):
                        msg = f"{label.title()} detected (AV Clip)"
                        if person_name:
                            msg += f" ({person_name})"
                        self.notifier.send_telegram(msg, mp4_path)
                        self.db.log_event("media", "mp4", 0.0, mp4_path)
                        mp4_created = True

                # Fallback to GIF
                if not mp4_created:
                    gif_path = os.path.join(label_folder, f"{label}_{timestamp}.gif")
                    if self.notifier.create_gif(frames, gif_path):
                        msg = f"{label.title()} detected"
                        if person_name:
                            msg += f" ({person_name})"
                        self.notifier.send_telegram(msg, gif_path)
                        self.db.log_event("media", "gif", 0.0, gif_path)

                self.notification_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logging.error(f"Notification worker error: {e}")

    def stop(self) -> None:
        self.running = False
