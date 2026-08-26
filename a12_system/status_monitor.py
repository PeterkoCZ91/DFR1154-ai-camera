"""Background status monitor: heartbeat, sabotage detection, day/night profiles."""

import logging
import os
import threading
import time
from datetime import date, datetime

import requests

from .mdns_resolver import resolve_camera_url


class StatusMonitor(threading.Thread):
    """
    Background thread for polling ESP32 status without blocking the video stream.
    Handles IR status, Audio status, Health checks, and SABOTAGE WATCHDOG.
    """

    def __init__(self, runtime_config, mqtt_client, notifier, db, shared_state, http_session=None, stats=None):
        super().__init__(name="StatusMonitor")
        self.runtime_config = runtime_config
        self.mqtt_client = mqtt_client
        self.notifier = notifier
        self.db = db
        self.shared_state = shared_state  # {'last_frame': timestamp}
        self.stats = stats
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

        # Profile hysteresis — prevents oscillation at lux thresholds
        self._pending_profile: str | None = None
        self._pending_since: float = 0.0
        self._profile_confirm_seconds: int = 90
        self._profile_apply_disabled_logged = False

        # Daily summary
        data_dir = os.environ.get("A12_DATA_DIR") or os.environ.get("A12_CONFIG_DIR")
        self._daily_summary_state_path = (
            os.path.join(data_dir, "daily_summary_date.txt") if data_dir else None
        )
        self._last_daily_date: date | None = self._load_daily_summary_date()
        # Resource monitoring
        self.resource_check_interval = 60
        self.last_resource_check = 0
        self._resource_alert_sent = False

        # Health restart tracking. Firmware power_health is based on lifetime counters,
        # so alert only when the counter increases or uptime is freshly short.
        self._last_total_restarts: int | None = None

    def _camera_url(self, *, force: bool = False) -> str:
        return resolve_camera_url(self.runtime_config.get("camera_url"), force=force)

    def _run_watchdog(self, current_time: float) -> None:
        """Heartbeat and stream watchdog."""
        self.mqtt_client.publish("camera/status/heartbeat", "ON")

        last_frame = self.shared_state.get("last_frame", current_time)
        stream_lag = current_time - last_frame

        timeout = self.runtime_config.get("security.sabotage_timeout", 60.0)

        if stream_lag > timeout:
            if not self.sabotage_triggered:
                logging.error(f"SABOTAGE DETECTED! Stream lost for {stream_lag:.1f}s")
                self.mqtt_client.publish("camera/status/sabotage", "ON")
                self.mqtt_client.publish("camera/status/connection", "offline")

                if self.runtime_config.get("security.mode") == "SECURITY":
                    # bypass_cooldown: this is the highest-priority alert the system
                    # has, and it was the only one subject to the global Telegram
                    # cooldown — the *less* urgent MONITOR message below always
                    # bypassed it. A routine clip sent a minute earlier silently
                    # swallowed the sabotage alarm.
                    self.notifier.send_telegram(
                        f"SABOTAGE! Camera signal lost ({int(stream_lag)}s)!",
                        bypass_cooldown=True,
                    )
                    self.mqtt_client.publish("camera/alarm/trigger", "ON")
                else:
                    self.notifier.send_telegram(
                        f"Camera stream lost ({int(stream_lag)}s) — reconnecting...",
                        bypass_cooldown=True,
                    )

                self.sabotage_triggered = True
        else:
            if self.sabotage_triggered:
                logging.info("Stream recovered (Sabotage cleared)")
                self.mqtt_client.publish("camera/status/sabotage", "OFF")
                self.mqtt_client.publish("camera/status/connection", "online")
                # Pairs with the loss alert above: suppressing the recovery message
                # leaves an operator staring at an unresolved alarm.
                self.notifier.send_telegram("Camera signal recovered.", bypass_cooldown=True)
                self.sabotage_triggered = False

            self.mqtt_client.publish("camera/status/connection", "online")

    def run(self) -> None:
        logging.info("Status Monitor started (Watchdog + Day/Night active)")
        while self.running:
            # One unhandled exception used to take this whole thread down, and with
            # it the sabotage watchdog, the heartbeat, day/night profiles and the
            # resource alerts — permanently, while the process kept running and the
            # logs kept looking healthy. A camera that went dark after that was
            # never reported. HAMonitor and AudioMonitor already guard their loops;
            # this one was the exception.
            try:
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
                    self._check_daily_summary()
                    self.last_adaptive = current_time

                if current_time - self.last_resource_check > self.resource_check_interval:
                    self._check_resources()
                    self.last_resource_check = current_time

                if current_time - self.last_health > self.health_interval:
                    self._check_health()
                    self.last_health = current_time
            except Exception as e:
                # Timestamps were already advanced for whatever ran, so a failing
                # sub-check cannot spin: the next iteration waits out its interval.
                logging.exception(f"Status Monitor iteration failed: {e}")

            time.sleep(1)

    def _run_day_night_logic(self) -> None:
        """Switch camera profiles based on lux, with 90s hysteresis to prevent oscillation."""
        if self.current_lux < 0:
            return

        profiles = self.runtime_config.get("camera_profiles")
        if not profiles:
            return
        if not profiles.get("apply_from_a12", False):
            if not self._profile_apply_disabled_logged:
                logging.info("A12 camera profile writes disabled; firmware owns DAY/DUSK/NIGHT profiles")
                self._profile_apply_disabled_logged = True
            return

        thresholds = profiles.get("thresholds", {})
        day_thresh = thresholds.get("lux_day_threshold", 250.0)
        dusk_thresh = thresholds.get("lux_dusk_threshold", 5.0)

        if self.current_lux < dusk_thresh:
            target_profile = "NIGHT"
        elif self.current_lux > day_thresh:
            target_profile = "DAY"
        else:
            target_profile = "DUSK"

        if target_profile == self.current_profile:
            self._pending_profile = None
            return

        # Different profile required — require stable reading for confirm_seconds before switching
        if self._pending_profile != target_profile:
            self._pending_profile = target_profile
            self._pending_since = time.time()
            logging.debug(
                f"Profile {target_profile} pending "
                f"(lux={self.current_lux:.2f}, current={self.current_profile})"
            )
            return

        if time.time() - self._pending_since < self._profile_confirm_seconds:
            return

        # Stable for confirm_seconds — apply
        logging.info(
            f"Switching to {target_profile} profile "
            f"(Lux: {self.current_lux:.2f}, stable {self._profile_confirm_seconds}s)"
        )
        settings = profiles.get(target_profile)
        if settings:
            try:
                camera_url = self._camera_url()
                resp = self.http_session.post(
                    f"{camera_url}/settings", json=settings, timeout=5
                )
                if resp.status_code == 200:
                    logging.info(f"Camera profile {target_profile} applied")
                    self.current_profile = target_profile
                    self._pending_profile = None
                    self.mqtt_client.publish("camera/status/profile", target_profile)
                else:
                    logging.error(f"Failed to apply profile: {resp.status_code}")
            except Exception as e:
                logging.error(f"Error applying profile: {e}")

    def _poll_status(self) -> None:
        try:
            camera_url = self._camera_url()

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
                        # Detect camera reboot: uptime < 3 min means NVS values are stale.
                        # Reset current_profile so _run_day_night_logic re-applies the profile.
                        uptime = data.get("uptime_seconds", 9999)
                        if uptime < 180 and self.current_profile != "UNKNOWN":
                            logging.info(
                                f"Camera reboot detected (uptime={uptime}s) — "
                                "resetting profile for re-application"
                            )
                            self.current_profile = "UNKNOWN"
                            self._pending_profile = None
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
            camera_url = self._camera_url()
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

                total_restarts = int(health.get("total_restarts", 0) or 0)
                uptime_seconds = int(health.get("uptime_seconds", 0) or 0)
                restarts_increased = (
                    self._last_total_restarts is not None
                    and total_restarts > self._last_total_restarts
                )
                self._last_total_restarts = total_restarts

                if health.get("power_health") == "suspect":
                    if restarts_increased or uptime_seconds < 3600:
                        msg = (
                            "ESP32 Power Warning: "
                            f"reason={health.get('last_restart_reason_name', 'unknown')} "
                            f"total_restarts={total_restarts} "
                            f"poweron={health.get('power_restarts_poweron', 0)} "
                            f"brownout={health.get('power_restarts_brownout', 0)} "
                            f"uptime={uptime_seconds}s"
                        )
                        self.notifier.send_telegram(msg, bypass_cooldown=True)
                        logging.warning(msg)
                    else:
                        logging.info(
                            "Suppressing historical ESP32 power_health=suspect "
                            "(total_restarts=%s, uptime=%ss)",
                            total_restarts,
                            uptime_seconds,
                        )
        except Exception as e:
            logging.error(f"Health check failed: {e}")

    def _run_adaptive_logic(self) -> None:
        if self.current_rssi == 0:
            return

        if self.current_rssi < -80:
            logging.warning(f"Weak signal ({self.current_rssi} dBm)")
        elif self.current_rssi > -60:
            logging.debug(f"Strong signal ({self.current_rssi} dBm)")

    def _check_resources(self) -> None:
        """Check container memory via cgroup v2, alert via Telegram if critical."""
        try:
            mem_current = int(open("/sys/fs/cgroup/memory.current").read())
            mem_max_raw = open("/sys/fs/cgroup/memory.max").read().strip()
            if mem_max_raw == "max":
                return

            mem_max = int(mem_max_raw)

            # Subtract inactive page cache to match docker stats working set
            inactive_file = 0
            try:
                for line in open("/sys/fs/cgroup/memory.stat"):
                    if line.startswith("inactive_file "):
                        inactive_file = int(line.split()[1])
                        break
            except Exception:
                pass

            mem_used = max(0, mem_current - inactive_file)
            mem_pct = mem_used / mem_max * 100
            mem_mb = mem_used // (1024 * 1024)
            max_mb = mem_max // (1024 * 1024)

            self.mqtt_client.publish("camera/status/mem_mb", str(mem_mb), retain=True)
            self.mqtt_client.publish("camera/status/mem_pct", f"{mem_pct:.0f}", retain=True)

            if mem_pct > 85:
                if not self._resource_alert_sent:
                    msg = (
                        f"A12 resource warning\n"
                        f"RAM: {mem_mb}/{max_mb} MB ({mem_pct:.0f}%)\n"
                        f"Consider reducing clip buffer or raising mem_limit in compose."
                    )
                    logging.warning(msg)
                    self.notifier.send_telegram(msg, bypass_cooldown=True)
                    self._resource_alert_sent = True
            elif mem_pct < 70:
                self._resource_alert_sent = False

        except FileNotFoundError:
            pass
        except Exception as e:
            logging.debug(f"Resource check error: {e}")

    def _load_daily_summary_date(self) -> date | None:
        if not self._daily_summary_state_path:
            return None
        try:
            with open(self._daily_summary_state_path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
            return date.fromisoformat(value) if value else None
        except FileNotFoundError:
            return None
        except Exception as e:
            logging.debug(f"Failed to load daily summary state: {e}")
            return None

    def _save_daily_summary_date(self, sent_date: date) -> None:
        if not self._daily_summary_state_path:
            return
        try:
            with open(self._daily_summary_state_path, "w", encoding="utf-8") as handle:
                handle.write(sent_date.isoformat())
        except Exception as e:
            logging.debug(f"Failed to save daily summary state: {e}")

    def _check_daily_summary(self) -> None:
        """Send daily stats summary at 8:00 AM (once per calendar day)."""
        now = datetime.now()
        today = date.today()
        if now.hour >= 8 and self._last_daily_date != today:
            self._send_daily_summary()
            self._last_daily_date = today
            self._save_daily_summary_date(today)

    def _send_daily_summary(self) -> None:
        if not self.stats:
            return
        summary = self.stats.get_summary()
        uptime = summary.get("session", {}).get("uptime_formatted", "?")

        counts = {}
        if self.db and hasattr(self.db, "get_event_counts_since"):
            counts = self.db.get_event_counts_since(time.time() - 86400)

        def count_type(event_type: str) -> int:
            return sum(value for (typ, _label), value in counts.items() if typ == event_type)

        summary_items = [
            ("PIR/HA spuštění", count_type("ha_sensor")),
            ("Lokální klipy", counts.get(("detection", "motion"), 0)),
            ("Potvrzená osoba", counts.get(("detection", "person"), 0)),
            ("Pes", counts.get(("detection", "dog"), 0)),
            ("Audio alerty", counts.get(("audio", "LOUD_NOISE"), 0)),
        ]
        event_lines = "\n".join(
            f"  {label}: {value}x" for label, value in summary_items if value > 0
        ) or "  (žádné)"

        msg = (
            "A12 denní přehled\n"
            f"Uptime: {uptime}\n"
            f"Události (24h):\n{event_lines}"
        )
        scorer_summary = summary.get("scorer", {})
        if scorer_summary.get("requests", 0) or scorer_summary.get("fallbacks", 0):
            # Scorer counters are cumulative since process start, unlike the 24h
            # event counts above — say so instead of implying a daily window.
            failures = scorer_summary.get("transport_failures", 0) + scorer_summary.get(
                "http_errors", 0
            )
            msg += (
                "\nScorer (od startu): "
                f"{scorer_summary.get('successes', 0)} OK / "
                f"{failures} chyb / "
                f"{scorer_summary.get('fallbacks', 0)} fallbacků"
                f", p95 {scorer_summary.get('request_seconds_p95', 0):.2f}s"
            )
        face_enabled = self.runtime_config.get("face_recognition.enabled", False)
        if face_enabled:
            known_faces = sum(
                value
                for (typ, label), value in counts.items()
                if typ == "face" and label not in {"unknown", "Unknown", "No face", "Error", "Invalid frame"}
            )
            unknown_faces = sum(
                value
                for (typ, label), value in counts.items()
                if typ == "face" and label in {"unknown", "Unknown"}
            )
            if known_faces or unknown_faces:
                msg += f"\nObličeje (24h): {known_faces} known / {unknown_faces} unknown"
        self.notifier.send_telegram(msg, bypass_cooldown=True)
        logging.info("Daily summary sent")

    def stop(self) -> None:
        self.running = False
