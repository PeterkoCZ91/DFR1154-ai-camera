# A12 Tools

## evaluate_face_unlock.py — Offline Quality and Confirmation Replay

Runs on a **copy** of saved `face_debug/*.jpg`, the two ONNX face models,
and the tagged SFace gallery. It never connects to HA, MQTT, Telegram, or a
camera. Use the same OpenCV version as the capture runtime for comparisons.

```bash
python3 -m a12_system.tools.evaluate_face_unlock \
  --data-dir /path/to/data-copy \
  --output-dir ./reports/face-replay \
  --allowed-name Alice
```

Outputs face-only quality measurements, threshold/window sensitivity tables,
Czech operator-facing HTML (`review.html`), a focused review of newly permitted
candidates (`review_new.html`), and a Czech annotation sheet (`oznaceni.csv`).
The translation resource is `tools/face_review_cs.json`; technical data in
`review.csv` retains stable field names for processing.
Results are simulations on recompressed images with the supplied gallery,
not historical lock actions or measured accuracy. Old crops have no physical
track IDs: the replay explicitly assumes episode continuity and does not
validate it. The grid thresholds are experimental and are not deployed.

`face_quality.py` implements the opt-in quality policy; `face_window.py`
implements a separate experimental window requiring an explicit physical
track ID. Neither module changes the production recognition pipeline.

## sanitize_gallery.py — Gallery Sanitization & Outlier Pruning

Prunes contaminated reference embeddings from an SFace gallery (`known_faces_sface.pkl`).
Filters extreme internal outliers, cross-polluted samples, and video burst frames:

```bash
python3 -m a12_system.tools.sanitize_gallery \
  --input /path/to/known_faces_sface.pkl \
  --output /path/to/known_faces_sface_clean.pkl \
  --report
```

1. **Extreme internal outliers:** drops samples with median similarity to their own cluster `< min_within` (default 0.40) or `max_cross >= within_median`.
2. **Cross-pollution:** drops samples whose maximum similarity to another enrolled identity is `>= max_cross` (default 0.35).
3. **Burst deduplication:** greedy exemplar selection per identity to eliminate video burst frames (`max_internal_sim < 0.88`), retaining diverse poses.

## a12 — Runtime CLI Wrapper

`tools/a12` is the recommended user-facing entry point for local Docker operation.
It can be called from any directory and uses `a12_system/docker-compose.yml` explicitly.

### Quick Start

```bash
cd a12_system
cp .env.example .env
./tools/a12 setup
nano /opt/a12-data/config.env
./tools/a12 doctor
./tools/a12 build
./tools/a12 up
./tools/a12 logs 80
```

### Useful Commands

| Command | What it does |
|---|---|
| `a12 doctor` | Checks Docker, config, model, camera `/health`, stream port 81, MQTT, Telegram, and basic git hygiene |
| `a12 setup` | Creates data dir, config template, model file, and screenshots folders |
| `a12 config` | Prints sanitized runtime paths and key config values |
| `a12 test-camera` | Checks ESP32 `/health` and port 81 stream reachability |
| `a12 build` / `a12 rebuild` | Builds the Docker image |
| `a12 up` / `a12 start` | Starts the A12 service |
| `a12 restart` | Restarts the A12 service |
| `a12 logs [N]` | Follows Docker logs, default last 50 lines |
| `a12 events [N]` | Shows recent SQLite events |
| `a12 tail` | Follows `/data/a12.log` |

### Multi-Instance Example

```bash
A12_DATA_DIR=/opt/a12-gate A12_COMPOSE_PROJECT=a12_gate A12_CONTAINER=a12-gate \
  ./tools/a12 up

A12_DATA_DIR=/opt/a12-yard A12_COMPOSE_PROJECT=a12_yard A12_CONTAINER=a12-yard \
  ./tools/a12 up
```

Use unique `CAMERA_ID`, `MQTT_BASE_TOPIC`, and `ESP32_MQTT_DEVICE` in each instance config.

---

## enroll_sface.py — Face Enrollment

Builds the SFace gallery A12 matches against, and points the whitelist at the
people it contains. Reads `<data-dir>/known_faces/<name>/*.jpg|png` and writes
`<data-dir>/known_faces_sface.pkl` plus `whitelisted_names` in `config.json`.

Run it where the ONNX models are — normally inside the container, which already
has OpenCV and needs no extra dependency:

```bash
docker compose -p a12_system exec a12 \
    python3 -m a12_system.tools.enroll_sface --data-dir /data
```

### Quick Start

```bash
# 1. Capture from the live camera (recommended: same lens, angle and light
#    that will do the matching)
python3 -m a12_system.tools.enroll_sface --data-dir /data --capture "Alice"

# 2. Or drop photos into /data/known_faces/Alice/ and just build
python3 -m a12_system.tools.enroll_sface --data-dir /data

# 3. Report what would happen, write nothing
python3 -m a12_system.tools.enroll_sface --data-dir /data --dry-run
```

Restart A12 afterwards — the gallery is read once in `Detector.__init__`:

```bash
docker compose -p a12_system restart a12
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--data-dir PATH` | `$A12_DATA_DIR` or `/data` | Holds `known_faces/`, the models and `config.json` |
| `--capture NAME` | — | Collect samples of NAME from the camera first |
| `--camera URL` | `ESP32_IP` from `config.env` | Camera base URL |
| `--auth USER:PASS` | from `config.env` | Camera HTTP auth |
| `--seconds N` | `30` | How long to capture for |
| `--out PATH` | `<data-dir>/known_faces_sface.pkl` | Gallery output |
| `--detector-score N` | `0.6` | YuNet confidence floor |
| `--dry-run` | — | Report only, write nothing |

### What it refuses, and why

- **A face smaller than the door can produce.** Samples are checked against the
  size an embedding actually needs; enrolling a face the camera will never see
  again at that size fits the threshold to unreachable data.
- **A near-duplicate pose.** Twenty frames of one head angle look like a large
  gallery and behave like one sample.
- **A gallery from another backend.** dlib and SFace embeddings are both 128-d
  and mean nothing to each other, so the file carries the backend that made it
  and a mismatch is refused rather than compared. A `known_faces.pkl` from
  before 2026-09 is dlib-era and is not convertible — the photos have to be
  re-embedded.

It also *reports* unusual samples without deleting them: similarity cannot tell
an extreme angle from a different person, and an extreme angle is the most
valuable pose in the set. A human looks at the photo and decides.

### Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `no reference photos at ...` | No `known_faces/<name>/` directory | Create it, or use `--capture` |
| `SFace backend unavailable, missing:` | ONNX models not in the data dir | Drop both model files there |
| `captured nothing usable` | Too far, too dark, or one pose | Move closer, stand under the light, move slowly |
| Recognition still alerts on a resident | Whitelist not applied | Restart A12 after enrolling |
