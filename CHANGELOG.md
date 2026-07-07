# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] - 2026-05-29

### Fixed (A12 — night false-positive reduction)

- **PIR YOLO gate (`pir_recording.require_yolo_for_telegram`).** New boolean flag (default `false`). When `true`, a PIR sensor trigger always saves a local MP4 clip but withholds the Telegram notification until YOLO confirms a person within the existing `external_yolo_until` window. Eliminates false "Motion detected" Telegram spam caused by an external PIR-activated street light turning on and triggering a brief brightness change in the camera frame. Configurable via `PIR_RECORDING_REQUIRE_YOLO_FOR_TELEGRAM=true` env var.

- **Brightness watchdog (`brightness_watchdog_threshold`, `brightness_watchdog_strikes`).** Detects AEC freeze on OV3660 (UXGA mode) where the sensor locks near-zero exposure and produces permanently dark frames. After N consecutive heartbeat periods (default 2 × 30 s) below the brightness threshold (default 20), the watchdog resets AEC/AGC to sane starting values and sends a Telegram notification. Wired via `camera_reset_fn` callback in `Application`.

### Fixed (Firmware + A12 — detection-stream stability)

- **Detection-stream capped at ~10 FPS + `SO_SNDTIMEO` raised 500 ms → 15000 ms (rate-limited streams only).** The stream dropped ~30×/hour (`Stream error: Response ended prematurely` in A12 logs, each followed by a full reconnect + settings re-apply + audio reconnect). Server-side instrumentation (`wsLog` in the handler break paths, readable via `/log`) proved drops were socket send failures. Root cause: the handler pushed up to 30 FPS of UXGA JPEG while the A12 consumer decodes at most 10 FPS (`stream_active_decode_fps`; 2 FPS idle), chronically backing up the ESP32 TCP send buffer. The rate cap keeps the wire rate near the useful consumer rate, and the longer send timeout lets the connection ride out short A12 stalls while staying below A12's 20 s frozen-stream reconnect. The GUI stream keeps the tight 500 ms timeout so dead browser tabs are reaped quickly.

- **A12 stream reader split into drain + decode threads.** The HTTP reader now only drains MJPEG bytes into a bounded raw-JPEG queue; `cv2.imdecode` and FPS throttling run in a separate decode thread. When A12 is busy encoding clips or sending notifications, the socket is still drained and stale frames are dropped locally instead of blocking the ESP32 sender.

### Added (Firmware — restart-rate detection, v3.12.48)

- **Boot-timestamp ring buffer for true restart-rate.** The cumulative power counters could only report lifetime totals ("103 POWERON ever"), which never clears and can't distinguish a healthy device from one currently rebooting in a loop. The firmware now persists the wall-clock time + reset reason of the last 16 boots to LittleFS (`/boot_history.bin`, magic+version guarded, survives power loss unlike RTC memory). `/health` exposes `restarts_1h`, `restarts_24h`, and `restart_time_synced`. `power_health` "current" now trips primarily on a burst (≥3 restarts within an hour) — the real signature of a failing supply — and self-clears. Timestamps are recorded lazily from `loop()` once NTP has synced (no valid time exists at boot); until then it falls back to the recent-boot heuristic.

### Fixed (Firmware — health/mDNS consistency, v3.12.48)

- **mDNS no longer advertises RTSP (554) under `LITE_MODE_NO_RUNTIME_TASKS`.** The RTSP server is skipped at boot in lite mode, but the mDNS record was still published unconditionally, so clients discovered a dead service and hung on connect. The `MDNS.addService("rtsp", ...)` call is now guarded by the same `#ifndef LITE_MODE_NO_RUNTIME_TASKS` as the server start. Audio (82) advertising is unchanged — that server runs whenever `INCLUDE_AUDIO` is set.
- **`power_health` "current" now recognises POWERON, not just BROWNOUT.** On this board a degraded supply manifests as a cold-boot POWERON (VBUS lost), not a brownout — the field unit shows 103 POWERON vs 1 brownout — so the previous BROWNOUT-only check never fired for the real failure mode. The current-suspect flag now trips on a recent (<10 min uptime) POWERON *or* BROWNOUT and self-clears once the device proves stable. Lifetime `power_health_lifetime` is unchanged. (True rate-based detection still needs persisted boot timestamps — tracked as TODO.)
- **`/health` JSON document over-provisioned (1280 → 2048 bytes).** After the stream-diagnostics and power-counter fields were added, the `StaticJsonDocument` was close to capacity; ArduinoJson truncates silently on overflow, which would hand A12 invalid JSON. The handler runs on the 20 KB control-server task, so the larger stack document is safe.
- **Stale `INCLUDE_TELEGRAM` comment corrected in `config.h`.** The comment claimed Telegram was disabled in A12 mode due to heap drift, but it is enabled via the `-DINCLUDE_TELEGRAM` build flag and the heap drift was fixed in v3.12.34. The comment now documents the actual single source of truth.

### Fixed (Firmware — tamper detection)

- **`TAMPER_MIN_LUX` raised 2.0 → 8.0.** Location has 3–5 lux ambient at night. The previous threshold of 2.0 barely filtered anything, causing AEC overcompensation after a neighbouring PIR street light flash (3–5 lux) to produce false "⚠️ Tamper alert: camera_covered" alerts. At 8.0 the gate only trips when genuine ambient light is present and the lens is simultaneously dark.

---

## [3.12.44] - 2026-05-27

### Added (A12 — Groq vision face recognition + Nuki unlock)

