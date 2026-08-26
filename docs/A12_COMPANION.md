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

**Face recognition + Nuki unlock (optional)**

A12 uses [Groq](https://console.groq.com) (LLaMA 4 Scout, free tier) to recognize
residents by comparing live camera frames against reference photos — no local model training
needed.

```bash
# 1. Get a free API key at https://console.groq.com → API Keys
#    Add to config.env:
GROQ_API_KEY=<GROQ_API_KEY>
NUKI_LOCK_ENTITY_ID=lock.nuki_smart_lock   # HA entity

# 2. Create a folder with 2–3 JPEG reference photos per person
mkdir -p /opt/a12-data/known_faces/alice
# copy or take snapshots — camera built-in /frame endpoint works great

# 3. Restart A12
a12_system/tools/a12 up
```

How A12 decides what to do with a detected person:

| Groq confidence | Action |
|---|---|
| ≥ 90% | Telegram skipped · Nuki unlocked automatically |
| 65–89% | Telegram sent with name hint · manual confirm |
| < 65% | Full Telegram alert — unknown visitor |

> [!NOTE]
> A12 requires the YOLO model file (`yolo11n.onnx`, ~6 MB). `setup.sh` downloads
> it automatically. If you prefer manual download, use the Ultralytics v8.3.0 asset
> release for `yolo11n.onnx`.

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
| `a12 enroll [args]` | Run `enroll_faces.py` for face whitelist enrollment |
| `a12 help` | Show this command list |

Decision audit rows are retained locally for 30 days by default. Set `DECISION_AUDIT_RETENTION_DAYS=0` to keep them indefinitely.

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
