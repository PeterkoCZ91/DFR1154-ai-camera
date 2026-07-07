"""MQTT client with Home Assistant discovery and Zigbee sensor handling."""

import json
import logging
import time

import requests

from .mdns_resolver import resolve_camera_url

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False


class MQTTClient:
    def __init__(self, config: dict, callback=None):
        self.config = config
        self.callback = callback
        self.client = None
        self.connected = False
        self.camera_id = self._slug(config.get("camera_id", "esp32_cam"), "esp32_cam")
        self.camera_name = str(config.get("camera_name", self.camera_id)).strip() or self.camera_id
        self.camera_label = str(config.get("telegram", {}).get("camera_label", self.camera_name)).strip() or self.camera_name
        self.log_prefix = f"[{self.camera_id}:{self.camera_name}]"
        mqtt_config = config.get("mqtt", {})
        default_node = "esp32_camera" if self.camera_id == "esp32_cam" else self.camera_id
        self.discovery_node = self._slug(mqtt_config.get("discovery_node", default_node), default_node)
        self.esp32_motion_topic = ""
        self.esp32_uncertain_topic = ""
        self._last_connect_fail_log = 0.0

        if not mqtt_config.get("enabled", False) or not MQTT_AVAILABLE:
            return

        broker = mqtt_config.get("broker_url")
        if not broker:
            logging.error("MQTT enabled but broker_url missing")
            return

        try:
            port = mqtt_config.get("port", 1883)
            username = mqtt_config.get("username")
            password = mqtt_config.get("password")

            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, clean_session=True)
            if username and password:
                self.client.username_pw_set(username, password)

            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect
            self.client.on_connect_fail = self._on_connect_fail
            self.client.reconnect_delay_set(min_delay=5, max_delay=60)

            logging.info(f"{self.log_prefix} Connecting to MQTT broker {broker}:{port}...")
            self.client.connect_async(broker, port, 60)
            self.client.loop_start()
        except Exception as e:
            logging.error(f"{self.log_prefix} MQTT init failed: {e}")

    @staticmethod
    def _slug(value, default: str) -> str:
        text = str(value or default).strip().lower()
        slug = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
        return slug or default

    def _unique_id(self, suffix: str) -> str:
        if self.camera_id == "esp32_cam":
            return f"esp32_cam_{suffix}"
        return f"{self.camera_id}_{suffix}"

    def _entity_name(self, default_name: str) -> str:
        if self.camera_id == "esp32_cam":
            return default_name
        return f"{self.camera_label} {default_name}"

    def _on_connect_fail(self, client, userdata):
        now = time.time()
        if now - self._last_connect_fail_log >= 60:
            self._last_connect_fail_log = now
            logging.warning(f"{self.log_prefix} MQTT broker unavailable; retrying in background")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            logging.info(f"{self.log_prefix} MQTT connected")
            self._publish_discovery()

            base_topic = self.config["mqtt"]["base_topic"]
            self.client.subscribe(f"{base_topic}/ir/set")
            self.client.subscribe(f"{base_topic}/ir/auto/set")
            self.client.subscribe("zigbee2mqtt/+")
            self.client.subscribe("camera/config/set/#")

            # ESP32 firmware signals (published by the camera's MQTT handler)
            esp32_device = self.config.get("esp32_mqtt_device", "ESP32-Camera")
            self.esp32_motion_topic = f"esp32cam/{esp32_device}/motion"
            self.esp32_uncertain_topic = f"esp32cam/{esp32_device}/person_uncertain"
            self.client.subscribe(self.esp32_motion_topic)
            self.client.subscribe(self.esp32_uncertain_topic)

            logging.info(
                f"Subscribed to {base_topic}/ir/*, zigbee2mqtt/+, camera/config/set/#, "
                f"{self.esp32_motion_topic}, {self.esp32_uncertain_topic}"
            )
        else:
            logging.error(f"{self.log_prefix} MQTT connection failed: {reason_code}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        try:
            topic = msg.topic
            payload = msg.payload.decode()
            base_topic = self.config["mqtt"]["base_topic"]
            camera_url = resolve_camera_url(self.config["camera_url"], force=True)

            logging.debug(f"{self.log_prefix} MQTT message: {topic} = {payload}")

            # IR LED Control
            if topic == f"{base_topic}/ir/set":
                state = payload.upper() == "ON"
                state_str = "on" if state else "off"
                try:
                    requests.post(
                        f"{camera_url}/ir-control",
                        json={"auto_mode": False, "state": state_str},
                        timeout=5,
                    )
                    logging.info(f"{self.log_prefix} IR LED set to {state_str} (Auto: False)")
                except Exception as e:
                    logging.error(f"Failed to set IR LED: {e}")

            elif topic == f"{base_topic}/ir/auto/set":
                auto_mode = payload.upper() == "ON"
                try:
                    requests.post(
                        f"{camera_url}/ir-control",
                        json={"auto_mode": auto_mode},
                        timeout=2,
                    )
                    logging.info(f"{self.log_prefix} IR Auto Mode set to {auto_mode}")
                except Exception as e:
                    logging.error(f"Failed to set IR Auto Mode: {e}")

            # Zigbee Sensors
            if topic.startswith("zigbee2mqtt/") and self.callback:
                try:
                    data = json.loads(payload)
                    sensor_name = topic.split("/")[-1]
                    self.callback("zigbee", {"name": sensor_name, "data": data})
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logging.error(f"Zigbee message error: {e}")

            # ESP32 firmware motion signal
            if self.esp32_motion_topic and topic == self.esp32_motion_topic and self.callback:
                detected = payload.strip().upper() == "ON"
                if detected:
                    logging.info(f"{self.log_prefix} ESP32 motion: ON")
                else:
                    logging.debug(f"{self.log_prefix} ESP32 motion: OFF")
                try:
                    self.callback("esp32_motion", {"detected": detected})
                except Exception as e:
                    logging.error(f"ESP32 motion callback error: {e}")

            # ESP32 UNCERTAIN person detection → route to A12 YOLO
            if self.esp32_uncertain_topic and topic == self.esp32_uncertain_topic and self.callback:
                try:
                    data = json.loads(payload)
                    confidence = float(data.get("confidence", 0.0))
                    logging.info(f"{self.log_prefix} ESP32 person_uncertain: conf={confidence:.2f}")
                    self.callback("esp32_person_uncertain", {"confidence": confidence})
                except Exception as e:
                    logging.error(f"ESP32 person_uncertain callback error: {e}")

            # Config Updates
            if topic.startswith("camera/config/set/") and self.callback:
                try:
                    self.callback("config", {"topic": topic, "payload": payload})
                except Exception as e:
                    logging.error(f"Config message error: {e}")

        except Exception as e:
            logging.error(f"MQTT message error: {e}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        logging.warning(f"{self.log_prefix} MQTT disconnected")

    def publish(self, topic_suffix: str, payload, retain: bool = False) -> None:
        if not self.client or not self.connected:
            return

        full_topic = f"{self.config['mqtt']['base_topic']}/{topic_suffix}"
        try:
            self.client.publish(full_topic, payload, retain=retain)
        except Exception as e:
            logging.error(f"{self.log_prefix} MQTT publish failed ({topic_suffix}): {e}")

    def _publish_discovery(self) -> None:
        if not self.config["mqtt"].get("discovery", False):
            return

        base_topic = self.config["mqtt"]["base_topic"]
        device_info = {
            "identifiers": ["esp32_camera_a12" if self.camera_id == "esp32_cam" else f"{self.camera_id}_a12"],
            "name": self.camera_name,
            "model": "ESP32-S3 FireBeetle 2",
            "manufacturer": "DFRobot",
        }

        # Motion Sensor
        self._publish_ha_config("binary_sensor", "motion", {
            "name": self._entity_name("Camera Motion"),
            "device_class": "motion",
            "state_topic": f"{base_topic}/motion",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": self._unique_id("motion"),
            "device": device_info,
        })

        # Person Detected
        self._publish_ha_config("binary_sensor", "person", {
            "name": self._entity_name("Person Detected"),
            "device_class": "occupancy",
            "state_topic": f"{base_topic}/person",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": self._unique_id("person"),
            "device": device_info,
        })

        # Audio Sensor
        if self.config.get("audio_enabled", False):
            self._publish_ha_config("sensor", "audio_level", {
                "name": self._entity_name("Audio Level"),
                "device_class": "signal_strength",
                "unit_of_measurement": "dB",
                "state_topic": f"{base_topic}/audio/level",
                "unique_id": self._unique_id("audio_level"),
                "device": device_info,
            })

        # IR Status
        self._publish_ha_config("binary_sensor", "ir_led_status", {
            "name": self._entity_name("IR LED Status"),
            "device_class": "light",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.ir_led_state | iif(true, 'ON', 'OFF') }}",
            "unique_id": self._unique_id("ir_led_status"),
            "device": device_info,
        })

        # IR Control Switch
        self._publish_ha_config("switch", "ir_led", {
            "name": self._entity_name("IR LED Control"),
            "icon": "mdi:led-on",
            "command_topic": f"{base_topic}/ir/set",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.ir_led_state | iif(true, 'ON', 'OFF') }}",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": self._unique_id("ir_switch"),
            "device": device_info,
        })

        # IR Auto Mode Switch
        self._publish_ha_config("switch", "ir_auto", {
            "name": self._entity_name("IR Auto Mode"),
            "icon": "mdi:brightness-auto",
            "command_topic": f"{base_topic}/ir/auto/set",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.auto_mode | iif(true, 'ON', 'OFF') }}",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": self._unique_id("ir_auto_switch"),
            "device": device_info,
        })

        # Camera profile (DAY / DUSK / NIGHT)
        self._publish_ha_config("sensor", "camera_profile", {
            "name": self._entity_name("Camera Profile"),
            "icon": "mdi:weather-sunset",
            "state_topic": f"{base_topic}/camera/status/profile",
            "unique_id": self._unique_id("profile"),
            "device": device_info,
        })

        # Stream FPS
        self._publish_ha_config("sensor", "stream_fps", {
            "name": self._entity_name("Stream FPS"),
            "icon": "mdi:video",
            "unit_of_measurement": "fps",
            "state_topic": f"{base_topic}/camera/status/stream_fps",
            "unique_id": self._unique_id("stream_fps"),
            "device": device_info,
        })

        # Connection status
        self._publish_ha_config("binary_sensor", "connection", {
            "name": self._entity_name("Camera Connection"),
            "device_class": "connectivity",
            "state_topic": f"{base_topic}/camera/status/connection",
            "payload_on": "online", "payload_off": "offline",
            "unique_id": self._unique_id("connection"),
            "device": device_info,
        })

        # Pipeline state
        self._publish_ha_config("sensor", "pipeline_state", {
            "name": self._entity_name("Pipeline State"),
            "icon": "mdi:state-machine",
            "state_topic": f"{base_topic}/camera/status/pipeline_state",
            "unique_id": self._unique_id("pipeline_state"),
            "device": device_info,
        })

        # Last event score
        self._publish_ha_config("sensor", "event_score", {
            "name": self._entity_name("Last Event Score"),
            "icon": "mdi:gauge",
            "state_topic": f"{base_topic}/camera/detection/score",
            "unique_id": self._unique_id("event_score"),
            "device": device_info,
        })

        # Animal detection
        self._publish_ha_config("sensor", "animal_detected", {
            "name": self._entity_name("Animal Detected"),
            "icon": "mdi:paw",
            "state_topic": f"{base_topic}/camera/detection/animal",
            "unique_id": self._unique_id("animal"),
            "device": device_info,
        })

    def _publish_ha_config(self, component: str, object_id: str, payload: dict) -> None:
        discovery_prefix = self.config["mqtt"].get("discovery_prefix", "homeassistant")
        topic = f"{discovery_prefix}/{component}/{self.discovery_node}/{object_id}/config"
        self.client.publish(topic, json.dumps(payload), retain=True)

    def stop(self) -> None:
        """Stop the MQTT client loop and disconnect."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logging.info(f"{self.log_prefix} MQTT client stopped")
