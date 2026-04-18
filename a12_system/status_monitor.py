"""Background status monitor: heartbeat, sabotage detection, day/night profiles."""

import logging
import threading
import time

import requests


class StatusMonitor(threading.Thread):
    """
    Background thread for polling ESP32 status without blocking the video stream.
    Handles IR status, Audio status, Health checks, and SABOTAGE WATCHDOG.
    """

    def __init__(self, runtime_config, mqtt_client, notifier, db, shared_state, http_session=None):
        super().__init__(name="StatusMonitor")
        self.runtime_config = runtime_config
        self.mqtt_client = mqtt_client
        self.notifier = notifier
        self.db = db
        self.shared_state = shared_state  # {'last_frame': timestamp}
        self.daemon = True
        self.running = True

        self.http_session = http_session or requests.Session()

        # Polling intervals
        config = runtime_config.get_all()
        self.status_interval = config.get("ir_control", {}).get("poll_interval", 10)
        self.health_interval = 3600
        self.adaptive_interval = 60
        self.heartbeat_interval = 5

        # Timers
        self.last_status = 0
        self.last_health = 0
        self.last_adaptive = 0
        self.last_heartbeat = 0

        # State
        self.current_rssi = 0
        self.current_lux = -1.0
        self.current_profile = "UNKNOWN"
        self.sabotage_triggered = False

    def _run_watchdog(self, current_time: float) -> None:
        """Heartbeat and stream watchdog."""
        self.mqtt_client.publish("camera/status/heartbeat", "ON")

        last_frame = self.shared_state.get("last_frame", current_time)
        stream_lag = current_time - last_frame

        timeout = self.runtime_config.get("security.sabotage_timeout", 30.0)

        if stream_lag > timeout:
            if not self.sabotage_triggered:
                logging.error(f"SABOTAGE DETECTED! Stream lost for {stream_lag:.1f}s")
                self.mqtt_client.publish("camera/status/sabotage", "ON")
                self.mqtt_client.publish("camera/status/connection", "offline")

                if self.runtime_config.get("security.mode") == "SECURITY":
                    self.notifier.send_telegram(
                        f"SABOTAGE! Camera signal lost ({int(stream_lag)}s)!"
                    )
                    self.mqtt_client.publish("camera/alarm/trigger", "ON")

                self.sabotage_triggered = True
        else:
            if self.sabotage_triggered:
                logging.info("Stream recovered (Sabotage cleared)")
                self.mqtt_client.publish("camera/status/sabotage", "OFF")
                self.mqtt_client.publish("camera/status/connection", "online")
                self.notifier.send_telegram("Camera signal recovered.")
                self.sabotage_triggered = False

            self.mqtt_client.publish("camera/status/connection", "online")

    def run(self) -> None:
        logging.info("Status Monitor started (Watchdog + Day/Night active)")
        while self.running:
            current_time = time.time()

            if current_time - self.last_heartbeat > self.heartbeat_interval:
                self._run_watchdog(current_time)
                self.last_heartbeat = current_time

            if current_time - self.last_status > self.status_interval:
                self._poll_status()
                self._run_day_night_logic()
                self.last_status = current_time

            if current_time - self.last_adaptive > self.adaptive_interval:
                self._run_adaptive_logic()
                self.last_adaptive = current_time

            if current_time - self.last_health > self.health_interval:
                self._check_health()
                self.last_health = current_time

            time.sleep(1)

    def _run_day_night_logic(self) -> None:
        """Switch camera profiles based on lux."""
        if self.current_lux < 0:
            return

        profiles = self.runtime_config.get("camera_profiles")
        if not profiles:
            return

        thresholds = profiles.get("thresholds", {})
        day_thresh = thresholds.get("lux_day_threshold", 250.0)
        dusk_thresh = thresholds.get("lux_dusk_threshold", 5.0)

        target_profile = self.current_profile

        if self.current_lux < dusk_thresh:
            target_profile = "NIGHT"
        elif self.current_lux > day_thresh:
            target_profile = "DAY"
        elif dusk_thresh <= self.current_lux <= day_thresh:
            target_profile = "DUSK"

        if target_profile != self.current_profile and target_profile != "UNKNOWN":
            logging.info(f"Switching to {target_profile} profile (Lux: {self.current_lux})")

            settings = profiles.get(target_profile)
            if settings:
                try:
                    camera_url = self.runtime_config.get("camera_url")
                    resp = self.http_session.post(
                        f"{camera_url}/settings", json=settings, timeout=5
                    )
                    if resp.status_code == 200:
                        logging.info(f"Camera profile {target_profile} applied")
                        self.current_profile = target_profile
                        self.mqtt_client.publish("camera/status/profile", target_profile)
                    else:
                        logging.error(f"Failed to apply profile: {resp.status_code}")
                except Exception as e:
                    logging.error(f"Error applying profile: {e}")

    def _poll_status(self) -> None:
        try:
            camera_url = self.runtime_config.get("camera_url")

            if self.runtime_config.get("ir_control", {}).get("enabled", False):
                try:
                    resp = self.http_session.get(f"{camera_url}/ir-status", timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        self.current_lux = data.get("lux", -1.0)
                        self.mqtt_client.publish("ir/status", resp.text)
                except Exception as e:
                    logging.debug(f"IR poll failed: {e}")
            else:
                try:
                    resp = self.http_session.get(f"{camera_url}/status", timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        self.current_lux = data.get("ambient_light_lux", -1.0)
                except Exception as e:
                    logging.debug(f"Status poll failed: {e}")

            if self.runtime_config.get("audio_enabled", False):
                try:
                    resp = self.http_session.get(f"{camera_url}/audio-status", timeout=5)
                    if resp.status_code == 200:
                        self.mqtt_client.publish("audio/status", resp.text)
                except Exception as e:
                    logging.debug(f"Audio poll failed: {e}")

        except Exception as e:
            logging.error(f"Status polling error: {e}")
            time.sleep(5)

    def _check_health(self) -> None:
        try:
            camera_url = self.runtime_config.get("camera_url")
            resp = self.http_session.get(f"{camera_url}/health", timeout=5)
            if resp.status_code == 200:
                health = resp.json()
                self.db.log_event("health", "check", 0.0, str(health))

                self.current_rssi = health.get("wifi_rssi", 0)
                self.mqtt_client.publish("rssi", str(self.current_rssi))

                if health.get("overall_health") == "degraded":
                    msg = f"ESP32 Health Warning: {health.get('issues', 'unknown')}"
                    self.notifier.send_telegram(msg)
                    logging.warning(msg)
        except Exception as e:
            logging.error(f"Health check failed: {e}")

    def _run_adaptive_logic(self) -> None:
        if self.current_rssi == 0:
            return

        if self.current_rssi < -80:
            logging.warning(f"Weak signal ({self.current_rssi} dBm)")
        elif self.current_rssi > -60:
            logging.debug(f"Strong signal ({self.current_rssi} dBm)")

    def stop(self) -> None:
        self.running = False
