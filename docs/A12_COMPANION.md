# A12 Companion

A12 is the optional Python companion for Enhanced mode. It runs beside the ESP32 firmware
on a Pi or x86 host and adds server-side YOLO verification, face recognition, AV clips,
Home Assistant / Zigbee fusion, and Telegram alert routing.

The ESP32 firmware remains usable standalone. A12 is an opt-in runtime layer.

## Runtime Boundary

- This repository owns ESP32 firmware and the A12 companion under `a12_system/`.
- The paired Tapo monitor repository owns the Pi-side Tapo daemon, shared scorer service,
  runtime topology, and public-release scan notes.
- A12 and Tapo share the scorer only through HTTP. They do not import each other's Python
  modules, and they keep separate alert rules and thresholds.

## Enhanced Mode Quickstart

```bash
# One-time setup: creates data dir, downloads YOLO model, copies config template
a12_system/tools/setup.sh

# Edit runtime config outside git
nano /opt/a12-data/config.env

# Check local requirements before starting
a12_system/tools/a12 doctor

# Build image, start, and watch logs
a12_system/tools/a12 build
a12_system/tools/a12 up
a12_system/tools/a12 logs 80
```

For Docker layout, multiple camera instances and runtime data ownership, see
[`../a12_system/DOCKER.md`](../a12_system/DOCKER.md).

## Detection, Face Recognition and Shared Scorer

**Local Face Recognition + Nuki Unlock**

A12 runs face detection and recognition locally with OpenCV's `cv2.dnn`: YuNet finds the face and its five landmarks, SFace turns it into a 128-d embedding compared by cosine similarity. No cloud API and no biometric data leaves the host.

```bash
# 1. Models live in the A12 data directory alongside YOLO:
#    face_detection_yunet_2023mar.onnx   (~230 KB)
#    face_recognition_sface_2021dec.onnx (~38 MB)
#
# 2. Add to config.env (start with the dry run: it logs and messages what it
#    would do and never opens the door):
FACE_RECOGNITION_ENABLED=true
FACE_UNLOCK_ENABLED=true
FACE_UNLOCK_DRY_RUN=true
FACE_UNLOCK_NAMES=alice,bob
FACE_UNLOCK_MIN_SCORE=0.60
FACE_UNLOCK_CONFIRMATIONS=2
FACE_MIN_FACE_SIZE=40                 # Faces smaller than this (px) are not checked
FACE_MAX_CHECKS_PER_EPISODE=20        # Usable face checks per approach
NUKI_LOCK_ENTITY_ID=lock.front_door   # HA lock entity

# 3. Put reference photos in <data-dir>/known_faces/<name>/*.jpg and build the
#    gallery (inside the container; --dry-run reports without writing):
python3 -m a12_system.tools.enroll_sface --data-dir /data

# 4. Optional: drop outliers and near-duplicates into a new file, then review
#    the report before swapping it in:
python3 -m a12_system.tools.sanitize_gallery \
    --input /data/known_faces_sface.pkl --output /data/known_faces_sface.clean.pkl --report
```

Photos taken by the door camera itself match best: a gallery built from
phone selfies scores noticeably lower through a wide-angle lens. The
`face_debug` crops (`FACE_DEBUG_CROP_DIR`) are exactly what recognition runs
on, so they make good enrolment material once sorted by hand.

**How an unlock happens**

1. The PIR, Home Assistant motion sensor or YOLO starts an episode; faces are
   checked inside the person box about twice a second, on a person track the
   lock keeps for itself.
2. Each check needs one face of at least `FACE_UNLOCK_MIN_FACE_SIZE` px, the
   quality policy, a listed name and `FACE_UNLOCK_MIN_SCORE`. Every check logs
   one `Face check:` line with its outcome and the reason.
3. `FACE_UNLOCK_CONFIRMATIONS` agreeing checks within `FACE_UNLOCK_WINDOW_SECONDS`
   confirm, but only if the landmarks show some head movement between them
   (`awaiting:spoof_planar` in the log means "still too flat or too still").
   Walking up normally is enough; standing frozen delays it.
4. Before opening, A12 checks that nobody opened the door another way a moment
   ago (see the next section), then calls HA `lock.open` and waits for the
   lock to report it.

Measured at one door: first recognised face to confirmation 2–3 s, then up to
~2.5 s for the lock's log and a few seconds of motor time, so roughly 7–9 s
from looking at the camera to an unlatched door.

