# Public Release Scan

Use this checklist before pushing public changes from the firmware/A12 repository.
It is written for Codex, Claude, or a human reviewer doing a fast release audit.

## Scope

This repository may contain three different kinds of data:

- ESP32 firmware source and public documentation, which are safe to publish.
- A12 companion source and example configs, which are safe only with placeholders.
- Runtime data, screenshots, local configs, face images, logs, databases and model files,
  which must stay out of git.

The paired Tapo monitor repository is separate:
<https://github.com/PeterkoCZ91/tapo-monitoring>. This repo may link to it, but should not
copy Tapo runtime logs, camera screenshots, LAN addresses, or deployment secrets.

## Required Checks

Run from the repository root:

```bash
git status --short --branch
git diff --check
python3 a12_system/test_config.py
```

If firmware code changed and PlatformIO is available, also run:

```bash
pio run -d firmware
```

## Secret and PII Scan

This scan intentionally excludes `.git`, vendored Edge Impulse sources, binary media and
known placeholder examples. Review every remaining hit manually.

```bash
rg -n \
  "(bot[0-9]+:|gsk_|sk-[A-Za-z0-9]|Bearer +[A-Za-z0-9._-]+|chat_id|api[_-]?key|token|password|secret|known_faces|events\.db|a12\.log)" \
  -g '!**/.git/**' \
  -g '!firmware/lib/ei-person-detection-fomo/**' \
  -g '!**/*.png' -g '!**/*.jpg' -g '!**/*.jpeg' -g '!**/*.webp' \
  .
```

Network examples such as `192.168.1.100`, `192.168.4.1`, and `192.168.x.x` are allowed
only when they are clearly generic examples or firmware default setup addresses. Replace
real deployment addresses with `<camera-ip>`, `<mqtt-ip>`, `<ha-url>`, or TEST-NET examples
such as `192.0.2.10`.

## Must Not Be Committed

Reject the commit if any of these appear as tracked files:

- `.env`, `config.env`, Home Assistant tokens, Telegram tokens, MQTT passwords.
- `known_faces/`, face encodings, private snapshots, clips, SD-card captures.
- `events.db`, logs, runtime databases, local deployment notes.
- YOLO or other model weights: `*.onnx`, `*.pt`, `*.weights`, `*.pkl`.
- Local-only handoff files such as `LOCAL_*.md` or `*_LOCAL.md`.

Useful commands:

```bash
git ls-files | rg "(^|/)(config\.env|\.env|known_faces|events\.db|a12\.log|LOCAL_|_LOCAL\.md|.*\.(onnx|pt|weights|pkl))$"
git ls-files | rg "\.(png|jpg|jpeg|webp|mp4|avi)$"
```

Media files are not automatically forbidden because documentation screenshots can be valid,
but every hit must be intentionally public and non-identifying.

## Documentation Expectations

Before pushing, confirm public docs explain:

- Standalone firmware vs Enhanced A12 mode.
- How A12 connects to the paired Tapo shared scorer over HTTP.
- That A12 and Tapo keep separate thresholds and alert rules.
- How to disable ESP32 Telegram when A12 owns notifications.
- Where runtime configuration lives and why it is gitignored.

The main A12 runtime entry point is [`A12_COMPANION.md`](A12_COMPANION.md). Privacy and
retention details are in [`DATA_PRIVACY.md`](DATA_PRIVACY.md).
