#include "mqtt_handler.h"
#include "config.h"
#include <WiFi.h>
#include <PubSubClient.h>
#include "ArduinoJson.h"
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include "event_log.h"

extern Config config;
extern bool is_recording;
extern bool startSDRecording();
extern void stopSDRecording();
extern void sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption);

// Set by mqttHandleCommands(), consumed by main loop (same pattern as Telegram /photo command)
volatile bool mqtt_snapshot_flag = false;

// Commands decoded in callback, executed in mqttHandleCommands() (main-loop context)
static volatile bool cmd_snapshot         = false;
static volatile bool cmd_recording_on     = false;
static volatile bool cmd_recording_off    = false;
static volatile bool cmd_person_on        = false;
static volatile bool cmd_person_off       = false;

static WiFiClient mqttWifiClient;
static PubSubClient mqttClient(mqttWifiClient);
static bool mqtt_initialized = false;
static unsigned long last_mqtt_reconnect = 0;
static unsigned long last_status_publish = 0;

// PubSubClient is not thread-safe. mqttLoop() runs from motionTask (Core 0) and
// publish helpers fire from motionTask + personDetectionTask. Without this lock
// the TCP write path corrupts on concurrent calls.
static SemaphoreHandle_t mqtt_mutex = NULL;

static inline bool mqttLock(TickType_t timeout = pdMS_TO_TICKS(200)) {
    if (!mqtt_mutex) return true;
    return xSemaphoreTake(mqtt_mutex, timeout) == pdTRUE;
}

static inline void mqttUnlock() {
    if (mqtt_mutex) xSemaphoreGive(mqtt_mutex);
}

// HA discovery topic prefix
static String ha_prefix = "homeassistant";
static String device_topic;  // e.g. "esp32cam/ESP32-Camera"

// Forward declarations
static void publishHADiscovery();
static void mqttPublishStates();

static void mqttCallback(char* topic, byte* payload, unsigned int length) {
    // Called inside mqttClient.loop() — mutex is held. Only set volatile flags here.
    char msg[32] = {0};
    size_t copyLen = (length < sizeof(msg) - 1) ? length : sizeof(msg) - 1;
    memcpy(msg, payload, copyLen);

    String t = String(topic);
    String cmd_base = device_topic + "/cmd/";

    if (t == cmd_base + "snapshot") {
        cmd_snapshot = true;
    } else if (t == cmd_base + "recording") {
        if (strncmp(msg, "ON", 2) == 0) cmd_recording_on = true;
        else cmd_recording_off = true;
    } else if (t == cmd_base + "person_detection") {
        if (strncmp(msg, "ON", 2) == 0) cmd_person_on = true;
        else cmd_person_off = true;
    }
}

static void mqttSubscribe() {
    String base = device_topic + "/cmd/";
    mqttClient.subscribe((base + "snapshot").c_str());
    mqttClient.subscribe((base + "recording").c_str());
    mqttClient.subscribe((base + "person_detection").c_str());
    Serial.println("MQTT: subscribed to command topics");
}

static void mqttReconnect() {
    if (mqttClient.connected()) return;
    if (millis() - last_mqtt_reconnect < 10000) return;  // retry every 10s
    last_mqtt_reconnect = millis();
    // MQTT connect allocates TLS/TCP buffers; rapid retries on low heap fragment
    // the remaining memory further. Defer until heap recovers.
    if (ESP.getFreeHeap() < 60000) {
        Serial.printf("MQTT: reconnect deferred, heap too low (%u B)\n", ESP.getFreeHeap());
        return;
    }
    if (config.mqtt_server.length() == 0) {
        Serial.println("MQTT: server not configured, skipping reconnect");
        return;
    }

    // Re-apply server in case settings changed at runtime after mqttInit()
    mqttClient.setServer(config.mqtt_server.c_str(), config.mqtt_port);
    device_topic = "esp32cam/" + config.device_name;

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
        mqttSubscribe();
        publishHADiscovery();
        mqttPublishStates();
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

    // Switch: Recording
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Recording";
        doc["stat_t"] = device_topic + "/recording_state";
        doc["cmd_t"]  = device_topic + "/cmd/recording";
        doc["pl_on"]  = "ON";
        doc["pl_off"] = "OFF";
        doc["icon"]   = "mdi:record-circle";
        doc["uniq_id"] = devId + "_recording";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/switch/" + devId + "/recording/config").c_str(), payload.c_str(), true);
    }

    // Switch: Person Detection
    {
        StaticJsonDocument<512> doc;
        doc["name"] = "Person Detection";
        doc["stat_t"] = device_topic + "/person_detection_state";
        doc["cmd_t"]  = device_topic + "/cmd/person_detection";
        doc["pl_on"]  = "ON";
        doc["pl_off"] = "OFF";
        doc["icon"]   = "mdi:account-eye";
        doc["uniq_id"] = devId + "_person_detect";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/switch/" + devId + "/person_detection/config").c_str(), payload.c_str(), true);
    }

    // Button: Snapshot
    {
        StaticJsonDocument<512> doc;
        doc["name"]    = "Snapshot";
        doc["cmd_t"]   = device_topic + "/cmd/snapshot";
        doc["pl_prs"]  = "TRIGGER";
        doc["icon"]    = "mdi:camera";
        doc["uniq_id"] = devId + "_snapshot";
        String payload;
        serializeJson(doc, payload);
        payload = payload.substring(0, payload.length() - 1) + "," + deviceJson + "}";
        mqttClient.publish((ha_prefix + "/button/" + devId + "/snapshot/config").c_str(), payload.c_str(), true);
    }

    Serial.println("MQTT: HA auto-discovery published");
}

