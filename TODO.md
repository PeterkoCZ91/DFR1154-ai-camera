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
- [x] ~~Runtime: production camera's night window disabled (`night_start_hour = night_end_hour = 0`)~~ — **this could never have taken effect, corrected 2026-09-15.** Those hours are only read when `time_based` is true, and it was false, so `updateCameraProfile()` went on choosing from the sealed LTR-308 and pinning `PROFILE_NIGHT` (AGC off) around the clock. Found on the live camera at 16:00 in daylight: `time_based=false`, hours back at 20/7, `profile=NIGHT`, image `mean=40.00 std=0.00`. Fixed by actually switching the source: `POST /ir-control {"time_based":true,"night_start_hour":0,"night_end_hour":0}` → `profile=DUSK`, `agc=1`, `mean=163 std=28.8`, and it survives a reboot (`/ir_config.json` on LittleFS). The enclosure measurement stands: boxed board 0.2-0.8 lux in daylight, bench board 111-132
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
  IR LED if night vision is wanted back. **Promoted 2026-09-15: this is not
  cosmetic, the sealed sensor was actively blinding the camera.** A lux reading
  of 0.3 in daylight pins the firmware to its NIGHT profile, AGC off, which is
  what produced every "flat grey image" episode back to 2026-09-11. Pinning
  `time_based` to DUSK is a workaround that gives up any real day/night
  adaptation; only the hole restores the signal. Same-day measurement, same firmware:
  the boxed board reads 0.2-0.8 lux in full daylight, the bench board 111-132.
  Until this is done the lux reading is not a signal, the day/night profile is
  pinned to DUSK as a workaround, and the camera has **no night illumination at
  all** — the IR LED lights the inside of the box. No software change can
  recover that capability.

### Tier 1 — alert quality (the actual product problem)

- [x] **2. DONE 2026-09-12. Split "no face resolvable" from "face seen, not a resident".** Today
  both collapse into `Unknown` and alert. Measured on 300 stored person frames
  with a YuNet detector: only **22%** contain a detectable face at all and only
  **6.7%** carry one at the >=80px an embedding needs. So a face-based gate must
  answer for the other ~93%, and both answers are bad — "unknown" keeps the alert
  volume, "known" hides a stranger who never looks at the lens. Separating the
  two is one enum value and is worth more than any model change.
- [x] **3. DONE 2026-09-12. Gate recognition on the PIR window and the YOLO person box.** The PIR
  (`binary_sensor.venkovni_senzor`) knows when somebody is standing in the
  doorway — which is exactly where faces are large. Measured with a person
  deliberately facing the camera: median face **99px**, max 131px, 76% of
  detections >=80px, against a median of 35px in ordinary passage. Running
  recognition only inside that window, on the already-computed
  `detection.py:318-320` person box, turns an always-on cost into a few frames
  per event — which is what the original disable was about (OOM kills at a 2GB
  limit, `~/.codex/memories/a12_system_v2.md`, 2026-05-06).
- [x] **4. DONE 2026-09-12. Decide per episode, not per frame.** `identify_person` is called once
  (`pipeline.py:1219`) on a single full frame. The clip buffer holds dozens.
  Literature puts frame-level aggregation at 63% -> 85%.
- [ ] **Not yet exercised on live data.** Items 2-4 ship behind
  `FACE_RECOGNITION_ENABLED=false`, and the container has no backend at all:
  `dlib`, `face_recognition`, `onnxruntime` and `insightface` are all absent
  (verified in the running image), so the lazy-import branch in
  `detection.py:63-76` is dead code today. Nothing here is proven in production
  until a backend exists — see item 5.
