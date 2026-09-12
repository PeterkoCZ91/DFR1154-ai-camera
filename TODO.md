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

## Recently Completed (2026-09-12)
- [x] A12: AEC/AGC unwedge recovery for sustained low-detail episodes — a wedged OV3660 exposure loop streams decodable but featureless JPEGs, so the freeze watchdog never trips and the camera stayed blind for 12 h (2026-09-11 21:00 -> 09-12 08:57). Bounded, persisted attempt budget; a failed write refunds its attempt and alerts separately. Replaces the reboot ladder removed in `099fa12`, which the same wedge had survived five times
- [x] A12: low-detail alert no longer claims "low light is possible" — `brightness=64` uniform grey is not darkness (real darkness reads ~5) and the old wording sent the operator after a lighting problem
- [x] A12: frame-health config extracted to `DetectionPipeline.configure_frame_health_watchdog()` so the wiring is testable without building the whole pipeline; test harnesses call it instead of re-declaring the attribute list
- [x] Docs: uniform-grey-vs-darkness diagnosis, the IR `auto_mode` trap and the two-boards-on-one-LAN collision written up in `docs/DFROBOT_HARDWARE_GUIDE.md`
- [x] Runtime: production camera's night window disabled (`night_start_hour = night_end_hour = 0`) — the enclosure seals both the LTR-308 and the IR LED, so NIGHT was optimising for an illuminator that lights only the inside of the box. Measured same-day, same firmware: boxed board reads 0.2-0.8 lux in daylight, bench board 111-132
- [x] Bench unit: second DFR1154 recovered from storage, flashed to 3.12.49, renamed `ESP32-Cam-Test`; healthy (camera, LTR-308, PDM mic, motion + person detect all init OK, ~253 ms inference). No SD card fitted
- [x] Hardware survey refreshed — no compelling replacement board. ESP32-P4 got its v3.x revision and the MIPI-CSI stack matured, but PlatformIO still has no official P4 support, P4-EYE is only a 2 MPx OV2710 with no IR, and nothing on the market still has an integrated IR illuminator

## Roadmap

Measured over the 7 days to 2026-09-12, so the ordering below follows the data
rather than the order things were thought of:

| decision outcome | 7 days | per day |
|---|---|---|
| `no_person_candidate` (sensor fired, nothing found) | 3809 | 544 |
| `below_notify_confidence` | 214 | 31 |
| **`recorded_and_notified` (actual Telegram alerts)** | **112** | **16** |
| `suppressed_by_cooldown` | 107 | 15 |

433 person candidates in the week (62/day) produce 16 alerts/day — and the
cooldown throws away almost exactly as many as it sends. That is the shape of
the problem: the system is not short of detections, it is short of a reason to
care about any particular one.

### Tier 0 — loose ends from 2026-09-12 (hours)

- [x] **Production camera answers to the wrong mDNS name.** DONE 2026-09-12 —
  the bench board was unplugged, `mdns_cache.json` (which had cached an unrelated
  device on the LAN) deleted, the camera rebooted to re-claim the name, A12
  restarted. Verified `ESP32-Camera.local` resolves to the camera again and the
  stream reconnected. Previously: While the bench board
  was on the LAN under the stock `device_name`, mDNS conflict resolution renamed
  the *production* responder: `ESP32-Camera-2.local` -> the production IP, and
  `ESP32-Camera.local` resolves to nothing. A12 keeps working only because
  `mdns_resolver.resolve_host()` falls back to its stale/persistent cache. A
  reboot makes it re-claim the name (~25 s of stream downtime).
- [ ] **Drill a second opening in the enclosure** over the LTR-308, and over the
  IR LED if night vision is wanted back. Same-day measurement, same firmware:
  the boxed board reads 0.2-0.8 lux in full daylight, the bench board 111-132.
  Until this is done the lux reading is not a signal, the day/night profile is
  pinned to DUSK as a workaround, and the camera has **no night illumination at
  all** — the IR LED lights the inside of the box. No software change can
  recover that capability.

### Tier 1 — alert quality (the actual product problem)

- [ ] **2. Split "no face resolvable" from "face seen, not a resident".** Today
  both collapse into `Unknown` and alert. Measured on 300 stored person frames
  with a YuNet detector: only **22%** contain a detectable face at all and only
  **6.7%** carry one at the >=80px an embedding needs. So a face-based gate must
  answer for the other ~93%, and both answers are bad — "unknown" keeps the alert
  volume, "known" hides a stranger who never looks at the lens. Separating the
  two is one enum value and is worth more than any model change.
- [ ] **3. Gate recognition on the PIR window and the YOLO person box.** The PIR
  (`binary_sensor.venkovni_senzor`) knows when somebody is standing in the
  doorway — which is exactly where faces are large. Measured with a person
  deliberately facing the camera: median face **99px**, max 131px, 76% of
  detections >=80px, against a median of 35px in ordinary passage. Running
  recognition only inside that window, on the already-computed
  `detection.py:318-320` person box, turns an always-on cost into a few frames
  per event — which is what the original disable was about (OOM kills at a 2GB
  limit, `~/.codex/memories/a12_system_v2.md`, 2026-05-06).
