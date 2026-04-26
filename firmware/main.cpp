#include "esp_camera.h"
#include <WiFi.h>
#include "esp_wifi.h"  // For esp_wifi_set_ps()
#include <HTTPClient.h>
#include <WiFiClientSecure.h>  // For Telegram photo upload (HTTPS multipart)
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "FS.h"
#include "LittleFS.h"
#include "SD.h"
#include "ArduinoJson.h"
#include "board_config.h"
#include "camera_server.h"
#include "camera_capture.h" // Added for camera_heartbeat
#include "config.h"
#include "ir_control.h"
#include "health_monitor.h"
#ifdef INCLUDE_AUDIO
#include "audio_handler.h"
#endif
#ifdef INCLUDE_MQTT
#include "mqtt_handler.h"
#endif
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <DNSServer.h>
#include "esp_heap_trace.h"
#include "esp_http_server.h"  // For httpd_stop() in OTA handler
#include "ws_log.h"           // Live log via SSE
#include "timelapse.h"        // Time-lapse periodic snapshots
#include "driver/i2c.h"       // For I2C timeout configuration

// External declarations for OTA protection
extern httpd_handle_t camera_httpd;
extern httpd_handle_t stream_httpd;
extern httpd_handle_t audio_httpd;
extern TaskHandle_t captureTaskHandle;
extern bool is_recording;
extern char lastRecordingFilename[64];
extern bool startSDRecording();
extern void stopSDRecording();

// --- DEBUG MODE ---
// Enable/disable heap tracing (for memory leak diagnostics)
#define ENABLE_HEAP_TRACE false  // true = zapnout monitoring
// -------------------

// =============================================================================
// PRODUCTION TODO: Before deploying outside lab environment:
//   1. Set LAB_MODE to false in config.h
//   2. Set strong credentials via POST /credentials (stored in NVS since v3.6.0)
//   3. HTTP auth will be enforced on mutable endpoints when LAB_MODE=false
//   4. Consider separate I2C bus for LTR308 if bus contention observed
// =============================================================================

// --- VERZE FIRMWARU ---
// FIRMWARE_VERSION is injected via build_flags in platformio.ini so the
// reported version stays in sync with the build that produced the binary.
#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "unknown"
#endif
// --------------------

// Global config instance
Config config;
static bool config_migrated = false;  // Set by loadConfig() when firmware version changes

// Init-time register readback (stored before captureTask starts, SCCB bus free)
int initZone0_1 = -1;  // 0x5688 expected 0x11
int initZone8_9 = -1;  // 0x568c expected 0xEE

// Global health instances
WiFiHealth wifi_health = {0, 0, 0, false, "unknown"}; // char[16] init
SystemStats sys_stats = {0, 0, 0};

// WiFi reconnect exponential backoff
static int reconnect_delay_ms = 10000;  // Start with 10s (was fixed at 10s before)
const int MAX_RECONNECT_DELAY = 60000;  // Max 60s between attempts

// Captive portal DNS server (AP mode only)
static DNSServer dnsServer;
static bool captive_portal_active = false;

// Heap Trace (Debug Mode)
#if ENABLE_HEAP_TRACE
#define HEAP_TRACE_RECORDS 100
static heap_trace_record_t trace_record[HEAP_TRACE_RECORDS];
static unsigned long wdt_reset_count = 0;
#endif

#ifdef INCLUDE_TELEGRAM
// Telegram async queue
#define TELEGRAM_MSG_MAX_LEN 512
// 16 slots absorbs bursts of motion + person-detect + MQTT status messages
// without silently losing notifications during peak activity.
#define TELEGRAM_QUEUE_SIZE 16
static QueueHandle_t telegramQueue = NULL;
static TaskHandle_t telegramTaskHandle = NULL;
static long telegram_last_update_id = 0;  // getUpdates offset tracking
static volatile uint32_t telegram_drop_count = 0;  // Telemetry: queue-full drops

struct TelegramMessage {
    char text[TELEGRAM_MSG_MAX_LEN];
    bool has_photo;           // true = send photo instead of text
    uint8_t* photo_data;      // JPEG data (PSRAM allocated, freed by consumer)
    size_t photo_len;         // JPEG data length
    bool has_document;        // true = send file from SD as document
    char document_path[64];   // SD card file path (e.g. "/rec_20260207_143000.avi")
};
#endif // INCLUDE_TELEGRAM

// Forward declarations
#ifdef INCLUDE_TELEGRAM
bool sendTelegramNotificationSync(const String& message);  // Blocking (only for boot)
void sendTelegramNotification(const String& message);       // Non-blocking (queue)
bool sendTelegramPhotoSync(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption);
void sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption);
bool sendTelegramDocumentSync(const char* sdPath, const String& caption);
void sendTelegramDocument(const char* sdPath, const String& caption);
#else
// Stub functions when Telegram is disabled
static inline bool sendTelegramNotificationSync(const String&) { return false; }
static inline void sendTelegramNotification(const String&) {}
static inline bool sendTelegramPhotoSync(const uint8_t*, size_t, const String&) { return false; }
static inline void sendTelegramPhoto(const uint8_t*, size_t, const String&) {}
static inline bool sendTelegramDocumentSync(const char*, const String&) { return false; }
static inline void sendTelegramDocument(const char*, const String&) {}
#endif
bool initCamera();

// Camera auto-profile: Day / Dusk / Night based on LTR-308 ambient light
// Called every 10s from loop(). Uses hysteresis to avoid flickering.
// Settings based on community research: esp32-camera Issue #203, ESPHome, AstroEdition
enum CameraProfile { PROFILE_DAY, PROFILE_DUSK, PROFILE_NIGHT };
static CameraProfile currentProfile = PROFILE_DUSK;  // Init matches DUSK defaults

void updateCameraProfile() {
    float lux = 0.0f;
    if (!readAmbientLight(&lux)) return;

    // Hysteresis thresholds (lux)
    // Day: >100 (drop to dusk at <60)
    // Dusk: 5-100 (drop to night at <5, rise to day at >100)
    // Night: <5 (rise to dusk at >15)
    CameraProfile target = currentProfile;
    switch (currentProfile) {
        case PROFILE_DAY:
            if (lux < 60.0f) target = PROFILE_DUSK;
            break;
        case PROFILE_DUSK:
            if (lux > 100.0f) target = PROFILE_DAY;
            else if (lux < 5.0f) target = PROFILE_NIGHT;
            break;
        case PROFILE_NIGHT:
            if (lux > 15.0f) target = PROFILE_DUSK;
            break;
    }

    if (target == currentProfile) return;
    currentProfile = target;

    sensor_t* s = esp_camera_sensor_get();
    if (!s) return;

    switch (currentProfile) {
        case PROFILE_DAY:
            // Bright daylight: clean image, low noise
            s->set_brightness(s, 0);
            s->set_contrast(s, 1);
            s->set_saturation(s, 0);       // OV3660 oversaturates, keep neutral
            s->set_sharpness(s, 1);
            s->set_denoise(s, 1);
            s->set_ae_level(s, 0);
            s->set_exposure_ctrl(s, 1);    // AEC auto
            s->set_aec2(s, 0);             // DSP exposure off (not needed in daylight)
            s->set_gain_ctrl(s, 1);        // AGC auto
            s->set_gainceiling(s, (gainceiling_t)3);  // 16x — clean, low noise
            s->set_lenc(s, 1);             // Lens correction ON
            Serial.printf("📷 Profile: DAY (%.0f lux) — AGC auto, gain 16x\n", lux);
            break;
        case PROFILE_DUSK:
            // Indoor / dim light: OV3660 driver accepts wider range than documented
            // brightness: -3..+3 (digital offset 0x10 per step, reg 0x5587)
            // ae_level: -5..+5 (AEC target = ((level+5)*10)+5, regs 0x3a0f-0x3a1f)
            s->set_brightness(s, 3);       // +3 = max digital boost (0x30, +48 offset)
            s->set_contrast(s, 1);         // +1 = boost midtone separation
            s->set_saturation(s, -1);      // Reduce color noise
            s->set_sharpness(s, 0);
            // Adaptive denoise: stronger at low lux end of DUSK range (5-250 lux)
            {
                int denoise_level = (lux > 150.0f) ? 2 : (lux >= 50.0f) ? 3 : 4;
                s->set_denoise(s, denoise_level);
            }
            s->set_ae_level(s, 5);         // +5 = max AEC target (105/255 = 41%)
            s->set_exposure_ctrl(s, 1);    // AEC auto
            s->set_aec2(s, 1);             // DSP exposure ON (ESPHome recommended)
            s->set_gain_ctrl(s, 1);        // AGC auto
            s->set_gainceiling(s, (gainceiling_t)6);  // 128x
            s->set_lenc(s, 1);             // Lens correction ON
            Serial.printf("📷 Profile: DUSK (%.0f lux) — brightness=3, ae_level=5, denoise=%d, gain 128x\n",
                          lux, (lux > 150.0f) ? 2 : (lux >= 50.0f) ? 3 : 4);
            break;
        case PROFILE_NIGHT:
            // Very low light: max everything, AGC OFF (Issue #203 key finding)
            s->set_brightness(s, 3);       // Max digital brightness (+48 offset)
            s->set_contrast(s, -1);        // Reduce contrast = less noise
            s->set_saturation(s, -2);      // Minimize color noise
            s->set_sharpness(s, -1);       // Softer = less noise
            s->set_denoise(s, 5);          // Strong denoise
            s->set_ae_level(s, 5);         // Max AEC target (105/255)
            s->set_exposure_ctrl(s, 1);    // AEC auto
            s->set_aec2(s, 1);             // DSP exposure ON
            s->set_gain_ctrl(s, 0);        // AGC OFF — sensor uses exposure, not gain
            s->set_agc_gain(s, 0);         // Manual gain = 0
            s->set_gainceiling(s, (gainceiling_t)6);  // 128x ceiling
            s->set_lenc(s, 0);             // Lens correction OFF (dark current)
            Serial.printf("📷 Profile: NIGHT (%.0f lux) — brightness=3, ae_level=5, AGC OFF\n", lux);
            break;
    }

    // Frame settling: AEC/AWB need ~5 frames to adapt after profile switch
    // Notify captureTask to discard frames (non-blocking, best-effort)
    Serial.printf("📷 Profile switch — AEC/AWB settling...\n");
}

