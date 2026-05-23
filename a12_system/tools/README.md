# A12 Tools

## enroll_faces.py — Face Enrollment

Register people for face recognition. The script encodes faces and saves them to
`known_faces.pkl`, then updates the whitelist in `config.json`.

### Requirements

```bash
pip install face-recognition opencv-python requests numpy
```

---

### Quick Start

```bash
# 1. Capture from camera (recommended — stand in front for ~40 seconds)
python tools/enroll_faces.py --name "John" --capture --camera http://192.168.1.100

# 2. Enroll from a folder of photos
python tools/enroll_faces.py --name "John" --photos ./photos/john/

# 3. List enrolled people
python tools/enroll_faces.py --list

# 4. Remove a person
python tools/enroll_faces.py --remove "John"
```

After enrollment, restart A12 to apply:
```bash
docker compose restart a12
```

---

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--name NAME` | — | Person name (required with `--capture` / `--photos`) |
| `--capture` | — | Live capture from camera |
| `--photos DIR` | — | Enroll from JPEG/PNG directory |
| `--list` | — | Show enrolled people and encoding counts |
| `--remove NAME` | — | Remove all encodings for a person |
| `--camera URL` | `http://192.168.1.100` | Camera base URL |
| `--auth USER:PASS` | `admin:admin` | Camera HTTP auth |
| `--count N` | `23` | Total frames to capture |
| `--interval SEC` | `1.5` | Seconds between captures |
| `--data-dir PATH` | `/opt/a12-data` | A12 data directory (overrides `A12_DATA_DIR` env) |

---

### Live Capture — Guided Phases

The capture mode guides you through 6 poses automatically:

| Phase | Instruction | Frames |
|---|---|---|
| Straight | Look straight at the camera | 5 |
| Left | Turn head slightly to the left | 4 |
| Right | Turn head slightly to the right | 4 |
| Up | Tilt head slightly up | 3 |
| Down | Tilt head slightly down | 3 |
| Free | Move freely — any angle | 4 |

**On-screen indicators:**
- **Green box** — face detected, capturing
- **Orange box** — too similar to previous frame, move more
- **Red box** — no face detected or multiple people in frame
- **Progress bar** — overall capture progress
- Press **Q** or **Esc** to stop early (already captured frames are saved)

**Tips for best results:**
- Distance: 0.5–1.5 m from camera
- Lighting: even, avoid strong backlight
- Capture both with and without glasses if applicable
- Keep your face centred — the green box confirms detection
- 20–30 encodings per person gives good accuracy

---

### Using a Custom Data Directory

```bash
# Docker default
python tools/enroll_faces.py --list --data-dir /opt/a12-data

# Or via environment variable
export A12_DATA_DIR=/opt/a12-data
python tools/enroll_faces.py --list
```

---

### Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `Camera UNREACHABLE` | Wrong URL or auth | Check `--camera` and `--auth` |
| `No face detected` | Too far, dark, or bad angle | Move closer, improve lighting |
| Multiple faces warning | Someone else in frame | Ensure only the enrollee is visible |
| Orange box constantly | Interval too short | Increase `--interval` (e.g. `--interval 2.5`) |
| Low recognition accuracy | Too few encodings | Add more with `--capture` (another 10–15 frames) |

---

### Example: Full Enrollment Session

```bash
# First person — live capture
python tools/enroll_faces.py \
  --name "Alice" \
  --capture \
  --camera http://192.168.1.100 \
  --auth admin:admin \
  --count 23

# Second person — from photos
python tools/enroll_faces.py \
  --name "Bob" \
  --photos ./photos/bob/ \
  --data-dir /opt/a12-data

# Verify
python tools/enroll_faces.py --list
#   Alice: 23 encodings
#   Bob: 18 encodings
#   Total: 41 encodings, 2 people

# Apply
docker compose restart a12
```