**Limits.** YuNet's landmarks jitter by 13–17 % of the eye distance at door
distances, about as much as a real head turn. The movement check stops most
still photos; a photo that is moved or tilted in front of the camera still
passes most of the time, and a video on a phone passes any geometric check.
Treat it as a hurdle, not anti-spoofing. If a photo must not open the door,
add a second factor with `FACE_UNLOCK_PRESENCE_ENTITY` (an HA entity such as a
phone tracker that must read `home`).

**Door already opened by a phone (Nuki Hub)**

A phone with the Nuki app (or Auto Unlock) opens the lock directly over
Bluetooth. Neither HA nor A12 is involved, and unlatching an unlocked lock ends
in the same `unlocked` state, so the HA lock entity often never shows it.
Without more information the camera would then recognise the resident walking
in and open the door a second time.

With [Nuki Hub](https://github.com/technyon/nuki_hub) as the bridge, A12 reads
the lock's own activity log instead:

1. In the Nuki Hub web UI, *Credentials* → enter the lock's security PIN (from
   the Nuki app). The status page must read **PIN valid**; without it the log
   cannot be read.
2. *Access Level Configuration* → enable **Publish authorization log** (and
   *Publish authorization entries information* so entries carry names).
3. Restart Nuki Hub.

A12 subscribes to `nukihub/lock/log`, `shortLog` and `rollingLog`. Nuki Hub
fetches the log whenever it reads the lock, so A12 asks it to
(`query/lockstate`) on face checks, at most every 20 s, and once more right
before a face would open, waiting up to 5 s for the answer (about 2.5 s on the
test lock). Any opening in the log by a name other than A12's own path blocks
face opening for the window below. An opening through A12's Telegram `/od`
blocks it the same way.

```bash
FACE_UNLOCK_SKIP_AFTER_EXTERNAL_OPEN_SECONDS=300  # 0 turns it off
FACE_UNLOCK_EXTERNAL_OPEN_HOLD_SECONDS=5          # wait for the fresh log before opening
FACE_UNLOCK_OWN_LOCK_NAMES=NukiHub                # log names that are A12/HA itself
NUKI_LOG_TOPIC=nukihub/lock/log                   # empty disables the subscription
```

In the log, `Nuki log: Unlatch by '<name>' at HH:MM:SS` shows each outside
opening as it arrives, and `Face unlock skipped for '<name>': door opened by
'<who>' Ns ago` shows a second opening that did not happen. Without Nuki Hub
the topics simply stay silent and face unlock behaves as before.

**Openings nobody here asked for.** Home Assistant buttons and automations
unlock through Nuki Hub too, so their log entries carry the same name as A12's
own (`FACE_UNLOCK_OWN_LOCK_NAMES`). A12 remembers when it sends `lock.open`
(face, `/od` or the button); a successful opening in the log that follows none
of those within about 40 s is sent to Telegram as "Door opened from Home
Assistant" and stored as an `unlock_external` event. Each log entry is
announced once, entries older than 2 minutes (a retained log read after a
restart) are ignored, and openings by phones are not announced.

**Key Pipeline Features:**
- **Fast cadence:** face checks run on intermediate frames inside the known person box, decoupled from the slower YOLO cycle.
- **Distance filtering and check budget:** faces smaller than `FACE_MIN_FACE_SIZE` are skipped without spending the check budget (`FACE_MAX_CHECKS_PER_EPISODE`, `FACE_MAX_ATTEMPTS_PER_EPISODE`); the budget refills every `FACE_BUDGET_REFILL_SECONDS` while the sensor still reports motion.
- **Sliding confirmation window:** distant, low-scoring sightings from the approach cannot poison the decision; only agreeing checks inside the window count.
- **CLAHE contrast fallback:** recovers part of the underexposed or backlit faces in a doorway.
- **Shadow threshold:** `FACE_UNLOCK_SHADOW_MIN_SCORE` logs when a lower threshold would have opened and the real one did not, to tune the threshold from real visits. It never opens the door.
- **Home Assistant unlatch and lock:** HA `lock.open` unlatches for hands-free entry, `lock.lock` locks on demand; both wait for the lock to confirm before reporting success.