// Helper: Get WiFi Quality string (returns static string, thread-safe)
const char* getWiFiQuality(int rssi) {
    if (rssi >= -50) return "excellent";
    if (rssi >= -60) return "good";
    if (rssi >= -70) return "fair";
    if (rssi >= -80) return "poor";
    return "critical";
}

// Helper: Load System Stats
SystemStats loadSystemStats() {
    SystemStats stats = {0, 0, 0};
    if (LittleFS.exists("/system_stats.bin")) {
        File file = LittleFS.open("/system_stats.bin", "r");
        if (file) {
            file.read((uint8_t*)&stats, sizeof(SystemStats));
            file.close();
        }
    }
    return stats;
}

// Helper: Save System Stats
void saveSystemStats(SystemStats stats) {
    File file = LittleFS.open("/system_stats.bin", "w");
    if (file) {
        file.write((uint8_t*)&stats, sizeof(SystemStats));
        file.close();
    }
}

// Helper: Check Camera Health
// MODIFIED: Passive check using heartbeat from captureTask
// This prevents race conditions with esp_camera_fb_get()
bool checkCameraHealth() {
    // Check if captureTask has updated the heartbeat recently
    if (millis() - camera_heartbeat > 10000) { // 10 seconds timeout
        Serial.printf("❌ Camera heartbeat lost! Last update: %lu ms ago\n", millis() - camera_heartbeat);
        return false;
    }
    return true;
}

// Helper: Recover Camera (never returns -- performs system restart)
void recoverCamera() {
    Serial.println("🔧 Camera stuck. Initiating system restart...");
    sendTelegramNotification("⚠️ Camera frozen (heartbeat lost). Restarting system...");

    vTaskDelay(pdMS_TO_TICKS(1000));
    ESP.restart(); // Safe restart is better than trying to hack driver deinit
    // unreachable
}

// Helper: Check Memory Health
// Alert policy: 30 min boot grace (so restart cycles don't spam), 6h rate limit,
// hysteresis of 5 KB (re-alert only if heap dropped further). Critical <30 KB
// still restarts immediately.
void checkMemoryHealth() {
    unsigned long freeHeap = ESP.getFreeHeap();

    // Warning threshold: 50 KB
    if (freeHeap < 50000 && freeHeap >= 30000) {
        Serial.printf("⚠️ LOW HEAP WARNING: %u bytes free\n", freeHeap);
        static unsigned long lastLowMemAlert = 0;
        static unsigned long lastAlertHeap = 0xFFFFFFFF;
        const unsigned long ALERT_INTERVAL_MS = 6UL * 3600000UL;  // 6 h
        const unsigned long BOOT_GRACE_MS = 30UL * 60UL * 1000UL; // 30 min
        const unsigned long HYSTERESIS_BYTES = 5000;              // 5 KB

        bool pastGrace = millis() > BOOT_GRACE_MS;
        bool pastInterval = (millis() - lastLowMemAlert) > ALERT_INTERVAL_MS;
        bool heapDroppedFurther = freeHeap + HYSTERESIS_BYTES <= lastAlertHeap;

        if (pastGrace && (pastInterval || heapDroppedFurther)) {
            sendTelegramNotification("⚠️ Low memory: " + String(freeHeap/1024) + " KB free");
            lastLowMemAlert = millis();
            lastAlertHeap = freeHeap;
        }
    }

    // Critical threshold: 30 KB - immediate restart
    if (freeHeap < 30000) {
        Serial.printf("❌ CRITICAL LOW HEAP: %u bytes! Restarting ESP32...\n", freeHeap);
        sendTelegramNotification("❌ CRITICAL low memory! Restarting ESP32...\nFree: " + String(freeHeap/1024) + " KB");
        vTaskDelay(pdMS_TO_TICKS(2000));
        ESP.restart();  // Hard restart on critical memory
    }
}

// Helper: Check WiFi Signal
// Alert policy: 30 min boot grace, 6h rate limit on weak/critical alerts to
// avoid spam during restart cycles. Latch resets when RSSI recovers >= -70.
void checkWiFiSignal() {
    if (WiFi.status() != WL_CONNECTED) {
        strncpy(wifi_health.quality, "disconnected", sizeof(wifi_health.quality) - 1);
        return;
    }

    wifi_health.rssi = WiFi.RSSI();
    strncpy(wifi_health.quality, getWiFiQuality(wifi_health.rssi), sizeof(wifi_health.quality) - 1);

    const unsigned long ALERT_INTERVAL_MS = 6UL * 3600000UL;  // 6 h
    const unsigned long BOOT_GRACE_MS = 30UL * 60UL * 1000UL; // 30 min
    bool pastGrace = millis() > BOOT_GRACE_MS;

    // Alert on signal degradation (-75..-85 dBm)
    if (wifi_health.rssi < -75 && wifi_health.rssi >= -85) {
        static unsigned long lastWeakAlert = 0;
        if (pastGrace && !wifi_health.signal_degraded &&
            (millis() - lastWeakAlert) > ALERT_INTERVAL_MS) {
            wifi_health.signal_degraded = true;
            lastWeakAlert = millis();
            String msg = "⚠️ Weak WiFi signal!\n";
            msg += "📶 RSSI: " + String(wifi_health.rssi) + " dBm\n";
            msg += "🎯 Quality: " + String(wifi_health.quality) + "\n";
            msg += "💡 Recommendation: check the antenna or add a WiFi repeater";
            sendTelegramNotification(msg);
        }
    } else if (wifi_health.rssi < -85) {
        static unsigned long lastCriticalAlert = 0;
        if (pastGrace && (millis() - lastCriticalAlert) > ALERT_INTERVAL_MS) {
            String msg = "❌ CRITICALLY weak WiFi signal!\n";
            msg += "📶 RSSI: " + String(wifi_health.rssi) + " dBm\n";
            msg += "🎯 Quality: " + String(wifi_health.quality) + "\n";
            msg += "⚠️ Stream may be unstable!\n";
            msg += "💡 Add a WiFi repeater!";
            sendTelegramNotification(msg);
            lastCriticalAlert = millis();
        }
    } else if (wifi_health.rssi >= -70) {
        wifi_health.signal_degraded = false;
    }
}

// LED control (LED_BUILTIN is already defined in board_config)
void blinkLED(int times, int delayMs = 100) {
  for(int i = 0; i < times; i++) {
    digitalWrite(LED_BUILTIN, HIGH);
    vTaskDelay(pdMS_TO_TICKS(delayMs));
    digitalWrite(LED_BUILTIN, LOW);
    vTaskDelay(pdMS_TO_TICKS(delayMs));
  }
}

Config getDefaultConfig() {
  Config c;
  c.wifi_ssid = "";            // Credentials loaded from NVS
  c.wifi_password = "";
  c.device_name = "ESP32-Camera";
  c.frame_size = FRAMESIZE_UXGA;  // 1600x1200 — OV3660 3MP (init sets max buffer size)
  c.jpeg_quality = 8;   // 5-63, lower = better quality (8 = good default for UXGA, ~110KB/frame)
  c.flip_vertical = true;   // Camera is mounted upside-down
  c.flip_horizontal = true; // Mirror correction
  c.telegram_bot_token = "";   // Credentials loaded from NVS
  c.telegram_chat_id = "";
  c.http_user = "admin";        // Non-sensitive default
  c.http_pass = "";
  c.ota_password = "";
  c.ap_password = "";
  c.motion_telegram_photo = false;    // Off by default
  c.motion_telegram_video = false;    // Off by default
  c.motion_telegram_cooldown = 30;    // 30 seconds between photos
  c.motion_notify_start_hour = 0;    // Active hours: all day by default
  c.motion_notify_end_hour = 23;
  c.person_detection_enabled = false;    // Off by default (Phase 1 stub)
  c.person_confidence_threshold = 0.6f;  // 60% confidence minimum
  c.person_detection_cooldown = 60;      // 60 seconds between AI-triggered photos
  c.mqtt_enabled = false;
  c.mqtt_server = "";
  c.mqtt_port = 1883;
  c.mqtt_user = "";
  c.mqtt_pass = "";
  c.sd_auto_record = false;
  c.sd_auto_record_duration = 30;
  c.sd_max_usage_percent = 90;
  c.sd_record_fps = 10;
  c.timelapse_enabled = false;
  c.timelapse_interval_sec = 60;
  c.version = FIRMWARE_VERSION;
  return c;
}

