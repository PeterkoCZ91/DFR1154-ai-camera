#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// --- LAB MODE ---
// When true: HTTP auth is bypassed on mutable endpoints (/settings, /record,
// /sd-clear, /ir-control, /credentials, /update). Convenient for bench testing
// without juggling Basic Auth credentials, but UNSAFE on untrusted networks.
//
// Production default: false. Override at build time if you really need it on
// a trusted dev network:  pio run -DLAB_MODE=1
#ifndef LAB_MODE
#define LAB_MODE false
#endif

// --- COMPILE-TIME FEATURE TOGGLES ---
// Comment out to disable feature and save RAM/flash
#define INCLUDE_TELEGRAM      // Telegram notifications (text, photo, AVI document)
#define INCLUDE_MQTT          // MQTT + Home Assistant discovery
#define INCLUDE_AUDIO         // I2S/PDM microphone + audio streaming
#define INCLUDE_PERSON_DETECT // Edge Impulse YOLO-Pro Pico person detection
#define INCLUDE_SD_RECORDING  // SD card AVI recording + auto-delete
#define INCLUDE_TIMELAPSE     // Periodic JPEG snapshots to SD

// --- v3.12 LITE MODE (runtime task suppression) ---
// When defined: skips starting audio I2S, audio_httpd:82, RTSP server,
// recording task, and timelapse task at boot. Code stays compiled,
// only runtime starts are bypassed. Frees ~42 KB SRAM contiguous heap
// to fix Telegram SSL handshake failures (needs ~30 KB max_alloc_heap)
// caused by fragmentation in default config (was max_alloc_heap≈32 KB).
// Use case: API + Telegram only, no live streaming or recording features.
#define LITE_MODE_NO_RUNTIME_TASKS

// Telegram getUpdates polling interval (ms).
// Each poll allocates ~5 KB on heap (HTTPClient + TLS + JSON) and leaves a
// ~1-2 KB residual fragment. At 5s polling this caused ~10 KB/min heap drift
// observed in v3.12.2 field testing (free_heap 102KB -> 50KB in 5min idle).
// 15s is a compromise: bot commands still feel responsive (~8s avg latency),
// drift drops to ~1.5-2 KB/min and heap plateaus >90 KB.
#define TELEGRAM_POLL_INTERVAL_MS 15000

// Configuration Struct
struct Config {
  // WiFi & Network
  String wifi_ssid;
  String wifi_password;
  String device_name;
  String version;

  // Telegram
  String telegram_bot_token;
  String telegram_chat_id;

  // Security
  String http_user;
  String http_pass;
  String ota_password;     // OTA update password (stored in NVS)
  String ap_password;      // Access Point password (stored in NVS)

  // Camera Settings
  int frame_size;        // FRAMESIZE_xxx
  int jpeg_quality;      // 5-63 (lower = better quality, OV3660 min ~4)
  bool flip_vertical;
  bool flip_horizontal;

  // Fine-tuning controls
  int brightness;        // -2 to 2
  int contrast;          // -2 to 2
  int saturation;        // -2 to 2
  int sharpness;         // -3 to 3
  int denoise;           // 0 to 8
  int ae_level;          // -2 to 2
  int aec_value;         // 0 to 1200
  int agc_gain;          // 0 to 30
  int gainceiling;       // 0 to 6 (gainceiling_t)
  int wb_mode;           // 0 to 4
  bool bpc;              // Black Pixel Correction
  bool wpc;              // White Pixel Correction
  bool raw_gma;          // Gamma Correction
  bool lenc;             // Lens Correction

  // Motion → Telegram notifications (independent flags)
  bool motion_telegram_photo;      // Send photo to Telegram on motion detection
  bool motion_telegram_video;      // Send AVI video to Telegram after recording stops
  int motion_telegram_cooldown;    // Cooldown between photos in seconds
  int motion_notify_start_hour;    // Active hours start (0-23, default 0)
  int motion_notify_end_hour;      // Active hours end (0-23, default 23)

  // Person Detection (Edge Impulse FOMO) -- Phase 1: stub inference
  bool person_detection_enabled;       // Cascade: motion → AI → Telegram (default false)
  float person_confidence_threshold;   // Min confidence to confirm person (0.0-1.0, default 0.6)
  int person_detection_cooldown;       // Cooldown between AI-triggered photos in seconds (default 60)

  // MQTT (Home Assistant integration)
  bool mqtt_enabled;
  String mqtt_server;
  int mqtt_port;
  String mqtt_user;       // Stored in NVS
  String mqtt_pass;       // Stored in NVS

  // SD Card auto-recording
  bool sd_auto_record;               // Auto-start recording on motion (default false)
  int sd_auto_record_duration;       // Keep recording N seconds after last motion (default 30)
  int sd_max_usage_percent;          // Auto-delete oldest files above this % (default 90)
  int sd_record_fps;                 // AVI recording framerate (default 10)

  // Time-lapse (periodic JPEG snapshots to SD)
  bool timelapse_enabled;            // Enable time-lapse capture (default false)
  int timelapse_interval_sec;        // Interval between captures in seconds (default 60)
};

#endif // CONFIG_H