- **Groq vision face recognition (`groq_vision.py`).** Drop-in replacement for local dlib face recognition. Uses LLaMA 4 Scout (free Groq API) to compare a live camera frame against JPEG reference photos stored in `known_faces/<name>/`. No model training required — add a person by dropping 2–3 photos in a folder.
  - `confidence ≥ 0.90` → known person: Telegram suppressed, Nuki unlock triggered automatically
  - `confidence 0.65–0.89` → uncertain: Telegram sent with name hint for manual confirmation
  - `confidence < 0.65` → unknown person: full Telegram alert
  - API calls rate-limited to 1 per 30 s to stay within Groq free tier (1000 req/min)

- **Nuki Smart Lock auto-unlock via Home Assistant.** When a known person is confirmed at high confidence, A12 calls `lock.unlock` on the configured HA entity (`NUKI_LOCK_ENTITY_ID`). Every unlock is logged locally with a photo in the event database.

- **Known-person motion suppression.** After a known person is identified, PIR-triggered motion Telegram notifications are suppressed for 60 s — no duplicate alerts when the same person triggers the PIR sensor multiple times in one pass.

### Fixed (A12 — detection stability)

- **PIR false-positive reduction.** Raised PIR-triggered YOLO thresholds: `YOLO_PIR_NOTIFY_CONFIDENCE_THRESHOLD` 0.45 → 0.60, `YOLO_PIR_PERSON_CONFIRMATIONS` 1 → 2. Lighting changes (clouds, passing cars) no longer trigger person alerts.
- **Memory spike fix.** `CLIP_POST_SECONDS` 15 → 30, `ADAPTIVE_CLIP_MAX_POST_SECONDS` 60 → 30, `AUDIO_BUFFER_SECONDS` 22 → 10. Prevents OOM on scenes with frequent PIR triggers (e.g., staircase-mounted sensors).
- **Stream read timeout** raised 15 s → 30 s to handle temporary WiFi latency without reconnect.
- **Sabotage detection timeout** raised 30 s → 60 s to reduce false tamper alerts during stream recovery.

### Fixed (Firmware)

- Removed stray `esp_task_wdt_reset()` calls from HTTP handlers and OTA handler. httpd tasks are not subscribed to the WDT; calling reset from them had no effect and cluttered the code.
- Reverted tamper detection lux-gating (v3.12.43 attempt) — caused a crash loop on first boot after OTA flash when the lux sensor read 0.0 before the first measurement cycle completed.

---

## [3.12.43] - 2026-05-23

### Fixed (Firmware — settings GUI)

- **`person_telegram_photo` toggle in web UI.** The "Person detection (AI)" settings section now shows a dedicated toggle for sending Telegram photos on AI detections (CONFIDENT/UNCERTAIN). Previously only configurable via `POST /settings` JSON API.
- **`person_recheck_interval` in web UI.** Idle scan interval (person check when no motion) now editable in settings (1–120 s, default 12 s).

---

## [3.12.42] - 2026-05-23

### Changed (Firmware — detection sensitivity, idle scan)

- **`PD_MIN_DETECTIONS` 2 → 1.** A single FOMO centroid is now sufficient for `raw_detected=true`. The temporal filter (`PD_TEMPORAL_FRAMES=2`) and ByteTrack (`TRACKER_MIN_HITS=2`) provide the false-positive rejection that the centroid count was approximating — requiring 2 centroids was silently dropping valid single-centroid person detections.
- **`IDLE_SCAN_MS` 30000 → 5000.** Person detection task now runs an idle scan every 5 s (was 30 s). Enables the temporal filter to get its second consecutive frame while the person is still in view.

### Fixed (Firmware)

- **`person_telegram_photo` independent of `motion_telegram_photo`.** Added dedicated `person_telegram_photo` bool config field (default `true`). Previously, disabling `motion_telegram_photo` (e.g. when running A12 as the notification engine) silently suppressed CONFIDENT and UNCERTAIN person detection photos as well. CONFIDENT and UNCERTAIN fallback paths now check `config.person_telegram_photo`; the AI motion fallback continues to check `config.motion_telegram_photo`. Exposed in `/status` and configurable via `POST /settings`.
- **`/status` JSON capacity** (`StaticJsonDocument` 1536 → 2048 bytes).

### Fixed (A12)

- **MQTT duplicate subscription** (`mqtt_client.py`). Client was re-subscribing to `esp32cam/.../motion` and `esp32cam/.../person_uncertain` on every MQTT reconnect. Added `clean_session=True` so the broker always starts a fresh session and subscriptions are set exactly once per connect.
- **Forced YOLO log label** (`pipeline.py`). Log now reads `shared_state["external_yolo_source"]` (`esp32_motion`, `esp32_uncertain`, or `ha_sensor`) instead of the hardcoded `"HA/Zigbee sensor"` string.

---

## [3.12.41] - 2026-05-23

### Changed (Firmware — TFLite Micro + ESP-NN, inference 3500ms → 314ms)

- **Model re-exported from Edge Impulse as TFLite Micro** (non-EON, `EI_CLASSIFIER_COMPILED=0`). Previous EON export (`_compiled.cpp`) was incompatible with ESP-NN runtime — all three EON+ESP-NN variants caused `CORRUPT HEAP` or `TG1WDT_SYS_RST`. TFLite Micro export uses the standard TFLite Micro interpreter which supports ESP-NN natively on ESP32-S3.
- **ESP-NN enabled** (`EI_CLASSIFIER_TFLITE_ENABLE_ESP_NN=1`). SIMD acceleration active.
- **Inference time: ~3500ms → ~314ms** (11× speedup). Measured on first idle scan after boot. Heap after inference: 79 KB (healthy, stable).
- **Static DRAM: 20.6%** (67 644 B) — TFLite Micro interpreter is slightly more compact than EON generated code.
- **Model version: 1.0.9** (`Person_detection_FOMO_inferencing`, deploy version 9). Library in `firmware/lib/ei-person-detection-fomo/`.
- **Version bump** — `3.12.41 (TFLite Micro + ESP-NN)`.

---