bool loadConfig() {
  if (!LittleFS.exists("/config.json")) {
    Serial.println("Config file not found, using defaults");
    config = getDefaultConfig();
    return false;
  }

  File file = LittleFS.open("/config.json", "r");
  if (!file) {
    Serial.println("Failed to open config file");
    config = getDefaultConfig();
    return false;
  }

  // FIXED: Use StaticJsonDocument to avoid heap fragmentation
  StaticJsonDocument<1536> doc;
  DeserializationError error = deserializeJson(doc, file);
  file.close();

  if (error) {
    Serial.println("Failed to parse config file");
    config = getDefaultConfig();
    return false;
  }

  // NOTE: Credentials (wifi, telegram, http auth, ota, ap) are loaded from NVS, not JSON
  config.device_name = doc["device_name"] | "ESP32-Camera";
  config.frame_size = doc["frame_size"] | FRAMESIZE_UXGA;
  config.jpeg_quality = doc["jpeg_quality"] | 10;
  config.flip_vertical = doc["flip_vertical"] | false;
  config.flip_horizontal = doc["flip_horizontal"] | false;
  config.motion_telegram_photo = doc["motion_telegram_photo"] | doc["motion_telegram_enabled"] | false;
  config.motion_telegram_video = doc["motion_telegram_video"] | false;
  config.motion_telegram_cooldown = doc["motion_telegram_cooldown"] | 30;
  config.motion_notify_start_hour = doc["motion_notify_start_hour"] | 0;
  config.motion_notify_end_hour = doc["motion_notify_end_hour"] | 23;
  config.person_detection_enabled = doc["person_detection_enabled"] | false;
  config.person_confidence_threshold = doc["person_confidence_threshold"] | 0.6f;
  config.person_detection_cooldown = doc["person_detection_cooldown"] | 60;
  config.mqtt_enabled = doc["mqtt_enabled"] | false;
  config.mqtt_server = doc["mqtt_server"] | "";
  config.mqtt_port = doc["mqtt_port"] | 1883;
  config.sd_auto_record = doc["sd_auto_record"] | false;
  config.sd_auto_record_duration = doc["sd_auto_record_duration"] | 30;
  config.sd_max_usage_percent = doc["sd_max_usage_percent"] | 90;
  config.sd_record_fps = doc["sd_record_fps"] | 10;
  config.timelapse_enabled = doc["timelapse_enabled"] | false;
  config.timelapse_interval_sec = doc["timelapse_interval_sec"] | 60;

  // Firmware version migration: when version changes, apply new optimal defaults
  // This ensures old config.json values don't persist across firmware updates
  String loaded_version = doc["version"] | "";
  if (loaded_version != FIRMWARE_VERSION) {
    Serial.printf("📦 Config migration: %s → %s\n", loaded_version.c_str(), FIRMWARE_VERSION);
    // v3.10.1+: UXGA default (was SVGA/XGA), quality 8 (was 10)
    if (config.frame_size < FRAMESIZE_UXGA) {
      Serial.printf("   frame_size: %d → %d (UXGA)\n", config.frame_size, FRAMESIZE_UXGA);
      config.frame_size = FRAMESIZE_UXGA;
    }
    if (config.jpeg_quality > 8) {
      Serial.printf("   jpeg_quality: %d → 8\n", config.jpeg_quality);
      config.jpeg_quality = 8;
    }
    config_migrated = true;
  }

  // Force version to match hardcoded firmware version
  config.version = FIRMWARE_VERSION;

  // Validate config ranges to prevent out-of-bound values
  config.jpeg_quality = constrain(config.jpeg_quality, 5, 63);
  config.motion_notify_start_hour = constrain(config.motion_notify_start_hour, 0, 23);
  config.motion_notify_end_hour = constrain(config.motion_notify_end_hour, 0, 23);
  config.motion_telegram_cooldown = constrain(config.motion_telegram_cooldown, 5, 3600);
  config.person_confidence_threshold = constrain(config.person_confidence_threshold, 0.1f, 1.0f);
  config.person_detection_cooldown = constrain(config.person_detection_cooldown, 5, 600);
  config.sd_auto_record_duration = constrain(config.sd_auto_record_duration, 3, 300);
  config.sd_max_usage_percent = constrain(config.sd_max_usage_percent, 10, 99);
  config.sd_record_fps = constrain(config.sd_record_fps, 1, 30);
  config.timelapse_interval_sec = constrain(config.timelapse_interval_sec, 5, 3600);
  config.mqtt_port = constrain(config.mqtt_port, 1, 65535);

  Serial.println("Config loaded successfully");
  return true;
}

bool saveConfig() {
  File file = LittleFS.open("/config.json", "w");
  if (!file) {
    Serial.println("Failed to create config file");
    return false;
  }

  // FIXED: Use StaticJsonDocument to avoid heap fragmentation
  // NOTE: Credentials are stored in NVS, not in config.json
  StaticJsonDocument<1024> doc;
  doc["device_name"] = config.device_name;
  doc["frame_size"] = config.frame_size;
  doc["jpeg_quality"] = config.jpeg_quality;
  doc["flip_vertical"] = config.flip_vertical;
  doc["flip_horizontal"] = config.flip_horizontal;
  doc["motion_telegram_photo"] = config.motion_telegram_photo;
  doc["motion_telegram_video"] = config.motion_telegram_video;
  doc["motion_telegram_cooldown"] = config.motion_telegram_cooldown;
  doc["motion_notify_start_hour"] = config.motion_notify_start_hour;
  doc["motion_notify_end_hour"] = config.motion_notify_end_hour;
  doc["person_detection_enabled"] = config.person_detection_enabled;
  doc["person_confidence_threshold"] = config.person_confidence_threshold;
  doc["person_detection_cooldown"] = config.person_detection_cooldown;
  doc["mqtt_enabled"] = config.mqtt_enabled;
  doc["mqtt_server"] = config.mqtt_server;
  doc["mqtt_port"] = config.mqtt_port;
  doc["sd_auto_record"] = config.sd_auto_record;
  doc["sd_auto_record_duration"] = config.sd_auto_record_duration;
  doc["sd_max_usage_percent"] = config.sd_max_usage_percent;
  doc["sd_record_fps"] = config.sd_record_fps;
  doc["timelapse_enabled"] = config.timelapse_enabled;
  doc["timelapse_interval_sec"] = config.timelapse_interval_sec;
  doc["version"] = config.version;

  if (serializeJson(doc, file) == 0) {
    Serial.println("Failed to write config file");
    file.close();
    return false;
  }

  file.close();
  Serial.println("Config saved successfully");
  return true;
}

// --- NVS Credential Storage ---
// Credentials are stored separately in NVS (Non-Volatile Storage) key-value store
// This keeps sensitive data out of the plaintext config.json file

void loadCredentialsFromNVS() {
    Preferences prefs;
    prefs.begin("creds", true); // read-only
    String val;
    val = prefs.getString("wifi_ssid", "");
    if (val.length() > 0) config.wifi_ssid = val;
    val = prefs.getString("wifi_pass", "");
    if (val.length() > 0) config.wifi_password = val;
    val = prefs.getString("tg_token", "");
    if (val.length() > 0) config.telegram_bot_token = val;
    val = prefs.getString("tg_chat", "");
    if (val.length() > 0) config.telegram_chat_id = val;
    val = prefs.getString("http_user", "");
    if (val.length() > 0) config.http_user = val;
    val = prefs.getString("http_pass", "");
    if (val.length() > 0) config.http_pass = val;
    val = prefs.getString("ota_pass", "");
    if (val.length() > 0) config.ota_password = val;
    val = prefs.getString("ap_pass", "");
    if (val.length() > 0) config.ap_password = val;
    val = prefs.getString("mqtt_user", "");
    if (val.length() > 0) config.mqtt_user = val;
    val = prefs.getString("mqtt_pass", "");
    if (val.length() > 0) config.mqtt_pass = val;
    prefs.end();
    Serial.println("Credentials loaded from NVS");
}

void saveCredentialsToNVS() {
    Preferences prefs;
    prefs.begin("creds", false); // read-write
    prefs.putString("wifi_ssid", config.wifi_ssid);
    prefs.putString("wifi_pass", config.wifi_password);
    prefs.putString("tg_token", config.telegram_bot_token);
    prefs.putString("tg_chat", config.telegram_chat_id);
    prefs.putString("http_user", config.http_user);
    prefs.putString("http_pass", config.http_pass);
    prefs.putString("ota_pass", config.ota_password);
    prefs.putString("ap_pass", config.ap_password);
    prefs.putString("mqtt_user", config.mqtt_user);
    prefs.putString("mqtt_pass", config.mqtt_pass);
    prefs.end();
    Serial.println("Credentials saved to NVS");
}

// One-time migration: populate NVS with hardcoded defaults on first boot
// IMPORTANT: Set credentials via /settings API or serial console, NOT hardcoded!
void migrateDefaultCredentials() {
    Serial.println("First boot or NVS empty — set credentials via /settings API");
    config.wifi_ssid = "";
    config.wifi_password = "";
    config.telegram_bot_token = "";
    config.telegram_chat_id = "";
    config.http_user = "admin";
    config.http_pass = "admin";
    config.ota_password = "";
    config.ap_password = "12345678";
    Serial.println("⚠️ Default credentials — configure via /settings");
}

// Optional: Configure I2C timeout to prevent deadlocks on OV3660
// This is a safety measure - only matters if camera sensor hangs I2C bus
void setupI2CTimeout() {
    // NOTE: Camera driver uses its own I2C init, so we only need to set timeout
    // after camera is initialized. This function is called for diagnostic purposes.
    Serial.println("ℹ️  I2C timeout will be configured by camera driver");
    // Full I2C reconfiguration would interfere with camera driver
    // This is kept as a placeholder for future advanced debugging
}

