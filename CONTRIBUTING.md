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
bash tools/setup.sh

# Install dependencies
pip install -r requirements.txt

# Run config tests
python3 test_config.py

# Start via Docker (recommended)
docker compose -p a12_system_v2 build
docker compose -p a12_system_v2 up -d

# Or use the CLI wrapper
bash a12 status
bash a12 logs
```

## Pull Request Guidelines

1. Fork → feature branch (`git checkout -b fix/stream-reconnect`)
2. Keep changes focused — one fix or feature per PR
3. Firmware: run `pio run` and confirm it compiles cleanly
4. A12: run `python3 a12_system/test_config.py` before submitting
5. Update `CHANGELOG.md` under the relevant section
6. Open PR with a clear description of what and why

## What We're Looking For

- Firmware stability fixes (heap, watchdog, camera init)
- A12 detection accuracy improvements
- Home Assistant / MQTT integration improvements
- New camera module support
- Documentation improvements

## What to Avoid

- Don't commit `config.env`, tokens, passwords, face encodings, or model weights
- Don't add cloud dependencies — this project is intentionally self-hosted
- Don't break the Standalone mode when changing A12 mode features

## Code Style

- **C++ (firmware):** follow existing style — 4-space indent, descriptive variable names, Serial.printf for debug output
- **Python (A12):** PEP 8, type hints where practical, no bare `except:`

## License

By contributing you agree that your contributions will be licensed under the [MIT License](LICENSE).
