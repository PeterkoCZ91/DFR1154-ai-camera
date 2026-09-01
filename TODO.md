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

## Recently Completed (2026-08-26)
- [x] A12: Shared-scorer failure classes split (transport / HTTP status / malformed) with exception-class-only telemetry, latency p50/p95/max
- [x] A12: Learning-data retention — `screenshots/person/` and `candidates/` opt out of the 2-day media sweep; a retention of `0` now means "keep forever" for files too
- [x] A12: Candidate snapshots for decisions that save no other media; recorded outcomes skipped so clips are not duplicated
- [x] A12: Miss snapshots (`screenshots/misses/`) for sensor-triggered decisions that found no candidate — previously the largest and only evidence-free population in the audit
- [x] A12: `decision_labels` ground truth + `a12 review` (interactive / `--stats` / `--list` / `--set`); `a12 calibrate` reports precision per confidence band
- [x] A12: `decision_audit.media_path` links a decision to the clip it produced
- [x] A12: Stream stalls persisted to `events.db` and counted in the daily summary
- [x] CI: `ruff check .` + `pytest -q` on every push, linter pinned
- [x] Fix: `a12` wrapper addressed the wrong compose project, so every lifecycle command ran against an empty project

## Next

The audit now records every decision together with the knobs that applied, and
keeps a frame for the decisions that produce no other evidence. That changes
what is worth doing next, and in which order — several of these are only worth
doing after the one above them.

- [ ] **1. Deploy the audit changes and let them collect.** Miss snapshots, the
  ground-truth table and the audit→media link are in the code but not on a
  camera until the image is rebuilt (`a12 build && a12 up`). Everything below
  needs the data they produce. `decision_audit` gains a column on first start;
  the migration is idempotent.
- [ ] **2. Recognise occupants instead of tuning thresholds.** In a household
  deployment nearly every person alert is an occupant, so alerting on "a person"
  spends the entire notification budget on expected events and buries the one
  that matters. The whitelist suppression path already exists
  (`face_recognition.whitelisted_names` → `skip_telegram`); what is missing is
  running it by default and treating **unrecognised** person as the alerting
  event rather than *person*. Ranks above threshold work: the confidence a
  known occupant scores is not what decides whether to alert.
- [ ] **3. Distinguish known from unknown in the labels.** `decision_labels.truth`
  is currently `person` / `not_person` / `unsure`. Splitting `person` into known
  and unknown makes the labels feed (2) directly instead of only feeding
  threshold work.
- [ ] **4. Set thresholds from labels, not from guesses.** After a fortnight of
  review, `a12 review --stats` shows precision per confidence band and the
  notify threshold follows from where it collapses. Not before: with no labels
  every threshold is still a guess.
- [ ] **5. Re-measure the notification cooldown.** It currently drops a large
  share of already-confirmed person events, which is either the right call or a
  silent loss depending on (2) — with occupant recognition in place, most of what
  it suppresses should not have been an alert at all. Measure after (2), not now.
- [ ] **6. Inference latency into `events.db`.** Scorer p50/p95/max is per-process
  and resets on every restart, so "were the misses concentrated when inference
  was slow?" cannot be asked historically.
- [ ] **7. Drop or populate `audio_stats`.** Zero rows; the audio monitor is off.

## Backlog
- [ ] **Security hardening:** Disable LAB_MODE, enforce HTTPS, remove hardcoded credentials
- [ ] **FTP/WebDAV upload:** Auto-offload recordings to NAS/server
- [ ] **Intercom:** Bidirectional audio (browser → ESP speaker)
- [ ] **Deep sleep + PIR wakeup:** Battery/solar deployment
- [ ] **Bluetooth Presence Locator:** BLE detection via separate ESP32
