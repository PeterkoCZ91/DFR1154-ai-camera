# Project TODOs

## Recently Completed (2026-05-23)
- [x] Firmware v3.12.32–34: Telegram reliability overhaul — boot hang fix, `telegramTask` priority 1→3, static `WiFiClientSecure` eliminates mbedTLS heap fragmentation, heap drift + `max_alloc` log, planned restart tier on slow leak
- [x] Firmware v3.12.35: `person_recheck_interval` config — decouples presence re-check timing from Telegram cooldown
- [x] Firmware v3.12.36: Three-state `PersonDecision` (`NONE`/`UNCERTAIN`/`CONFIDENT`), PERF counters in 30s log, `/status` exposes `person_last_decision`
- [x] Firmware v3.12.38: UNCERTAIN→Telegram in standalone mode (no MQTT), AI fallback timeout 12s→5s
- [x] Firmware v3.12.39 + A12: UNCERTAIN detections routed via MQTT to A12 (`force_yolo_event`), YOLO verifies before Telegram; graceful standalone fallback

## Recently Completed (2026-05-18)
- [x] Firmware v3.12.16: SD debug logger (`sd_logger.cpp/h`) — session-based, 4 KB buffer, auto-stop 4 h, cleanup 30 d/50 MB
- [x] Firmware v3.12.16: Dashboard expanded to 10 info cards, 5 toggles, 13 tools
- [x] Firmware v3.12.16: `/status` extended with `chip_temp_c`, `stream_fps`, `psram_usage_pct`, `camera_profile`
- [x] A12: Event scoring (PIR+YOLO score gates), adaptive clip, animals→Telegram, PIR recording without YOLO
- [x] A12: Pre-event buffer always active (no more empty pre-footage on first trigger)
- [x] A12: HA/MQTT control surface expanded (10 new runtime keys), daily Telegram summary at 08:00
- [x] A12: Docker resource limits (cpus=1.0, mem=1 GB), video quality local 960×720 / Telegram 640×480

## Recently Completed (2026-04-09)
- [x] A12_System_v2: Full Python modernization (15 modules, paho-mqtt v2, thread safety, persistent DB)
- [x] Firmware: Compile-time feature toggles (`INCLUDE_TELEGRAM/MQTT/AUDIO/PERSON_DETECT/SD_RECORDING/TIMELAPSE`)
- [x] Firmware: Live log system (`/log`, `/log-viewer` HTML auto-refresh)
- [x] Firmware: Time-lapse mode (periodic JPEG snapshots to SD, per-day YYYYMMDD folders)
- [x] Firmware: `FRAME_BUFFER_SLOT_SIZE` #define (replaced 7x hardcoded `256*1024`)
- [x] Firmware: Consolidated 3 Telegram functions into `telegramMultipartUpload()`
- [x] Firmware: Refactored `startCameraServer()` with `registerGetEndpoint/PostEndpoint/CrudEndpoint` helpers
- [x] Docs: Moved 6 stale .md files to `old_documentation/`

## Previously Completed (2025-11-27)
- [x] Ring Buffer (Zero-Copy) & `vTaskDelayUntil`
- [x] RTSP Server (Port 554)
- [x] `/telemetry` endpoint (JSON)
- [x] SD Card Recording (`/record`)
- [x] Motion Detection implementation
- [x] Animal/Bird filter (A12 System)
- [x] Fix ESP32 GUI (Socket Starvation)

## Pending
- [ ] **Security hardening:** Disable LAB_MODE, enforce HTTPS, remove hardcoded credentials
- [ ] **FTP/WebDAV upload:** Auto-offload recordings to NAS/server
- [ ] **Intercom:** Bidirectional audio (browser → ESP speaker)
- [ ] **Deep sleep + PIR wakeup:** Battery/solar deployment
- [ ] **Bluetooth Presence Locator:** BLE detection via separate ESP32
