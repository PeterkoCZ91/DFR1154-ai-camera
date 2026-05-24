# Contributing to DFR1154 AI Camera

Thanks for your interest in contributing! This project covers ESP32-S3 firmware (PlatformIO/C++) and a Python companion system (A12). Both areas welcome contributions.

## Ways to Contribute

- **Bug reports** — open a [GitHub Issue](../../issues/new?template=bug_report.md)
- **Feature requests** — open a [GitHub Issue](../../issues/new?template=feature_request.md)
- **Pull requests** — firmware fixes, A12 improvements, documentation

## Before You Start

- Check [open issues](../../issues) and [open PRs](../../pulls) to avoid duplicates
- For large changes, open an issue first to discuss the approach
- Read [`a12_system/DOCKER.md`](a12_system/DOCKER.md) before working on A12
- Read [`firmware/MIGRATION_ESP_DL.md`](firmware/MIGRATION_ESP_DL.md) before touching detection code

## Development Setup

### Firmware (ESP32-S3)

```bash
# Install PlatformIO
pip install platformio

# Build
cd firmware
pio run

# Flash (USB)
pio run --target upload
```

Key files: `firmware/camera_server.cpp`, `firmware/config.h`, `firmware/board_config.h`

### A12 Companion (Python)

```bash
cd a12_system

# First-time setup (creates data dir, copies config)
./tools/a12 setup

# Edit runtime config outside git
nano /opt/a12-data/config.env

# Run diagnostics and tests
./tools/a12 doctor
python3 test_config.py

# Start via Docker wrapper
./tools/a12 build
./tools/a12 up
./tools/a12 logs 80
```

## Public Repository Hygiene

Before opening an issue or PR, sanitize logs and examples. Use placeholders such as `<camera-ip>`, `<mqtt-ip>`, `<telegram-token>`, and `<redacted>`. Runtime files belong outside git; see `.gitignore`, `a12_system/.gitignore`, and `SECURITY.md`.

## Pull Request Guidelines

1. Fork → feature branch (`git checkout -b fix/stream-reconnect`)
2. Keep changes focused — one fix or feature per PR
3. Firmware: run `pio run` and confirm it compiles cleanly
4. A12: run `python3 a12_system/test_config.py` and review `cd a12_system && ./tools/a12 doctor` before submitting
5. Update `CHANGELOG.md` under the relevant section
6. Open PR with a clear description of what and why

## What We're Looking For

- Firmware stability fixes (heap, watchdog, camera init)
- A12 detection accuracy improvements
- Home Assistant / MQTT integration improvements
- New camera module support
- Documentation improvements

## What to Avoid

- Don't commit `config.env`, `.env`, tokens, passwords, Wi-Fi credentials, Home Assistant tokens, MQTT credentials, Telegram bot tokens, face encodings, logs, databases, private camera screenshots, or model weights
- Don't paste private IP addresses or secrets into public issues, PRs, screenshots, or documentation examples
- Don't add cloud dependencies — this project is intentionally self-hosted
- Don't break the Standalone mode when changing A12 mode features

## Code Style

- **C++ (firmware):** follow existing style — 4-space indent, descriptive variable names, Serial.printf for debug output
- **Python (A12):** PEP 8, type hints where practical, no bare `except:`

## License

By contributing you agree that your contributions will be licensed under the [MIT License](LICENSE).
