# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [3.12.5] - 2026-04-18

### Added — English UI (continued)

- **Settings page fully translated** — `SETTINGS_HTML_GZ` blob in `camera_server.cpp` rebuilt with English labels (Basic / Image / Auto controls / Telegram notifications / Person detection / SD recording / MQTT). `<html lang="cs">` → `lang="en">`. Blob shrunk 5023 → 4963 bytes.
- **Captive-portal WiFi setup translated** — `CAPTIVE_PORTAL_HTML` form placeholders and status messages all English.

## [3.12.4] - 2026-04-18

### Changed — User-facing strings

- **Dashboard (INDEX_HTML_GZ) translated to English** — title, section headings, toggle text, and JS status strings. Re-gzipped 2943 → 2919 bytes.
- **Telegram bot rewritten in English** — commands renamed (`/foto` → `/photo`, `/detekce` → `/detect`, `/osoba` → `/person`, `/hlidej` → `/watch`, `/ticho` → `/silent`, `/hodiny` → `/hours`, `/prah` → `/threshold`, `/nahraj` → `/record`). All response captions, help text, and boot / low-heap / WiFi-signal alerts translated.
- **Motion and person-detection captions** — `Detekovany pohyb!` → `Motion detected!`, `Osoba detekovana!` → `Person detected!`, AVI document caption translated.

## [3.12.3] - 2026-04-18

### Fixed — Ring buffer and tracker

- **Ring-buffer publish ordering** (`camera_capture.cpp`) — release `ref_count` before publishing `current_frame_index` (both atomic SEQ_CST). Readers can never observe a new index that still has the writer sentinel.
- **`acquireFrameReader()` bounded spin** (`camera_capture.h`) — 16-attempt retry with `taskYIELD()` replaces the unbounded CAS loop; prevents CPU burn on reader contention and yields to lower-priority tasks between retries.
- **Tracker `notified` flag reset on delete** (`tracker.cpp`) — slots now explicitly clear `notified=false` when a track is deleted, so recycled slots can't inherit the previous occupant's state via any code path.
- **FOMO temporal counter reset on decode failure** (`person_detection.cpp`) — `consecutive_detections = 0` on `prepareInput()` failure; a recovered frame can no longer pass the temporal filter on a single hit and fire a false positive inherited from pre-corruption detections.

### Security

- **`LAB_MODE` default off** (`config.h`) — overridable at build time with `-DLAB_MODE=1` for bench testing.
- **`RASPBERRY_PI_IP` compile define** (`camera_server.cpp`) — replaced hardcoded LAN IP with `UDP_LOG_IP` default `127.0.0.1`, override via build flag.
- **Telegram queue capacity 5 → 16** (`main.cpp`) — accommodates burst motion events without silent drops; drop counter telemetry added.
- **TLS trade-off documented** — comment above `client.setInsecure()` spells out the MITM risk and CA-bundle alternative.

### Cleanup

- **Motion grid bounds clamp** (`motion_detection.cpp`) — `bw` / `bh` clamped to `MOTION_GRID_W` / `MOTION_GRID_H`.
- **`ir_handler.cpp`** — fixed `\\n` copy-paste typos in `Serial.printf`.
- **`CAPTURE_SENSOR_POLL_FRAMES` define** replaces magic `% 150` literal.
- **Time-lapse SD-full guard** — skip capture when SD ≥ 98 % full, rate-limited warning.
- **Main loop** — removed redundant `updateIRAutoMode()` / `updateCameraProfile()` calls; `captureTask` already runs these every ~5 s, `loop()` was starved anyway.

---

## [3.11.0] - 2026-04-10

### Added — Detection Improvements

- **Temporal filter for person detection** (`person_detection.cpp`) — requires 2+ consecutive detected frames before confirming a person, eliminating ~90% of FOMO false positives. Adds `consecutive_detections` counter and `PD_TEMPORAL_FRAMES` constant.
- **ByteTrack-style object tracker** (`tracker.h/cpp`, NEW) — lightweight Kalman tracker with constant-velocity prediction and greedy nearest-neighbor matching. Maintains persistent track IDs across frames so the same person doesn't trigger duplicate notifications. ~1 KB SRAM, ~2-5 ms per frame.
- **Histogram normalization clamp** (`person_detection.cpp`) — `normalizeContrast()` skips frames with intensity range < 20, preventing IR sensor noise from being amplified into false detections at true night.
- **Night motion mode rework** (`motion_detection.cpp`) — replaces blanket suppression below 10 lux with stricter requirements: 2.5× pixel threshold, 2.5× trigger percentage, and 2+ neighbor clustering. Now detects actual night motion while filtering sensor noise.
- **Sudden brightness change reset** (`motion_detection.cpp`) — when average brightness jumps by more than 80/255 between consecutive frames (e.g. lights on/off, sunset, headlights), the EMA background model is instantly reset to prevent false motion during the transition.
- **LTR-308 sample rate 5× faster** (`ir_handler.cpp`) — measurement rate changed from 500 ms to 100 ms (register `0x21` instead of `0x03`). Faster reaction to sunset, headlights, and room light changes. Lux conversion factor recalculated for new integration time (`0.0333` instead of `0.00833`), mask updated to 17-bit (was 20-bit).
- **OV3660 advanced ISP register tuning** (`main.cpp:initCamera`) — init-time SCCB writes for features not exposed in `sensor_t` API:
  - `0x5000 = 0xA7` — confirm BPC/WPC/GMA/LENC enabled
  - `0x5001 = 0xA3` — enable SDE (Special Digital Effects)
  - `0x5025 = 0x03` — auto BPC/WPC adaptation
  - `0x5580 = 0x40` — enable 2D noise reduction
  - `0x5583 = 0x10`, `0x5584 = 0x10` — Y/UV denoise thresholds