bool initCamera() {
  camera_config_t cam_config = {};  // Zero-init all fields to prevent UB
  cam_config.ledc_channel = LEDC_CHANNEL_0;
  cam_config.ledc_timer = LEDC_TIMER_0;
  cam_config.pin_d0 = Y2_GPIO_NUM;
  cam_config.pin_d1 = Y3_GPIO_NUM;
  cam_config.pin_d2 = Y4_GPIO_NUM;
  cam_config.pin_d3 = Y5_GPIO_NUM;
  cam_config.pin_d4 = Y6_GPIO_NUM;
  cam_config.pin_d5 = Y7_GPIO_NUM;
  cam_config.pin_d6 = Y8_GPIO_NUM;
  cam_config.pin_d7 = Y9_GPIO_NUM;
  cam_config.pin_xclk = XCLK_GPIO_NUM;
  cam_config.pin_pclk = PCLK_GPIO_NUM;
  cam_config.pin_vsync = VSYNC_GPIO_NUM;
  cam_config.pin_href = HREF_GPIO_NUM;
  cam_config.pin_sccb_sda = SIOD_GPIO_NUM;
  cam_config.pin_sccb_scl = SIOC_GPIO_NUM;
  cam_config.pin_pwdn = PWDN_GPIO_NUM;
  cam_config.pin_reset = RESET_GPIO_NUM;
  cam_config.xclk_freq_hz = 20000000; // 20MHz standard for OV3660
  cam_config.pixel_format = PIXFORMAT_JPEG;

  // Frame buffer location
  // IMPORTANT: Init at max usable resolution to allocate large enough driver buffers.
  // Then switch to config.frame_size after init. Cannot go ABOVE init resolution at runtime!
  // OV3660 3MP: UXGA (1600x1200) — fits 256KB ring buffer slots at quality 10
  if(psramFound()) {
    cam_config.frame_size = FRAMESIZE_UXGA;  // 1600x1200 — OV3660 3MP, max for 256KB slots
    cam_config.jpeg_quality = config.jpeg_quality;
    cam_config.fb_count = 2;
    cam_config.grab_mode = CAMERA_GRAB_LATEST;
    cam_config.fb_location = CAMERA_FB_IN_PSRAM;
    Serial.printf("PSRAM found, init at UXGA (1600x1200) for buffer allocation (target: %d)\n", config.frame_size);
  } else {
    cam_config.frame_size = FRAMESIZE_SVGA;
    cam_config.jpeg_quality = 12;
    cam_config.fb_count = 1;
    cam_config.grab_mode = CAMERA_GRAB_LATEST;
    cam_config.fb_location = CAMERA_FB_IN_DRAM;
    Serial.println("PSRAM not found, using 1 frame buffer");
  }

  // Initialize camera
  esp_err_t err = esp_camera_init(&cam_config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed with error 0x%x\n", err);
    return false;
  }

  // Apply camera settings
  sensor_t * s = esp_camera_sensor_get();
  if (s != NULL) {
    // Apply saved resolution (downscale from UXGA init — OV3660 can't upscale)
    if (config.frame_size <= FRAMESIZE_UXGA && config.frame_size != FRAMESIZE_UXGA) {
      s->set_framesize(s, (framesize_t)config.frame_size);
      Serial.printf("📷 Resolution: set to framesize %d (downscaled from UXGA init)\n", config.frame_size);
    } else {
      Serial.printf("📷 Resolution: UXGA (1600x1200) — init framesize preserved\n");
    }

    // Flip settings (from config.json)
    s->set_vflip(s, config.flip_vertical ? 1 : 0);
    s->set_hmirror(s, config.flip_horizontal ? 1 : 0);

    // OV3660-optimized DUSK defaults (most common indoor scenario)
    // Driver accepts wider range than documented: brightness ±3, ae_level ±5
    s->set_brightness(s, 3);       // +3 = max digital boost (0x30 offset in ISP)
    s->set_contrast(s, 1);        // +1 = boost midtone separation
    s->set_saturation(s, -1);     // Reduce color noise (OV3660 oversaturates)
    s->set_sharpness(s, 0);
    s->set_denoise(s, 3);

    // Auto exposure + gain + white balance — max aggressive for indoor light
    s->set_exposure_ctrl(s, 1);    // AEC on
    s->set_aec2(s, 1);            // DSP-level exposure ON (ESPHome recommendation)
    s->set_ae_level(s, 5);        // +5 = max AEC target (105/255 = 41% target brightness)
    s->set_gain_ctrl(s, 1);       // AGC on (profile switches to OFF at night)
    s->set_gainceiling(s, (gainceiling_t)6); // 128x
    s->set_whitebal(s, 1);        // AWB on
    s->set_awb_gain(s, 1);        // AWB gain on
    s->set_wb_mode(s, 0);         // Auto WB mode

    // BPC/WPC/gamma/lens correction
    s->set_bpc(s, 1);             // Bad pixel correction ON
    s->set_wpc(s, 1);             // White pixel correction ON
    s->set_raw_gma(s, 1);         // Gamma correction ON
    s->set_lenc(s, 1);            // Lens correction ON (profile disables at night)

    // Zone-weighted AEC metering (SCCB free at init time, before captureTask)
    // 4x4 grid, 16 zones. Reduce top row weight (ceiling/windows), boost room.
    // Each reg = 2 zones (hi nibble=zone N+1, lo nibble=zone N). Range: 0-F.
    // Tested: aggressive 0x00/0xFF worse than soft 0x11/0xEE (71 vs 73 avg)
    s->set_reg(s, 0x5688, 0xff, 0x11);  // Zones 0,1: low (top row)
    s->set_reg(s, 0x5689, 0xff, 0x11);  // Zones 2,3: low (top row)
    s->set_reg(s, 0x568a, 0xff, 0x62);  // Zones 4,5: medium
    s->set_reg(s, 0x568b, 0xff, 0x62);  // Zones 6,7: medium
    s->set_reg(s, 0x568c, 0xff, 0xee);  // Zones 8,9: high (room)
    s->set_reg(s, 0x568d, 0xff, 0xee);  // Zones 10,11: high
    s->set_reg(s, 0x568e, 0xff, 0xee);  // Zones 12,13: high (floor)
    s->set_reg(s, 0x568f, 0xff, 0xee);  // Zones 14,15: high

    // Verify zone weights took effect (init-time readback, SCCB is free)
    // Store in globals so /reg-debug can compare init vs runtime values
    initZone0_1 = s->get_reg(s, 0x5688, 0xff);
    initZone8_9 = s->get_reg(s, 0x568c, 0xff);
    Serial.printf("📷 Zone weight verify: 0x5688=0x%02X(want 0x11) 0x568c=0x%02X(want 0xEE)\n", initZone0_1, initZone8_9);

    // --- OV3660 advanced ISP features (init-time only, SCCB free) ---
    s->set_reg(s, 0x5000, 0xff, 0xA7);  // ISP Control: LENC+GMA+BPC+WPC+COLOR+AWB
    s->set_reg(s, 0x5001, 0xff, 0xA3);  // ISP Control 2: SDE+UV_AVG+AWB_GAIN
    s->set_reg(s, 0x5025, 0x03, 0x03);  // Auto BPC/WPC adaptation

    // 2D Noise Reduction (0x5580) DISABLED — writing 0x40 to this register causes
    // inverted/negative image on this OV3660 revision. Confirmed by binary search
    // debug: all other ISP regs are safe, only 0x5580 triggers inversion.
    // Registers 0x5583/0x5584 (Y/UV denoise thresholds) also skipped as they
    // depend on 0x5580 NR enable bit.
    // Trade-off: slightly noisier image in low-light/IR. Acceptable for person detection.

    int reg5000 = s->get_reg(s, 0x5000, 0xff);
    Serial.printf("📷 ISP advanced: 0x5000=0x%02X(want 0xA7), NR disabled (0x5580 causes inversion)\n", reg5000);

    Serial.println("📷 Camera initialized (brightness=3, ae_level=5, zone-weighted AEC, 2D-NR, BPC/WPC auto)");
    // NOTE: updateCameraProfile() called from setup() AFTER initIRControl() (LTR-308 init)
  }

  Serial.println("Camera initialized successfully");
  return true;
}

bool connectWiFi() {
  Serial.println("\nAttempting to connect to WiFi...");
  Serial.printf("SSID: %s\n", config.wifi_ssid.c_str());

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Disable WiFi sleep for stable streaming

  // FIXED: Aggressively disable WiFi power saving to prevent random disconnects
  // ESP32-S3 can re-enable power saving after long runtime due to thermal throttling
  // This forces WiFi to stay awake permanently for stable streaming
  esp_wifi_set_ps(WIFI_PS_NONE);  // Disable ALL power saving modes
  Serial.println("✅ WiFi power saving DISABLED (WIFI_PS_NONE)");

  WiFi.begin(config.wifi_ssid.c_str(), config.wifi_password.c_str());

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    // FIXED: Use vTaskDelay instead of delay() to allow FreeRTOS scheduler to run
    // delay() blocks all tasks, which can freeze camera capture and trigger false WDT
    vTaskDelay(pdMS_TO_TICKS(500));
    Serial.print(".");
    blinkLED(1, 50);
    attempts++;

    // Reset watchdog every 5 attempts (2.5s) to avoid timeout
    if (attempts % 5 == 0) {
      esp_task_wdt_reset(); // Keep WDT happy during long WiFi connect
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifi_health.reconnect_count++;
    wifi_health.last_reconnect_time = millis();
    
    if (wifi_health.reconnect_count >= 3) {
        sendTelegramNotification("⚠️ Frequent WiFi dropouts!\nReconnects in the past hour: " + String(wifi_health.reconnect_count));
    }
    Serial.println("\n\nWiFi connected!");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Signal strength (RSSI): ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");
    blinkLED(3, 200);
    return true;
  }

  Serial.println("\n\nWiFi connection failed!");
  return false;
}

void startAccessPoint() {
  Serial.println("\nStarting Access Point mode...");

  String apSSID = config.device_name + "_AP";
  const char* apPass = config.ap_password.length() > 0 ? config.ap_password.c_str() : "12345678";

  WiFi.mode(WIFI_AP);
  WiFi.softAP(apSSID.c_str(), apPass);

  Serial.println("Access Point started!");
  Serial.printf("SSID: %s\n", apSSID.c_str());
  Serial.printf("Password: %s\n", apPass);
  Serial.print("AP IP address: ");
  Serial.println(WiFi.softAPIP());

  // Start DNS server for captive portal (redirect all domains to AP IP)
  dnsServer.start(53, "*", WiFi.softAPIP());
  captive_portal_active = true;
  Serial.println("✅ Captive portal DNS started (all domains → AP IP)");

  blinkLED(5, 100);
}

#ifdef INCLUDE_TELEGRAM
// Synchronous Telegram send (blocking, ~5s max) -- use only during boot/shutdown
bool sendTelegramNotificationSync(const String& message) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    return false;
  }

  HTTPClient http;
  String url = "https://api.telegram.org/bot" + config.telegram_bot_token + "/sendMessage";

  http.begin(url);
  http.setTimeout(5000);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<2048> doc;
  doc["chat_id"] = config.telegram_chat_id.c_str();
  doc["text"] = message.c_str();
  doc["parse_mode"] = "HTML";

  String jsonString;
  serializeJson(doc, jsonString);

  int httpCode = http.POST(jsonString);

  if (httpCode < 0) {
    Serial.printf("Telegram request failed: %s\n", http.errorToString(httpCode).c_str());
  } else if (httpCode != 200) {
    Serial.printf("Telegram API error: HTTP %d\n", httpCode);
  }

  http.end();
  return (httpCode == 200);
}

/**
 * Generic Telegram multipart upload (photo or document).
 * @param apiMethod   "sendPhoto" or "sendDocument"
 * @param fieldName   "photo" or "document"
 * @param filename    Display filename (e.g. "motion.jpg" or "rec_123.avi")
 * @param contentType MIME type (e.g. "image/jpeg" or "video/x-msvideo")
 * @param data        Binary data (RAM buffer) — NULL if using sdFile
 * @param dataLen     Length of data — 0 if using sdFile
 * @param sdFile      Open SD file — NULL if using RAM data. Caller must close.
 * @param caption     Caption text
 * @param timeoutMs   Response timeout in ms
 * @return true on HTTP 200
 */