## [3.12.40] - 2026-05-23

### Changed (Firmware — DRAM optimization, ESP-NN investigation closed)

- **`ws_log` ring buffer moved from static DRAM to PSRAM.** Was `100 × 200` chars = 20 KB static DRAM. Now allocated via `heap_caps_malloc(MALLOC_CAP_SPIRAM)` at init, capacity reduced to `60 × 160`. If PSRAM unavailable, log history silently disabled (HTTP `/log` still works, just no history).
- **`sd_logger` 4 KB write buffer moved from static DRAM to PSRAM.** Was a static `char sdl_buf[4096]` in BSS. Now `heap_caps_malloc` at `sdLogInit()`. SD log sessions disabled if PSRAM alloc fails.
- **`CORE_DEBUG_LEVEL` lowered 3 → 1.** ESP-IDF debug verbosity reduced now that boot stability issues are resolved. Saves serial bandwidth and minor CPU overhead.
- **Static DRAM reduced: 29.0% → 21.6% (95 028 → 70 940 bytes).** Net saving ~24 KB, giving more headroom for runtime heap.
- **ESP-NN investigation closed.** All three approaches tested and failed on DFR1154 with the current Edge Impulse EON export:
  - `EI_CLASSIFIER_ALLOCATION_STATIC + ESP_NN=1`: DRAM 56.8%, boots, but first PD trigger causes `CORRUPT HEAP` in `tflite_learn_*_reset()` at `ei_free()`.
  - `ESP_NN=1` without static arena (PSRAM arena): DRAM 21.7%, same `CORRUPT HEAP` on first PD trigger.
  - `ESP_NN=1` + enlarged arena (140 KB): heap corruption gone, but `TG1WDT_SYS_RST` on first idle scan — ESP-NN + PSRAM dynamic arena interaction causes WDT.
  - **Root cause:** EI EON compiler generates code incompatible with ESP-NN runtime on this hardware. Not a DRAM budget problem — a model export problem.
  - **Path forward:** re-export the model from Edge Impulse as plain **TFLite Micro** (non-EON) format, which uses the standard TFLite Micro interpreter known to work with ESP-NN on ESP32-S3.
- **Version bump** — `3.12.40 (DRAM opt, ESP-NN investigated)`.

---

## [3.12.39] - 2026-05-23

### Added (Firmware + A12 — UNCERTAIN→MQTT routing)

- **UNCERTAIN detections now routed via MQTT to A12 for YOLO verification.** When `PersonDecision::UNCERTAIN` fires and MQTT broker is reachable, ESP32 publishes `esp32cam/{device}/person_uncertain` (`{"detected":true,"confidence":0.XX}`). A12 receives it, opens a YOLO window (`external_yolo_source = "esp32_uncertain"`), and runs server-side inference — Telegram alert sent only if YOLO confirms. No Telegram spam from borderline ESP32 detections.
- **Graceful degradation (standalone mode).** If MQTT not connected, UNCERTAIN falls back to direct Telegram (v3.12.38 behavior). No silent drops.
- **`mqttPersonUncertainConnected()` + `mqttPublishPersonUncertain(float)`** added to `mqtt_handler.cpp/h`.
- **A12 `mqtt_client.py`:** subscribes to `person_uncertain` topic, parses JSON payload, fires `esp32_person_uncertain` callback.
- **A12 `__main__.py`:** `esp32_person_uncertain` handler opens YOLO window, sets `external_yolo_source = "esp32_uncertain"`, calls `force_yolo_event.set()`.
- **Version bump** — `3.12.39 (UNCERTAIN→MQTT+A12)`.

---

## [3.12.38] - 2026-05-23

### Added (Firmware — UNCERTAIN→Telegram fallback, AI fallback 5s)

- **`UNCERTAIN` detections send Telegram directly** when MQTT/A12 not available (standalone mode). Caption includes confidence % and Subject ID. Uses same track dedup (`objectTracker.getNextUnnotified()`) and `person_detection_cooldown` as `CONFIDENT` to prevent alert flooding.
- **`MOTION_AI_FALLBACK_MS` reduced 12 000 → 5 000 ms.** One FOMO inference cycle takes ~2.2 s; 5 s gives one full cycle + overhead before the fallback motion photo fires. Reduces latency for scenes where AI finds no person.
- **`g_lastPersonPhotoTime` updated on UNCERTAIN Telegram send** — suppresses redundant AI fallback photo for the same event.
- **Version bump** — `3.12.38 (UNCERTAIN→Telegram)`.

---

## [3.12.36] - 2026-05-23

### Changed / Added (Firmware — three-state PD, PERF log, split cooldown)