- **Motion detection 4× detail upgrade** (`motion_detection.h/cpp`):
  - `SCALE_FACTOR` changed from 8 to 4 (UXGA: 200×150 → 400×300 working resolution)
  - `MOTION_GRID_W/H` increased from 64×48 to 128×96 (max block grid)
  - PSRAM allocation increased to ~576 KB total (gray_alloc + rgb565_alloc)
  - SRAM increase: ~18 KB for larger static block_motion/block_mask arrays
  - 4× more detail for detecting smaller / more distant objects

### Added — Architecture & Operations

- **Compile-time feature toggles** (`config.h`) — 6 new `#define INCLUDE_*` flags: `INCLUDE_TELEGRAM`, `INCLUDE_MQTT`, `INCLUDE_AUDIO`, `INCLUDE_PERSON_DETECT`, `INCLUDE_SD_RECORDING`, `INCLUDE_TIMELAPSE`. Disable any feature at compile time to save flash + RAM. Stub functions provided for disabled Telegram so the rest of the code compiles unchanged.
- **Live log system** (`ws_log.h/cpp`, NEW):
  - Ring buffer of last 100 log lines (200 chars each, ~20 KB)
  - `wsLog()` thread-safe printf-style API (also writes to Serial)
  - `/log` endpoint — plain text response of full buffer
  - `/log-viewer` endpoint — HTML page with 2 s polling, color theme, download button
- **Time-lapse mode** (`timelapse.h/cpp`, NEW):
  - Periodic JPEG snapshots from ring buffer to SD card
  - Configurable interval (5-3600 s) via `timelapse_interval_sec`
  - Per-day folder organization: `/sdcard/YYYYMMDD/tl_HHMMSS.jpg`
  - Runs as low-priority FreeRTOS task on Core 0
- **Per-day SD folders** (`getPerDayDir()` in `timelapse.cpp`) — automatic `/sdcard/YYYYMMDD/` directory creation, used by both AVI recording and time-lapse
- **Web dashboard overhaul** (`camera_server.cpp` INDEX_HTML_GZ):
  - 8 info cards: IP, Uptime, RAM (color-coded), PSRAM, WiFi RSSI (color-coded), Lux, Profile, FW
  - 4 detection toggles: motion photo, person AI, recording, IR LED
  - 8 tool buttons: Snapshot, Settings, Live Log, SD files, Status JSON, Health, Motion debug, Telemetry
  - Real-time status updates every 3 s with offline detection
  - Dark theme matching `/log-viewer`
  - HTML size: 11.6 KB raw → 2.9 KB gzipped (PROGMEM)
- **`FRAME_BUFFER_SLOT_SIZE` constant** (`camera_capture.h`) — replaces 7 hardcoded `256 * 1024` literals across multiple files

### Added — A12 Python System v2 (`a12_system/`)

- **Complete rewrite** of A12_System (Python surveillance) into 15 modules, 2629 lines (from 9 modules / 2900 lines in v1)
- **Module split:**
  - `utils.py` → `config.py` + `runtime_config.py` + `logging_setup.py`
  - `notifications.py` → `notifier.py` + `mqtt_client.py`
  - `a12.py` → `__main__.py` + `pipeline.py` + `status_monitor.py`
- **paho-mqtt v2 migration** — `Client(CallbackAPIVersion.VERSION2)`, new callback signatures
- **Thread safety fixes** — motion counter protected by `threading.Lock`, `RLock` in `stats.py` (was deadlock-prone)
- **Persistent SQLite connection** (`database.py`) — replaces open/close per operation, adds 3 indexes
- **Removed YOLOv4 Darknet code** (`detection.py`) — only ONNX YOLOv5/v8/v11 supported
- **`config.env.example`** template + `test_config.py` smoke tests (6 tests passing)
- **`telegramMultipartUpload()` consolidation** — replaces 3 duplicate Telegram send functions

### Refactored — Firmware

- **Consolidated Telegram functions** (`main.cpp`) — `sendTelegramPhotoSync()` + `sendTelegramDocumentSync()` merged into one generic `telegramMultipartUpload()` helper. ~95 lines removed.
- **Refactored `startCameraServer()`** (`camera_server.cpp`) — added `registerGetEndpoint()`, `registerPostEndpoint()`, `registerCrudEndpoint()` helper functions. Endpoint registration block reduced from ~120 lines to ~40 lines.
- **Removed dead code** — commented-out RTSP task code, unused YOLOv4 Darknet path