static bool telegramMultipartUpload(
    const char* apiMethod, const char* fieldName,
    const char* filename, const char* contentType,
    const uint8_t* data, size_t dataLen,
    File* sdFile,
    const String& caption, unsigned long timeoutMs)
{
  size_t payloadLen = data ? dataLen : (sdFile ? sdFile->size() : 0);
  if (payloadLen == 0) return false;

  WiFiClientSecure client;
  // SECURITY: setInsecure() disables TLS certificate validation, so a MITM on
  // the local network could intercept the bot token and chat messages. Using
  // client.setCACert(ISRG_ROOT_X1_PEM) would validate api.telegram.org
  // properly, at the cost of ~2 KB flash for the embedded CA + needing manual
  // rotation when Let's Encrypt changes roots. Acceptable trade-off on a
  // trusted LAN; revisit before shipping to untrusted networks.
  client.setInsecure();

  if (!client.connect("api.telegram.org", 443, 15000)) {
    Serial.printf("Telegram %s: connection failed\n", apiMethod);
    return false;
  }

  String boundary = "----ESP32Upload";
  String head = "--" + boundary + "\r\n"
                "Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
                + config.telegram_chat_id + "\r\n"
                "--" + boundary + "\r\n"
                "Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
                + caption + "\r\n"
                "--" + boundary + "\r\n"
                "Content-Disposition: form-data; name=\"" + fieldName + "\"; filename=\""
                + filename + "\"\r\n"
                "Content-Type: " + contentType + "\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";

  size_t totalLen = head.length() + payloadLen + tail.length();

  client.println("POST /bot" + config.telegram_bot_token + "/" + apiMethod + " HTTP/1.1");
  client.println("Host: api.telegram.org");
  client.println("Content-Length: " + String(totalLen));
  client.println("Content-Type: multipart/form-data; boundary=" + boundary);
  client.println("Connection: close");
  client.println();
  client.print(head);

  // Upload payload in chunks
  size_t sent = 0;
  uint8_t buf[2048];
  while (sent < payloadLen) {
    size_t chunk;
    if (data) {
      // RAM buffer
      chunk = min((size_t)sizeof(buf), payloadLen - sent);
      memcpy(buf, data + sent, chunk);
    } else {
      // SD file
      chunk = min((size_t)sizeof(buf), payloadLen - sent);
      chunk = sdFile->read(buf, chunk);
      if (chunk == 0) {
        Serial.printf("Telegram %s: read error at %u/%u\n", apiMethod, (unsigned)sent, (unsigned)payloadLen);
        client.stop();
        return false;
      }
    }

    size_t written = client.write(buf, chunk);
    if (written == 0) {
      Serial.printf("Telegram %s: write failed at %u/%u\n", apiMethod, (unsigned)sent, (unsigned)payloadLen);
      client.stop();
      return false;
    }
    sent += written;

    // Yield periodically during long uploads
    if ((sent % (64 * 1024)) == 0) vTaskDelay(pdMS_TO_TICKS(1));
  }

  client.print(tail);

  // Read response
  unsigned long start = millis();
  while (!client.available() && millis() - start < timeoutMs) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  bool success = false;
  if (client.available()) {
    String line = client.readStringUntil('\n');
    success = line.indexOf("200") > 0;
    if (success) {
      Serial.printf("Telegram %s sent: %s (%uKB)\n", apiMethod, filename, (unsigned)(payloadLen / 1024));
    } else {
      Serial.printf("Telegram %s API error: %s\n", apiMethod, line.c_str());
    }
  } else {
    Serial.printf("Telegram %s: response timeout\n", apiMethod);
  }

  client.stop();
  return success;
}

// Photo upload (RAM JPEG buffer)
bool sendTelegramPhotoSync(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) return false;
  if (!jpeg_data || jpeg_len == 0) return false;
  return telegramMultipartUpload("sendPhoto", "photo", "motion.jpg", "image/jpeg",
                                  jpeg_data, jpeg_len, NULL, caption, 10000);
}

// Document upload (SD file streaming)
bool sendTelegramDocumentSync(const char* sdPath, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) return false;

  File docFile = SD.open(sdPath, FILE_READ);
  if (!docFile) { Serial.printf("Telegram document: cannot open %s\n", sdPath); return false; }

  size_t fileSize = docFile.size();
  if (fileSize == 0 || fileSize > 50 * 1024 * 1024) {
    docFile.close();
    return false;
  }

  const char* filename = sdPath;
  const char* slash = strrchr(sdPath, '/');
  if (slash) filename = slash + 1;

  bool ok = telegramMultipartUpload("sendDocument", "document", filename, "video/x-msvideo",
                                     NULL, 0, &docFile, caption, 30000);
  docFile.close();
  return ok;
}

/**
 * Non-blocking Telegram document send — queues SD file path for background delivery.
 */
void sendTelegramDocument(const char* sdPath, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    return;
  }
  if (telegramQueue == NULL) return;

  TelegramMessage msg;
  memset(&msg, 0, sizeof(msg));
  msg.has_document = true;
  strncpy(msg.document_path, sdPath, sizeof(msg.document_path) - 1);

  size_t capLen = min((size_t)caption.length(), (size_t)(TELEGRAM_MSG_MAX_LEN - 1));
  memcpy(msg.text, caption.c_str(), capLen);
  msg.text[capLen] = '\0';

  if (xQueueSend(telegramQueue, &msg, 0) != pdTRUE) {
    telegram_drop_count++;
    Serial.println("📎 Telegram document queue full, dropped");
  }
}

// Handle incoming Telegram bot command (called from checkTelegramUpdates)
void handleTelegramCommand(const String& text) {
    Serial.printf("📱 Telegram command: %s\n", text.c_str());

    if (text == "/photo") {
        // Snapshot from ring buffer via CAS reader pattern
        int idx = current_frame_index;
        if (idx < 0) {
            sendTelegramNotificationSync("Camera not ready.");
            return;
        }
        int rc = __atomic_load_n(&frameBuffers[idx].ref_count, __ATOMIC_SEQ_CST);
        if (rc < 0) {
            sendTelegramNotificationSync("Snapshot unavailable (write in progress).");
            return;
        }
        __atomic_fetch_add(&frameBuffers[idx].ref_count, 1, __ATOMIC_SEQ_CST);
        sendTelegramPhotoSync(frameBuffers[idx].data, frameBuffers[idx].len, "Snapshot from /photo");
        __atomic_fetch_sub(&frameBuffers[idx].ref_count, 1, __ATOMIC_SEQ_CST);

    } else if (text == "/status") {
        unsigned long uptime = millis() / 1000;
        unsigned long days = uptime / 86400;
        unsigned long hours = (uptime % 86400) / 3600;
        unsigned long mins = (uptime % 3600) / 60;
        IRConfig ir = getIRConfig();
        String msg = "📊 <b>ESP32-S3 Status</b>\n\n";
        // System
        msg += "🌐 IP: " + WiFi.localIP().toString() + "\n";
        msg += "⏱ Uptime: " + String(days) + "d " + String(hours) + "h " + String(mins) + "m\n";
        struct tm ti;
        if (getLocalTime(&ti, 0)) {
            char timeBuf[20];
            strftime(timeBuf, sizeof(timeBuf), "%H:%M:%S %d.%m.", &ti);
            msg += "🕐 Time: " + String(timeBuf) + "\n";
        }
        msg += "💾 Heap: " + String(ESP.getFreeHeap() / 1024) + " KB\n";
        msg += "💿 PSRAM: " + String(ESP.getFreePsram() / 1024) + " KB\n";
        msg += "📶 WiFi: " + String(WiFi.RSSI()) + " dBm (" + WiFi.SSID() + ")\n";
        // Hardware
        msg += "💡 IR: " + String(ir.ir_led_current_state ? "ON" : "OFF");
        msg += " (" + String(ir.auto_mode ? "auto" : "manual") + ")\n";
        if (SD.cardSize() > 0) {
            msg += "💽 SD: " + String((uint32_t)(SD.usedBytes() / (1024*1024))) + "/" + String((uint32_t)(SD.cardSize() / (1024*1024))) + " MB\n";
        } else {
            msg += "💽 SD: not mounted\n";
        }
        // Detection settings
        msg += "\n<b>Settings:</b>\n";
        msg += "🔍 Photo: " + String(config.motion_telegram_photo ? "ON" : "OFF") + "\n";
        msg += "🎬 Video: " + String(config.motion_telegram_video ? "ON" : "OFF") + "\n";
        msg += "💾 Local AVI: " + String(config.sd_auto_record ? "ON" : "OFF") + "\n";
        msg += "🧠 AI person: " + String(config.person_detection_enabled ? "ON" : "OFF") + "\n";
        msg += "⏰ Hours: " + String(config.motion_notify_start_hour) + "-" + String(config.motion_notify_end_hour) + "\n";
        msg += "⏱ Cooldown: " + String(config.motion_telegram_cooldown) + "s\n";
        msg += "ℹ️ FW: " + String(FIRMWARE_VERSION);
        sendTelegramNotificationSync(msg);

    } else if (text == "/detect") {
        config.motion_telegram_photo = !config.motion_telegram_photo;
        saveConfig();
        String msg = "🔍 Motion photo: " + String(config.motion_telegram_photo ? "ON" : "OFF");
        sendTelegramNotificationSync(msg);

    } else if (text == "/person") {
        config.person_detection_enabled = !config.person_detection_enabled;
        saveConfig();
        String msg = "🧠 AI person detection: " + String(config.person_detection_enabled ? "ON" : "OFF");
        sendTelegramNotificationSync(msg);

    } else if (text.startsWith("/ir")) {
        String arg = text.substring(3);
        arg.trim();
        if (arg == "on") {
            IRConfig ir = getIRConfig();
            ir.auto_mode = false;
            ir.manual_state = true;
            setIRConfig(ir);
            sendTelegramNotificationSync("💡 IR LED: ON (manual)");
        } else if (arg == "off") {
            IRConfig ir = getIRConfig();
            ir.auto_mode = false;
            ir.manual_state = false;
            setIRConfig(ir);
            sendTelegramNotificationSync("💡 IR LED: OFF (manual)");
        } else if (arg == "auto") {
            IRConfig ir = getIRConfig();
            ir.auto_mode = true;
            setIRConfig(ir);
            sendTelegramNotificationSync("💡 IR LED: AUTO");
        } else {
            sendTelegramNotificationSync("Usage: /ir on | /ir off | /ir auto");
        }

    } else if (text == "/watch") {
        config.motion_telegram_photo = true;
        config.motion_telegram_video = true;
        config.sd_auto_record = true;
        config.person_detection_enabled = true;
        config.motion_notify_start_hour = 0;
        config.motion_notify_end_hour = 23;
        saveConfig();
        sendTelegramNotificationSync("🛡 <b>WATCH mode active</b>\n\n🔍 Photo: ON\n🎬 Video: ON\n💾 Local AVI: ON\n🧠 AI: ON\n⏰ 24/7");

    } else if (text == "/silent") {
        config.motion_telegram_photo = false;
        config.motion_telegram_video = false;
        config.sd_auto_record = false;
        config.person_detection_enabled = false;
        saveConfig();
        sendTelegramNotificationSync("🔇 <b>SILENT mode active</b>\n\nAll disabled. No notifications, no recordings.");

    } else if (text.startsWith("/hours")) {
        String arg = text.substring(6);
        arg.trim();
        int dashPos = arg.indexOf('-');
        if (dashPos <= 0) {
            sendTelegramNotificationSync("Usage: /hours HH-HH (e.g. /hours 22-7)");
        } else {
            int startH = arg.substring(0, dashPos).toInt();
            int endH = arg.substring(dashPos + 1).toInt();
            if (startH < 0 || startH > 23 || endH < 0 || endH > 23) {
                sendTelegramNotificationSync("Error: hours must be 0-23");
            } else {
                config.motion_notify_start_hour = startH;
                config.motion_notify_end_hour = endH;
                saveConfig();
                String msg = "⏰ Active hours: " + String(startH) + ":00 - " + String(endH) + ":00";
                sendTelegramNotificationSync(msg);
            }
        }

    } else if (text.startsWith("/cooldown")) {
        String arg = text.substring(9);
        arg.trim();
        int val = arg.toInt();
        if (val < 5 || val > 3600) {
            sendTelegramNotificationSync("Usage: /cooldown N (5-3600 seconds)");
        } else {
            config.motion_telegram_cooldown = val;
            saveConfig();
            String msg = "⏱ Cooldown: " + String(val) + "s";
            sendTelegramNotificationSync(msg);
        }

    } else if (text.startsWith("/threshold")) {
        String arg = text.substring(10);
        arg.trim();
        float val = arg.toFloat();
        if (val < 0.1f || val > 1.0f) {
            sendTelegramNotificationSync("Usage: /threshold N.N (0.1-1.0)");
        } else {
            config.person_confidence_threshold = val;
            saveConfig();
            String msg = "🎯 AI threshold: " + String(val, 2) + " (" + String((int)(val * 100)) + "%)";
            sendTelegramNotificationSync(msg);
        }

    } else if (text.startsWith("/video")) {
        String arg = text.substring(6);
        arg.trim();
        int seconds = arg.toInt();
        if (seconds < 3 || seconds > 60) {
            sendTelegramNotificationSync("Usage: /video N (3-60 seconds)\ne.g. /video 10");
        } else if (!SD.cardSize()) {
            sendTelegramNotificationSync("❌ SD card not ready.");
        } else if (is_recording) {
            sendTelegramNotificationSync("⚠️ Already recording. Use /record to stop.");
        } else {
            if (startSDRecording()) {
                String msg = "🎬 Recording clip " + String(seconds) + "s...";
                sendTelegramNotificationSync(msg);
                vTaskDelay(pdMS_TO_TICKS(seconds * 1000));
                char savedFile[64];
                strncpy(savedFile, lastRecordingFilename, sizeof(savedFile));
                stopSDRecording();
                if (savedFile[0] != '\0') {
                    sendTelegramDocument(savedFile, "Clip " + String(seconds) + "s");
                }
            } else {
                sendTelegramNotificationSync("❌ Could not start recording.");
            }
        }

    } else if (text == "/record") {
        if (!SD.cardSize()) {
            sendTelegramNotificationSync("❌ SD card not ready.");
        } else if (is_recording) {
            stopSDRecording();
            sendTelegramNotificationSync("⏹ Recording stopped: " + String(lastRecordingFilename));
            // Send the recorded file
            if (lastRecordingFilename[0] != '\0') {
                sendTelegramDocument(lastRecordingFilename, "Recording from /record");
            }
        } else {
            if (startSDRecording()) {
                sendTelegramNotificationSync("⏺ AVI recording started: " + String(lastRecordingFilename));
            } else {
                sendTelegramNotificationSync("❌ Could not start recording.");
            }
        }

    } else if (text == "/ip") {
        String msg = "🌐 <b>Network info</b>\n\n";
        msg += "IP: " + WiFi.localIP().toString() + "\n";
        msg += "SSID: " + WiFi.SSID() + "\n";
        msg += "Web: http://" + WiFi.localIP().toString() + "/\n";
        msg += "Stream: http://" + WiFi.localIP().toString() + ":81/stream\n";
        msg += "RTSP: rtsp://" + WiFi.localIP().toString() + ":554/mjpeg/1";
        sendTelegramNotificationSync(msg);

    } else if (text == "/restart") {
        sendTelegramNotificationSync("🔄 Restarting...");
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP.restart();

    } else if (text == "/help") {
        String msg = "📋 <b>Commands:</b>\n\n";
        msg += "<b>Modes:</b>\n";
        msg += "/watch - All ON (photo+video+AI, 24/7)\n";
        msg += "/silent - All OFF (no notifications)\n\n";
        msg += "<b>Snapshots and video:</b>\n";
        msg += "/photo - Snapshot from camera\n";
        msg += "/video N - Record clip (3-60s)\n";
        msg += "/record - Start/stop AVI recording\n\n";
        msg += "<b>Detection:</b>\n";
        msg += "/detect - Motion photo ON/OFF\n";
        msg += "/person - AI person detection ON/OFF\n\n";
        msg += "<b>Settings:</b>\n";
        msg += "/ir on|off|auto - IR LED\n";
        msg += "/hours HH-HH - Active hours\n";
        msg += "/cooldown N - Cooldown (5-3600s)\n";
        msg += "/threshold N.N - AI threshold (0.1-1.0)\n\n";
        msg += "<b>System:</b>\n";
        msg += "/status - System status\n";
        msg += "/ip - Network info\n";
        msg += "/restart - Restart camera";
        sendTelegramNotificationSync(msg);

    } else {
        sendTelegramNotificationSync("Unknown command. Type /help for list.");
    }
}