- [x] **5. DONE 2026-09-12. Backend enabled: YuNet + SFace.** Verified inside the
  running container: OpenCV 4.11 exposes both `cv2.FaceDetectorYN_create` and
  `cv2.FaceRecognizerSF_create`, and calling them with a bogus path fails in the
  ONNX importer — the implementations are real, only the model files are
  missing. They run on `cv2.dnn`, the same engine the YOLO path already uses,
  and inherit the existing `A12_CV_THREADS=1` pinning for free. Dropping
  `face_detection_yunet_*.onnx` and `face_recognition_sface_*.onnx` into
  `${A12_DATA_DIR}` is the whole delta: no pip install, no image rebuild — the
  same convention `yolo11n.onnx` already follows. insightface `buffalo_s` is
  the opposite: a new pip dependency, a second ONNX runtime, an image rebuild,
  and a model download into `~/.insightface`, outside the bind mount, so it
  would not survive a container recreate. Headroom is 850 MB of the 1 GB limit,
  but measured at idle with the face path dead — measure the backend's real
  resident cost before trusting it.
- [ ] **6. Derive the match threshold from data — now measurable.** `0.363`
  cosine is OpenCV's published operating point for SFace, not one fitted here,
  exactly the mistake dlib's `0.6` was. First numbers, measured 2026-09-12 on
  the 10 enrolled reference photos and the last 300 stored person frames:
  - **The pipeline is sound.** Leave-one-out over the reference photos scores
    a median of **0.891** (min 0.529) against the same person, and **10/10**
    clear the threshold. Detection, alignment, embedding and matching all work.
  - **In the wild, 5 of 70 face-bearing frames matched**, and **all five had a
    face >=80px** (26 such frames); of the 44 frames with a smaller face, none
    matched at all. That is the PIR gate's premise confirmed from a second
    direction: below ~80px an embedding is not worth computing.
  - **This says nothing yet about the miss rate**, because those 300 frames are
    unlabelled — a non-match is equally consistent with "a different person" and
    with "the resident, missed". The household has two adults and only one is
    enrolled, so many are certainly other people. Fitting a threshold needs
    labelled frames: enrol the second person, then compare within-person and
    between-person distributions the way `tools/enroll_sface.py` already reports
    for the gallery itself.
 `tolerance = 0.6` is the
  library default, never fitted. Measured: at 35 degrees of head pitch dlib's
  distance for the *same person* is 0.566 against that 0.6 — the error budget is
  spent by geometry before a stranger appears.
- [x] **A. DONE 2026-09-15. One database row per episode.** Written when the
  episode closes — on the next occurrence, or from the heartbeat once the quiet
  gap has passed, so a visit nobody follows still lands with its own timestamp
  instead of the next morning's. Mutation testing earned its keep here: with
  the first version of the tests, deleting *either* close left all 324 other
  tests green, i.e. a pipeline that never wrote the row would have shipped.
- [x] **B. DONE 2026-09-15. `undecided` split out of `unavailable`.** "Matched
  a resident, but not often enough to confirm" is a real observation; "the
  check could not run" is an absence of one. It carries no name, a resolved
  stranger still outranks it, and rows written before today keep mapping to the
  old value.
- [x] **C. DONE 2026-09-15. Deleted the dlib branch.** Two things had to move
  first. It carried the only `try/except` around the check and the SFace path
  had none, so deleting it naively would have let a backend fault propagate out
  of `identify_person` into the detection loop. And `tools/enroll_faces.py`,
  deleted with it, was the only writer of `face_recognition.whitelisted_names`
  — a resident who is recognised but not whitelisted is alerted about anyway —
  so `enroll_sface.py` took that over. `tools/README.md` documented the dead
  tool at length and the live one not at all.
- [x] **D. DONE 2026-09-15. Crops off and deleted — and the cap was a fiction.**
  `_face_debug_written` started at zero in every `DetectionPipeline`, so each
  A12 restart granted a fresh quota of 200, and a camera crash loop restarts
  A12 repeatedly. It is seeded from the directory now. The flag had already
  been commented out in `config.env`; the 46 crops that had accumulated
  (6 resident, 14 stranger, 26 no_face) were deleted.