### Documentation

- **`MIGRATION_ESP_DL.md`** (NEW) — detailed step-by-step plan for migrating from Edge Impulse FOMO to Espressif's ESP-DL YOLOv11n pedestrian detection (Tier 2 future work, ~2-3 day effort, includes rollback plan)
- **README.md complete rewrite** — production-grade documentation following the project's standard format (In 3 Points, tables over bullets, ASCII diagrams, troubleshooting collapsibles, comparison table, FAQ, real-world deployment metrics)
- **Cleaned up old documentation** — removed `AI_SESSION_LOG.md`, `CLEANUP_LOG.md`, `TASK_A_stream_refactor.md`, `TASK_B_rtsp_optimization.md`, and other stale Czech-language docs

### Stats

- **Build:** SUCCESS — RAM 32.3% (105 KB / 320 KB), Flash 25.2% (1.59 MB / 6.29 MB)
- **PSRAM runtime:** ~1.7 MB / 8 MB (ring buffer 768 KB + motion 576 KB + FOMO 384 KB)
- **Inference time:** Motion ~10 ms, FOMO ~150 ms, Tracker ~3 ms

---

## [3.10.5] - 2026-03-15

### Fixed

- **Person detection re-check photo spam** — duplicate notifications for the same person across consecutive frames (mitigated in v3.11 by ByteTrack tracker + temporal filter)

---

## [3.10.4] - 2026-02-25

### Added

- **Serial telemetry logger** (`tools/serial_telemetry.py`) — captures ESP32 serial output into a SQLite database with 37 regex parsing patterns and 9 normalized tables. Runs in background as systemd autostart.
- **Heap line includes RSSI + uptime** (`main.cpp`) — for telemetry correlation

---

## [3.10.3] - 2026-02-22

### Changed

- **LTR-308 lux reading** moved to `captureTask` (after `fb_return()`) to fix SCCB bus contention with camera driver
- **DAY profile threshold** changed to 100 lux

---

## [3.10.2] - 2026-02-20

### Fixed

- **LTR-308 lux stuck at boot value** — added `readAmbientLightSCCBSafe()` called from `captureTask` every 150 frames (was reading from separate task and getting `0xFF`)
- **Config migration** for new framesize defaults
- **Framesize auto-reboot** when changing via HTTP

---

## [3.10.1] - 2026-02-18

### Added

- **AI presence re-check + cooldown fix** — periodic FOMO inference even without motion to catch stationary people

---

## [3.10.0] - 2026-02-15

### Added

- **MQTT motion + brightness sensors** for Home Assistant
- **Config validation** with constrain() bounds
- **Motion detection tuning** (block-based at VGA, 5% threshold, 2-frame confirm)

### Changed

- **HTML dropdown framesize values fixed** — were swapped in v3.9.x (UXGA labeled as XGA, etc.)

---

## [3.9.x] - 2026-01

### Camera tuning

- DUSK profile added (3-level auto-profile: DAY/DUSK/NIGHT)
- OV3660 brightness tuned: avg 81/255 at 7.6 lux indoor (was 59 baseline)
- Zone-weighted AEC metering
- AGC gain ceiling 128× at night
- ESP-NN disabled (was causing heap corruption)

---

## [3.8.x] - 2025-12

### Added

- **AVI recording** (RIFF/AVI 1.0 with MJPEG video + PCM16 audio interleaved)
- **Granular notifications** — separate `motion_telegram_photo`, `motion_telegram_video`, `sd_auto_record` flags
- **Ring buffer copy-and-release pattern** for motion + recording tasks (prevents starvation)

---

## [3.7.0] - 2025-12

### Added

- **Edge Impulse FOMO person detection** (64×64 grayscale, int8, 140 KB tensor arena)
- **Person detection task** (Core 0, prio 3)
- **Cascade flow:** motion → AI → photo (replaces direct motion → photo)
- **ROI mask** — per-block on/off via `/roi-mask` GET/POST

---

## [3.6.0] - 2025-12

### Added

- **NVS credential storage** — WiFi, MQTT, Telegram, OTA, AP passwords moved from source code to encrypted NVS via `/credentials` endpoint
- **HTTP basic auth** on mutable endpoints (bypassed when `LAB_MODE true`)

---

## [3.5.0 — 3.0.0] - 2025-11

### Added

- **Lock-free ring buffer** with CAS protocol (3 × 256 KB PSRAM slots)
- **RTSP server** on port 554 (MJPEG 1)
- **OTA updates** with auto-rollback
- **Sabotage detection** watchdog
- **Day/Night auto-profiles** based on LTR-308 lux
- **AP mode fallback** with captive portal
- **Multi-server HTTP architecture** (port 80 API + port 81 streams + port 82 audio)
- **PDM audio streaming** via I2S MEMS mic

---

## [2.x] - 2025-10

### Foundation

- Camera driver (esp32-camera + OV3660)
- MJPEG streaming
- Basic block-based motion detection
- WiFi connection management
- HTTP server skeleton
