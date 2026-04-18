"""MQTT client with Home Assistant discovery and Zigbee sensor handling."""

import json
import logging

import requests

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

        mqtt_config = config.get("mqtt", {})
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

            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if username and password:
                self.client.username_pw_set(username, password)

            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect

            logging.info(f"Connecting to MQTT broker {broker}...")
            self.client.connect(broker, port, 60)
            self.client.loop_start()
        except Exception as e:
            logging.error(f"MQTT init failed: {e}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            logging.info("MQTT Connected")
            self._publish_discovery()

            base_topic = self.config["mqtt"]["base_topic"]
            self.client.subscribe(f"{base_topic}/ir/set")
            self.client.subscribe(f"{base_topic}/ir/auto/set")
            self.client.subscribe("zigbee2mqtt/+")
            self.client.subscribe("camera/config/set/#")

            logging.info(f"Subscribed to {base_topic}/ir/*, zigbee2mqtt/+, camera/config/set/#")
        else:
            logging.error(f"MQTT Connection failed: {reason_code}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        try:
            topic = msg.topic
            payload = msg.payload.decode()
            base_topic = self.config["mqtt"]["base_topic"]
            camera_url = self.config["camera_url"]

            logging.debug(f"MQTT Message: {topic} = {payload}")

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
                    logging.info(f"IR LED set to {state_str} (Auto: False)")
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
                    logging.info(f"IR Auto Mode set to {auto_mode}")
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
        logging.warning("MQTT Disconnected")

    def publish(self, topic_suffix: str, payload, retain: bool = False) -> None:
        if not self.client or not self.connected:
            return

        full_topic = f"{self.config['mqtt']['base_topic']}/{topic_suffix}"
        try:
            self.client.publish(full_topic, payload, retain=retain)
        except Exception as e:
            logging.error(f"MQTT publish failed: {e}")

    def _publish_discovery(self) -> None:
        if not self.config["mqtt"].get("discovery", False):
            return

        base_topic = self.config["mqtt"]["base_topic"]
        device_info = {
            "identifiers": ["esp32_camera_a12"],
            "name": "ESP32 AI Camera",
            "model": "ESP32-S3 FireBeetle 2",
            "manufacturer": "DFRobot",
        }

        # Motion Sensor
        self._publish_ha_config("binary_sensor", "motion", {
            "name": "Camera Motion",
            "device_class": "motion",
            "state_topic": f"{base_topic}/motion",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": "esp32_cam_motion",
            "device": device_info,
        })

        # Person Detected
        self._publish_ha_config("binary_sensor", "person", {
            "name": "Person Detected",
            "device_class": "occupancy",
            "state_topic": f"{base_topic}/person",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": "esp32_cam_person",
            "device": device_info,
        })

        # Audio Sensor
        if self.config.get("audio_enabled", False):
            self._publish_ha_config("sensor", "audio_level", {
                "name": "Audio Level",
                "device_class": "signal_strength",
                "unit_of_measurement": "dB",
                "state_topic": f"{base_topic}/audio/level",
                "unique_id": "esp32_cam_audio_level",
                "device": device_info,
            })

        # IR Status
        self._publish_ha_config("binary_sensor", "ir_led_status", {
            "name": "IR LED Status",
            "device_class": "light",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.ir_led_state | iif(true, 'ON', 'OFF') }}",
            "unique_id": "esp32_cam_ir_led_status",
            "device": device_info,
        })

        # IR Control Switch
        self._publish_ha_config("switch", "ir_led", {
            "name": "IR LED Control",
            "icon": "mdi:led-on",
            "command_topic": f"{base_topic}/ir/set",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.ir_led_state | iif(true, 'ON', 'OFF') }}",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": "esp32_cam_ir_switch",
            "device": device_info,
        })

        # IR Auto Mode Switch
        self._publish_ha_config("switch", "ir_auto", {
            "name": "IR Auto Mode",
            "icon": "mdi:brightness-auto",
            "command_topic": f"{base_topic}/ir/auto/set",
            "state_topic": f"{base_topic}/ir/status",
            "value_template": "{{ value_json.auto_mode | iif(true, 'ON', 'OFF') }}",
            "payload_on": "ON", "payload_off": "OFF",
            "unique_id": "esp32_cam_ir_auto_switch",
            "device": device_info,
        })

    def _publish_ha_config(self, component: str, object_id: str, payload: dict) -> None:
        discovery_prefix = self.config["mqtt"].get("discovery_prefix", "homeassistant")
        topic = f"{discovery_prefix}/{component}/esp32_camera/{object_id}/config"
        self.client.publish(topic, json.dumps(payload), retain=True)

    def stop(self) -> None:
        """Stop the MQTT client loop and disconnect."""
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logging.info("MQTT Client stopped")