- [ ] **7. Re-measure the notification cooldown.** It drops 15 events/day against
  16 sent. Whether that is right depends entirely on (2)-(4).

### Tier 1b — face unlock (new goal, 2026-09-12 evening)

The goal changed mid-session: the point is not only quieter alerts but
**unlocking the Nuki lock by face**. That inverts which error matters — a false
accept stops being a missed notification and becomes a stranger let into the
flat — and the measurements below were taken with that in mind.

- [ ] **8. Decide it on a second camera first.** The M5Stack Unit CamS3 5MP
  (`~/Plocha/M5Stack/CamS3-Firmware`, PY260, UXGA 1600x1200) captures at 1.56x
  the linear resolution of the current 1024x768. Tonight's successful matches
  had faces of 110-136px and the failures 61-76px; scaled, those failures land
  at 95-119px, inside the range that already works. It also carries its own
  white LED, which removes the dependency on a hallway light that goes out when
  somebody stands still — that killed two capture attempts tonight. And it
  already serves the same endpoint contract A12 consumes (`/detection-stream`
  on 81, `/health`, `/frame`), so swapping is an `ESP32_IP` change, not code.
  **Unknown and not to be assumed: the lens FOV is documented nowhere in that
  repo**, and the PY260's low-light behaviour is unmeasured. Run the same
  measurement as tonight and compare the two score distributions.
- [ ] **9. Two galleries, because the two uses want opposite things.**
  Measured tonight, and the single most important result of the session:
  | gallery | enrolled person | non-enrolled person |
  |---|---|---|
  | 10 clean reference photos | 0.195 - 0.749 | 0.031 - **0.196** |
  | + 31 door-angle samples | 0.531 - 0.879 | 0.216 - **0.620** |
  Adding door-angle samples lifted recall to 9/9 above 0.5 — and lifted the
  **non-enrolled** person to 0.620, above the enrolled person's worst 0.531.
  The ranges overlap, so no threshold separates them. More data made the
  matcher more sensitive and less discriminating. So: suppression uses the
  wide gallery (it wants recall), unlocking uses the clean one (it wants
  precision, and there it had zero false accepts with a 0.37 margin).
- [ ] **10. Unlock design, when a camera is chosen.** Clean gallery, threshold
  0.60, three confirmations, only inside the PIR window, attempt limit and
  cooldown, and a Telegram message on every unlock so an unexpected one is
  visible immediately. `NUKI_LOCK_ENTITY_ID` and `_trigger_nuki_unlock()`
  already exist but hang off the dead Groq branch and never fire.
- [ ] **11. Accepted risk, recorded not resolved: no liveness detection.**
  SFace has none, so a photo on a phone held up to the lens produces a higher
  similarity than the owner does in poor light. Raised, and the owner decided
  to proceed. Nothing in the design mitigates it; a second factor would
  (household phone present in HA, or a recent Nuki unlock), and that is the
  cheapest real improvement if the risk ever stops being acceptable.
- [ ] **12. Enrol the second adult.** Still the prerequisite for item 6 and for
  any unlock threshold: without her enrolled, she is pushed towards the other
  person's score instead of matching her own entry. Two attempts failed
  tonight — the first because the enrolment floor (100px/0.85) was above
  anything the door can produce, the second because the PIR light went out
  while she stood still. `tools/enroll_sface.py --capture <name> --seconds 60`,
  standing close, moving slowly so the light stays on.

- [x] **13. DONE 2026-09-13. Camera crashes after the swap: bench config.** The
  board that became the production camera on 2026-09-12 was still configured
  for bench use — firmware motion detection, person detection and Telegram all
  on, none of which belong in the PIR-first role where the camera is only a
  frame source. It reached **45 restarts, four an hour**, with `PANIC(4)`
  between them and stall spikes of 105-141 an hour. Turning those five settings
  off at 18:19 gave zero panics and 92 minutes of unbroken uptime in the next
  hour. Recorded in `docs/DFROBOT_HARDWARE_GUIDE.md` along with the reboot-log
  trap: `PANIC(4)` is the cause, `SW(3)` after `reboot_cmd` is A12's watchdog
  cleaning up after it.
