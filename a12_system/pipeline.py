"""Detection pipeline: per-frame processing with sensor fusion and notifications."""

import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np


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
        camera_reset_fn=None,
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
        self.camera_id = str(runtime_config.get("camera_id", "esp32_cam")).strip() or "esp32_cam"
        self.camera_name = str(runtime_config.get("camera_name", self.camera_id)).strip() or self.camera_id
        self.telegram_label = str(runtime_config.get("telegram.camera_label", self.camera_name)).strip()
        self.log_prefix = f"[{self.camera_id}:{self.camera_name}]"

        # Frame buffer for GIF/MP4 creation. Keep it small and PIR-gated:
        # full-resolution decoded camera frames are too expensive to retain.
        self.clip_fps = max(1, int(runtime_config.get("clip_buffer_fps", 5)))
        self.clip_frame_size = (
            max(160, int(runtime_config.get("clip_frame_width", 640))),
            max(120, int(runtime_config.get("clip_frame_height", 480))),
        )
        # Separate size for Telegram preview — smaller than local clip buffer
        self.telegram_preview_size = (
            int(runtime_config.get("telegram_preview_width", 640)),
            int(runtime_config.get("telegram_preview_height", 480)),
        )
        self.last_clip_buffer_sample = 0.0
        self.clip_buffer_interval = 1.0 / self.clip_fps
        self.recording_buffer_until = 0.0
        self.clip_pre_seconds = max(
            0, int(runtime_config.get("clip_pre_seconds", runtime_config.get("gif.duration_seconds", 5)))
        )
        self.post_buffer_seconds = max(0, int(runtime_config.get("clip_post_seconds", 15)))
        buffer_seconds = self.clip_pre_seconds + self.post_buffer_seconds + 2
        self.frame_buffer: deque = deque(maxlen=max(1, self.clip_fps * buffer_seconds))

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
        self.person_notify_confidence = runtime_config.get(
            "yolo.notify_confidence_threshold",
            runtime_config.get("yolo.confidence_threshold", 0.55),
        )
        self.pir_person_notify_confidence = runtime_config.get(
            "yolo.pir_notify_confidence_threshold",
            min(self.person_notify_confidence, 0.45),
        )
        self.person_confirmations_required = max(
            1, int(runtime_config.get("yolo.person_confirmations", 2))
        )
        self.pir_person_confirmations_required = max(
            1, int(runtime_config.get("yolo.pir_person_confirmations", 1))
        )
        self.person_confirmation_streaks = {"camera": 0, "pir": 0}

        # FPS tracking
        self.last_heartbeat = 0
        self.heartbeat_interval = 30
        self.frame_count = 0
        self.stream_fps = 10  # updated from heartbeat

        # Periodic YOLO
        self.last_periodic_yolo = 0
        self.periodic_yolo_interval = runtime_config.get("periodic_yolo_interval", 30)
        self.external_yolo_interval = float(
            runtime_config.get("external_trigger_yolo_interval_seconds", 2.0)
        )
        self.last_external_yolo = 0.0

        # Event scoring
        self.event_scoring_enabled = bool(runtime_config.get("event_scoring.enabled", True))
        self.event_notify_threshold = int(runtime_config.get("event_scoring.notify_threshold", 70))
        self.event_local_record_threshold = int(
            runtime_config.get("event_scoring.local_record_threshold", 45)
        )
        self.require_sensor_for_recording = bool(
            runtime_config.get("require_sensor_for_recording", True)
        )
        self.pir_recording_enabled = bool(runtime_config.get("pir_recording.enabled", True))
        self.pir_recording_label = str(runtime_config.get("pir_recording.label", "motion")).strip() or "motion"
        self.pir_recording_send_telegram = bool(
            runtime_config.get("pir_recording.send_telegram", True)
        )
        self.pir_recording_bypass_cooldown = bool(
            runtime_config.get("pir_recording.bypass_cooldown", True)
        )
        self.pir_recording_require_yolo = bool(
            runtime_config.get("pir_recording.require_yolo_for_telegram", False)
        )
        self.pir_recording_cooldown = max(
            0, int(runtime_config.get("pir_recording.cooldown_seconds", 30))
        )
        self.last_pir_record_time = 0.0
        self.last_pir_sensor_activity_recorded = 0.0

        # Media cleanup
        self.last_cleanup = 0.0
        self.cleanup_interval = max(
            60, int(runtime_config.get("media_cleanup_interval_seconds", 3600))
        )
        self.cleanup_max_age_days = max(
            0.25, float(runtime_config.get("media_retention_days", 2))
        )

        # Async notification worker
        queue_maxsize = max(1, int(runtime_config.get("notification_queue_maxsize", 10)))
        self.notification_queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._notify_thread = threading.Thread(target=self._notification_worker, daemon=True)
        self._notify_thread.start()

        # Groq vision face recognition
        self.groq_vision = None
        groq_api_key = runtime_config.get("groq_api_key", "")
        groq_faces_dir = os.path.join(script_dir, "known_faces")
        if groq_api_key:
            from .groq_vision import GroqVision
            self.groq_vision = GroqVision(groq_api_key, groq_faces_dir)
            logging.info(f"{self.log_prefix} Groq vision face recognition enabled")
        else:
            logging.info(f"{self.log_prefix} Groq vision disabled (no GROQ_API_KEY)")

        # HA URL for Nuki unlock
        self.ha_url = runtime_config.get("home_assistant_url", "")
        self.ha_token = runtime_config.get("home_assistant_token", "")
        self.nuki_entity_id = runtime_config.get("nuki_lock_entity_id", "lock.nuki_smart_lock")

        # Suppress motion Telegram when known person was recently identified
        self._known_person_until = 0.0

        # Brightness watchdog — detects AEC freeze (OV3660 UXGA issue)
        self._camera_reset_fn = camera_reset_fn
        self._dark_frame_threshold = int(runtime_config.get("brightness_watchdog_threshold", 20))
        self._dark_consecutive_required = int(runtime_config.get("brightness_watchdog_strikes", 2))
        self._dark_consecutive_count = 0
        self._last_exposure_reset = 0.0

    def _trigger_nuki_unlock(self, name: str):
        if not self.ha_url or not self.ha_token:
            logging.warning(f"{self.log_prefix} Nuki unlock skipped — no HA config")
            return
        try:
            import requests as _req
            _req.post(
                f"{self.ha_url}/api/services/lock/unlock",
                headers={"Authorization": f"Bearer {self.ha_token}", "Content-Type": "application/json"},
                json={"entity_id": self.nuki_entity_id},
                timeout=5,
            )
            logging.info(f"{self.log_prefix} Nuki unlock triggered for '{name}'")
            self.notifier.send_telegram(f"Odemknuto pro {name}", bypass_cooldown=True)
        except Exception as e:
            logging.error(f"{self.log_prefix} Nuki unlock failed: {e}")

    def _telegram_message(self, message: str) -> str:
        if self.telegram_label:
            return f"{self.telegram_label}: {message}"
        return message

    def _media_name(self, label: str, timestamp: str, suffix: str) -> str:
        return f"{self.camera_id}_{label}_{timestamp}{suffix}"

    def _send_recovery_snapshot(self, frame) -> None:
        try:
            tmp_path = os.path.join(self.screenshot_folder, f"{self.camera_id}_recovery.jpg")
            cv2.imwrite(tmp_path, frame)
            self.notifier.send_telegram(
                self._telegram_message("Camera view after recovery:"), media_path=tmp_path, bypass_cooldown=True
            )
        except Exception as e:
            logging.warning(f"{self.log_prefix} Recovery snapshot failed: {e}")

    def process_frame(self, frame) -> None:
        """Main per-frame processing callback."""
        if not self.running:
            return

        if self.shared_state.pop("send_recovery_snapshot", False):
            self._send_recovery_snapshot(frame)

        self.frame_count += 1
        self.shared_state["last_frame"] = time.time()

        current_time = time.time()

        # Heartbeat logging
        if current_time - self.last_heartbeat > self.heartbeat_interval:
            elapsed = current_time - self.last_heartbeat
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            if fps > 1:
                self.stream_fps = round(fps)
            rssi = self.status_monitor.current_rssi if self.status_monitor else 0
            pipeline_state = self.shared_state.get("pipeline_state", "unknown")
            decode_fps = int(self.shared_state.get("stream_decode_fps", 0))
            logging.info(
                f"{self.log_prefix} Stream active | state={pipeline_state} | FPS: {fps:.1f} "
                f"| target_decode_fps={decode_fps} | Frames: {self.frame_count} "
                f"| buffer={len(self.frame_buffer)} | queue={self.notification_queue.qsize()} "
                f"| RSSI: {rssi}dBm"
            )
            self.mqtt_client.publish("camera/status/pipeline_state", pipeline_state, retain=True)
            self.mqtt_client.publish("camera/status/stream_fps", f"{fps:.1f}")
            self.mqtt_client.publish("camera/status/decode_fps_target", str(decode_fps), retain=True)
            self.mqtt_client.publish("camera/status/frame_buffer_len", str(len(self.frame_buffer)))
            self.mqtt_client.publish("camera/status/notification_queue", str(self.notification_queue.qsize()))
            self.last_heartbeat = current_time
            self.frame_count = 0

            # Brightness watchdog: detect AEC freeze (OV3660 gets stuck at near-zero exposure)
            if self._camera_reset_fn:
                small = cv2.resize(frame, (160, 120))
                brightness = float(np.mean(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)))
                self.shared_state["last_frame_brightness"] = brightness
                if brightness < self._dark_frame_threshold:
                    self._dark_consecutive_count += 1
                    logging.warning(
                        f"{self.log_prefix} Dark frame detected (brightness={brightness:.1f},"
                        f" strike {self._dark_consecutive_count}/{self._dark_consecutive_required})"
                    )
                    if (
                        self._dark_consecutive_count >= self._dark_consecutive_required
                        and current_time - self._last_exposure_reset > 300
                    ):
                        logging.warning(f"{self.log_prefix} AEC freeze suspected — resetting exposure")
                        self._dark_consecutive_count = 0
                        self._last_exposure_reset = current_time
                        try:
                            self._camera_reset_fn()
                        except Exception as _e:
                            logging.error(f"{self.log_prefix} Exposure reset failed: {_e}")
                else:
                    self._dark_consecutive_count = 0

        # Motion detection
        # Primary: OpenCV frame differencing (disabled when motion.threshold=0)
        motion_enabled = self.runtime_config.get("motion.threshold", 50) > 0
        motion_detected = self.detector.detect_motion(frame) if motion_enabled else False
        # Secondary: ESP32 firmware motion signal received via MQTT (esp32cam/<device>/motion).
        # Uses a 3-second sticky window — camera publishes OFF every 500ms between checks,
        # so a boolean flag would reset before A12 processes the next frame.
        esp32_motion_window = 3.0
        esp32_motion_detected = (
            time.time() - float(self.shared_state.get("last_esp32_motion", 0.0)) < esp32_motion_window
        )
        if esp32_motion_detected:
            motion_detected = True

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
            source = self.shared_state.get("external_yolo_source", "external_trigger")
            logging.info(f"Forced YOLO check ({source})!")
            run_yolo = True
            yolo_reason = "external_trigger"
            self.last_external_yolo = current_time
            self.force_yolo_event.clear()
        elif current_time < float(self.shared_state.get("external_yolo_until", 0.0)):
            if current_time - self.last_external_yolo >= self.external_yolo_interval:
                source = self.shared_state.get("external_yolo_source", "external_trigger")
                logging.info(
                    "External trigger YOLO window active "
                    f"({source}, interval={self.external_yolo_interval:.1f}s)"
                )
                run_yolo = True
                yolo_reason = "external_trigger_window"
                self.last_external_yolo = current_time
        elif motion_detected and counter_val % self.yolo_check_interval == 0:
            run_yolo = True
            yolo_reason = "motion_detected"
        elif (current_time - self.last_periodic_yolo) > self.periodic_yolo_interval:
            logging.info(f"Periodic YOLO check (no motion for {self.periodic_yolo_interval}s)")
            run_yolo = True
            yolo_reason = "periodic"
            self.last_periodic_yolo = current_time

        self._buffer_clip_frame_if_needed(frame, current_time)
        self._handle_pir_recording(frame, current_time)

        if not run_yolo:
            return

        # YOLO inference
        all_detections = self.detector.detect_objects(frame)

        person_candidates = [(l, c) for l, c in all_detections if l == "person"]
        is_pir_triggered = yolo_reason.startswith("external_trigger")
        detection_profile = "pir" if is_pir_triggered else "camera"
        notify_confidence = (
            self.pir_person_notify_confidence
            if is_pir_triggered
            else self.person_notify_confidence
        )
        confirmations_required = (
            self.pir_person_confirmations_required
            if is_pir_triggered
            else self.person_confirmations_required
        )
        person_detections = [
            (l, c) for l, c in person_candidates if c >= notify_confidence
        ]
        animal_detections = [(l, c) for l, c in all_detections if l != "person"]

        person_found = len(person_detections) > 0
        if person_candidates and not person_found:
            max_conf = max(c for _l, c in person_candidates)
            logging.info(
                "Person candidate below notify threshold: "
                f"{max_conf:.2f} < {notify_confidence:.2f} "
                f"(profile={detection_profile}, reason={yolo_reason})"
            )

        if person_found:
            self.person_confirmation_streaks[detection_profile] += 1
            confirmation_streak = self.person_confirmation_streaks[detection_profile]
            other_profile = "camera" if detection_profile == "pir" else "pir"
            self.person_confirmation_streaks[other_profile] = 0
            if confirmation_streak < confirmations_required:
                max_conf = max(c for _l, c in person_detections)
                logging.info(
                    "Person candidate held for confirmation "
                    f"({confirmation_streak}/{confirmations_required}, "
                    f"confidence={max_conf:.2f}, profile={detection_profile}, "
                    f"reason={yolo_reason})"
                )
                person_found = False
                person_detections = []
        else:
            self.person_confirmation_streaks[detection_profile] = 0

        self.stats.record_motion_event(esp32_motion_detected, motion_detected, person_found)
        self.mqtt_client.publish("person", "ON" if person_found else "OFF")

        if self.runtime_config.get("debug_detection", False):
            logging.debug(f"YOLO: person={person_found}, detections={len(person_detections)}")

        if person_detections:
            self.shared_state["last_person_activity"] = current_time
            self.shared_state["last_event_activity"] = current_time
            confirmation_streak = self.person_confirmation_streaks[detection_profile]
            self._handle_person_detections(
                frame,
                person_detections,
                yolo_reason,
                detection_profile,
                confirmation_streak,
            )

        if animal_detections:
            self._handle_animal_detections(frame, animal_detections)

    def _score_event(
        self,
        confidence: float,
        sensor_confirmed: bool,
        is_pir_triggered: bool,
        confirmation_streak: int,
    ) -> int:
        """Score a person event so PIR-assisted detections can be faster than camera-only ones."""
        if not self.event_scoring_enabled:
            return self.event_notify_threshold

        yolo_score = int(self.runtime_config.get("event_scoring.yolo_person_score", 55))
        sensor_score = int(self.runtime_config.get("event_scoring.sensor_active_score", 45))
        pir_score = int(self.runtime_config.get("event_scoring.pir_trigger_score", 15))
        confirmation_score = int(self.runtime_config.get("event_scoring.confirmation_score", 15))
        confidence_bonus_score = int(
            self.runtime_config.get("event_scoring.confidence_bonus_score", 25)
        )

        score = yolo_score
        if sensor_confirmed:
            score += sensor_score
        if is_pir_triggered:
            score += pir_score
        if confirmation_streak >= 2:
            score += confirmation_score

        confidence_floor = self.pir_person_notify_confidence if is_pir_triggered else self.person_notify_confidence
        confidence_span = max(0.01, 1.0 - confidence_floor)
        confidence_ratio = max(0.0, min(1.0, (confidence - confidence_floor) / confidence_span))
        score += round(confidence_bonus_score * confidence_ratio)
        return score

    def _should_buffer_clip_frames(self, current_time: float) -> bool:
        """Buffer clips only after PIR/HA activity or while a clip is being completed."""
        if current_time < float(self.shared_state.get("external_yolo_until", 0.0)):
            return True
        if current_time < self.recording_buffer_until:
            return True
        return bool(self.ha_monitor and self.ha_monitor.is_any_sensor_active())

    def _buffer_clip_frame_if_needed(self, frame, current_time: float) -> None:
        """Keep a rolling pre-event buffer at idle FPS; ramp to clip_fps during active windows.

        Always buffering (even in idle) ensures pre-event frames exist when the first
        PIR trigger fires — without this the 5s pre-buffer would always be empty.
        """
        if self._should_buffer_clip_frames(current_time):
            interval = self.clip_buffer_interval  # full clip FPS during active window
        else:
            idle_fps = max(1, int(self.runtime_config.get("stream_idle_decode_fps", 2)))
            interval = 1.0 / idle_fps

        if current_time - self.last_clip_buffer_sample < interval:
            return

        clip_frame = cv2.resize(frame, self.clip_frame_size)
        self.frame_buffer.append((current_time, clip_frame))
        self.last_clip_buffer_sample = current_time

    def _handle_person_detections(
        self,
        frame,
        detections: list,
        yolo_reason: str,
        detection_profile: str,
        confirmation_streak: int,
    ) -> None:
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
                current_active_sensors = self.ha_monitor.get_active_sensors()
                recent_active_sensors = self.ha_monitor.get_recently_active_sensors(sensor_window)
                active_sensors = sorted(set(current_active_sensors + recent_active_sensors))
                sensor_confirmed = len(active_sensors) > 0

            # ESP32 firmware motion signal counts as sensor confirmation
            if not sensor_confirmed:
                esp32_motion_age = time.time() - float(self.shared_state.get("last_esp32_motion", 0.0))
                if esp32_motion_age < sensor_window:
                    sensor_confirmed = True
                    active_sensors.append("esp32_motion")

            # Event classification
            event_classification = "CAMERA_ONLY"
            event_priority = "normal"
            should_notify = True
            alarm_triggered = False
            is_pir_triggered = detection_profile == "pir"
            event_score = self._score_event(
                float(confidence),
                sensor_confirmed,
                is_pir_triggered,
                confirmation_streak,
            )

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
            self.mqtt_client.publish("camera/detection/score", str(event_score))

            if self.require_sensor_for_recording and not sensor_confirmed:
                logging.info(
                    "Person event ignored for recording: no PIR/HA sensor confirmation "
                    f"(profile={detection_profile}, reason={yolo_reason}, score={event_score})"
                )
                continue

            should_record_locally = event_score >= self.event_local_record_threshold
            if event_score < self.event_notify_threshold:
                should_notify = False
                logging.info(
                    "Person event kept local only "
                    f"(score={event_score}, notify_threshold={self.event_notify_threshold}, "
                    f"profile={detection_profile}, reason={yolo_reason})"
                )

            if not should_record_locally:
                logging.info(
                    "Person event ignored below local record threshold "
                    f"(score={event_score}, threshold={self.event_local_record_threshold})"
                )
                continue

            # Cooldown check
            if label in self.last_save_time:
                if (time.time() - self.last_save_time[label]) < self.cooldown_seconds:
                    continue

            self.stats.record_detection(label)
            self.db.log_event("detection", label, float(confidence))

            # Face recognition — Groq vision (primary) or dlib fallback
            person_name = ""
            skip_telegram = False
            groq_decision = None

            if self.groq_vision is not None:
                gname, gconf, gdecision = self.groq_vision.identify(frame)
                groq_decision = gdecision
                if gdecision == "cooldown":
                    pass  # rate limit — skip silently
                else:
                    self.db.log_event("face", gname or "unknown", gconf)
                    self.mqtt_client.publish("face", gname or "unknown")
                    logging.info(f"{self.log_prefix} Groq face: {gname} ({gconf:.2f}) → {gdecision}")

                if gdecision == "auto":
                    person_name = gname
                    skip_telegram = True
                    self._known_person_until = time.time() + 60.0
                    logging.info(f"{self.log_prefix} Known person '{gname}' — Telegram skipped, unlock queued")
                    self._trigger_nuki_unlock(gname)
                elif gdecision == "confirm":
                    person_name = gname
                    # notifier will send Telegram with unlock button — handled in notify call below

            elif self.runtime_config.get("face_recognition", {}).get("enabled", False):
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
            jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)

            if skip_telegram:
                continue

            display_label = label
            if alarm_triggered:
                display_label = "ALARM! " + label.upper()

            self._queue_notification(
                display_label,
                timestamp,
                person_name,
                label_folder,
                send_telegram=should_notify,
                event_score=event_score,
            )

    def _handle_pir_recording(self, frame, current_time: float) -> None:
        """Create a clip for each new PIR/HA trigger even when YOLO finds no person."""
        if not self.pir_recording_enabled:
            return

        sensor_activity = float(self.shared_state.get("last_sensor_activity", 0.0))
        if sensor_activity <= 0 or sensor_activity <= self.last_pir_sensor_activity_recorded:
            return

        external_window_active = current_time < float(
            self.shared_state.get("external_yolo_until", 0.0)
        )
        sensor_active = bool(self.ha_monitor and self.ha_monitor.is_any_sensor_active())
        if not external_window_active and not sensor_active:
            return

        if current_time - self.last_pir_record_time < self.pir_recording_cooldown:
            logging.info(
                "PIR recording cooldown active "
                f"({current_time - self.last_pir_record_time:.0f}s / "
                f"{self.pir_recording_cooldown}s)"
            )
            self.last_pir_sensor_activity_recorded = sensor_activity
            return

        label = self.pir_recording_label
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label_folder = os.path.join(self.screenshot_folder, label)
        os.makedirs(label_folder, exist_ok=True)

        jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
        cv2.imwrite(jpg_path, frame)
        self.db.log_event("detection", label, 1.0)
        self.db.log_event("media", "jpg", 0.0, jpg_path)
        self.stats.record_detection(label)

        self.last_pir_record_time = current_time
        self.last_pir_sensor_activity_recorded = sensor_activity
        self.shared_state["last_event_activity"] = current_time

        known_person_active = current_time < self._known_person_until
        logging.info("PIR trigger queued recording without YOLO confirmation")
        # When require_yolo_for_telegram is True, suppress Telegram from the PIR path.
        # The existing external_yolo_until window is already open; if YOLO confirms a person,
        # _handle_person_detections will send Telegram through its own path.
        pir_send_telegram = (
            self.pir_recording_send_telegram
            and not known_person_active
            and not self.pir_recording_require_yolo
        )
        self._queue_notification(
            label,
            timestamp,
            "",
            label_folder,
            send_telegram=pir_send_telegram,
            bypass_telegram_cooldown=self.pir_recording_bypass_cooldown,
            event_score=None,
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
            self.stats.record_detection(label)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label_folder = os.path.join(self.screenshot_folder, label)
            os.makedirs(label_folder, exist_ok=True)
            self.last_save_time[label] = time.time()

            jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
            cv2.imwrite(jpg_path, frame)
            self.db.log_event("media", "jpg", 0.0, jpg_path)

            self._queue_notification(
                label,
                timestamp,
                "",
                label_folder,
                send_telegram=True,
                event_score=None,
            )

    def _queue_notification(
        self,
        label: str,
        timestamp: str,
        person_name: str,
        label_folder: str,
        send_telegram: bool = True,
        bypass_telegram_cooldown: bool = False,
        event_score: int | None = None,
    ) -> None:
        """Queue async notification task for GIF/MP4 creation."""
        audio_data = None
        if self.audio_monitor and self.audio_monitor.running:
            audio_data = self.audio_monitor.get_audio_data()

        queued_at = time.time()
        max_post_seconds = max(
            self.post_buffer_seconds,
            int(self.runtime_config.get("adaptive_clip.max_post_seconds", 60)),
        )
        self.recording_buffer_until = max(
            self.recording_buffer_until,
            queued_at + max_post_seconds + 2,
        )
        self.shared_state["recording_buffer_until"] = self.recording_buffer_until
        pre_cutoff = queued_at - self.clip_pre_seconds
        pre_frames = [
            buffered_frame
            for frame_ts, buffered_frame in list(self.frame_buffer)
            if frame_ts >= pre_cutoff and frame_ts <= queued_at
        ]

        task = {
            "label": label,
            "pre_frames": pre_frames,
            "frame_buffer_ref": self.frame_buffer,
            "post_seconds": self.post_buffer_seconds,
            "queued_at": queued_at,
            "stream_fps": self.stream_fps,
            "audio_data": audio_data,
            "timestamp": timestamp,
            "person_name": person_name,
            "label_folder": label_folder,
            "send_telegram": send_telegram,
            "bypass_telegram_cooldown": bypass_telegram_cooldown,
            "event_score": event_score,
        }
        try:
            self.notification_queue.put_nowait(task)
        except queue.Full:
            logging.warning(
                f"{self.log_prefix} Notification queue full; dropping {label} event "
                f"at {timestamp}"
            )

    def _cleanup_old_media(self) -> None:
        """Remove media files older than cleanup_max_age_days from screenshots folder."""
        cutoff = time.time() - self.cleanup_max_age_days * 86400
        removed = 0
        try:
            for root, _dirs, files in os.walk(self.screenshot_folder):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        removed += 1
            if removed:
                logging.info(f"Cleanup: removed {removed} media files older than {self.cleanup_max_age_days}d")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

    def _sample_preview_frames(self, frames: list, seconds: int, fps: int) -> list:
        """Sample a short preview across the whole local clip."""
        if not frames:
            return []
        target_count = max(1, min(len(frames), int(seconds) * int(fps)))
        if target_count >= len(frames):
            return frames
        if target_count == 1:
            return [frames[len(frames) // 2]]
        last = len(frames) - 1
        return [frames[round(i * last / (target_count - 1))] for i in range(target_count)]

    def _wait_for_event_tail(self, queued_at: float, base_post_seconds: int) -> float:
        """Wait for post-event frames, extending while PIR/YOLO activity continues."""
        if base_post_seconds <= 0:
            return queued_at

        if not bool(self.runtime_config.get("adaptive_clip.enabled", True)):
            time.sleep(base_post_seconds)
            return queued_at + base_post_seconds

        idle_seconds = max(1, int(self.runtime_config.get("adaptive_clip.idle_seconds", 10)))
        max_post_seconds = max(
            base_post_seconds,
            int(self.runtime_config.get("adaptive_clip.max_post_seconds", 60)),
        )
        min_deadline = queued_at + base_post_seconds
        max_deadline = queued_at + max_post_seconds

        while self.running:
            now = time.time()
            if now >= max_deadline:
                logging.info(f"Adaptive clip reached max post window ({max_post_seconds}s)")
                return max_deadline

            last_activity = max(
                float(self.shared_state.get("last_person_activity", 0.0)),
                float(self.shared_state.get("last_sensor_activity", 0.0)),
                min(float(self.shared_state.get("external_yolo_until", 0.0)), now),
            )
            if self.ha_monitor and self.ha_monitor.is_any_sensor_active():
                last_activity = now

            if now >= min_deadline and now - last_activity >= idle_seconds:
                return now

            time.sleep(0.5)

        return time.time()

    def _notification_worker(self) -> None:
        """Background thread to handle media creation and Telegram notifications."""
        logging.info(f"{self.log_prefix} Notification worker started")
        while self.running:
            try:
                task = self.notification_queue.get(timeout=1)
                label = task["label"]
                audio_data = task.get("audio_data")
                timestamp = task["timestamp"]
                person_name = task.get("person_name", "")
                label_folder = task["label_folder"]
                send_telegram = bool(task.get("send_telegram", True))
                bypass_telegram_cooldown = bool(task.get("bypass_telegram_cooldown", False))
                event_score = task.get("event_score")

                # Wait for post-detection frames
                post_seconds = task.get("post_seconds", 0)
                if post_seconds > 0:
                    post_deadline = self._wait_for_event_tail(task.get("queued_at", time.time()), post_seconds)
                    if self.audio_monitor and self.audio_monitor.running:
                        audio_data = self.audio_monitor.get_audio_data()
                else:
                    post_deadline = task.get("queued_at", time.time())

                # Merge pre + post frames by timestamp. This keeps a real
                # event window instead of whatever happens to remain in deque.
                pre_frames = task.get("pre_frames", [])
                buf_ref = task.get("frame_buffer_ref")
                queued_at = task.get("queued_at", time.time())
                post_deadline = post_deadline + 0.5
                buffered = list(buf_ref) if buf_ref is not None else []
                post_frames = [
                    buffered_frame
                    for frame_ts, buffered_frame in buffered
                    if frame_ts > queued_at and frame_ts <= post_deadline
                ]
                frames = pre_frames + post_frames

                # Try MP4 first. Audio is optional; if MP4 fails, keep GIF as
                # the fallback so Telegram still gets a motion preview.
                mp4_created = False
                mp4_path = os.path.join(label_folder, self._media_name(label, timestamp, ".mp4"))
                msg = f"{label.title()} detected (AV Clip)" if audio_data else f"{label.title()} detected (Video)"
                if person_name:
                    msg += f" ({person_name})"

                if self.notifier.create_mp4(frames, audio_data, mp4_path, fps=task.get("stream_fps", 10)):
                    self.db.log_event("media", "mp4", 0.0, mp4_path)
                    mp4_created = True

                    media_mode = str(
                        self.runtime_config.get("telegram.media_mode", "mp4")
                    ).lower()
                    if not send_telegram:
                        logging.info(
                            "Telegram skipped by event score; local MP4 saved "
                            f"(score={event_score}, path={mp4_path})"
                        )
                    elif media_mode == "mp4":
                        self.notifier.send_telegram(
                            self._telegram_message(msg), mp4_path, bypass_cooldown=bypass_telegram_cooldown
                        )
                    elif media_mode == "preview_mp4":
                        preview_path = os.path.join(label_folder, self._media_name(label, timestamp, "_preview.mp4"))
                        preview_seconds = int(self.runtime_config.get("telegram.preview_seconds", 4))
                        preview_fps = int(self.runtime_config.get("telegram.preview_fps", 4))
                        preview_frames = self._sample_preview_frames(
                            frames, preview_seconds, preview_fps
                        )
                        if self.notifier.create_mp4(
                            preview_frames,
                            None,
                            preview_path,
                            fps=preview_fps,
                            size=self.telegram_preview_size,
                            crf=28,
                            min_size=1_000,
                        ):
                            self.notifier.send_telegram(
                                self._telegram_message(f"{msg} - local MP4 saved"),
                                preview_path,
                                bypass_cooldown=bypass_telegram_cooldown,
                            )
                            self.db.log_event("media", "mp4_preview", 0.0, preview_path)
                        else:
                            self.notifier.send_telegram(
                                self._telegram_message(f"{msg} - local MP4 saved"),
                                bypass_cooldown=bypass_telegram_cooldown,
                            )
                    elif media_mode == "snapshot":
                        jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            jpg_path,
                            bypass_cooldown=bypass_telegram_cooldown,
                        )
                    elif media_mode == "text":
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            bypass_cooldown=bypass_telegram_cooldown,
                        )
                    elif media_mode == "none":
                        logging.info(f"Telegram media skipped; local MP4 saved: {mp4_path}")
                    else:
                        logging.warning(f"Unknown telegram.media_mode={media_mode}; sending text only")
                        self.notifier.send_telegram(
                            self._telegram_message(f"{msg} - local MP4 saved"),
                            bypass_cooldown=bypass_telegram_cooldown,
                        )

                # Fallback to GIF
                gif_created = False
                if not mp4_created:
                    gif_path = os.path.join(label_folder, self._media_name(label, timestamp, ".gif"))
                    if self.notifier.create_gif(frames, gif_path):
                        msg = f"{label.title()} detected"
                        if person_name:
                            msg += f" ({person_name})"
                        if send_telegram:
                            self.notifier.send_telegram(
                                self._telegram_message(msg), gif_path, bypass_cooldown=bypass_telegram_cooldown
                            )
                        else:
                            logging.info(
                                "Telegram GIF skipped by event score; local GIF saved "
                                f"(score={event_score}, path={gif_path})"
                            )
                        self.db.log_event("media", "gif", 0.0, gif_path)
                        gif_created = True

                # Fallback to JPEG snapshot
                if not mp4_created and not gif_created:
                    jpg_path = os.path.join(label_folder, self._media_name(label, timestamp, ".jpg"))
                    if os.path.exists(jpg_path) and send_telegram:
                        msg = f"{label.title()} detected"
                        if person_name:
                            msg += f" ({person_name})"
                        self.notifier.send_telegram(
                            self._telegram_message(msg), jpg_path, bypass_cooldown=bypass_telegram_cooldown
                        )
                        self.db.log_event("media", "jpg_notify", 0.0, jpg_path)

                self.notification_queue.task_done()
            except queue.Empty:
                if time.time() - self.last_cleanup > self.cleanup_interval:
                    self._cleanup_old_media()
                    self.last_cleanup = time.time()
                continue
            except Exception as e:
                logging.error(f"Notification worker error: {e}")

    def stop(self) -> None:
        self.running = False