void initMQTT() {
    if (!config.mqtt_enabled || config.mqtt_server.length() == 0) {
        Serial.println("MQTT: Disabled");
        return;
    }

    if (mqtt_mutex == NULL) {
        mqtt_mutex = xSemaphoreCreateMutex();
        if (!mqtt_mutex) {
            Serial.println("MQTT: failed to create mutex, aborting init");
            return;
        }
    }

    device_topic = "esp32cam/" + config.device_name;

    mqttClient.setServer(config.mqtt_server.c_str(), config.mqtt_port);
    mqttClient.setBufferSize(1024);  // HA discovery payloads can be large
    mqttClient.setCallback(mqttCallback);
    mqtt_initialized = true;
    Serial.printf("MQTT: Initialized (server=%s:%d, topic=%s)\n",
                  config.mqtt_server.c_str(), config.mqtt_port, device_topic.c_str());
}

void mqttLoop() {
    if (!mqtt_initialized || !config.mqtt_enabled) return;
    if (WiFi.status() != WL_CONNECTED) return;

    if (!mqttLock()) return;  // skip iteration on contention rather than block

    if (!mqttClient.connected()) {
        mqttReconnect();
    }
    mqttClient.loop();

    bool need_status = mqttClient.connected() && (millis() - last_status_publish > 60000);
    if (need_status) {
        last_status_publish = millis();
    }
    mqttUnlock();

    if (need_status) {
        mqttPublishStatus();  // takes lock internally
    }
}

void mqttPublishMotion(bool detected) {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/motion").c_str(), detected ? "ON" : "OFF");
    }
    mqttUnlock();
}

void mqttPublishPerson(bool detected, float confidence) {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/person").c_str(), detected ? "ON" : "OFF");
        if (detected) {
            mqttClient.publish((device_topic + "/person_confidence").c_str(), String(confidence, 2).c_str());
        }
    }
    mqttUnlock();
}

bool mqttPersonUncertainConnected() {
    return mqtt_initialized && mqttClient.connected();
}

void mqttPublishPersonUncertain(float confidence) {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        char buf[48];
        snprintf(buf, sizeof(buf), "{\"detected\":true,\"confidence\":%.2f}", confidence);
        mqttClient.publish((device_topic + "/person_uncertain").c_str(), buf);
    }
    mqttUnlock();
}

void mqttPublishMotionScore(int score, float percent) {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/motion_score").c_str(), String(score).c_str());
        mqttClient.publish((device_topic + "/motion_percent").c_str(), String(percent, 1).c_str());
    }
    mqttUnlock();
}

void mqttPublishBrightness(uint8_t brightness) {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/brightness").c_str(), String(brightness).c_str());
    }
    mqttUnlock();
}

void mqttPublishStatus() {
    if (!mqtt_initialized) return;

    StaticJsonDocument<256> doc;
    doc["rssi"] = WiFi.RSSI();
    doc["heap_kb"] = ESP.getFreeHeap() / 1024;
    doc["psram_kb"] = ESP.getFreePsram() / 1024;
    doc["uptime_s"] = (unsigned long)(esp_timer_get_time() / 1000000);
    doc["ip"] = WiFi.localIP().toString();

    String payload;
    serializeJson(doc, payload);

    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/status").c_str(), payload.c_str());
    }
    mqttUnlock();
}

static void mqttPublishStates() {
    if (!mqtt_initialized) return;
    if (!mqttLock()) return;
    if (mqttClient.connected()) {
        mqttClient.publish((device_topic + "/recording_state").c_str(),
                           is_recording ? "ON" : "OFF", true);
        mqttClient.publish((device_topic + "/person_detection_state").c_str(),
                           config.person_detection_enabled ? "ON" : "OFF", true);
    }
    mqttUnlock();
}

void mqttHandleCommands() {
    if (!mqtt_initialized) return;

    if (cmd_snapshot) {
        cmd_snapshot = false;
        mqtt_snapshot_flag = true;
        Serial.println("MQTT: snapshot command received");
    }

    bool stateChanged = false;

    if (cmd_recording_on) {
        cmd_recording_on = false;
        if (!is_recording) {
            startSDRecording();
            logEvent(EVT_RECORDING_STARTED, "mqtt");
        }
        stateChanged = true;
    }
    if (cmd_recording_off) {
        cmd_recording_off = false;
        if (is_recording) {
            stopSDRecording();
        }
        stateChanged = true;
    }
    if (cmd_person_on) {
        cmd_person_on = false;
        config.person_detection_enabled = true;
        Serial.println("MQTT: person detection ON");
        stateChanged = true;
    }
    if (cmd_person_off) {
        cmd_person_off = false;
        config.person_detection_enabled = false;
        Serial.println("MQTT: person detection OFF");
        stateChanged = true;
    }

    if (stateChanged) mqttPublishStates();
}
