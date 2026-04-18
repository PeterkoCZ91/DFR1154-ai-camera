"""Home Assistant sensor polling for motion/door sensor fusion."""

import logging
import threading
import time
from typing import Callable, Optional

import requests


class HAMonitor(threading.Thread):
    """Background thread polling HA sensors, triggering callbacks on state changes."""

    def __init__(self, config: dict, callback: Optional[Callable] = None):
        super().__init__(name="HAMonitor")
        self.config = config
        self.callback = callback
        self.daemon = True
        self.running = False

        self.ha_url = config.get("home_assistant_url", "http://homeassistant.local:8123")
        self.ha_token = config.get("home_assistant_token")

        if not self.ha_token:
            logging.error("HA Monitor: No Home Assistant token configured!")
            return

        self.headers = {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }

        self.poll_interval = config.get("ha_poll_interval", 1.0)
        self.monitored_sensors: list[str] = config.get("ha_monitored_sensors", [])

        # State tracking
        self.sensor_states: dict[str, str] = {}
        self.sensor_last_active: dict[str, float] = {}

        logging.info(f"HA Monitor initialized (polling every {self.poll_interval}s)")
        logging.info(f"   Monitoring {len(self.monitored_sensors)} sensors")

    def _get_sensor_states(self) -> dict[str, dict]:
        """Fetch current states of monitored sensors from HA."""
        try:
            response = requests.get(
                f"{self.ha_url}/api/states",
                headers=self.headers,
                timeout=5,
            )
            if response.status_code == 200:
                all_states = response.json()
                return {
                    s["entity_id"]: s
                    for s in all_states
                    if s.get("entity_id") in self.monitored_sensors
                }
            else:
                logging.warning(f"HA API returned status {response.status_code}")
                return {}
        except requests.exceptions.Timeout:
            logging.warning("HA API timeout")
            return {}
        except Exception as e:
            logging.error(f"HA API error: {e}")
            return {}

    def run(self) -> None:
        logging.info("HA Monitor thread started")
        self.running = True

        # Initialize states
        initial_states = self._get_sensor_states()
        for entity_id, state_obj in initial_states.items():
            self.sensor_states[entity_id] = state_obj.get("state")
            friendly_name = state_obj.get("attributes", {}).get("friendly_name", entity_id)
            logging.info(f"   Sensor: {friendly_name}: {self.sensor_states[entity_id]}")

        while self.running:
            try:
                current_states = self._get_sensor_states()

                for entity_id, state_obj in current_states.items():
                    new_state = state_obj.get("state")
                    old_state = self.sensor_states.get(entity_id, "unknown")

                    if old_state != new_state and new_state != "unavailable":
                        friendly_name = state_obj.get("attributes", {}).get("friendly_name", entity_id)
                        attributes = state_obj.get("attributes", {})

                        logging.info(f"HA Sensor: {friendly_name} -> {new_state}")
                        self.sensor_states[entity_id] = new_state

                        if new_state == "on":
                            self.sensor_last_active[entity_id] = time.time()

                        if self.callback:
                            try:
                                self.callback(
                                    entity_id=entity_id,
                                    sensor_name=friendly_name,
                                    old_state=old_state,
                                    new_state=new_state,
                                    attributes=attributes,
                                )
                            except Exception as e:
                                logging.error(f"HA callback error: {e}")

                time.sleep(self.poll_interval)

            except Exception as e:
                logging.error(f"HA Monitor error: {e}")
                time.sleep(5)

    def stop(self) -> None:
        logging.info("HA Monitor stopping...")
        self.running = False

    def get_current_states(self) -> dict[str, str]:
        return self.sensor_states.copy()

    def is_any_sensor_recently_active(self, window_seconds: float = 10.0) -> bool:
        """Check if any sensor was active within the time window."""
        current_time = time.time()
        return any(
            (current_time - t) <= window_seconds
            for t in self.sensor_last_active.values()
        )

    def get_recently_active_sensors(self, window_seconds: float = 10.0) -> list[str]:
        """Get entity IDs of sensors active within the time window."""
        current_time = time.time()
        return [
            entity_id
            for entity_id, t in self.sensor_last_active.items()
            if (current_time - t) <= window_seconds
        ]
