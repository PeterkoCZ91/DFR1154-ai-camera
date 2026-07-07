# Security Policy

This project is intended for self-hosted local-network camera and automation setups.
Do not expose the ESP32 camera dashboard, MJPEG streams, RTSP endpoint, MQTT broker, Home Assistant token, Telegram bot token, or A12 runtime data directory directly to the public internet.

For what data the system handles, what can leave your LAN, and how to run it fully offline, see [docs/DATA_PRIVACY.md](docs/DATA_PRIVACY.md).

## Reporting a Vulnerability

Please do not open a public issue that contains credentials, private IP addresses, Telegram tokens, MQTT passwords, Home Assistant tokens, Wi-Fi credentials, face encodings, database dumps, or camera screenshots from a private location.

If you find a vulnerability, open a minimal GitHub issue that describes the affected component and impact without secrets or private deployment details. If GitHub private vulnerability reporting is enabled for this repository, use that for sensitive reports.

## If a Secret Was Exposed

If a token, password, or credential was committed or posted publicly:

1. Revoke or rotate the affected secret first.
2. Remove the secret from the current files.
3. Check whether it exists in git history before publishing or pushing further.
4. If it was already pushed to GitHub, follow GitHub's sensitive-data removal guidance and coordinate history cleanup before relying on deletion alone.

## Public Issue Hygiene

Before posting logs or screenshots:

- Replace local IPs with placeholders such as `<camera-ip>` or `<mqtt-ip>`.
- Replace tokens/passwords with `<redacted>`.
- Do not attach `config.env`, `.env`, `events.db`, `a12.log`, `known_faces.pkl`, screenshots from private locations, or model files.
- Prefer short, relevant log excerpts over full runtime dumps.

## Supported Scope

Security fixes are accepted for the current main branch. Older local deployments should be updated manually after reviewing the changelog and configuration changes.
