#!/usr/bin/env python3
"""
ESP32 kamera – lehký 24/7 stres test:
- Každých 30 s GET /health a /status
- Každých 60 s uložit jeden JPEG frame ze streamu
- Každou hodinu volitelně uložit krátký MJPEG chunk (10 s)
Loguje do rotujícího logu stress_test.log a ukládá artefakty do ./tools/artifacts.
"""

import json
import logging
import os
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

# Uprav podle své IP
ESP32_HOST = "192.168.1.100"
STREAM_URL = f"http://{ESP32_HOST}:81/stream"
HEALTH_URL = f"http://{ESP32_HOST}/health"
STATUS_URL = f"http://{ESP32_HOST}/status"

HEALTH_INTERVAL = 15        # s
STATUS_INTERVAL = 15        # s
SNAPSHOT_INTERVAL = 30      # s
VIDEO_INTERVAL = 1800       # s (pokud nechceš videa, nastav na 0)
VIDEO_DURATION = 10         # s délka MJPEG chunku

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
LOG_PATH = Path(__file__).parent / "stress_test.log"

# Telegram (volitelné): nastav env TELEGRAM_TOKEN a TELEGRAM_CHAT_ID
# Default test credentials (lab only); override via env for production.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_COOLDOWN = 300  # s mezi alerty, aby to nespamovalo


def setup_logging() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


def ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def fetch_json(url: str):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning("Request failed %s: %s", url, e)
        notify(f"ESP32 test: Request failed {url}: {e}")
        return None


def save_snapshot() -> None:
    try:
        with requests.get(STREAM_URL, stream=True, timeout=10) as r:
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")
            if "boundary=" not in content_type:
                logging.warning("Snapshot: boundary not found in Content-Type (%s)", content_type)
            data = b""
            for chunk in r.iter_content(chunk_size=2048):
                if not chunk:
                    continue
                data += chunk
                eoi = data.find(b"\xff\xd9")
                if eoi != -1:
                    jpeg = data[: eoi + 2]
                    out = ARTIFACT_DIR / f"snapshot_{ts()}.jpg"
                    out.write_bytes(jpeg)
                    logging.info("Snapshot saved: %s (%d bytes)", out, len(jpeg))
                    return
        logging.warning("Snapshot: did not find JPEG EOI marker")
        notify("ESP32 test: Snapshot failed (no JPEG EOI)")
    except Exception as e:
        logging.warning("Snapshot failed: %s", e)
        notify(f"ESP32 test: Snapshot failed: {e}")


def save_mjpeg_chunk(duration_s: int) -> None:
    start = time.time()
    out = ARTIFACT_DIR / f"mjpeg_{ts()}.mjpg"
    bytes_written = 0
    try:
        with requests.get(STREAM_URL, stream=True, timeout=10) as r, open(out, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                f.write(chunk)
                bytes_written += len(chunk)
                if time.time() - start >= duration_s:
                    break
        logging.info("MJPEG chunk saved: %s (%.1f KB)", out, bytes_written / 1024)
    except Exception as e:
        logging.warning("MJPEG chunk failed: %s", e)
        notify(f"ESP32 test: MJPEG chunk failed: {e}")
        if out.exists():
            out.unlink(missing_ok=True)


_last_notify = 0.0


def notify(text: str) -> None:
    """Odešle Telegram alert, pokud je nastaven token/ID a drží cooldown."""
    global _last_notify
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    now = time.time()
    if now - _last_notify < TELEGRAM_COOLDOWN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, timeout=10, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        if resp.ok:
            _last_notify = now
        else:
            logging.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logging.warning("Telegram error: %s", e)


def main() -> None:
    setup_logging()
    logging.info("Starting stress test against %s", ESP32_HOST)
    notify(f"ESP32 test: starting against {ESP32_HOST}")
    last_health = last_status = last_snap = last_video = 0.0

    while True:
        now = time.time()

        if now - last_health >= HEALTH_INTERVAL:
            health = fetch_json(HEALTH_URL)
            if health:
                logging.info("Health: %s", json.dumps(health))
            last_health = now

        if now - last_status >= STATUS_INTERVAL:
            status = fetch_json(STATUS_URL)
            if status:
                logging.info(
                    "Status: free_heap=%s rssi=%s clients=%s uptime=%s",
                    status.get("free_heap"),
                    status.get("wifi_rssi"),
                    status.get("clients"),
                    status.get("uptime"),
                )
            last_status = now

        if now - last_snap >= SNAPSHOT_INTERVAL:
            save_snapshot()
            last_snap = now

        if VIDEO_INTERVAL > 0 and now - last_video >= VIDEO_INTERVAL:
            save_mjpeg_chunk(VIDEO_DURATION)
            last_video = now

        time.sleep(1)


if __name__ == "__main__":
    main()