// Poll Telegram getUpdates API for incoming commands.
// Heap-optimized: fixed char[] URL, StaticJsonDocument, zero-copy text,
// long-vs-long chat_id compare. See config.h TELEGRAM_POLL_INTERVAL_MS.
void checkTelegramUpdates() {
    long expected_chat_id = atol(config.telegram_chat_id.c_str());

    HTTPClient http;

    char url[256];
    snprintf(url, sizeof(url),
             "https://api.telegram.org/bot%s/getUpdates?offset=%ld&timeout=0&limit=5",
             config.telegram_bot_token.c_str(),
             telegram_last_update_id + 1);

    http.begin(url);
    http.setTimeout(3000);

    int httpCode = http.GET();
    if (httpCode != 200) {
        if (httpCode > 0) {
            Serial.printf("📱 Telegram getUpdates: HTTP %d\n", httpCode);
        }
        http.end();
        return;
    }

    String payload = http.getString();
    http.end();

    StaticJsonDocument<1536> doc;
    DeserializationError err = deserializeJson(doc, payload);
    if (err) {
        Serial.printf("📱 Telegram getUpdates: JSON parse error: %s\n", err.c_str());
        return;
    }

    if (!doc["ok"].as<bool>()) return;

    JsonArray results = doc["result"].as<JsonArray>();
    for (JsonObject update : results) {
        long update_id = update["update_id"].as<long>();
        if (update_id > telegram_last_update_id) {
            telegram_last_update_id = update_id;
        }

        JsonObject message = update["message"];
        if (message.isNull()) continue;

        long chat_id = message["chat"]["id"].as<long>();

        if (chat_id != expected_chat_id) {
            Serial.printf("📱 Telegram: ignored message from chat %ld\n", chat_id);
            continue;
        }

        const char* text = message["text"];
        if (text != nullptr && text[0] != '\0') {
            handleTelegramCommand(String(text));
        }
    }
}

// Telegram background task -- processes messages from queue + polls for incoming commands
void telegramTask(void* param) {
  TelegramMessage msg;
  Serial.println("📱 Telegram async task started (bidirectional)");

  while (true) {
    // Wait for outgoing message OR TELEGRAM_POLL_INTERVAL_MS timeout for polling incoming commands
    if (xQueueReceive(telegramQueue, &msg, pdMS_TO_TICKS(TELEGRAM_POLL_INTERVAL_MS)) == pdTRUE) {
      if (WiFi.status() != WL_CONNECTED) {
        Serial.println("📱 Telegram: WiFi not connected, dropping message");
        continue;
      }

      bool ok;
      if (msg.has_document && msg.document_path[0] != '\0') {
        // Document message -- stream from SD (can block up to 120s for large files)
        ok = sendTelegramDocumentSync(msg.document_path, String(msg.text));
      } else if (msg.has_photo && msg.photo_data != NULL && msg.photo_len > 0) {
        // Photo message -- multipart upload (can block up to 15s)
        ok = sendTelegramPhotoSync(msg.photo_data, msg.photo_len, String(msg.text));
        // Free PSRAM copy allocated by sendTelegramPhoto()
        free(msg.photo_data);
        msg.photo_data = NULL;
      } else {
        // Text message (can block up to 5s)
        ok = sendTelegramNotificationSync(String(msg.text));
      }
      if (!ok) {
        Serial.println("📱 Telegram: Send failed, message dropped");
      }

      // Small delay between messages to avoid rate limiting
      vTaskDelay(pdMS_TO_TICKS(500));
    } else {
      // Timeout -- poll for incoming Telegram commands
      if (WiFi.status() == WL_CONNECTED &&
          config.telegram_bot_token.length() > 0) {
        checkTelegramUpdates();
      }
    }
  }
}

// Non-blocking Telegram send -- queues message for background delivery
void sendTelegramNotification(const String& message) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    return;
  }

  // If queue not ready yet (early boot), fall back to sync
  if (telegramQueue == NULL) {
    sendTelegramNotificationSync(message);
    return;
  }

  TelegramMessage msg;
  memset(&msg, 0, sizeof(msg));  // Zero-init (has_photo=false, photo_data=NULL)
  // Truncate if needed -- better than overflow
  size_t copyLen = min((size_t)message.length(), (size_t)(TELEGRAM_MSG_MAX_LEN - 1));
  memcpy(msg.text, message.c_str(), copyLen);
  msg.text[copyLen] = '\0';

  // Non-blocking enqueue -- if queue full, drop the message
  if (xQueueSend(telegramQueue, &msg, 0) != pdTRUE) {
    telegram_drop_count++;
    Serial.println("📱 Telegram queue full, message dropped");
  }
}

