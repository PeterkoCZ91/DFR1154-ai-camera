#include "esp_camera.h"
#include <WiFi.h>
#include "esp_wifi.h"  // For esp_wifi_set_ps()
#include "esp_system.h"
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
#include <Wire.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <DNSServer.h>
#include "esp_heap_trace.h"
#include "tracker.h"
#include "esp_http_server.h"  // For httpd_stop() in OTA handler
#include "ws_log.h"           // Live log via SSE
#include "sd_logger.h"        // Session-based SD debug logger
#include "timelapse.h"        // Time-lapse periodic snapshots
#include "driver/i2c.h"       // For I2C timeout configuration
#include "event_log.h"

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
static bool main_task_wdt_registered = false;

static inline void resetMainTaskWdt() {
  if (main_task_wdt_registered) {
    esp_task_wdt_reset();
  }
}

// Init-time register readback (stored before captureTask starts, SCCB bus free)
int initZone0_1 = -1;  // 0x5688 expected 0x11
int initZone8_9 = -1;  // 0x568c expected 0xEE

// Global health instances
WiFiHealth wifi_health = {0, 0, 0, false, "unknown"}; // char[16] init
SystemStats sys_stats = {0, 0, 0, 0, 0, 0, 0};

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
// 6 slots keeps PSRAM use bounded while absorbing short person/motion bursts.
#define TELEGRAM_QUEUE_SIZE 6
static QueueHandle_t telegramQueue = NULL;
static TaskHandle_t telegramTaskHandle = NULL;
static long telegram_last_update_id = 0;  // getUpdates offset tracking
static bool telegram_boot_drained = false; // true after startup drain (skip old messages)
static volatile uint32_t telegram_drop_count = 0;  // Telemetry: queue-full drops
static volatile uint32_t telegram_sent_count = 0;  // Telemetry: confirmed HTTP 200 sends
static volatile uint32_t telegram_fail_count = 0;  // Telemetry: final send failures
static volatile bool telegram_upload_in_progress = false;  // true while TLS upload uses transient heap
static unsigned long telegramBackoffUntil = 0;      // millis() deadline for 429 backoff
#define TELEGRAM_PHOTO_MAX_ATTEMPTS 2

struct TelegramMessage {
    char text[TELEGRAM_MSG_MAX_LEN];
    bool has_photo;           // true = send photo instead of text
    uint8_t* photo_data;      // JPEG data (PSRAM allocated, freed by consumer)
    size_t photo_len;         // JPEG data length
    bool has_document;        // true = send file from SD as document
    char document_path[64];   // SD card file path (e.g. "/rec_20260207_143000.avi")
    uint8_t attempts;         // send attempts already made by telegramTask
    int person_track_id;      // >0 marks tracker notified only after Telegram success
};


uint32_t getTelegramQueueDepth() {
  return telegramQueue ? (uint32_t)uxQueueMessagesWaiting(telegramQueue) : 0;
}
uint32_t getTelegramDropCount() { return telegram_drop_count; }
uint32_t getTelegramSentCount() { return telegram_sent_count; }
uint32_t getTelegramFailCount() { return telegram_fail_count; }
bool isTelegramUploadInProgress() { return telegram_upload_in_progress; }
bool isTelegramQueueReady() { return telegramQueue != NULL; }
bool isTelegramTaskReady() { return telegramTaskHandle != NULL; }

// PERF counters from camera_capture.cpp
extern uint32_t getPerfMotionTriggers();
extern uint32_t getPerfAiFallbacks();
extern uint32_t getPerfPdPositives();
extern uint32_t getPerfPdNegatives();
extern uint32_t getPerfLastInferenceMs();

static void freeTelegramMessagePayload(TelegramMessage& msg) {
  if (msg.has_photo && msg.photo_data != NULL) {
    free(msg.photo_data);
    msg.photo_data = NULL;
    msg.photo_len = 0;
  }
}

static bool dropOldestQueuedTelegramPhoto() {
  if (telegramQueue == NULL) return false;

  UBaseType_t queued = uxQueueMessagesWaiting(telegramQueue);
  for (UBaseType_t i = 0; i < queued; i++) {
    TelegramMessage oldMsg;
    if (xQueueReceive(telegramQueue, &oldMsg, 0) != pdTRUE) {
      break;
    }

    if (oldMsg.has_photo && oldMsg.photo_data != NULL) {
      freeTelegramMessagePayload(oldMsg);
      telegram_drop_count++;
      Serial.println("📷 Telegram photo queue full, replaced oldest photo");
      return true;
    }

    // Preserve non-photo messages; if this ever fails the consumer raced us.
    if (xQueueSend(telegramQueue, &oldMsg, 0) != pdTRUE) {
      freeTelegramMessagePayload(oldMsg);
      telegram_drop_count++;
      Serial.println("📱 Telegram queue race, message dropped");
      return false;
    }
  }

  return false;
}
#endif // INCLUDE_TELEGRAM

// Forward declarations
#ifdef INCLUDE_TELEGRAM
bool sendTelegramNotificationSync(const String& message);  // Blocking (only for boot)
void sendTelegramNotification(const String& message);       // Non-blocking (queue)
bool sendTelegramPhotoSync(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption);
bool sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption, int person_track_id = -1);
bool sendTelegramDocumentSync(const char* sdPath, const String& caption);
void sendTelegramDocument(const char* sdPath, const String& caption);
#else
// Stub functions when Telegram is disabled
static inline bool sendTelegramNotificationSync(const String&) { return false; }
static inline void sendTelegramNotification(const String&) {}
static inline bool sendTelegramPhotoSync(const uint8_t*, size_t, const String&) { return false; }
static inline bool sendTelegramPhoto(const uint8_t*, size_t, const String&, int = -1) { return false; }
static inline bool sendTelegramDocumentSync(const char*, const String&) { return false; }
static inline void sendTelegramDocument(const char*, const String&) {}
#endif
bool initCamera();

// Camera auto-profile: Day / Dusk / Night based on LTR-308 ambient light
// Called every 10s from loop(). Uses hysteresis to avoid flickering.
// Settings based on community research: esp32-camera Issue #203, ESPHome, AstroEdition
enum CameraProfile { PROFILE_DAY, PROFILE_DUSK, PROFILE_NIGHT };
static CameraProfile currentProfile = PROFILE_DUSK;  // Init matches DUSK defaults