- [ ] **4. Decide per episode, not per frame.** `identify_person` is called once
  (`pipeline.py:1219`) on a single full frame. The clip buffer holds dozens.
  Literature puts frame-level aggregation at 63% -> 85%.
- [ ] **5. If recognition is revived, replace dlib.** Benchmarked on this machine
  (1 core, 640x480, ~110px face): the current dlib HOG+encode path costs
  **251.9 ms**; `buffalo_s` (SCRFD-500M + ArcFace MobileFaceNet) costs **41.8 ms**
  at 207 MB RSS with a 512-d embedding. Six times cheaper than the thing that
  caused the OOM, and stronger. `buffalo_l` is the opposite trap: 618 ms, 669 MB.
  YuNet+SFace (46.7 ms) needs **no new dependency at all** — OpenCV 4.12 already
  ships both APIs. dlib publishes no wheel, so its 20-minute compile is inherent.
  Raise `mem_limit` from 1g (currently using 398 MiB) before enabling anything.
- [ ] **6. Derive the match threshold from data.** `tolerance = 0.6` is the
  library default, never fitted. Measured: at 35 degrees of head pitch dlib's
  distance for the *same person* is 0.566 against that 0.6 — the error budget is
  spent by geometry before a stranger appears.
- [ ] **7. Re-measure the notification cooldown.** It drops 15 events/day against
  16 sent. Whether that is right depends entirely on (2)-(4).

### Tier 2 — firmware (batch into one flash)

Each flash interrupts the production camera, so these ship together, not one at
a time.

- [ ] **`updateIRAutoMode()` early-returns when `auto_mode` is false**, so the
  `time_based` night window is never evaluated and the IR LED stays pinned to
  `manual_state` forever. `POST /ir-control` sets `auto_mode = false` by itself
  for `state: "on"`/`"off"`, so one Home Assistant toggle permanently kills the
  night automation. Currently harmless only because the night window is disabled.
- [ ] **Give `device_name` a unique default** (e.g. a MAC suffix) so two boards
  cannot collide on mDNS and MQTT out of the box. Careful: changing the default
  renames the production camera the next time its `/config.json` is absent,
  which breaks the companion's mDNS lookup until its config is updated.

### Tier 3 — observability, once Tier 1 is collecting

- [x] **Stream stalls: classification fixed, window reverted — 2026-09-12.** The
  socket read timeout (30 s) was longer than the freeze heuristic (20 s), so it
  could never fire first and every transport break was recorded as an image-level
  `frozen` event instead of `stream_ended`. That ordering fix stands; it now runs
  18 s / 20 s, both configurable (`STREAM_READ_TIMEOUT`, `STREAM_FREEZE_TIMEOUT`).
  **The window was also narrowed to 2 s / 3 s at 17:43 and reverted at 18:53 the
  same evening** after an hour of measurement: 49 teardowns in that hour and
  25.8 % of it blind, against ~1 break/hour and ~0.6 % before. Tearing the stream
  down is not free — the camera caps concurrent detection clients and holds the
  stale slot, so a reconnect took a median 14 s to deliver frames again. The
  detection window is therefore bounded from below by the cost of acting on it,
  not by the idle frame gap; gaps shorter than a reconnect heal on their own.
- [ ] **Make a reconnect cheap before narrowing the window again.** A median 14 s
  reconnect is what forces a 20 s window. Raising or clearing the camera's
  detection-client cap (or making it release the stale slot on FIN) would let the
  window drop to seconds without churn. Measure the gap-length distribution
  between 2 s and 20 s first — ~39 breaks/hour live in that band and currently
  heal by themselves.
- [ ] **Original observation, kept for context:** 447 `frozen` + 74 `stream_ended` in
  the same 7 days (~75/day). Each one costs a reconnect. Decide whether that is
  the expected cost of MJPEG over WiFi at this RSSI or a defect worth chasing —
  right now nobody has decided, which is the worst of both.
- [ ] **6. Inference latency into `events.db`.** Scorer p50/p95/max is per-process
  and resets on every restart, so "were the misses concentrated when inference
  was slow?" cannot be asked historically.
- [ ] **7. Drop or populate `audio_stats`.** Zero rows; the audio monitor is off.

### Not on the roadmap, deliberately

- **Replacing the camera board.** Survey refreshed 2026-09-12: ESP32-P4 got its
  v3.x revision and the MIPI-CSI stack matured, but PlatformIO still has no
  official P4 support (only the pioarduino fork, which has already broken this
  repo's platform install once — see the comment in `firmware/platformio.ini`),
  P4-EYE is a 2 MPx OV2710 with no IR, and nothing on the market has an
  integrated IR illuminator. Neither of the 2026-09-11/12 failures was a
  hardware fault. Revisit when PlatformIO supports P4 officially, or when OV3660
  resolution genuinely becomes the limit.

## Backlog
- [ ] **Security hardening:** Disable LAB_MODE, enforce HTTPS, remove hardcoded credentials
- [ ] **FTP/WebDAV upload:** Auto-offload recordings to NAS/server
- [ ] **Intercom:** Bidirectional audio (browser → ESP speaker)
- [ ] **Deep sleep + PIR wakeup:** Battery/solar deployment
- [ ] **Bluetooth Presence Locator:** BLE detection via separate ESP32