- [x] **13b. DONE 2026-09-15. `PANIC(4)` found: a mutex taken twice by one task.**
  `mqttLoop()` holds `mqtt_mutex` while calling `mqttReconnect()`, which on a
  successful connect called `mqttPublishStates()` — which takes the same
  non-recursive mutex from the same task. Timeout, then FreeRTOS asserts the
  holder is not the running task. Fixed in 3.12.51 by splitting out
  `mqttPublishStatesLocked()`; the only nested path of the seven lock sites.

  **Every successful MQTT connect crashed the board**, so item 13's "bench
  config" diagnosis was a correlation: turning firmware features off reduced how
  often the link churned, it did not remove the crash. The bug shipped with
  `mqtt_handler.cpp` itself in v3.12.43 (`aada0a5`, 2026-05-23) — **115 days
  live**.

  Found by reading the serial console on the bench board, which had been
  attached by USB for months with nothing reading it. There is no coredump
  partition, so `reason=PANIC(4)` is all a board without a cable can ever say.
  Reproduced twice, identical backtrace, both within one second of
  `MQTT: HA auto-discovery published`; zero after the flash.

  The bench board looked healthy only because it had **no MQTT credentials at
  all** and never entered the path — the second time that board's broken login
  has masked a bug (see 3.12.50's retracted antenna diagnosis).

- [ ] **14. No hurry: confirm the stall spikes are gone.** Nothing depends on
  this and the camera is healthy; it only settles whether the blocking MQTT
  connect explains the daytime bursts as well as the blackouts. The suspicious
  shape is a spike with **no people in it** — 2026-09-13 11:00 had 150 stream
  stalls and zero detections, which no amount of load or darkness explains but
  a broker hiccup driving the retry loop would.

  Baselines to beat: 628 stalls on 09-13 and 396 on 09-14 (spikes of 105-150 an
  hour), against 45 camera restarts on 09-13. Since firmware 3.12.50 and the
  bench-config cleanup the camera has held 17 h+ of uptime.

  **2026-09-15 evening — first numbers, promising but not yet an answer.**
  Stalls per hour across today, against the two fixes that landed in the middle
  of it: 5.8/h before the mutex fix (77 in 13.3 h), 1.5/h between it and the
  camera-profile fix (4 in 2.7 h), 1.8/h after both (8 in ~4.5 h). Daily totals:
  628 on 09-13, 448 on 09-14, 89 today. Hours 13:00 and 14:00 carried 20 and 40
  detections with **zero** stalls — the shape this item was looking for.
  **Not closed**, because today is not a clean day: it contains three firmware
  flashes, several deliberate reboots and two container recreates, each of which
  breaks the stream by design. A clean measurement is a full day with nothing
  touched, and that starts now.

  **The crash explains the restarts, not the stalls.** The
  morning burst splits cleanly in two: 10:13-10:52 stalls (`errno=104`,
  ECONNRESET) on a camera with 15 h of uptime and no restart at all, and only
  then the restart cluster from 10:52. So the stalls come first and the panics
  follow, which means 13b removes the second half and leaves this item open on
  its first half. Re-measure against a day on 3.12.51.

  ```sql
  SELECT substr(datetime,1,13) h,
         SUM(type='stream_stall') stalls,
         SUM(type='detection')    detections
  FROM events WHERE datetime >= '<a full day after 2026-09-14 16:00>'
  GROUP BY h ORDER BY h;
  ```

  A day of single-digit hours with no people-free spikes closes it. Spikes that
  survive mean something else is still there, and the next step would be a ping
  log running across one so the link can be watched during it rather than after.

- [x] **15. DONE 2026-09-14. The 11-15 s camera blackouts were MQTT, not radio.**
  `PubSubClient::connect()` blocks with a 15 s default socket timeout, from the
  main loop, holding `mqttLock()`, and the whole device stops answering for the
  duration. A board whose broker credentials are rejected retries every 10 s and
  spends a quarter of its life unreachable. Fixed with
  `mqttClient.setSocketTimeout(2)`; verified on the same board, same position:
  25.4 % loss with four 11-12 s blackouts before, 0.0 % and none after.
  **This retracts the 2026-09-12 diagnosis that board .160 had a failing
  antenna** — it has rejected credentials, nothing more.
- [x] **16. DONE 2026-09-14. Per-board `device_name` default.** Now carries an
  eFuse-MAC suffix, so two cameras cannot claim one mDNS name. Only applies to a
  board with no `config.json`; an OTA preserves LittleFS, so existing names are
  unchanged.
- [ ] **17. `updateIRAutoMode()` early return — reconsidered, not a bug.**
  `if (!irConfig.auto_mode) return;` reads as "manual mode, do not touch the
  LED", which is coherent: `auto_mode` is the master switch and `time_based`
  only selects time or sensor. Dropped from the batch rather than changed
  blindly. If the time window is ever wanted, the fix is configuration
  (`{"state":"auto"}` plus `time_based`), not code.

### Tier 2 — firmware (batch into one flash)

Each flash interrupts the production camera, so these ship together, not one at
a time.

- [ ] **`updateIRAutoMode()` early-returns when `auto_mode` is false**, so the
  `time_based` night window is never evaluated and the IR LED stays pinned to
  `manual_state` forever. `POST /ir-control` sets `auto_mode = false` by itself
  for `state: "on"`/`"off"`, so one Home Assistant toggle permanently kills the
  night automation. **Harmless for the LED — which must stay off anyway while it
  is sealed in the box — and 2026-09-15 showed it does not touch the camera
  profile at all: `updateCameraProfile()` reads `irConfig.time_based` directly,
  independent of `auto_mode`. That is what made the DUSK pin possible without
  waking the LED.**
- [x] **DONE in 3.12.50 — `device_name` defaults to an eFuse-MAC suffix**, so two
  boards cannot collide on mDNS and MQTT out of the box. Existing installs keep
  their name: `device_name` lives in `/config.json` on LittleFS, which an OTA
  preserves. (Duplicate of item 16; left here marked done rather than deleted so
  the Tier 2 batch list stays readable.)

- [x] **DONE 2026-09-15 (3.12.53). Log the reason at every `ESP.restart()`.**
  All nine call sites go through `restartWithReason()` now, which writes an
  `EVT_RESTART` event and only then restarts. Verified end-to-end on both
  boards: `restart | reboot_cmd` immediately precedes `boot | reason=SW(3)`.
  Previously: all call sites in
  `main.cpp` and `camera_server.cpp` restart without writing anything to
  `/events`, so the reboot log can only ever say `SW(3)` — the code, never the
  cause. On 2026-09-15 a production restart could not be explained beyond a
  guess (`recoverCamera()` after three failed `checkCameraHealth()` calls) for
  exactly this reason. One `logEvent()` before each restart, and the log finally
  distinguishes a heap bailout from a camera-health bailout from an OTA. Same
  lesson as the MQTT state added to `/status` in 3.12.52: the failure was
  visible, its cause was not.

- [ ] **Full firmware rebuilds intermittently hit a GCC internal compiler
  error.** Twice on 2026-09-15, both times inside the Edge Impulse SDK
  (`test_helpers.cpp`, then `micro_interpreter.cpp` with
  `internal compiler error: in ggc_set_mark, at ggc-page.cc:1551`), both times
  green on an immediate retry with no change. Only full rebuilds are affected,
  which is what a changed `-DFIRMWARE_VERSION` forces. Harmless so far because
  the retry works, but it would fail a CI that builds firmware — and CI does
  not build firmware today, which is its own gap.

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