const char* getCameraProfileString() {
    switch (currentProfile) {
        case PROFILE_DAY:   return "DAY";
        case PROFILE_NIGHT: return "NIGHT";
        default:            return "DUSK";
    }
}

void updateCameraProfile() {
    float lux = 0.0f;
    if (!readAmbientLight(&lux)) return;

    // Skip first call when LTR-308 hasn't done a real read yet (lux still 0.0).
    // initCamera() already applied DUSK defaults; captureTask will call us again
    // after the first readAmbientLightSCCBSafe() with a real value.
    if (lux == 0.0f) return;

    // Hysteresis thresholds (lux)
    // Day:   >100  (drop to dusk at <60)
    // Dusk:  15-100 (drop to night at <15, rise to day at >100)
    // Night: <15   (rise to dusk at >20 — 5 lux gap prevents oscillation)
    // Note: NIGHT (AGC-off, exposure-based) handles low-lux scenes with bright spots better
    // than DUSK (AGC-on). Center-heavy AEC zone weights cause DUSK to underexpose dim scenes
    // with a bright window or lamp in frame. Raised threshold 12→15 to catch these earlier.
    CameraProfile target = currentProfile;
    switch (currentProfile) {
        case PROFILE_DAY:
            if (lux < 60.0f) target = PROFILE_DUSK;
            break;
        case PROFILE_DUSK:
            if (lux > 100.0f) target = PROFILE_DAY;
            else if (lux < 15.0f) target = PROFILE_NIGHT;
            break;
        case PROFILE_NIGHT:
            if (lux > 20.0f) target = PROFILE_DUSK;
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

const char* getResetReasonName(uint32_t reason) {
    switch ((esp_reset_reason_t)reason) {
        case ESP_RST_UNKNOWN:   return "UNKNOWN";
        case ESP_RST_POWERON:   return "POWERON";
        case ESP_RST_EXT:       return "EXT";
        case ESP_RST_SW:        return "SW";
        case ESP_RST_PANIC:     return "PANIC";
        case ESP_RST_INT_WDT:   return "INT_WDT";
        case ESP_RST_TASK_WDT:  return "TASK_WDT";
        case ESP_RST_WDT:       return "WDT";
        case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
        case ESP_RST_BROWNOUT:  return "BROWNOUT";
        case ESP_RST_SDIO:      return "SDIO";
#ifdef ESP_RST_USB
        case ESP_RST_USB:       return "USB";
#endif
#ifdef ESP_RST_JTAG
        case ESP_RST_JTAG:      return "JTAG";
#endif
#ifdef ESP_RST_EFUSE
        case ESP_RST_EFUSE:     return "EFUSE";
#endif
#ifdef ESP_RST_PWR_GLITCH
        case ESP_RST_PWR_GLITCH:return "PWR_GLITCH";
#endif
#ifdef ESP_RST_CPU_LOCKUP
        case ESP_RST_CPU_LOCKUP:return "CPU_LOCKUP";
#endif
        default:                return "OTHER";
    }
}

// Helper: Load System Stats
static constexpr uint32_t SYSTEM_STATS_MAGIC = 0xA12C2601;
static constexpr uint32_t SYSTEM_STATS_VERSION = 2;

struct PersistedSystemStats {
    uint32_t magic;
    uint32_t version;
    SystemStats stats;
};

struct LegacySystemStatsV1 {
    uint32_t total_restarts;
    uint32_t last_restart_reason;
    unsigned long longest_uptime;
};

SystemStats loadSystemStats() {
    SystemStats stats = {0, 0, 0, 0, 0, 0, 0};
    if (LittleFS.exists("/system_stats.bin")) {
        File file = LittleFS.open("/system_stats.bin", "r");
        if (file) {
            size_t size = file.size();
            if (size == sizeof(PersistedSystemStats)) {
                PersistedSystemStats persisted = {};
                if (file.read((uint8_t*)&persisted, sizeof(persisted)) == sizeof(persisted) &&
                    persisted.magic == SYSTEM_STATS_MAGIC &&
                    persisted.version == SYSTEM_STATS_VERSION) {
                    stats = persisted.stats;
                }
            } else if (size == sizeof(SystemStats)) {
                file.read((uint8_t*)&stats, sizeof(SystemStats));
            } else if (size == sizeof(LegacySystemStatsV1)) {
                LegacySystemStatsV1 legacy = {};
                if (file.read((uint8_t*)&legacy, sizeof(legacy)) == sizeof(legacy)) {
                    stats.total_restarts = legacy.total_restarts;
                    stats.last_restart_reason = legacy.last_restart_reason;
                    stats.longest_uptime = legacy.longest_uptime;
                }
            } else {
                Serial.printf("⚠️ Unknown system_stats.bin size (%u), resetting stats\n", (unsigned)size);
            }
            file.close();
        }
    }
    return stats;
}

// Helper: Save System Stats
void saveSystemStats(SystemStats stats) {
    File file = LittleFS.open("/system_stats.bin", "w");
    if (file) {
        PersistedSystemStats persisted = {
            SYSTEM_STATS_MAGIC,
            SYSTEM_STATS_VERSION,
            stats
        };
        file.write((uint8_t*)&persisted, sizeof(persisted));
        file.close();
    }
}

// --- Boot-timestamp ring buffer (restart-rate detection) ---
// The cumulative counters in SystemStats can only say "N POWERON restarts ever".
// A burst of restarts in a short window is the real signature of a degraded power
// supply, so we also persist the wall-clock time of the last N boots and derive a
// rate from them. Survives power loss (stored in LittleFS, not RTC memory which a
// POWERON clears). Timestamps are recorded lazily from loop() once NTP has synced,
// because at boot there is no valid wall-clock time yet.
static constexpr uint32_t BOOT_HISTORY_MAGIC = 0xB0074157;
static constexpr uint32_t BOOT_HISTORY_VERSION = 1;
static constexpr uint8_t BOOT_HISTORY_CAP = 16;
static constexpr time_t BOOT_TIME_SANITY = 1700000000; // 2023-11-14; reject pre-NTP junk

struct BootRecord {
    uint32_t epoch;   // unix time recorded shortly after boot (0 = unknown)
    uint8_t  reason;  // esp_reset_reason_t of that boot
    uint8_t  _pad[3];
};

struct BootHistory {
    uint32_t   magic;
    uint32_t   version;
    uint8_t    count;                     // valid records (<= CAP)
    uint8_t    head;                      // next write slot (ring)
    uint8_t    _pad[2];
    BootRecord records[BOOT_HISTORY_CAP];
};

BootHistory boot_history = {};
static bool boot_recorded = false;

static BootHistory loadBootHistory() {
    BootHistory h = {};
    h.magic = BOOT_HISTORY_MAGIC;
    h.version = BOOT_HISTORY_VERSION;
    if (LittleFS.exists("/boot_history.bin")) {
        File f = LittleFS.open("/boot_history.bin", "r");
        if (f) {
            if (f.size() == sizeof(BootHistory)) {
                BootHistory tmp = {};
                if (f.read((uint8_t*)&tmp, sizeof(tmp)) == sizeof(tmp) &&
                    tmp.magic == BOOT_HISTORY_MAGIC &&
                    tmp.version == BOOT_HISTORY_VERSION &&
                    tmp.count <= BOOT_HISTORY_CAP && tmp.head < BOOT_HISTORY_CAP) {
                    h = tmp;
                }
            }
            f.close();
        }
    }
    return h;
}

static void saveBootHistory() {
    File f = LittleFS.open("/boot_history.bin", "w");
    if (f) {
        f.write((uint8_t*)&boot_history, sizeof(boot_history));
        f.close();
    }
}

// Count recorded boots whose timestamp falls within the last `window_seconds`.
// Returns 0 when wall-clock time isn't available yet (caller treats as "unknown").
uint8_t getRestartsInWindow(uint32_t window_seconds) {
    struct tm ti;
    if (!getLocalTime(&ti, 0)) return 0;   // NTP not synced
    time_t now = time(NULL);
    if (now < BOOT_TIME_SANITY) return 0;
    uint8_t n = 0;
    for (uint8_t i = 0; i < boot_history.count && i < BOOT_HISTORY_CAP; i++) {
        uint32_t e = boot_history.records[i].epoch;
        if (e != 0 && (uint32_t)now >= e && ((uint32_t)now - e) <= window_seconds) n++;
    }
    return n;
}

bool bootTimeRecorded() { return boot_recorded; }

// Record this boot's timestamp once NTP time is available. Idempotent per boot.
static void maybeRecordBoot() {
    if (boot_recorded) return;
    struct tm ti;
    if (!getLocalTime(&ti, 0)) return;     // keep waiting for NTP
    time_t now = time(NULL);
    if (now < BOOT_TIME_SANITY) return;    // implausible time, keep waiting

    uint8_t idx = boot_history.head % BOOT_HISTORY_CAP;
    boot_history.records[idx].epoch = (uint32_t)now;
    boot_history.records[idx].reason = (uint8_t)sys_stats.last_restart_reason;
    boot_history.head = (uint8_t)((boot_history.head + 1) % BOOT_HISTORY_CAP);
    if (boot_history.count < BOOT_HISTORY_CAP) boot_history.count++;
    saveBootHistory();
    boot_recorded = true;

    Serial.printf("📊 Boot recorded @ %lu (reason=%s). Restarts last 1h=%u, 24h=%u\n",
                  (unsigned long)now, getResetReasonName(sys_stats.last_restart_reason),
                  getRestartsInWindow(3600), getRestartsInWindow(86400));
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
    unsigned long maxAllocHeap = ESP.getMaxAllocHeap();

    // Warning threshold: 55 KB — operating baseline after static TLS client is ~64 KB;
    // 55 KB gives 9 KB margin before triggering while avoiding false positives.
    if (freeHeap < 55000 && freeHeap >= 30000) {
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
            char evDetail[EVENT_DETAIL_LEN];
            snprintf(evDetail, sizeof(evDetail), "%uKB", freeHeap / 1024);
            logEvent(EVT_LOW_MEMORY, evDetail);
            if (freeHeap >= 50000) {
                sendTelegramNotification("⚠️ Low memory: " + String(freeHeap/1024) + " KB free");
            }
            lastLowMemAlert = millis();
            lastAlertHeap = freeHeap;
        }
    }

    // Critical threshold: tolerate short TLS/upload allocation spikes.
    static unsigned long criticalLowSince = 0;
    if (freeHeap < 30000) {
        bool telegramBusy = false;
#ifdef INCLUDE_TELEGRAM
        telegramBusy = telegram_upload_in_progress;
#endif
        if (telegramBusy) {
            Serial.printf("⚠️ CRITICAL LOW HEAP transient during Telegram upload: free=%u max=%u; restart deferred\n",
                          freeHeap, maxAllocHeap);
            criticalLowSince = 0;
            return;
        }

        if (criticalLowSince == 0) {
            criticalLowSince = millis();
            Serial.printf("⚠️ CRITICAL LOW HEAP sample: free=%u max=%u; waiting for confirmation\n",
                          freeHeap, maxAllocHeap);
            return;
        }

        if (millis() - criticalLowSince < 15000) {
            return;
        }

        Serial.printf("❌ SUSTAINED CRITICAL LOW HEAP: free=%u max=%u! Restarting ESP32...\n",
                      freeHeap, maxAllocHeap);
        // Best-effort alert only — heap is already critical, skip if too low
        if (freeHeap >= 20000) {
            sendTelegramNotification("❌ CRITICAL low memory! Restarting ESP32...\nFree: " + String(freeHeap/1024) + " KB");
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
        ESP.restart();  // Hard restart on sustained critical memory
    } else {
        criticalLowSince = 0;
    }

    // Planned restart: heap consistently below 50 KB for >60s but above 30 KB floor.
    // Catches slow leaks before they hit the emergency threshold.
    static unsigned long low50Since = 0;
    if (freeHeap < 50000 && freeHeap >= 30000) {
        if (low50Since == 0) low50Since = millis();
        bool telegramBusy = false;
#ifdef INCLUDE_TELEGRAM
        telegramBusy = telegram_upload_in_progress;
#endif
        if (!telegramBusy && millis() - low50Since > 60000) {
            Serial.printf("⚠️ Heap below 50 KB for >60s (%u B) — planned restart\n", freeHeap);
            logEvent(EVT_LOW_MEMORY, ("planned_restart:" + String(freeHeap/1024) + "k").c_str());
            vTaskDelay(pdMS_TO_TICKS(200));
            esp_restart();
        }
    } else {
        low50Since = 0;
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
  c.jpeg_quality = 12;  // 5-63, lower = better quality; 12 keeps Telegram JPEGs smaller/stabler
  c.flip_vertical = true;   // Camera is mounted upside-down
  c.flip_horizontal = true; // Mirror correction
  c.telegram_bot_token = "";   // Credentials loaded from NVS
  c.telegram_chat_id = "";
  c.http_user = "admin";
  c.http_pass = "admin";
  c.ota_password = "";
  c.ap_password = "";
  c.motion_detection_enabled = true;  // On by default for backward compatibility
  c.motion_telegram_photo = false;    // Off by default
  c.motion_telegram_video = false;    // Off by default
  c.motion_telegram_cooldown = 30;    // 30 seconds between photos
  c.motion_notify_start_hour = 0;    // Active hours: all day by default
  c.motion_notify_end_hour = 23;
  c.person_detection_enabled = false;    // Off by default (Phase 1 stub)
  c.person_telegram_photo = true;        // Send photo on person detection (independent of motion_telegram_photo)
  c.person_confidence_threshold = 0.6f;  // 60% confidence minimum
  c.person_detection_cooldown = 60;      // 60 seconds between AI-triggered photos
  c.person_recheck_interval = 12;        // 12 seconds between presence re-checks
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
  config.jpeg_quality = doc["jpeg_quality"] | 12;
  config.flip_vertical = doc["flip_vertical"] | false;
  config.flip_horizontal = doc["flip_horizontal"] | false;
  config.motion_detection_enabled = doc["motion_detection_enabled"] | true;
  config.motion_telegram_photo = doc["motion_telegram_photo"] | doc["motion_telegram_enabled"] | false;
  config.motion_telegram_video = doc["motion_telegram_video"] | false;
  config.motion_telegram_cooldown = doc["motion_telegram_cooldown"] | 30;
  config.motion_notify_start_hour = doc["motion_notify_start_hour"] | 0;
  config.motion_notify_end_hour = doc["motion_notify_end_hour"] | 23;
  config.person_detection_enabled = doc["person_detection_enabled"] | false;
  config.person_telegram_photo = doc["person_telegram_photo"] | true;
  config.person_confidence_threshold = doc["person_confidence_threshold"] | 0.6f;
  config.person_detection_cooldown = doc["person_detection_cooldown"] | 60;
  config.person_recheck_interval = doc["person_recheck_interval"] | 12;
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
    // v3.12.27+: keep UXGA but compress Telegram-bound JPEGs enough for stable TLS upload.
    if (config.frame_size < FRAMESIZE_UXGA) {
      Serial.printf("   frame_size: %d → %d (UXGA)\n", config.frame_size, FRAMESIZE_UXGA);
      config.frame_size = FRAMESIZE_UXGA;
    }
    if (config.jpeg_quality < 12) {
      Serial.printf("   jpeg_quality: %d → 12\n", config.jpeg_quality);
      config.jpeg_quality = 12;
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
  config.person_recheck_interval = constrain(config.person_recheck_interval, 5, 120);
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
  doc["motion_detection_enabled"] = config.motion_detection_enabled;
  doc["motion_telegram_photo"] = config.motion_telegram_photo;
  doc["motion_telegram_video"] = config.motion_telegram_video;
  doc["motion_telegram_cooldown"] = config.motion_telegram_cooldown;
  doc["motion_notify_start_hour"] = config.motion_notify_start_hour;
  doc["motion_notify_end_hour"] = config.motion_notify_end_hour;
  doc["person_detection_enabled"] = config.person_detection_enabled;
  doc["person_telegram_photo"] = config.person_telegram_photo;
  doc["person_confidence_threshold"] = config.person_confidence_threshold;
  doc["person_detection_cooldown"] = config.person_detection_cooldown;
  doc["person_recheck_interval"] = config.person_recheck_interval;
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
  cam_config.pin_sccb_sda = -1;       // Share Wire's I2C bus (Wire.begin called before initCamera)
  cam_config.pin_sccb_scl = -1;
  cam_config.sccb_i2c_port = 0;      // Wire uses port 0
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

    // AEC zone metering (4x4 grid, 16 zones).
    // Each reg covers 2 zones (hi nibble=zone N+1, lo nibble=zone N). Range: 0-F.
    // Top rows (windows/ceiling) get low weight; bottom rows (subject/floor) get high weight.
    // This prevents bright windows from crushing the AEC target and darkening the scene.
    s->set_reg(s, 0x5688, 0xff, 0x11);  // Zones 0,1: 1 (top row — windows)
    s->set_reg(s, 0x5689, 0xff, 0x11);  // Zones 2,3: 1
    s->set_reg(s, 0x568a, 0xff, 0x62);  // Zones 4,5: medium
    s->set_reg(s, 0x568b, 0xff, 0x62);  // Zones 6,7: medium
    s->set_reg(s, 0x568c, 0xff, 0xee);  // Zones 8,9: high (subject/room)
    s->set_reg(s, 0x568d, 0xff, 0xee);  // Zones 10,11: high
    s->set_reg(s, 0x568e, 0xff, 0xee);  // Zones 12,13: high (floor)
    s->set_reg(s, 0x568f, 0xff, 0xee);  // Zones 14,15: high

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
      resetMainTaskWdt(); // Keep WDT happy during long WiFi connect
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    wifi_health.reconnect_count++;
    wifi_health.last_reconnect_time = millis();
    logEvent(EVT_WIFI_RECONNECT);
    if (wifi_health.reconnect_count >= 3) {
        sendTelegramNotification("⚠️ Frequent WiFi dropouts!\nReconnects in the past hour: " + String(wifi_health.reconnect_count));
    }
    Serial.println("\n\nWiFi connected!");
    Serial.print("IP address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Signal strength (RSSI): ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    // Override DNS: set Google's public DNS as primary + secondary.
    // The router's DNS sometimes fails to resolve api.telegram.org after WiFi connect,
    // causing photo upload DNS errors (-54). 8.8.8.8/8.8.4.4 is more reliable.
    WiFi.config(WiFi.localIP(), WiFi.gatewayIP(), WiFi.subnetMask(),
                IPAddress(8, 8, 8, 8), IPAddress(8, 8, 4, 4));
    Serial.println("✅ DNS set to 8.8.8.8 / 8.8.4.4");

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
// Shared TLS client — allocated once, never freed. Eliminates repeated alloc/free
// of the ~35 KB mbedTLS context that fragments the heap on every Telegram send.
// All sends are serialized through telegramTask, so there is no concurrent access.
static WiFiClientSecure s_telegram_client;

// Synchronous Telegram send (blocking, ~5s max) -- use only during boot/shutdown
bool sendTelegramNotificationSync(const String& message) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    return false;
  }

  HTTPClient http;
  s_telegram_client.stop();
  s_telegram_client.setInsecure();
  String url = "https://api.telegram.org/bot" + config.telegram_bot_token + "/sendMessage";
  http.begin(s_telegram_client, url);
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
    logEvent(EVT_TELEGRAM_FAILED, http.errorToString(httpCode).c_str());
    Serial.printf("Telegram request failed: %s\n", http.errorToString(httpCode).c_str());
  } else if (httpCode == 429) {
    uint32_t retryAfterSec = 10;
    String body = http.getString();
    int idx = body.indexOf("\"retry_after\":");
    if (idx >= 0) retryAfterSec = (uint32_t)body.substring(idx + 14).toInt();
    retryAfterSec = min(retryAfterSec, (uint32_t)60);
    telegramBackoffUntil = millis() + retryAfterSec * 1000UL;
    logEvent(EVT_TELEGRAM_FAILED, "429 Too Many Requests");
    Serial.printf("📱 Telegram 429: backoff %us\n", retryAfterSec);
  } else if (httpCode != 200) {
    logEvent(EVT_TELEGRAM_FAILED, ("HTTP " + String(httpCode)).c_str());
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

  struct TelegramUploadGuard {
    TelegramUploadGuard() { telegram_upload_in_progress = true; }
    ~TelegramUploadGuard() { telegram_upload_in_progress = false; }
  } uploadGuard;

  // stop() before each use resets session state without re-allocating the SSL context.
  // SECURITY: setInsecure() skips certificate validation (MITM possible on untrusted LANs).
  s_telegram_client.stop();
  s_telegram_client.setInsecure();
  if (!s_telegram_client.connect("api.telegram.org", 443, 15000)) {
    Serial.printf("Telegram %s: connection failed\n", apiMethod);
    logEvent(EVT_TELEGRAM_FAILED, "connection refused");
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

  s_telegram_client.println("POST /bot" + config.telegram_bot_token + "/" + apiMethod + " HTTP/1.1");
  s_telegram_client.println("Host: api.telegram.org");
  s_telegram_client.println("Content-Length: " + String(totalLen));
  s_telegram_client.println("Content-Type: multipart/form-data; boundary=" + boundary);
  s_telegram_client.println("Connection: close");
  s_telegram_client.println();
  s_telegram_client.print(head);

  // Upload payload in chunks — 512B keeps TLS record overhead manageable on weak WiFi
  size_t sent = 0;
  uint8_t buf[512];
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
        s_telegram_client.stop();
        return false;
      }
    }

    size_t written = s_telegram_client.write(buf, chunk);
    if (written == 0) {
      Serial.printf("Telegram %s: write failed at %u/%u\n", apiMethod, (unsigned)sent, (unsigned)payloadLen);
      logEvent(EVT_TELEGRAM_FAILED, "write_failed");
      s_telegram_client.stop();
      return false;
    }
    sent += written;

    // Yield periodically during long uploads
    if ((sent % (64 * 1024)) == 0) vTaskDelay(pdMS_TO_TICKS(1));
  }

  s_telegram_client.print(tail);

  // Read response
  unsigned long start = millis();
  while (!s_telegram_client.available() && millis() - start < timeoutMs) {
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  bool success = false;
  if (s_telegram_client.available()) {
    String statusLine = s_telegram_client.readStringUntil('\n');
    // Parse HTTP status code: "HTTP/1.1 200 OK" → 200
    int statusCode = 0;
    int sp = statusLine.indexOf(' ');
    if (sp > 0) statusCode = statusLine.substring(sp + 1, sp + 4).toInt();
    success = (statusCode == 200);

    if (success) {
      Serial.printf("Telegram %s sent: %s (%uKB)\n", apiMethod, filename, (unsigned)(payloadLen / 1024));
    } else {
      Serial.printf("Telegram %s API error: %s\n", apiMethod, statusLine.c_str());
      if (statusCode != 429) {  // 429 is logged below with backoff detail
        String detail = "api_" + String(statusCode);
        logEvent(EVT_TELEGRAM_FAILED, detail.c_str());
      }
    }

    if (statusCode == 429) {
      uint32_t retryAfterSec = 10;
      // Scan response headers for Retry-After
      while (s_telegram_client.available()) {
        String hdr = s_telegram_client.readStringUntil('\n');
        hdr.trim();
        if (hdr.length() == 0) break; // end of headers
        if (hdr.startsWith("Retry-After:")) {
          retryAfterSec = (uint32_t)hdr.substring(12).toInt();
          break;
        }
      }
      retryAfterSec = min(retryAfterSec, (uint32_t)60);
      telegramBackoffUntil = millis() + retryAfterSec * 1000UL;
      Serial.printf("📱 Telegram 429: backoff %us\n", retryAfterSec);
      logEvent(EVT_TELEGRAM_FAILED, ("429_backoff:" + String(retryAfterSec) + "s").c_str());
    }
  } else {
    Serial.printf("Telegram %s: response timeout\n", apiMethod);
    logEvent(EVT_TELEGRAM_FAILED, "response_timeout");
  }

  s_telegram_client.stop();
  return success;
}

// Photo upload (RAM JPEG buffer)
bool sendTelegramPhotoSync(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) return false;
  if (!jpeg_data || jpeg_len == 0) return false;
  unsigned long heap = ESP.getFreeHeap();
  unsigned long maxAlloc = ESP.getMaxAllocHeap();
  if (heap < 55000 || maxAlloc < 32000) {
    // heap < 55 KB: not enough total RAM headroom.
    // max_alloc < 32 KB: heap too fragmented — mbedTLS needs a ~32 KB contiguous
    // block for its SSL input/output record buffers; below this the TLS malloc fails
    // mid-handshake and leaves the heap in a corrupt state.
    Serial.printf("📷 sendTelegramPhoto: skipped (heap=%lu max_alloc=%lu)\n", heap, maxAlloc);
    logEvent(EVT_TELEGRAM_FAILED, ("heap_low:" + String(heap / 1024) + "k/" + String(maxAlloc / 1024) + "k").c_str());
    return false;
  }
  return telegramMultipartUpload("sendPhoto", "photo", "motion.jpg", "image/jpeg",
                                  jpeg_data, jpeg_len, NULL, caption, 15000);
}

// Document upload (SD file streaming)
bool sendTelegramDocumentSync(const char* sdPath, const String& caption) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) return false;
  if (ESP.getFreeHeap() < 50000) {
    Serial.printf("📄 sendTelegramDocument: skipped, heap too low (%u B)\n", ESP.getFreeHeap());
    return false;
  }

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
    bool draining = !telegram_boot_drained;  // first poll = drain old messages silently

    for (JsonObject update : results) {
        long update_id = update["update_id"].as<long>();
        if (update_id > telegram_last_update_id) {
            telegram_last_update_id = update_id;
        }

        if (draining) continue;  // skip processing — just advance the offset

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

    if (draining) {
        telegram_boot_drained = true;
        Serial.printf("📱 Telegram boot drain: skipped old messages, offset=%ld\n", telegram_last_update_id);
    }
}

// Telegram background task -- processes messages from queue + polls for incoming commands
void telegramTask(void* param) {
  TelegramMessage msg;
  Serial.println("📱 Telegram async task started (bidirectional)");

  while (true) {
    // Heartbeat: confirm task is alive (printed every 10s)
    {
      static unsigned long lastHB = 0;
      if (millis() - lastHB > 10000) {
        Serial.printf("📱 telegramTask alive: uptime=%lus queue=%u backoff_rem=%lus heap=%u\n",
                      millis()/1000,
                      telegramQueue ? uxQueueMessagesWaiting(telegramQueue) : 0,
                      telegramBackoffUntil > millis() ? (telegramBackoffUntil - millis())/1000 : 0,
                      ESP.getFreeHeap());
        lastHB = millis();
      }
    }

    // Honour 429 backoff -- messages stay in queue, getUpdates polling also paused
    if (millis() < telegramBackoffUntil) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }

    // Wait for outgoing message OR TELEGRAM_POLL_INTERVAL_MS timeout for polling incoming commands
    if (xQueueReceive(telegramQueue, &msg, pdMS_TO_TICKS(TELEGRAM_POLL_INTERVAL_MS)) == pdTRUE) {
      if (WiFi.status() != WL_CONNECTED) {
        telegram_fail_count++;
        logEvent(EVT_TELEGRAM_FAILED, "wifi_disconnected");
        Serial.println("📱 Telegram: WiFi not connected, dropping message");
        freeTelegramMessagePayload(msg);
        continue;
      }

      bool ok;
      if (msg.has_document && msg.document_path[0] != '\0') {
        // Document message -- stream from SD (can block up to 120s for large files)
        ok = sendTelegramDocumentSync(msg.document_path, String(msg.text));
      } else if (msg.has_photo && msg.photo_data != NULL && msg.photo_len > 0) {
        // Photo message -- multipart upload (can block up to 15s)
        ok = sendTelegramPhotoSync(msg.photo_data, msg.photo_len, String(msg.text));
        if (!ok && msg.attempts + 1 < TELEGRAM_PHOTO_MAX_ATTEMPTS) {
          msg.attempts++;
          Serial.printf("📱 Telegram photo send failed, retry %u/%u queued\n",
                        (unsigned)(msg.attempts + 1), (unsigned)TELEGRAM_PHOTO_MAX_ATTEMPTS);
          logEvent(EVT_TELEGRAM_FAILED, "photo_retry");
          vTaskDelay(pdMS_TO_TICKS(2500));
          if (xQueueSendToFront(telegramQueue, &msg, 0) == pdTRUE) {
            continue;
          }
        }
        if (ok) {
          telegram_sent_count++;
          char detail[EVENT_DETAIL_LEN];
          if (msg.person_track_id > 0) {
            snprintf(detail, sizeof(detail), "photo %uKB id=#%d",
                     (unsigned)(msg.photo_len / 1024), msg.person_track_id);
            objectTracker.markNotified(msg.person_track_id);
          } else {
            snprintf(detail, sizeof(detail), "photo %uKB", (unsigned)(msg.photo_len / 1024));
          }
          logEvent(EVT_TELEGRAM_SENT, detail);
        } else {
          telegram_fail_count++;
          logEvent(EVT_TELEGRAM_FAILED, "photo_final");
        }
        // Free PSRAM copy allocated by sendTelegramPhoto()
        freeTelegramMessagePayload(msg);
      } else {
        // Text message (can block up to 5s)
        ok = sendTelegramNotificationSync(String(msg.text));
      }
      if (ok && !msg.has_photo) {
        telegram_sent_count++;
        logEvent(EVT_TELEGRAM_SENT, msg.has_document ? "document" : "text");
      } else if (!ok && !msg.has_photo) {
        telegram_fail_count++;
        Serial.println("📱 Telegram: Send failed, message dropped");
      }

      // Small delay between messages to avoid rate limiting
      vTaskDelay(pdMS_TO_TICKS(500));
    } else {
      // getUpdates polling disabled: each poll opens a TLS session and leaves
      // ~0.44 KB permanent heap fragment. After ~1 h the heap is fragmented
      // enough that new TLS sends fail with "connection refused".
      // Incoming commands are available via the web UI instead.
      (void)0;
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
bool sendTelegramPhoto(const uint8_t* jpeg_data, size_t jpeg_len, const String& caption, int person_track_id) {
  if (config.telegram_bot_token.length() == 0 || config.telegram_chat_id.length() == 0) {
    telegram_fail_count++;
    Serial.println("📷 sendTelegramPhoto: credentials missing");
    logEvent(EVT_TELEGRAM_FAILED, "photo_no_credentials");
    return false;
  }
  if (jpeg_data == NULL || jpeg_len == 0 || jpeg_len > 512 * 1024) {
    telegram_fail_count++;
    Serial.printf("📷 sendTelegramPhoto: Invalid JPEG data len=%u\n", (unsigned)jpeg_len);
    logEvent(EVT_TELEGRAM_FAILED, "photo_invalid_jpeg");
    return false;
  }

  // If the async queue was not created, fall back to a bounded synchronous send.
  // This is slower, but it is better than confirming a person and dropping the alert.
  if (telegramQueue == NULL) {
    Serial.println("📷 sendTelegramPhoto: queue unavailable, using sync fallback");
    bool ok = sendTelegramPhotoSync(jpeg_data, jpeg_len, caption);
    if (ok) {
      telegram_sent_count++;
      char detail[EVENT_DETAIL_LEN];
      if (person_track_id > 0) {
        snprintf(detail, sizeof(detail), "photo_sync %uKB id=#%d", (unsigned)(jpeg_len / 1024), person_track_id);
        objectTracker.markNotified(person_track_id);
      } else {
        snprintf(detail, sizeof(detail), "photo_sync %uKB", (unsigned)(jpeg_len / 1024));
      }
      logEvent(EVT_TELEGRAM_SENT, detail);
    } else {
      telegram_fail_count++;
      logEvent(EVT_TELEGRAM_FAILED, "photo_sync");
    }
    return ok;
  }

  // Allocate PSRAM copy so the ring buffer slot can be released immediately
  uint8_t* copy = (uint8_t*)ps_malloc(jpeg_len);
  if (copy == NULL) {
    telegram_fail_count++;
    Serial.printf("📷 sendTelegramPhoto: PSRAM alloc failed len=%u\n", (unsigned)jpeg_len);
    logEvent(EVT_TELEGRAM_FAILED, "photo_psram_alloc");
    return false;
  }
  memcpy(copy, jpeg_data, jpeg_len);

  TelegramMessage msg;
  memset(&msg, 0, sizeof(msg));
  msg.has_photo = true;
  msg.photo_data = copy;
  msg.photo_len = jpeg_len;
  msg.person_track_id = person_track_id;

  // Caption goes into text field
  size_t capLen = min((size_t)caption.length(), (size_t)(TELEGRAM_MSG_MAX_LEN - 1));
  memcpy(msg.text, caption.c_str(), capLen);
  msg.text[capLen] = '\0';

  // Non-blocking enqueue. If the queue is full, replace the oldest queued
  // photo with this fresher frame while preserving text/document messages.
  if (xQueueSend(telegramQueue, &msg, 0) != pdTRUE) {
    if (dropOldestQueuedTelegramPhoto() &&
        xQueueSend(telegramQueue, &msg, 0) == pdTRUE) {
      return true;
    }

    telegram_drop_count++;
    Serial.println("📷 Telegram photo queue full, dropped newest photo");
    logEvent(EVT_TELEGRAM_FAILED, "photo_queue_full");
    freeTelegramMessagePayload(msg);
    return false;
  }

  return true;
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
  Serial.println("Initializing Watchdog timer (60s timeout, auto-restart)...");
  esp_task_wdt_deinit(); // Deinit default WDT first

  esp_task_wdt_config_t wdt_config = {
      .timeout_ms = 60000,   // 60s — main loop resets every 30s (30s safety margin)
      .idle_core_mask = 0,   // Don't monitor idle tasks
      .trigger_panic = true  // Force restart on hang (SD/Telegram tasks are separate)
  };

  if (esp_task_wdt_init(&wdt_config) == ESP_OK) {
      Serial.println("✅ WDT initialized (loopTask will be added after setup)");
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
  switch ((esp_reset_reason_t)sys_stats.last_restart_reason) {
    case ESP_RST_POWERON:   sys_stats.poweron_restarts++; break;
    case ESP_RST_BROWNOUT:  sys_stats.brownout_restarts++; break;
    case ESP_RST_WDT:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:  sys_stats.wdt_restarts++; break;
    default:                sys_stats.other_restarts++; break;
  }
  saveSystemStats(sys_stats);
  Serial.printf("📊 Total restarts: %u\n", sys_stats.total_restarts);
  Serial.printf("📊 Last reset reason: %s (%u)\n",
                getResetReasonName(sys_stats.last_restart_reason),
                sys_stats.last_restart_reason);
  Serial.printf("📊 Power resets: poweron=%u brownout=%u wdt=%u other=%u\n",
                sys_stats.poweron_restarts, sys_stats.brownout_restarts,
                sys_stats.wdt_restarts, sys_stats.other_restarts);

  char bootDetail[EVENT_DETAIL_LEN];
  snprintf(bootDetail, sizeof(bootDetail), "reason=%s(%u),n=%u",
           getResetReasonName(sys_stats.last_restart_reason),
           sys_stats.last_restart_reason,
           sys_stats.total_restarts);
  initEventLog(bootDetail);

  // Load boot-timestamp history for restart-rate detection. This boot's own
  // timestamp is appended later (from loop()) once NTP has synced.
  boot_history = loadBootHistory();

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

  // Init I2C bus before camera so camera SCCB can share Wire's driver (same GPIO 8/9)
  Wire.begin(I2C_SDA, I2C_SCL);

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

  // Telegram async polling starts after setup finishes the boot notification.
  // That avoids TLS getUpdates work overlapping the rest of initialization.

  // Start camera server
  // Initialize live log system (before camera server so handlers get registered)
  wsLogInit();

  startCameraServer();

  // Initialize IR Night Vision control (LTR-308 ambient light sensor init here)
  initIRControl();

  // Apply camera profile immediately now that LTR-308 is available
  updateCameraProfile();

#ifdef INCLUDE_AUDIO
  initAudio();
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

#ifdef INCLUDE_TELEGRAM
  // Must be created here, BEFORE mDNS/OTA — both of those can hang on this
  // framework (ESP32-S3 + ArduinoOTA UDP/3232 is known broken; mDNS.begin()
  // also blocks indefinitely on some boot cycles). Queue created early ensures
  // person-detections that fire during the remaining setup don't fall back to the
  // slow sync path, and telegramTask can start draining immediately.
  telegramQueue = xQueueCreate(TELEGRAM_QUEUE_SIZE, sizeof(TelegramMessage));
  if (telegramQueue) {
    xTaskCreatePinnedToCore(
      telegramTask, "Telegram", 32768, NULL, 3, &telegramTaskHandle, 0
    );
    Serial.println("✅ Telegram async queue + task started");
  } else {
    Serial.println("⚠️ Failed to create Telegram queue (falling back to sync)");
  }
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
    resetMainTaskWdt();

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
    resetMainTaskWdt();
  }

  // Initialize mDNS (required for OTA hostname discovery)
  if (WiFi.status() == WL_CONNECTED) {
    if (MDNS.begin(config.device_name.c_str())) {
      Serial.printf("✅ mDNS started: %s.local\n", config.device_name.c_str());
      MDNS.addService("http", "tcp", 80);
      MDNS.addService("http", "tcp", 81);
#ifdef INCLUDE_AUDIO
      MDNS.addService("http", "tcp", 82);
#endif
#ifndef LITE_MODE_NO_RUNTIME_TASKS
      // Only advertise RTSP when the server is actually started (LITE_MODE skips it,
      // see camera_server.cpp) — otherwise clients discover a dead service and hang.
      MDNS.addService("rtsp", "tcp", 554);
#endif
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

  if (esp_task_wdt_add(NULL) == ESP_OK) {
      main_task_wdt_registered = true;
      Serial.println("✅ loopTask added to WDT");
  } else {
      Serial.println("⚠️ Failed to add loopTask to WDT");
  }
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
      resetMainTaskWdt();
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

  // Record this boot's timestamp once NTP is available (restart-rate detection).
  // Retries every 5 s until it succeeds, then stops (idempotent per boot).
  static unsigned long lastBootRecTry = 0;
  if (!boot_recorded && millis() - lastBootRecTry > 5000) {
      lastBootRecTry = millis();
      maybeRecordBoot();
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

  // WiFi signal check + stack watermarks every hour
  static unsigned long lastWiFiCheck = 0;
  if (millis() - lastWiFiCheck > 3600000) {
      checkWiFiSignal();
      Serial.printf("STACK HWM: telegram=%u loop=%u\n",
          telegramTaskHandle ? uxTaskGetStackHighWaterMark(telegramTaskHandle) : 0,
          uxTaskGetStackHighWaterMark(NULL));
      lastWiFiCheck = millis();
  }

  // Reset reconnect counter every hour
  static unsigned long lastReconnectReset = 0;
  if (millis() - lastReconnectReset > 3600000) {
      wifi_health.reconnect_count = 0;
      lastReconnectReset = millis();
  }

  // SD logger periodic tick (flush + auto-stop check)
  sdLogTick();

  // Heap monitoring
  static unsigned long lastHeapCheck = 0;
  static uint32_t heapBaseline = 0;
  if (millis() - lastHeapCheck > 30000) {
    uint32_t fh = ESP.getFreeHeap(), ma = ESP.getMaxAllocHeap();
    if (heapBaseline == 0) heapBaseline = fh;
    uint32_t fragPct = (ma * 100) / fh;
    int32_t drift = (int32_t)fh - (int32_t)heapBaseline;
    Serial.printf("Heap: %u free, max_alloc=%u frag=%u%% drift=%+d | PSRAM: %u | RSSI: %d dBm | uptime: %lus\n",
                  fh, ma, fragPct, drift, ESP.getFreePsram(), WiFi.RSSI(), millis() / 1000);
    Serial.printf("PERF: infer_ms=%u motion=%u pd_pos=%u pd_neg=%u tg_sent=%u fallback=%u\n",
                  getPerfLastInferenceMs(), getPerfMotionTriggers(),
                  getPerfPdPositives(), getPerfPdNegatives(),
                  getTelegramSentCount(), getPerfAiFallbacks());
    lastHeapCheck = millis();
  }

  // Keep WiFi alive with exponential backoff retry logic
  static unsigned long lastCheck = 0;
  static int reconnect_attempts = 0;
  const int MAX_RECONNECT_ATTEMPTS = 5;

  if (millis() - lastCheck > reconnect_delay_ms) {  // Dynamic interval (exponential backoff)
    if (WiFi.status() != WL_CONNECTED && WiFi.getMode() == WIFI_STA) {
      // WiFi.begin() allocates ~30-50 KB internally; skip attempt if heap is
      // already low to avoid worsening memory pressure during recovery.
      // 55 KB matches the TLS guard — baseline is ~64 KB so this allows reconnect
      // under normal conditions (was 70 KB which always deferred at current baseline).
      if (ESP.getFreeHeap() < 55000) {
        Serial.printf("WiFi reconnect deferred: heap too low (%u B)\n", ESP.getFreeHeap());
        lastCheck = millis();
        return;
      }
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
  mqttLoop();
  mqttHandleCommands();
  if (mqtt_snapshot_flag) {
      mqtt_snapshot_flag = false;
      int idx = current_frame_index;
      if (idx >= 0) {
          int rc = __atomic_load_n(&frameBuffers[idx].ref_count, __ATOMIC_SEQ_CST);
          if (rc >= 0) {
              __atomic_fetch_add(&frameBuffers[idx].ref_count, 1, __ATOMIC_SEQ_CST);
              sendTelegramPhotoSync(frameBuffers[idx].data, frameBuffers[idx].len, "📷 Snapshot (MQTT)");
              __atomic_fetch_sub(&frameBuffers[idx].ref_count, 1, __ATOMIC_SEQ_CST);
          }
      }
  }
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
