# Support

Use GitHub Issues for reproducible bugs and feature requests. This project is a local self-hosted ESP32/DFR1154 camera system with an optional A12 Python companion, so useful reports need enough environment detail to reproduce the problem.

## Before Opening an Issue

For firmware issues:

```bash
cd firmware
pio run
pio device monitor
```

For A12 Docker issues:

```bash
cd a12_system
./tools/a12 doctor
./tools/a12 status
./tools/a12 logs 80
```

For camera connectivity:

```bash
cd a12_system
./tools/a12 test-camera
```

## What to Include

- Hardware model: DFR1154 or exact ESP32-CAM variant.
- Firmware version or git commit.
- Whether A12 is enabled.
- Whether MQTT/Home Assistant/Telegram are enabled.
- Short sanitized logs showing the failure.
- Steps to reproduce.

## What Not to Include

Do not post real tokens, passwords, Wi-Fi credentials, private IP addresses, Home Assistant long-lived tokens, Telegram bot tokens, MQTT credentials, `config.env`, `.env`, databases, face encodings, model files, or private camera screenshots.

Use placeholders like `<camera-ip>`, `<mqtt-ip>`, `<telegram-token>`, and `<redacted>`.
