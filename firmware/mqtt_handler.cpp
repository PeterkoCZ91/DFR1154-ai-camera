#include "mqtt_handler.h"
#include "config.h"
#include <WiFi.h>
#include <PubSubClient.h>
#include "ArduinoJson.h"

extern Config config;

static WiFiClient mqttWifiClient;
static PubSubClient mqttClient(mqttWifiClient);
static bool mqtt_initialized = false;
static unsigned long last_mqtt_reconnect = 0;
static unsigned long last_status_publish = 0;

// HA discovery topic prefix
static String ha_prefix = "homeassistant";
static String device_topic;  // e.g. "esp32cam/ESP32-Camera"

// Forward declaration
static void publishHADiscovery();

static void mqttReconnect() {
    if (mqttClient.connected()) return;
    if (millis() - last_mqtt_reconnect < 10000) return;  // retry every 10s
    last_mqtt_reconnect = millis();

    String clientId = "esp32cam-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.printf("MQTT: Connecting to %s:%d...\n", config.mqtt_server.c_str(), config.mqtt_port);

    bool connected;
    if (config.mqtt_user.length() > 0) {
        connected = mqttClient.connect(clientId.c_str(), config.mqtt_user.c_str(), config.mqtt_pass.c_str());
    } else {
        connected = mqttClient.connect(clientId.c_str());
    }

    if (connected) {
        Serial.println("MQTT: Connected");
        // Publish HA auto-discovery configs
        publishHADiscovery();
    } else {
        Serial.printf("MQTT: Failed, rc=%d\n", mqttClient.state());
    }
}

static void publishHADiscovery() {
    String devName = config.device_name;
    String devId = "esp32cam_" + String((uint32_t)ESP.getEfuseMac(), HEX);

    // Device JSON block (shared across all entities)
    String deviceJson = "\"dev\":{\"ids\":[\"" + devId + "\"],\"name\":\"" + devName + "\",\"mf\":\"DFRobot\",\"mdl\":\"FireBeetle2 ESP32-S3 OV3660\",\"sw\":\"" + String(config.version) + "\"}";

    // Binary sensor: Motion
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Motion";
        doc["stat_t"] = device_topic + "/motion";
        doc["dev_cla"] = "motion";
        doc["pl_on"] = "ON";
        doc["pl_off"] = "OFF";
        doc["uniq_id"] = devId + "_motion";
        String payload;
        serializeJson(doc, payload);
        // Inject device block
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/binary_sensor/" + devId + "/motion/config").c_str(), payload.c_str(), true);
    }

    // Binary sensor: Person
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Person";
        doc["stat_t"] = device_topic + "/person";
        doc["dev_cla"] = "occupancy";
        doc["pl_on"] = "ON";
        doc["pl_off"] = "OFF";
        doc["uniq_id"] = devId + "_person";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/binary_sensor/" + devId + "/person/config").c_str(), payload.c_str(), true);
    }

    // Sensor: Motion Score (changed blocks count)
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Motion Score";
        doc["stat_t"] = device_topic + "/motion_score";
        doc["icon"] = "mdi:motion-sensor";
        doc["uniq_id"] = devId + "_motion_score";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/motion_score/config").c_str(), payload.c_str(), true);
    }

    // Sensor: Motion Percent
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Motion Percent";
        doc["stat_t"] = device_topic + "/motion_percent";
        doc["unit_of_meas"] = "%";
        doc["icon"] = "mdi:percent";
        doc["uniq_id"] = devId + "_motion_pct";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/motion_pct/config").c_str(), payload.c_str(), true);
    }

    // Sensor: Brightness (avg frame brightness, day/night indicator)
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Brightness";
        doc["stat_t"] = device_topic + "/brightness";
        doc["unit_of_meas"] = "";
        doc["icon"] = "mdi:brightness-6";
        doc["uniq_id"] = devId + "_brightness";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/brightness/config").c_str(), payload.c_str(), true);
    }

    // Sensor: WiFi RSSI
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "WiFi Signal";
        doc["stat_t"] = device_topic + "/status";
        doc["val_tpl"] = "{{ value_json.rssi }}";
        doc["unit_of_meas"] = "dBm";
        doc["dev_cla"] = "signal_strength";
        doc["uniq_id"] = devId + "_rssi";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/rssi/config").c_str(), payload.c_str(), true);
    }

    // Sensor: Free Heap
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Free Heap";
        doc["stat_t"] = device_topic + "/status";
        doc["val_tpl"] = "{{ value_json.heap_kb }}";
        doc["unit_of_meas"] = "KB";
        doc["icon"] = "mdi:memory";
        doc["uniq_id"] = devId + "_heap";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/heap/config").c_str(), payload.c_str(), true);
    }

    // Sensor: Uptime
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Uptime";
        doc["stat_t"] = device_topic + "/status";
        doc["val_tpl"] = "{{ value_json.uptime_s }}";
        doc["unit_of_meas"] = "s";
        doc["dev_cla"] = "duration";
        doc["icon"] = "mdi:timer-outline";
        doc["uniq_id"] = devId + "_uptime";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/sensor/" + devId + "/uptime/config").c_str(), payload.c_str(), true);
    }

    Serial.println("MQTT: HA auto-discovery published");
}