// Non-blocking Telegram photo send -- copies JPEG to PSRAM and queues for background delivery
void sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    return;
  }
  if (jpeg_data == NULL || jpeg_len == 0 || jpeg_len > 512 * 1024) {
    Serial.println("📷 sendTelegramPhoto: Invalid JPEG data");
    return;
  }

  // If queue not ready yet (early boot), skip (don't block with sync photo)
  if (telegramQueue == NULL) {
    return;
  }

  // Allocate PSRAM copy so the ring buffer slot can be released immediately
  uint8_t* copy = (uint8_t*)ps_malloc(jpeg_len);
  if (copy == NULL) {
    Serial.println("📷 sendTelegramPhoto: PSRAM alloc failed");
    return;
  }
  memcpy(copy, jpeg_data, jpeg_len);

  TelegramMessage msg;
  memset(&msg, 0, sizeof(msg));
  msg.has_photo = true;
  msg.photo_data = copy;
  msg.photo_len = jpeg_len;

  // Caption goes into text field
  size_t capLen = min((size_t)caption.length(), (size_t)(TELEGRAM_MSG_MAX_LEN - 1));
  memcpy(msg.text, caption.c_str(), capLen);
  msg.text[capLen] = '\0';

  // Non-blocking enqueue -- if queue full, free PSRAM and drop
  if (xQueueSend(telegramQueue, &msg, 0) != pdTRUE) {
    telegram_drop_count++;
    Serial.println("📷 Telegram photo queue full, dropped");
    free(copy);
  }
}
#endif // INCLUDE_TELEGRAM

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  // Initialize Watchdog timer - SAFE CONFIGURATION
  // Timeout: 3 minutes to accommodate:
  //  - WiFi reconnect (up to 100s)
  //  - SD card operations (write/close can take 2-3s)
  //  - Camera init retries (6s)
  //  - Telegram notifications (10-20s)
  Serial.println("Initializing Watchdog timer (SAFE: 180s timeout, no panic)...");
  esp_task_wdt_deinit(); // Deinit default WDT first

  esp_task_wdt_config_t wdt_config = {
      .timeout_ms = 180000,  // 3 minutes (very safe for SD operations)
      .idle_core_mask = 0,   // Don't monitor idle tasks
      .trigger_panic = false // Warning only, no panic restart
  };

  if (esp_task_wdt_init(&wdt_config) == ESP_OK) {
      if (esp_task_wdt_add(NULL) == ESP_OK) { // Add main loop task
          Serial.println("✅ SOFT WDT enabled (monitoring mode, 180s timeout)");
      } else {
          Serial.println("⚠️ Failed to add task to WDT");
      }
  } else {
      Serial.println("⚠️ WDT init failed - continuing without WDT");
  }

  vTaskDelay(pdMS_TO_TICKS(1000));

  // Initialize Heap Tracing (Debug Mode)
  #if ENABLE_HEAP_TRACE
  heap_trace_init_standalone(trace_record, HEAP_TRACE_RECORDS);
  heap_trace_start(HEAP_TRACE_LEAKS);
  Serial.println("🔍 HEAP TRACING ENABLED (Debug Mode)");
  Serial.println("   - Memory leaks will be logged every hour");
  #endif

  Serial.println("\n\n");
  Serial.println("===========================================");
  Serial.println("  ESP32-S3 AI Camera - Streaming Version");
  Serial.println("===========================================");

  // Initialize LittleFS
  Serial.println("Mounting LittleFS...");
  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS mount failed!");
    blinkLED(10, 50);
  } else {
    Serial.println("LittleFS mounted successfully");
  }

  // Load System Stats
  sys_stats = loadSystemStats();
  sys_stats.total_restarts++;
  sys_stats.last_restart_reason = esp_reset_reason();
  saveSystemStats(sys_stats);
  Serial.printf("📊 Total restarts: %u\n", sys_stats.total_restarts);

  // Load configuration (non-credential settings from config.json)
  loadConfig();

  // Persist migrated config (new firmware version → updated defaults)
  if (config_migrated) {
    saveConfig();
    Serial.println("📦 Migrated config saved to flash");
  }

  // Load credentials from NVS (separate key-value store)
  loadCredentialsFromNVS();

  // First boot migration: if NVS was empty, populate with hardcoded defaults
  if (config.wifi_ssid.length() == 0) {
    migrateDefaultCredentials();
    saveCredentialsToNVS();
  }

  // One-time Telegram credentials update for v3.8.0
  // (NVS has old token from previous version, force-update to new one)
  {
    Preferences prefs;
    prefs.begin("creds", false);
    if (!prefs.getBool("tg_v38", false)) {
      // Credentials loaded from NVS — no hardcoded tokens
      config.telegram_bot_token = "";
      config.telegram_chat_id = "";
      prefs.putString("tg_token", config.telegram_bot_token);
      prefs.putString("tg_chat", config.telegram_chat_id);
      prefs.putBool("tg_v38", true);
      Serial.println("📱 Telegram credentials updated for v3.8.0");
    }
    prefs.end();
  }

  // Optional: Setup I2C timeout (diagnostic only, camera driver handles I2C)
  setupI2CTimeout();

  // FIXED: Initialize camera with retry logic (NO AUTO-RESTART to avoid loop!)
  bool camera_init_success = false;
  for (int retry = 0; retry < 3 && !camera_init_success; retry++) {
    if (retry > 0) {
      Serial.printf("🔄 Camera init retry %d/3...\n", retry + 1);
      vTaskDelay(pdMS_TO_TICKS(2000));
    }
    camera_init_success = initCamera();
  }

  if (!camera_init_success) {
    Serial.println("❌ Camera initialization failed after 3 attempts!");
    blinkLED(10, 100);
    sendTelegramNotification("❌ Camera failed to initialize after 3 attempts. Waiting for manual restart...");
    // NOTE: NO ESP.restart() here - would cause restart loop if camera hardware issue!
    // Better to let ESP32 run without camera than restart infinitely
  }

  // Connect to WiFi or start AP
  if (!connectWiFi()) {
    startAccessPoint();
  }

  // Configure NTP time sync (for time-based IR control)
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("Configuring NTP time sync...");
    // CET/CEST: UTC+1 winter, UTC+2 summer (auto DST via POSIX TZ string)
    configTzTime("CET-1CEST,M3.5.0,M10.5.0/3", "pool.ntp.org", "time.nist.gov");
    Serial.println("NTP time sync configured");
  }

#ifdef INCLUDE_TELEGRAM
  // Start Telegram async queue + task (before any notifications)
  telegramQueue = xQueueCreate(TELEGRAM_QUEUE_SIZE, sizeof(TelegramMessage));
  if (telegramQueue) {
    xTaskCreatePinnedToCore(
      telegramTask,          // Task function
      "Telegram",            // Task name
      8192,                  // Stack size (document upload needs 2KB SD buffer + TLS)
      NULL,                  // Parameters
      2,                     // Priority (LOW - background)
      &telegramTaskHandle,   // Task handle
      0                      // Core 0 (network tasks)
    );
    Serial.println("✅ Telegram async queue + task started");
  } else {
    Serial.println("⚠️ Failed to create Telegram queue (falling back to sync)");
  }
#endif

  // Start camera server
  // Initialize live log system (before camera server so handlers get registered)
  wsLogInit();

  startCameraServer();

  // Initialize IR Night Vision control (LTR-308 ambient light sensor init here)
  initIRControl();

  // Apply camera profile immediately now that LTR-308 is available
  updateCameraProfile();

#if defined(INCLUDE_AUDIO) && !defined(LITE_MODE_NO_RUNTIME_TASKS)
  // Initialize Audio (I2S PDM Microphone) - Plain I2S mode
  initAudio();
#elif defined(LITE_MODE_NO_RUNTIME_TASKS)
  Serial.println("⚡ LITE_MODE: initAudio() skipped (frees ~8KB SRAM I2S DMA)");
#endif

#ifdef INCLUDE_MQTT
  // Initialize MQTT (Home Assistant integration)
  initMQTT();
#endif

#if defined(INCLUDE_TIMELAPSE) && !defined(LITE_MODE_NO_RUNTIME_TASKS)
  // Start time-lapse task (periodic JPEG snapshots to SD)
  startTimelapseTask();
#elif defined(LITE_MODE_NO_RUNTIME_TASKS)
  Serial.println("⚡ LITE_MODE: timelapse task skipped (frees ~4KB SRAM stack)");
