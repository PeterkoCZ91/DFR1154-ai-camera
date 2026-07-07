# Data Privacy & Anonymization

This project is a **self-hosted, local-first** security camera. By design, image and
audio processing happens on the ESP32 and on the A12 companion running on your own
machine — not in the cloud. This document explains exactly what data the system
touches, where it stays, what can leave your network, and how to minimize or anonymize
it. For vulnerability reporting and secret-handling, see [SECURITY.md](../SECURITY.md).

> TL;DR — With Telegram and cloud vision disabled, **no image, audio, or biometric data
> ever leaves your LAN.** The only things that can leave are notifications you explicitly
> enable, and they go to services *you* configure (your Telegram chat, your MQTT broker).

---

## What data the system handles

| Data | Where it is processed | Where it is stored | Leaves your LAN? |
|------|----------------------|--------------------|------------------|
| Live video (MJPEG / RTSP) | On-camera + A12 decode | Not stored (stream only) | No |
| Motion / person clips + snapshots | A12 | Local data dir (`/data/screenshots`, gitignored) | Only if you enable Telegram |
| Audio (PDM mic) | On-camera | Local only; on SD if AVI recording enabled | No |
| Face reference images / encodings | A12 (optional, **off by default**) | `known_faces/` (gitignored) | No — encodings never transmitted |
| Detection metadata (labels, scores, timestamps) | A12 | `events.db` (SQLite, gitignored) | No |
| Runtime logs | A12 | `a12.log` (gitignored) | No |
| Network identifiers (camera IP/MAC, broker IP, tokens, chat_id) | — | `config.env` (gitignored) | Used only to reach the services you configured |

Nothing in the tables above is committed to the public repository — see
[What the repository excludes](#what-the-public-repository-excludes).

---

## What can leave your network (and how to turn it off)

There are exactly two paths out of your LAN, both opt-in and off-by-default for image data:

### 1. Telegram notifications
When enabled, the system sends alert text and — depending on flags — snapshots or short
clips to **your** Telegram chat. That traffic goes to Telegram's servers.

Control it in `config.env`:

```env
# Master switches — set to false to keep imagery fully local
MOTION_TELEGRAM_PHOTO=false          # no photo on AI-fallback motion
PERSON_TELEGRAM_PHOTO=false          # no photo on person detection
MOTION_TELEGRAM_VIDEO=false          # no AVI/MP4 upload
PIR_RECORDING_REQUIRE_YOLO_FOR_TELEGRAM=true   # withhold Telegram until YOLO confirms
```

With all photo/video switches off, Telegram receives text alerts only (or nothing if you
leave the bot token unset).

### 2. Cloud vision (optional, disabled)
An optional Groq/LLaVA face-identification path exists in `groq_vision.py`. It is
**disabled by default** and was intentionally deactivated in this deployment for privacy
(sending frames to a third-party model is a deepfake / data-exfiltration risk). To keep it
off, simply leave `GROQ_API_KEY` unset. Do not enable it unless you accept that camera
frames will be transmitted to an external provider.

### Everything else stays on the LAN
MQTT state (motion/person/sensor values) is published only to the local broker you
configure (e.g. Home Assistant). It never traverses the internet unless your broker does.

---

## Running fully offline

To guarantee zero data egress:

1. Leave `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY` unset (or set all `*_TELEGRAM_*` flags to `false`).
2. Point `MQTT_BROKER` at a broker on your own network only.
3. Do not expose the camera dashboard, MJPEG (`:81`), RTSP (`:554`), audio (`:82`), or the
   MQTT broker to the public internet (see SECURITY.md).

In this mode the camera is a closed-loop local sensor: video/audio are analyzed and
discarded or stored locally, and only Home Assistant on your LAN sees the results.

---

## Biometric data (faces)

- Face recognition is **optional and disabled by default**.
- Reference photos live in `known_faces/` and are **gitignored** — they are never part of
  the repository or any release.
- Face encodings are computed and matched locally; they are not transmitted anywhere.
- To remove all biometric data, delete the `known_faces/` directory and any stored clips
  that contain identifiable people. There is no cloud copy to revoke.

---

## Data retention

- Clips, snapshots, `events.db`, and logs accumulate in the local data directory. They are
  **your** responsibility to rotate or purge; the project does not upload or back them up.
- SD-card AVI recording (if enabled) has an auto-delete/rotation policy to bound storage —
  configure the retention window to match your needs and local laws.
- Metadata in `events.db` (timestamps, detection labels, media paths) does not contain
  faces, but it does reveal **activity patterns** for a location. Treat the data directory
  as sensitive and keep it off shared or public storage.

---

## What the public repository excludes

The repository is structured so that **no personal or deployment-specific data can be
committed**. `.gitignore` excludes, among others:

```
config.env  .env  *.pem  *.key      # secrets, tokens, keys
*.db  *.log  logs/                    # event history and runtime logs
known_faces/  camera_photos/          # biometric + captured imagery
screenshots/ (except docs/)  recordings/
*.onnx  *.weights  *.pt  *.pkl        # model weights
LOCAL_*.md  *_LOCAL.md                # local handover / secrets notes
```

Configuration is shared only through `config.env.example`, which contains **placeholders**
(e.g. `192.168.1.100`, `admin:admin`, empty tokens) — never real IPs, tokens, or chat IDs.

---

## Contributor checklist (keep PII out of commits)

Before every commit, run the privacy audit and confirm it prints nothing:

```bash
git diff --cached | grep "^+" | grep -v "^+++" \
  | grep -iE "192\.168\.[0-9]+\.[0-9]+|token|password|secret|chat_id|api[_-]?key|bearer"
```

If it matches anything, **stop** — move the value into `config.env` (gitignored) and use a
placeholder in the example file. When sharing logs or screenshots in an issue, scrub local
IPs, tokens, and any frames that identify people or reveal a private location, per the
Public Issue Hygiene section of SECURITY.md.

---

## Summary

- Local-first: image/audio/biometric processing is on-device by default.
- Two opt-in egress paths (Telegram, cloud vision), both controllable and off for imagery
  by default.
- All PII and deployment data are gitignored; the repo ships only placeholder config.
- You own retention: rotate/purge the local data directory; there is no cloud copy.
