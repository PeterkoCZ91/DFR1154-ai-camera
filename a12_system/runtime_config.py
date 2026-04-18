"""Thread-safe runtime configuration manager with MQTT-driven updates."""

import copy
import json
import logging
import threading


class RuntimeConfig:
    """
    Thread-safe runtime configuration manager.
    Allows dynamic config changes without restart.
    """

    def __init__(self, config: dict):
        self._config = config
        self._lock = threading.Lock()
        self._callbacks: list = []

    def get(self, key: str, default=None):
        """Thread-safe config read. Supports dot-notation: 'security.mode'."""
        with self._lock:
            keys = key.split(".")
            value = self._config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k, default)
                else:
                    return default
            return value

    def set(self, key: str, value) -> None:
        """Thread-safe config write with callback notification."""
        with self._lock:
            keys = key.split(".")
            config = self._config

            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]

            old_value = config.get(keys[-1])
            config[keys[-1]] = value

            logging.info(f"Config updated: {key} = {value} (was: {old_value})")

            for callback in self._callbacks:
                try:
                    callback(key, value, old_value)
                except Exception as e:
                    logging.error(f"Config callback error: {e}")

    def get_all(self) -> dict:
        """Get full config dict (thread-safe deep copy)."""
        with self._lock:
            return copy.deepcopy(self._config)

    def register_callback(self, callback) -> None:
        """Register callback function for config changes."""
        self._callbacks.append(callback)

    def update_from_mqtt(self, topic: str, payload) -> bool:
        """
        Update config from MQTT message.
        Topic format: camera/config/set/<key>
        Returns True if config was updated.
        """
        if not topic.startswith("camera/config/set/"):
            return False

        key = topic.replace("camera/config/set/", "")

        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")

            # Parse payload: JSON first, then bool/number/string
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                payload_lower = payload.lower()
                if payload_lower in ("true", "on", "1"):
                    value = True
                elif payload_lower in ("false", "off", "0"):
                    value = False
                elif payload_lower.replace(".", "", 1).replace("-", "", 1).isdigit():
                    value = float(payload) if "." in payload else int(payload)
                else:
                    value = payload

            # Map MQTT keys to internal config paths
            config_map = {
                "mode": "security.mode",
                "person_detection": "security.person_detection",
                "animal_detection": "security.animal_detection",
                "motion_sensitivity": "motion.threshold",
                "yolo_confidence": "yolo.confidence_threshold",
                "audio_detection": "audio_enabled",
                "ir_control": "ir_control.enabled",
            }

            internal_key = config_map.get(key)
            if not internal_key:
                logging.warning(f"Unknown MQTT config key: {key}")
                return False

            if key == "mode" and value not in ["MONITOR", "SECURITY", "TEST"]:
                logging.error(f"Invalid mode: {value}. Must be MONITOR/SECURITY/TEST")
                return False

            self.set(internal_key, value)

            # Special handling for security mode transitions
            if key == "mode":
                if value == "SECURITY":
                    self.set("security.require_sensor_confirmation", True)
                    self.set("security.sabotage_detection", True)
                    self.set("audio_enabled", True)
                    logging.warning("SECURITY MODE ACTIVATED - Sensor confirmation REQUIRED")
                elif value == "MONITOR":
                    self.set("security.require_sensor_confirmation", False)
                    self.set("security.sabotage_detection", False)
                    logging.info("MONITOR MODE - Logging only, no alarms")
                elif value == "TEST":
                    self.set("security.require_sensor_confirmation", False)
                    self.set("security.sabotage_detection", False)
                    logging.info("TEST MODE - Debug logging enabled")

            return True

        except Exception as e:
            logging.error(f"Failed to parse MQTT config: {e}")
            return False