#endif

  Serial.println("\n===========================================");
  Serial.println("  Camera Ready!");
  Serial.println("===========================================");

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("  Local IP: http://%s\n", WiFi.localIP().toString().c_str());
    Serial.printf("  Device Name: %s\n", config.device_name.c_str());
  } else {
    Serial.printf("  AP IP: http://%s\n", WiFi.softAPIP().toString().c_str());
  }

  Serial.println("  Endpoints:");
  Serial.println("    /                 - Web GUI");
  Serial.println("    /frame            - Single JPEG");
  Serial.println("    /stream           - MJPEG Stream (GUI)");
  Serial.println("    /detection-stream - MJPEG Stream (Detection)");
  Serial.println("    /status           - Status JSON");
  Serial.println("    /settings         - Settings (GET/POST)");
  Serial.println("    /ir-status        - IR Status JSON");
  Serial.println("    /ir-control       - IR Control (POST)");
  Serial.println("===========================================\n");

  blinkLED(2, 500);

  // Send Telegram boot notification
  if (WiFi.status() == WL_CONNECTED) {
    // Let the IP address stabilize briefly
    vTaskDelay(pdMS_TO_TICKS(500));

    Serial.println("📱 Sending Telegram notification...");

    String ip = WiFi.localIP().toString();
    int rssi = WiFi.RSSI();
    unsigned long freeHeap = ESP.getFreeHeap();
    unsigned long freePsram = ESP.getFreePsram();

    // Validate IP (must not be 0.0.0.0)
    if (ip == "0.0.0.0" || ip.length() == 0) {
      Serial.println("⚠️  IP address invalid, waiting 1 second...");
      vTaskDelay(pdMS_TO_TICKS(1000));
      ip = WiFi.localIP().toString();
    }

    String message = "🚀 <b>ESP32-S3 AI Camera started!</b>\n\n";
    message += "ℹ️ <b>Version:</b> " + String(FIRMWARE_VERSION) + "\n";

    // Safely add IP
    if (ip.c_str() != nullptr && ip != "0.0.0.0" && ip.length() > 0) {
        message += "📡 <b>IP address:</b> " + ip + "\n";
    } else {
        message += "📡 <b>IP address:</b> (unknown)\n";
    }

    message += "📶 <b>WiFi signal:</b> " + String(rssi) + " dBm\n";
    message += "💾 <b>Free RAM:</b> " + String(freeHeap/1024) + " KB\n";
    message += "💿 <b>Free PSRAM:</b> " + String(freePsram/1024/1024) + " MB\n\n";

    // Safely add link
    if (ip.c_str() != nullptr && ip != "0.0.0.0" && ip.length() > 0) {
        message += "🌐 <a href=\"http://" + ip + "\">Open web dashboard</a>";
    }

    // Use sync version for boot message (we want confirmation it was sent)
    if (sendTelegramNotificationSync(message)) {
      Serial.println("✅ Telegram notification sent");
      Serial.printf("   IP: %s, RSSI: %d dBm\n", ip.c_str(), rssi);
    } else {
      Serial.println("⚠️  Failed to send Telegram notification");
    }
  }

  // Initialize mDNS (required for OTA hostname discovery)
  if (WiFi.status() == WL_CONNECTED) {
    if (MDNS.begin(config.device_name.c_str())) {
      Serial.printf("✅ mDNS started: %s.local\n", config.device_name.c_str());
    } else {
      Serial.println("⚠️ mDNS init failed");
    }
  }

  // Initialize OTA
  ArduinoOTA.setHostname(config.device_name.c_str());
  if (config.ota_password.length() > 0) {
    ArduinoOTA.setPassword(config.ota_password.c_str());
  }
  
  ArduinoOTA.onStart([]() {
    String type = (ArduinoOTA.getCommand() == U_FLASH) ? "sketch" : "filesystem";
    Serial.println("Start updating " + type);

    // CRITICAL: Stop camera capture before OTA to prevent crashes
    if (captureTaskHandle) {
        vTaskSuspend(captureTaskHandle);
        Serial.println("✅ Camera capture suspended for OTA");
    }

    // Stop all HTTP servers to free memory for OTA
    if (stream_httpd) {
        httpd_stop(stream_httpd);
        stream_httpd = NULL;
        Serial.println("✅ Stream HTTP server stopped for OTA");
    }
    if (camera_httpd) {
        httpd_stop(camera_httpd);
        camera_httpd = NULL;
        Serial.println("✅ Camera HTTP server stopped for OTA");
    }
    if (audio_httpd) {
        httpd_stop(audio_httpd);
        audio_httpd = NULL;
        Serial.println("✅ Audio HTTP server stopped for OTA");
    }

    Serial.println("✅ All services stopped, OTA update starting...");
  });
  ArduinoOTA.onEnd([]() {
    Serial.println("\nEnd");
  });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    Serial.printf("Progress: %u%%\r", (progress / (total / 100)));
  });
  ArduinoOTA.onError([](ota_error_t error) {
    Serial.printf("Error[%u]: ", error);
    if (error == OTA_AUTH_ERROR) Serial.println("Auth Failed");
    else if (error == OTA_BEGIN_ERROR) Serial.println("Begin Failed");
    else if (error == OTA_CONNECT_ERROR) Serial.println("Connect Failed");
    else if (error == OTA_RECEIVE_ERROR) Serial.println("Receive Failed");
    else if (error == OTA_END_ERROR) Serial.println("End Failed");

    // OTA failed -- restart to restore all services (servers were stopped in onStart)
    Serial.println("⚠️ OTA failed, restarting to restore services...");
    vTaskDelay(pdMS_TO_TICKS(500));
    ESP.restart();
  });
  ArduinoOTA.begin();
  Serial.println("OTA Ready");
}

void loop() {
  ArduinoOTA.handle(); // Handle OTA updates

  // Process captive portal DNS requests (AP mode only)
  if (captive_portal_active) {
    dnsServer.processNextRequest();

    // Auto-retry WiFi STA every 5 minutes when in AP fallback mode
    static unsigned long lastAPRetry = 0;
    if (config.wifi_ssid.length() > 0 && millis() - lastAPRetry > 300000) {
      lastAPRetry = millis();
      Serial.println("🔄 AP fallback: retrying WiFi STA...");
      WiFi.mode(WIFI_AP_STA);
      if (connectWiFi()) {
        Serial.println("✅ WiFi reconnected from AP fallback, switching to STA mode");
        dnsServer.stop();
        captive_portal_active = false;
        WiFi.mode(WIFI_STA);
      } else {
        Serial.println("⚠️ WiFi still unavailable, staying in AP mode");
        WiFi.mode(WIFI_AP);
      }
    }
  }

  // Reset WDT every 30 seconds (well within 180s timeout)
  static unsigned long lastWdtReset = 0;
  if (millis() - lastWdtReset > 30000) {
      esp_task_wdt_reset();
      lastWdtReset = millis();

      // WDT Reset Counter (Diagnostic)
      #if ENABLE_HEAP_TRACE
      wdt_reset_count++;
      if (wdt_reset_count % 100 == 0) {  // Log every 100 resets (~50 min)
          Serial.printf("ℹ️  WDT reset count: %lu (uptime: %lu hours)\n",
                        wdt_reset_count,
                        millis() / 3600000);
      }
      #endif
  }

  // Heap Trace Dump (hourly in debug mode)
  #if ENABLE_HEAP_TRACE
  static unsigned long last_heap_dump = 0;
  if (millis() - last_heap_dump > 3600000) {  // 1 hodina
      Serial.println("\n=== HEAP TRACE DUMP ===");
      heap_trace_dump();
      Serial.println("=======================\n");
      last_heap_dump = millis();
  }
  #endif

  // FIXED: Camera health check with retry counter
  static unsigned long lastCameraCheck = 0;
  static int camera_failure_count = 0;
  const int MAX_CAMERA_FAILURES = 3;

  if (millis() - lastCameraCheck > 60000) {
      if (!checkCameraHealth()) {
          camera_failure_count++;
          Serial.printf("⚠️ Camera health check failed (%d/%d)\n", camera_failure_count, MAX_CAMERA_FAILURES);

          if (camera_failure_count >= MAX_CAMERA_FAILURES) {
              Serial.println("❌ Camera failed multiple times, restarting system...");
              sendTelegramNotification("⚠️ Camera failed " + String(MAX_CAMERA_FAILURES) + " times, restarting system...");
              recoverCamera();  // Never returns -- performs ESP.restart()
          }
      } else {
          camera_failure_count = 0;  // Reset counter on successful check
      }
      lastCameraCheck = millis();
  }

  // Memory check every 30 seconds
  static unsigned long lastMemCheck = 0;
  if (millis() - lastMemCheck > 30000) {
      checkMemoryHealth();
      lastMemCheck = millis();
  }

  // WiFi signal check every hour
  static unsigned long lastWiFiCheck = 0;
  if (millis() - lastWiFiCheck > 3600000) { // 1 hour
      checkWiFiSignal();
      lastWiFiCheck = millis();
  }

  // Reset reconnect counter every hour
  static unsigned long lastReconnectReset = 0;
  if (millis() - lastReconnectReset > 3600000) {
      wifi_health.reconnect_count = 0;
      lastReconnectReset = millis();
  }

  // Heap monitoring
  static unsigned long lastHeapCheck = 0;
  if (millis() - lastHeapCheck > 30000) { // Log every 30 seconds
    Serial.printf("Heap: %u bytes free, PSRAM: %u bytes free, RSSI: %d dBm, uptime: %lus\n",
                  ESP.getFreeHeap(), ESP.getFreePsram(), WiFi.RSSI(), millis() / 1000);
    lastHeapCheck = millis();
  }

  // Keep WiFi alive with exponential backoff retry logic
  static unsigned long lastCheck = 0;
  static int reconnect_attempts = 0;
  const int MAX_RECONNECT_ATTEMPTS = 5;

  if (millis() - lastCheck > reconnect_delay_ms) {  // Dynamic interval (exponential backoff)
    if (WiFi.status() != WL_CONNECTED && WiFi.getMode() == WIFI_STA) {
      reconnect_attempts++;
      Serial.printf("WiFi disconnected, attempt %d/%d (delay: %dms)\n",
                    reconnect_attempts, MAX_RECONNECT_ATTEMPTS, reconnect_delay_ms);

      if (connectWiFi()) {
        reconnect_attempts = 0;
        reconnect_delay_ms = 10000;  // Reset delay on success
        Serial.println("✅ WiFi reconnected successfully");

        // Notify on reconnect
        String message = "✅ WiFi connection restored\n";
        message += "📡 IP: " + WiFi.localIP().toString() + "\n";
        message += "📶 Signal: " + String(WiFi.RSSI()) + " dBm";
        sendTelegramNotification(message);
      } else {
        // Exponential backoff: double the delay (max 60s)
        reconnect_delay_ms = min(reconnect_delay_ms * 2, MAX_RECONNECT_DELAY);
        Serial.printf("⏳ Next WiFi retry in %d ms\n", reconnect_delay_ms);

        // Fallback to AP mode after max reconnect attempts (instead of restart loop)
        if (reconnect_attempts >= MAX_RECONNECT_ATTEMPTS) {
          Serial.println("⚠️ Max WiFi reconnect attempts reached, switching to AP mode...");
          startAccessPoint();
          reconnect_attempts = 0;
          reconnect_delay_ms = 10000;
          // Try STA again after 5 minutes in AP mode
          static unsigned long apFallbackTime = 0;
          apFallbackTime = millis();
        }
      }
    } else {
      reconnect_attempts = 0;      // Reset on successful connection
      reconnect_delay_ms = 10000;  // Reset delay when connection is stable
    }
    lastCheck = millis();
  }

#ifdef INCLUDE_MQTT
  // MQTT loop (reconnect + keepalive)
  mqttLoop();
#endif

  // IR LED auto-mode and camera day/night profile are driven by captureTask
  // every CAPTURE_SENSOR_POLL_FRAMES (~5s) — it has reliable timing regardless
  // of loop() scheduling, and runs on the same core as SCCB to stay bus-safe.

  vTaskDelay(pdMS_TO_TICKS(100));

  // Slow LED blink when everything is healthy and WiFi is connected
  if (WiFi.status() == WL_CONNECTED) {
    static unsigned long lastLedToggle = 0;
    if (millis() - lastLedToggle > 1000) { // Toggle every 1 second
      digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
      lastLedToggle = millis();
    }
  } else {
    // Ensure LED is off if not connected to WiFi (unless blinkLED is active)
    digitalWrite(LED_BUILTIN, LOW);
  }
}
