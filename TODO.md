# Project TODOs

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