**Interactive Telegram Bot:**
- Running on the server via `TelegramBotService` (no ESP32 TLS polling or memory fragmentation).
- Commands & Aliases: `/photo` / `/foto` (live frame capture), `/unlock` / `/odemknout` / `/od` (instant Nuki unlatch via HA `lock.open`), `/lock` / `/zamknout` / `/za` (Nuki door lock via HA `lock.lock`), `/status` / `/stav` (system & stream overview), `/mute [min]` / `/unmute`. Face enrolment is not offered in chat: a photo sent through Telegram does not match what the door camera sees, so enrol with `a12 enroll` from camera frames.
- Command Menu Picker: Automatically registers the command list in the Telegram client UI via the `set_my_commands` API upon bot startup.
- Inline Action Buttons: Visitor and stranger notifications include `[ 🔓 Unlock ]` and `[ 🔕 Mute 15m ]` with one-tap confirmation.

**Shared YOLO scorer (optional, opt-in)**

A12 can use a shared HTTP scorer instead of running YOLO in-process. This keeps the
local `cv2.dnn` model as the default and fallback, but lets multiple cameras share one
stronger model on another host.

The paired Tapo monitor repo is
[tapo-monitoring](https://github.com/PeterkoCZ91/tapo-monitoring). It owns the Pi-side
daemon, shared scorer service, runtime topology, and public-release scan docs; this
firmware repo stops at the camera / A12 companion boundary.

```env
YOLO_BACKEND=http
YOLO_SCORER_URL=http://SCORER_HOST:8766/score
# Optional; default is 4 seconds for a bounded shared-scorer CPU queue.
YOLO_REMOTE_TIMEOUT_SECONDS=4
```

The scorer contract is:

```http
POST http://SCORER_HOST:8766/score
Content-Type: image/jpeg
```

```json
{
  "person": 0.87,
  "animal": 0.0,
  "classes": {"person": 0.87, "dog": 0.41}
}
```

A12 reads `classes`, filters it through `YOLO_CLASSES`, applies
`YOLO_CONFIDENCE_THRESHOLD`, and returns the same `(label, confidence)` detections to the
pipeline. The HTTP deadline is configurable with `YOLO_REMOTE_TIMEOUT_SECONDS`; if the
scorer is unavailable or exceeds that deadline, A12 falls back to the local model if it
loaded.

A12 keeps aggregate-only counters for these calls (`scorer` in `stats.json`). Failures
are split rather than pooled: `transport_failures` means the scorer could not be
reached, `http_errors` means it answered non-2xx, `malformed_responses` means it
answered something A12 could not use, and `fallbacks` counts local-model fallbacks.
`error_kinds` holds exception class names and HTTP statuses only — never exception
text, which can carry the scorer URL. `request_seconds_max` covers failed attempts
too, so it is the number to size `YOLO_REMOTE_TIMEOUT_SECONDS` against.
Recalibrate thresholds before production use with a different shared model; scores from a
larger model are not directly comparable to `yolo11n`.

> [!NOTE]
> When using A12, disable ESP32 Telegram to avoid duplicate alerts and free heap:
> comment out `#define INCLUDE_TELEGRAM` in `firmware/config.h` before flashing. See
> [Two Deployment Modes](../README.md#two-deployment-modes).

## A12 CLI

The `a12_system/tools/a12` script is the recommended local entry point for A12 Docker
operation. You can run it directly from the repository, or symlink it to your `$PATH`
later.

```bash
cd a12_system
./tools/a12 setup
./tools/a12 doctor
./tools/a12 build
./tools/a12 up
./tools/a12 logs 80
```

| Command | Description |
|---------|-------------|
| `a12 doctor` | Check Docker, config, model, camera `/health`, stream port 81, MQTT, Telegram config, container state, and git hygiene |
| `a12 setup` | Create the runtime data dir, config template, model file, and screenshots folders |
| `a12 config` | Print sanitized runtime paths and key config values |
| `a12 test-camera` | Check ESP32 `/health` and MJPEG stream reachability |
| `a12 status` | Container status + CPU/RAM snapshot |
| `a12 logs [N]` | Stream last N lines of container logs (default 50), follows |
| `a12 restart` | Restart the A12 service |
| `a12 build` / `a12 rebuild` | Rebuild the Docker image |
| `a12 up` / `a12 start` | Start the compose stack in detached mode |
| `a12 down` / `a12 stop` | Stop and remove the compose stack |
| `a12 events [N]` | Query last N events from `events.db` (default 20) |
| `a12 calibrate [days]` | Aggregate the local YOLO/policy audit for the selected period (default 7 days) |
| `a12 tail` | Follow `a12.log` on the host |
| `a12 enroll [args]` | Run `enroll_sface.py` in the container: build the face gallery and whitelist |
| `a12 help` | Show this command list |

Decision audit rows are retained locally for 30 days by default. Set `DECISION_AUDIT_RETENTION_DAYS=0` to keep them indefinitely.

### Learning data retention

An audit row is only checkable against ground truth if its image still exists, so
two folders under `screenshots/` opt out of the default `MEDIA_RETENTION_DAYS=2`
sweep:

| Folder | Retention | Purpose |
| --- | --- | --- |
| `person/` | `PERSON_MEDIA_RETENTION_DAYS` (default 30, clamped up to `MEDIA_RETENTION_DAYS`) | Confirmed person clips outlive the audit rows that reference them |
| `candidates/` | follows `DECISION_AUDIT_RETENTION_DAYS` | The frame behind a person-candidate audit row |
| `misses/` | follows `DECISION_AUDIT_RETENTION_DAYS` | The frame behind a sensor-triggered "nobody here" row |

`candidates/` exists for the decisions that save nothing else: a rejected or
still-unconfirmed candidate leaves no clip, so without the snapshot there is no
way to see what YOLO actually scored at 0.55 — or at an unconfirmed 0.92. Outcomes
that already write their own media (`recorded_and_notified`, `recorded_local_only`)
are skipped so the clip is not duplicated. `CANDIDATE_SNAPSHOT_MIN_INTERVAL_SECONDS`
rate-limits the folder; at the default 1.0 it measures roughly 11 MB/day.

`misses/` covers the largest and previously blindest population in the audit: a
sensor fired and YOLO found no candidate at all. That decision is either a real
miss — the failure this project cares most about — or a cat, a branch or rain,
and nothing but the frame can tell the two apart. Those rows carry no
`candidate_label`, so the candidate rule above never applied to them. Periodic
checks are excluded: with no sensor behind it, "nobody here" is not a claim that
anybody was there. `MISS_SNAPSHOT_MIN_INTERVAL_SECONDS` is a separate limiter
from the candidate one on purpose, so a busy candidate stream cannot starve it.
See `docs/DATA_PRIVACY.md` — this folder keeps frames of people the system never
alerted on.

Setting a retention to `0` means "keep indefinitely" everywhere, including the
files — a `0` never sweeps a folder empty.

### Ground truth

The audit records every knob that applied to a decision, which is enough to
replay what a different threshold would have done — but only once something says
whether the frame actually held a person. `a12 review` is that something:

```bash
a12 review              # interactive pass: [p] person [n] not a person [u] unsure
a12 review --stats      # coverage, precision per confidence band, inference latency by outcome
a12 review --list       # unlabeled snapshot paths, for an external viewer
a12 review --set 8697 person
```

Verdicts land in `decision_labels`, keyed by the audit row id that each snapshot
filename carries. The table denormalises the confidence and outcome it describes
and is **never pruned**: audit rows expire after
`DECISION_AUDIT_RETENTION_DAYS`, but a human-checked label is the only thing in
this database that cannot be recomputed. `a12 calibrate` reports precision per
band once labels exist, and says so plainly while they do not.

`--stats` and `--list` need no display; interactive review opens a cv2 window.

**Environment:** The script uses `A12_DATA_DIR` (default `/opt/a12-data`) for the data
volume path. For multiple cameras, also set unique `A12_COMPOSE_PROJECT`,
`A12_CONTAINER`, `CAMERA_ID`, `MQTT_BASE_TOPIC`, and `ESP32_MQTT_DEVICE` values:

```bash
A12_DATA_DIR=/home/user/a12-gate A12_COMPOSE_PROJECT=a12_gate A12_CONTAINER=a12-gate a12 up
```

## Public Repo Hygiene

A12 runtime data is intentionally outside git. Do not commit `config.env`, `.env`, tokens,
MQTT credentials, Home Assistant tokens, Wi-Fi credentials, logs, databases, face encodings,
private screenshots, or model weights.

Use placeholders such as `<camera-ip>`, `<mqtt-ip>`, `<telegram-token>`, and
`<GROQ_API_KEY>` in issues, docs and examples.