void initMQTT() {
    if (!config.mqtt_enabled || config.mqtt_server.length() == 0) {
        Serial.println("MQTT: Disabled");
        return;
    }

    device_topic = "esp32cam/" + config.device_name;

    mqttClient.setServer(config.mqtt_server.c_str(), config.mqtt_port);
    mqttClient.setBufferSize(1024);  // HA discovery payloads can be large
    mqtt_initialized = true;
    Serial.printf("MQTT: Initialized (server=%s:%d, topic=%s)\n",
                  config.mqtt_server.c_str(), config.mqtt_port, device_topic.c_str());
}

void mqttLoop() {
    if (!mqtt_initialized || !config.mqtt_enabled) return;
    if (WiFi.status() != WL_CONNECTED) return;

    if (!mqttClient.connected()) {
        mqttReconnect();
    }
    mqttClient.loop();

    // Publish status every 60 seconds
    if (mqttClient.connected() && millis() - last_status_publish > 60000) {
        mqttPublishStatus();
        last_status_publish = millis();
    }
}

void mqttPublishMotion(bool detected) {
    if (!mqtt_initialized || !mqttClient.connected()) return;
    mqttClient.publish((device_topic + "/motion").c_str(), detected ? "ON" : "OFF");
}

void mqttPublishPerson(bool detected, float confidence) {
    if (!mqtt_initialized || !mqttClient.connected()) return;
    mqttClient.publish((device_topic + "/person").c_str(), detected ? "ON" : "OFF");
    if (detected) {
        mqttClient.publish((device_topic + "/person_confidence").c_str(), String(confidence, 2).c_str());
    }
}

void mqttPublishMotionScore(int score, float percent) {
    if (!mqtt_initialized || !mqttClient.connected()) return;
    mqttClient.publish((device_topic + "/motion_score").c_str(), String(score).c_str());
    mqttClient.publish((device_topic + "/motion_percent").c_str(), String(percent, 1).c_str());
}

void mqttPublishBrightness(uint8_t brightness) {
    if (!mqtt_initialized || !mqttClient.connected()) return;
    mqttClient.publish((device_topic + "/brightness").c_str(), String(brightness).c_str());
}

void mqttPublishStatus() {
    if (!mqtt_initialized || !mqttClient.connected()) return;

    StaticJsonDocument<256> doc;
    doc["rssi"] = WiFi.RSSI();
    doc["heap_kb"] = ESP.getFreeHeap() / 1024;
    doc["psram_kb"] = ESP.getFreePsram() / 1024;
    doc["uptime_s"] = (unsigned long)(esp_timer_get_time() / 1000000);
    doc["ip"] = WiFi.localIP().toString();

    String payload;
    serializeJson(doc, payload);
    mqttClient.publish((device_topic + "/status").c_str(), payload.c_str());
}