- **Three-state `PersonDecision` enum** (`NONE` / `UNCERTAIN` / `CONFIDENT`). Confidence ≥ 75% → `CONFIDENT` → Telegram (unchanged). Confidence ≥ runtime threshold (default 60%) but < 75% → `UNCERTAIN` → serial log only, A12 routing pending (task #5). Below threshold → `NONE`. Current decision visible via `/status` as `person_last_decision`.
- **PERF counters added to 30s log.** Every 30s serial log now includes: `PERF: infer_ms=%u motion=%u pd_pos=%u pd_neg=%u tg_sent=%u fallback=%u`. Counters: `motion_trigger_count` (motions that woke PD task), `ai_fallback_count` (fallback photos sent without AI confirm), `pd_positive_count` / `pd_negative_count` (inference outcomes), `last_inference_ms`.
- **ESP-NN investigation (attempted, reverted):** `EI_CLASSIFIER_ALLOCATION_STATIC` + ESP-NN=1 tested — causes crash on boot (DRAM budget: 210/327 KB = 64%, leaves only ~118 KB heap, insufficient for WiFi + FreeRTOS + TLS simultaneously). Tensor arena (117 KB) remains in PSRAM. ESP-NN stays disabled until DRAM budget is reduced or a PSRAM+ESP-NN path is found.
- **Version bump** — `3.12.36 (three-state PD, PERF log, split cooldown)`.

---

## [3.12.35] - 2026-05-23

### Changed (Firmware — split person cooldown + recheck)

- **`person_recheck_interval` config field added** (default 12 s, range 5–120 s). Previously `personDetectionTask` used `person_detection_cooldown` (60 s) for both inter-photo cooldown and presence re-check wait. Now they are independent: `person_detection_cooldown` controls Telegram rate, `person_recheck_interval` controls how quickly a stationary person's presence state is re-evaluated and cleared after they leave.
- **Version bump** — `3.12.35 (split person cooldown + recheck)`.

---

## [3.12.34] - 2026-05-23

### Fixed (Firmware — heap TLS reuse + thresholds)

- **Shared `s_telegram_client` (static WiFiClientSecure) for all Telegram sends.** `sendTelegramNotificationSync` previously used `HTTPClient::begin(url)` which internally creates a new `WiFiClientSecure` per call, causing the same mbedTLS context alloc/free fragmentation that was fixed in `telegramMultipartUpload` in v3.12.33. Both paths now share one static instance (`s_telegram_client`) — allocated once, never freed. All sends are serialized through `telegramTask` so there is no concurrent access risk.
- **`max_alloc` guard in `sendTelegramPhotoSync` raised from 18 KB → 32 KB.** mbedTLS needs a ~32 KB contiguous heap block for SSL input/output record buffers. 18 KB was too low to catch fragmentation before TLS fails mid-handshake.
- **Low-heap warning threshold in `checkMemoryHealth` lowered from 65 KB → 55 KB.** Operating baseline after static TLS client is ~64 KB; 65 KB was triggering false warnings on every 30s check.
- **WiFi reconnect heap guard lowered from 70 KB → 55 KB** (line in `loop()`). At 70 KB the guard always deferred reconnect attempts since baseline heap is ~64 KB — WiFi could never recover from a dropout.
- **Heap log enriched:** 30s periodic log now includes `max_alloc`, `frag%` (max_alloc/free_heap), and `drift` (bytes lost since boot baseline). Makes trend analysis possible from serial log alone.
- **Task stack high-water marks** logged hourly alongside WiFi signal check.
- **Planned restart tier added to `checkMemoryHealth`:** if heap stays below 50 KB for >60s (but above the 30 KB emergency floor), performs a clean `esp_restart()`. Catches slow leaks well before the hard crash threshold.
- **Version bump** — `3.12.34 (heap TLS reuse + thresholds)`.

---

## [3.12.33] - 2026-05-23

### Fixed (Firmware — TLS heap fragmentation)

- **`WiFiClientSecure` instance in `telegramMultipartUpload` changed to `static`.** Each TLS connection previously allocated ~35 KB of SSL context and record buffers, then freed them, leaving the heap fragmented. After 12 sends `max_alloc` dropped from 36 KB → 20 KB. The static instance is created once; `client.stop()` before each use resets session state without re-allocating the underlying TLS context.
- **Heap guard in `sendTelegramPhotoSync` changed to dual check:** `free_heap < 55 000 || max_alloc < 18 000`. The old `free_heap < 65 000` fired even when TLS could still connect (observed: TLS succeeded with 65 KB free), and also failed to detect a fragmented heap with plenty of total free bytes but no contiguous block for mbedTLS.
- **Version bump** — `3.12.33 (TLS heap defrag)`.

---

## [3.12.32] - 2026-05-23

### Fixed (Firmware — Telegram boot hang + TLS CPU starvation)

- **`telegramQueue` and `telegramTask` are now created before `MDNS.begin()` and `ArduinoOTA.begin()`.** On ESP32-S3 + Arduino 3.0.0 both calls can hang indefinitely on some boot cycles; the queue was silently never created and all photos fell through to the sync fallback path forever.
- **`telegramTask` priority raised from 1 → 3** (equal to `personDetectionTask`). At priority 1 the TLS handshake (RSA/ECC operations) was starved of Core 0 time by higher-priority tasks, causing `client.connect()` to hang without a timeout and holding ~32 KB of TLS buffers until the heap crashed.
- **Heap guard in `sendTelegramPhotoSync` raised from 50 KB → 65 KB.** mbedTLS allocates ~32 KB for input/output SSL record buffers; the old 50 KB threshold left too little margin, causing TLS to fail mid-handshake and corrupt heap state.
- **`telegramTask` heartbeat log** (every 10 s): `📱 telegramTask alive: uptime= queue= backoff_rem= heap=` — confirms the task is alive and shows queue depth / backoff state without needing USB serial re-attach.
- **`telegramTask` stack increased 24 576 → 32 768 bytes** to give TLS + JSON operations safe headroom.
- **Version bump** — `3.12.32 (telegram boot hang + TLS fix)`.

---

## [3.12.31] - 2026-05-22

### Fixed (Firmware - Telegram queue fallback)

- **Added a synchronous fallback when the Telegram async queue is unavailable.** A confirmed person alert now attempts direct photo upload instead of being dropped before enqueue.
- **Added explicit enqueue failure reasons.** `sendTelegramPhoto()` now logs `photo_no_credentials`, `photo_invalid_jpeg`, `photo_psram_alloc`, `photo_queue_full`, or `photo_sync` instead of only returning false.
- **Added queue/task readiness to `/status`.** `telegram_queue_ready` and `telegram_task_ready` show whether the async Telegram path exists after boot.
- **Version bump** - `3.12.31 (telegram queue fallback)`.

---

## [3.12.30] - 2026-05-22

### Fixed (Firmware - person photo enqueue)

- **Person alerts now queue the same JPEG frame that AI just confirmed.** This removes the race where a `person` event could be logged, but the later attempt to acquire a fresh ring-buffer frame failed and no Telegram photo was queued.
- **Added last person inference telemetry to `/status`.** `person_last_detected`, `person_last_confidence`, and `person_last_detections` show whether the latest AI scan actually passed the detector and with what score.
- **Photo enqueue failures are now visible.** Failed person-photo queue attempts log `telegram_failed: photo_enqueue`, and `sendTelegramPhoto()` increments Telegram failure telemetry for missing credentials, invalid JPEG, missing queue, or PSRAM allocation failure.
- **Version bump** - `3.12.30 (person photo enqueue fix)`.

---

## [3.12.29] - 2026-05-22

### Fixed (Firmware — Telegram delivery confirmation)

- **Person tracks are now marked notified only after Telegram confirms the photo upload with HTTP 200.** If a photo is queued but the async upload later fails, the same track remains unnotified and can retry after the person cooldown instead of being silently suppressed.
- **Added `telegram_sent` events.** Successful photo/text/document sends now appear in `/events`, including photo size and person track id when applicable.
- **Added Telegram queue telemetry to `/status`.** `telegram_queue_depth`, `telegram_uploading`, `telegram_sent`, `telegram_fail`, and `telegram_drops` expose whether photos are stuck in queue, currently uploading, confirmed sent, failed, or dropped.
- **Version bump** — `3.12.29 (telegram delivery telemetry)`.

---

## [3.12.28] - 2026-05-22

### Fixed (Firmware — person detection trigger)

- **Person detection no longer depends only on motion.** `personDetectionTask` now performs an idle AI scan every 30 seconds when no presence is active, so slow or stationary arrivals can still be detected even when the motion grid stays below threshold.
- **Keeps existing motion-triggered fast path.** Motion still wakes the AI task immediately; the idle scan is only a fallback for missed motion triggers.
- **Version bump** — `3.12.28 (idle person scan)`.

---

## [3.12.27] - 2026-05-22

### Fixed (Firmware — Telegram photo delivery)

- **Person detection was firing, but photos could still disappear after one TLS write failure.** `telegramTask` now retries a failed photo upload once before freeing the PSRAM JPEG copy. This keeps the retry bounded, avoiding the old unbounded retry/leak pattern.
- **Photo queue handoff now returns success/failure.** Person tracks are marked notified only after the photo is accepted into the Telegram queue; if queueing fails, the next presence re-check can try again.
- **JPEG quality default/migration changed to 12 for standalone Telegram.** UXGA frames at quality 8 were often ~90-110 KB and hit `write_failed` on the weak HTTPS path; quality 12 keeps the photo smaller while preserving enough detail for detection review.
- **Version bump** — `3.12.27 (telegram photo retry)`.

---

## [3.12.26] - 2026-05-22

### Fixed (Firmware — standalone Telegram)

- **DNS override po WiFi připojení** — po `WiFi.begin()` se nyní nastaví primární DNS na `8.8.8.8` a sekundární na `8.8.4.4`. Router DNS intermittentně selhal při překladu `api.telegram.org` těsně po WiFi připojení (error -54, DNS timeout), což způsobovalo `telegram_failed: connection refused` na každý pokus o odeslání fotky. `HTTPClient` (textové notifikace) byl vůči tomuto méně citlivý díky odlišnému timingu při bootu.
  ```cpp
  // connectWiFi() — po WL_CONNECTED
  WiFi.config(WiFi.localIP(), WiFi.gatewayIP(), WiFi.subnetMask(),
              IPAddress(8, 8, 8, 8), IPAddress(8, 8, 4, 4));
  ```
- **Odstraněna retry smyčka v `sendTelegramPhotoSync`** — předchozí implementace s 3 pokusy (každý volal celý `telegramMultipartUpload` včetně TLS handshake) způsobovala pomalý heap leak (~470 B/session z mbedTLS entropy/RNG kontextu). Za ~3 h provozu s četnými detekcemi klesl heap z 110 KB na 26 KB → `heap < 50000` check skipoval všechny fotky. Zpět na single attempt; DNS fix zajišťuje spolehlivost prvního pokusu.
- **Chunk size upload snížen 2048 → 512 B** — menší TLS záznamy jsou šetrnější k heap fragmentaci při souběžném běhu person-detection inference (která alokuje dočasné buffery na heap).
- **Event log rozšířen o silent failure paths** v `telegramMultipartUpload`:
  - `write_failed` — `client.write()` vrátil 0 (TCP drop uprostřed uploadu)
  - `response_timeout` — server neodpověděl do timeoutMs
  - `api_XXX` — Telegram API vrátil non-200 kód
  - `429_backoff:Xs` — rate limiting (dříve logováno pouze na Serial)

### Build profile (standalone, INCLUDE_TELEGRAM)

- RAM: ~29 % (94 884 B / 327 KB), Flash: ~25 % (1 599 605 B / 6.29 MB)
- `free_heap` při bootu: ~105 KB, stabilní (bez driftu po 30+ min)

---

## [3.12.18] - 2026-05-22

### Changed (Firmware)

- **Motion detection thresholds tightened** — `MOTION_PIXEL_THRESHOLD` raised 20 → 28, `MOTION_UPPER_PCT` lowered 70 → 50 %, `MOTION_CONFIRM_FRAMES` raised 2 → 3, `MOTION_EMA_ALPHA_DAY` raised 0.92 → 0.95, training period 15 → 20 frames. Eliminates false triggers caused by gradual lighting shifts (sunrise/sunset, indoor lights switching).
- **Brightness-jump reset threshold** lowered 80 → 40. Faster background reset on abrupt light changes (e.g. switching a room light on/off).
- **HTTP OTA** (`/update`) — POST endpoint accepts raw firmware binary; GET serves an upload form. `[env:ota]` in `platformio.ini` uses `curl --data-binary` for `pio run -e ota -t upload`. Replaces broken ArduinoOTA (UDP/3232).
- **MQTT reconnect** — `mqttReconnect()` re-calls `setServer()` on every attempt so credentials configured after boot (via `/credentials`) take effect without a reboot.

### Changed (A12)

- **ESP32 MQTT motion as PIR trigger** — `esp32cam/<device>/motion ON` now opens a 15 s YOLO window (`external_yolo_until`), fires `force_yolo_event`, and sets `sensor_confirmed = True` in person-event scoring. Result: YOLO score jumps from 55 (camera-only) to 115+ (sensor + PIR bonus), crossing the 70-point notify threshold. Previously the camera's own motion signal was not counted as sensor confirmation and no Telegram was sent.
- **Motion MQTT log level** — `motion: OFF` messages downgraded INFO → DEBUG. Eliminates 2 log lines/second spam in production.

---

## [A12 v2 — 2026-05-20] - 2026-05-20

Changes to the A12 companion system (`a12_system/`).

### Added

- **Reconnect exponential backoff** — stream reconnect delay now scales from 5 s up to 60 s max (5 → 10 → 20 → 40 → 60 s) instead of a fixed 5 s interval. Eliminates hundreds of identical log lines during long camera outages.
- **Recovery snapshot** — when the ESP32 stream comes back after a `STUCK` state, A12 captures the first frame and sends it to Telegram alongside the "back online" message. Lets you visually confirm the camera view without opening logs.
- **Container resource monitor** — `StatusMonitor` checks cgroup v2 memory every 60 s. Sends a Telegram alert when working-set RAM exceeds 85 % of the container limit; clears below 70 %. Also publishes `camera/status/mem_mb` and `camera/status/mem_pct` to MQTT.
- **Face recognition gate in daily summary** — the "Obličeje: X known / Y unknown" line in the daily Telegram digest is now suppressed when `face_recognition.enabled=false`. Previously it showed stale cumulative counts from earlier sessions even after face recognition was disabled.

### Changed

- **Docker `mem_limit`** — reduced from `1500m` to `1g`. Idle consumption ~170 MB; measured peak (full clip buffer + YOLO + ffmpeg) ~270 MB; 1 GB gives comfortable headroom.
- **DOCKER.md** — replaced hardcoded local paths with generic `/opt/a12-data` example and added data-directory setup steps.

---

## [3.12.16] - 2026-05-18

### Added

- **SD debug logger** (`sd_logger.cpp`, `sd_logger.h`) — session-based logging to microSD. 4 KB in-RAM buffer, flush every 60 s, auto-stop after 4 h, automatic cleanup (files older than 30 days / total size > 50 MB). Zero overhead when disabled. API: `POST /sd-log {"action":"start|stop|flush","timeout_h":N}`, `GET /sd-log`. Dashboard **SD Log** toggle wires directly to this API.
- **Dashboard SD Log toggle** — fifth firmware toggle on the dashboard; mirrors the sd-log start/stop API. No page reload required.

### Changed

- **Dashboard info cards expanded to 10** — IP, Uptime, RAM (color-coded), PSRAM (+ usage % sublabel), WiFi RSSI (color-coded), FPS, Chip temperature (°C), Lux, Camera profile, Firmware version.
- **`/status` response extended** — now returns `is_recording`, `camera_profile`, `chip_temp_c`, `wifi_channel`, `stream_fps`, `psram_usage_pct`.
- **Dashboard tool count: 13** — added PSRAM Stats, Stream Stats, Audio Status, Audio Stream entries to the tools panel.
- **Version bump** — `3.12.16 (SD logger, dashboard+status fixes)`.

### Build profile (A12 mode)

- RAM: 35.3 % (115 756 B / 327 KB), Flash: 22.5 % (1 418 041 B / 6.29 MB)
- `free_heap` at boot: **82–87 KB**, `max_alloc_heap`: ~74 KB
- Config: `INCLUDE_TELEGRAM=off`, `INCLUDE_AUDIO=on`, `LITE_MODE=on`

---

## [A12 v2 — 2026-05] - 2026-05-18

Changes to the A12 companion system (`a12_system/`) accumulated since initial Docker release.

### Added

- **Event scoring** — every detection event gets a numeric score (PIR contribution + YOLO confidence × confirmations). Two configurable gates: `notify_threshold` (default 70) triggers Telegram, `local_record_threshold` (default 45) saves a local clip. Events below both thresholds are silently discarded. Eliminates most false-positive notifications without increasing confirmation counts.
- **PIR recording without YOLO** — `_handle_pir_recording` saves a clip + snapshot on every new PIR trigger even when YOLO finds nothing (useful for fast-moving subjects that exit frame before the 2-frame confirmation window).
- **Adaptive clip length** — post-event window extends while PIR or YOLO remains active; hard cap at `max_post_seconds` (60 s). No manual tuning needed for long walk-through events.
- **Pre-event buffer always active** — frame buffer fills continuously at idle FPS so a full 5 s of pre-frames is available on the first PIR trigger. Previously the buffer was empty on the first event of the session and produced clips with no pre-footage.
- **Animals → Telegram** — bird / cat / dog detections now follow the same notification pipeline as persons: MP4/GIF/JPEG clip + Telegram message with confidence and timestamp. Previously animal detections were logged only.
- **HA/MQTT expanded control surface** — `runtime_config.update_from_mqtt` now accepts 10 additional keys over MQTT: `periodic_yolo_interval`, `notify_threshold`, `local_record_threshold`, `telegram_media_mode`, `telegram_enabled`, `face_recognition`, `require_sensor`, `pir_cooldown`, `telegram_cooldown`, `detection_cooldown`. Home Assistant discovery exposes pipeline state, event score, stream FPS, and an animal binary sensor.
- **Daily Telegram summary** — sent every day at 08:00: 24 h detection delta, face recognition stats (if enabled), system uptime.
- **Face recognition best-match** — `face_distance()` compares against all known encodings and returns the closest match. Previously the first encoding that passed the threshold was accepted, producing wrong IDs when multiple people share similar encodings.
- **`requirements-face.txt`** — separate pip requirements file for the optional face recognition stack (`dlib`, `face_recognition`). Main `requirements.txt` stays lean; face stack is installed only when `FACE_RECOGNITION_ENABLED=true`.

### Changed

- **Telegram media mode dispatch** — configurable per-event: `mp4` / `preview_mp4` (short re-encoded preview) / `snapshot` / `text` / `none`. `preview_mp4` re-encodes a short sampled clip before sending to stay within Telegram file size limits.
- **Pre+post frame merge** — `frame_buffer` stores `(timestamp, frame)` tuples; notification worker splices the correct pre+post window at send time instead of snapshotting the deque at queue time. Eliminates off-by-one timing between recorded and sent footage.
- **MP4 → GIF → JPEG fallback chain** — notification worker tries MP4 first, GIF on encode failure, JPEG snapshot as last resort. Prevents silent send failures for low-resource hosts.
- **Profile hysteresis** — camera profile (DAY / DUSK / NIGHT) switches only after 90 s of stable lux. Eliminates NIGHT ↔ DUSK oscillation around the threshold in mixed lighting.
- **Media retention** — clips older than `MEDIA_RETENTION_DAYS` (default 2) are deleted daily by the notification worker. Previously no automatic cleanup was performed.
- **`PERIODIC_YOLO_INTERVAL` default 10 s → 300 s** (5 min) — idle YOLO checks in camera-only mode reduced to lower CPU usage on PC; configurable live via MQTT.
- **Docker resource limits** — `cpus: 1.0`, `mem_limit: 1g`, `memswap: 1.5g`, `pids: 64`. Idle consumption ~200 MiB; peak (YOLO + ffmpeg + full clip buffer) ~635 MB.
- **Video quality** — local MP4 at 960 × 720 (CRF 28), Telegram preview at 640 × 480. Balances storage and bandwidth.

### Notes

- Face recognition is **disabled by default** (`FACE_RECOGNITION_ENABLED=false`). 81 face encodings for one user exist in `known_faces.pkl` (not committed). Re-enable: set `FACE_RECOGNITION_ENABLED=true` in `config.env` and rebuild the Docker image.
- After every code change: `docker compose build && docker compose up -d` — source is baked into the image, not bind-mounted.

---

## [3.12.15] - 2026-05-05

### Changed

- **`INCLUDE_TELEGRAM` disabled in A12 mode** (`config.h`) — when running with the A12 companion, Telegram is handled entirely by A12 (which adds face recognition context and AV clips). Disabling it on the firmware side removes `WiFiClientSecure` / SSL overhead, freeing **~45 KB max_alloc_heap** (47 KB → 90 KB). Standalone deployment re-enables it by commenting back `INCLUDE_TELEGRAM`.
- **Audio decoupled from `LITE_MODE_NO_RUNTIME_TASKS`** (`main.cpp`, `camera_server.cpp`) — audio I2S init and `audio_httpd:82` now follow `#ifdef INCLUDE_AUDIO` independently. Previously audio was bundled with RTSP under the LITE_MODE guard; A12 requires audio for AV clip recording but not RTSP.
- **`LITE_MODE_NO_RUNTIME_TASKS` retained** — RTSP server (~16 KB), recording task (~6 KB), and timelapse task (~4 KB) remain disabled. RTSP is not used by the A12 stream pipeline (`/frame` + `/detection-stream` via HTTP). LITE_MODE off was tested overnight and failed: `min_free_heap` dropped to 13 508 bytes, triggering a crash loop.
- **Telegram stub added to `camera_capture.cpp`** — `#ifndef INCLUDE_TELEGRAM` inline stubs for `sendTelegramPhoto`, `sendTelegramNotification`, `sendTelegramDocument` fix linker errors when `INCLUDE_TELEGRAM` is undefined.
- **Version bump** — `3.12.15 (audio decoupled from LITE_MODE)`.

### Build profile

- RAM: 34.0 % (111 KB / 327 KB), Flash: **22.4 %** (1.41 MB / 6.29 MB, −225 KB from removing SSL)
- max_alloc_heap at boot: **90 KB** (no Telegram) vs 47 KB (with Telegram)

---

## [3.12.14] - 2026-05-04

### Changed

- **WDT 180 s → 60 s + `trigger_panic = true`** (`main.cpp`) — main loop resets WDT every 30 s, giving a 30 s safety margin. Previously 180 s / `trigger_panic = false` was a warning-only watchdog that never actually recovered a hang; now it forces a clean restart.
- **SD consecutive-failure bailout** (`camera_server.cpp`) — after 3 consecutive `SD.open()` failures `sd_disabled_this_session = true` skips all further recording attempts until reboot, preventing infinite-retry log spam. Sends Telegram notification on disable. Write failures mid-recording also notify.
- **HTTP server recv/send timeout 5 s → 2 s** (`camera_server.cpp`) — `http_config.recv_wait_timeout` and `send_wait_timeout` reduced for the port-80 API server. Stalled connections are released 2.5× faster; stream servers (81, 82) retain default timeouts.

### Build profile

- RAM: 34.3 % (112 KB / 327 KB), Flash: 25.3 % (1.59 MB / 6.29 MB)

---

## [3.12.8] - 2026-05-04

### Fixed

- **Telegram alert spam on restart cycles** (`main.cpp`) — `static uint32_t lastLowMemAlert` resets to 0 on every boot, so a device with recurring restarts sent one low-memory alert per boot every hour. Alert is now skipped for the first 5 minutes after boot (`pastGrace` guard) and only fires if free heap has actually recovered since the last alert.

---

## [3.12.7] - 2026-05-03

### Fixed

- **SCCB I2C bus conflict** (`main.cpp`) — `Wire.begin(SDA, SCL)` was called in `initIRControl()` *after* `esp_camera_init()` had already installed its own SCCB driver on ESP-IDF I2C port 0. Re-initialising Wire reinstalled the driver, leaving the bus in Wire's state. All subsequent `set_vflip / set_hmirror / set_brightness / set_contrast` SCCB writes failed silently while `s->status.*` was already updated — so `/status` reported the new value even though the hardware ignored it. Fix: `Wire.begin()` is now called in `setup()` **before** the `initCamera()` retry loop; the camera config uses `pin_sccb_sda = -1` and `sccb_i2c_port = 0` so `esp_camera_init()` calls `SCCB_Use_Port(0)` instead of `SCCB_Init()`, sharing Wire's already-installed driver. Runtime sensor changes now apply correctly.

### Added

- **Telegram credentials via web UI** (`camera_server.cpp`, `main.cpp`) — bot token and chat ID can be saved from the Settings → Telegram notifications section without recompiling. Values are stored in NVS and survive OTA updates.
- **CZ/EN language toggle** (`camera_server.cpp`) — dashboard (`/`) and settings (`/settings-page`) now include a language toggle button (top-right corner). Preference is stored in browser `localStorage`; default is Czech. All UI strings are fully translated in both languages.

### Build profile (unchanged)

- RAM 32.3 % (105 KB / 320 KB), Flash 25.0 % (1.57 MB / 6.29 MB)
- Free heap at boot ~99 KB, PSRAM ~4 MB free
- arduino-esp32 **3.0.0 still pinned**

---

## [3.12.6] - 2026-04-26

### Fixed — Concurrency audit

- **MQTT mutex** (`mqtt_handler.cpp`) — `PubSubClient` is not thread-safe; previously `mqttPublishMotion/Person/Status/Audio/StateOnline` could be called from `motionTask` (Core 0) and `loop()` (Core 1) concurrently, racing on the underlying `WiFiClient`. Added `mqtt_mutex` with 200 ms timeout and skip-on-contention so callers never block the camera path.
- **Recording mutex** (`camera_server.cpp`) — `recordingTask` and HTTP `/record-stop` both touched `aviWriter`, `recordFile`, and the audio I2S handle without coordination. All AVI writes, the auto-stop branch (45 MB / 120 s), and the public `stopSDRecording()` now run inside `recordingMutex`. Stop path simplified to a single 2 s critical section.
- **Audio I2S serialization** (`audio_handler.cpp/.h`) — three readers (HTTP `/stream.wav`, `recordingTask`, `getAudioLevel()`) hit the same `i2s_chan_handle_t` with no lock. Added `i2s_rx_mutex` and `audioReadLocked()` wrapper; `recordingTask` switched from raw `i2s_channel_read` to the wrapper.
- **Audio stream buffer leak** (`audio_handler.cpp`) — `audioStreamHandler` returned without `free()` on WAV-header send failure. Added matching `free(local_stream_buf)`.
- **`getAudioLevel()` race** (`audio_handler.cpp`) — global `audio_buffer[4096]` shared between concurrent `/audio-status` callers replaced with stack-local `int16_t buf[1024]`; ~2 KB stack, sufficient for RMS.
- **Person detection ring-buffer hold** (`camera_capture.cpp`) — slow FOMO inference (~3.5 s) used to keep the source slot's `ref_count > 0` for the entire window, starving `captureTask` and stalling motion. Frame is now copied into a dedicated PSRAM `pdFrameCopy` buffer and the slot released *before* inference runs.
- **Atomic pending sensor action** (`camera_capture.cpp`) — `applyPendingSensorSettings()` swapped the read+clear sequence for `__atomic_exchange_n` so concurrent settings POSTs cannot lose an action between the load and the store.
- **Per-track Telegram dedup** (`camera_capture.cpp`) — replaced the time-only person cooldown with `objectTracker.getNextUnnotified()` + `markNotified()`. The cooldown remains as a flood guard, but the same person no longer re-alerts every minute while in view.
- **Time-lapse SD path** (`timelapse.cpp/.h`) — `/sdcard/YYYYMMDD/` prefix replaced with `/YYYYMMDD/` (Arduino `SD.h` mounts at root). Removed redundant `SD.begin()` inside the loop; cheap `SD.cardSize() == 0` health check used instead.

### Changed

- **Firmware version single source of truth** (`platformio.ini`, `main.cpp`) — `FIRMWARE_VERSION` now defined as a build flag in `platformio.ini`. `main.cpp` falls back to `"unknown"` if the flag is missing. Stops the version string from drifting between source and tag.
- **Person detection caption** (`camera_capture.cpp`) — Telegram caption rewritten to spell out the ByteTrack subject ID:

  ```
  Person detected
  Confidence: 92%
  Subject ID: #12 (new tracked person — not re-alerted while in view)
  ```

  Previously read `Person detected! (track #12, confidence: 92%)` which was easy to misread as an alert count.

### Cleanup

- **Stale duplicate headers removed** — root-level `include/config.h`, `include/board_config.h`, `include/audio_handler.h` deleted; the firmware tree's own copies under `firmware/` are the only definitions now. Prevents schema drift between the two locations.

### Build profile (unchanged)

- RAM 32.3 % (105 KB / 320 KB), Flash 25.0 % (1.57 MB / 6.29 MB)
- Free heap at boot ~99 KB, PSRAM ~5 MB free
- arduino-esp32 **3.0.0 still pinned**

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
