#!/usr/bin/env python3
"""
ESP32 Camera Photo Collector — stahuje snímky přímo z kamery přes HTTP.

Dva režimy:
  --interval N   Periodický snapshot každých N sekund
  --on-motion    Sleduje sériák, při motion/person detekci stáhne snímek

Usage:
  python3 tools/telegram_download.py                           # snapshot každých 60s
  python3 tools/telegram_download.py --interval 30             # každých 30s
  python3 tools/telegram_download.py --on-motion               # jen při detekci
  python3 tools/telegram_download.py --on-motion --interval 60 # obojí
  python3 tools/telegram_download.py --ip 192.168.1.200        # jiná IP

Dependencies: requests, pyserial (pro --on-motion)
"""

import argparse
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("Chybí requests: pip install requests")
    sys.exit(1)

# --- Config ---
CAMERA_IP = "192.168.1.100"
FRAME_ENDPOINT = "/frame"
DEFAULT_INTERVAL = 60  # seconds

# Colors
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Stats
stats = {"captured": 0, "errors": 0, "motion": 0, "person": 0, "bytes": 0}
running = True


def grab_frame(ip, out_dir, reason="periodic"):
    """Download a single JPEG frame from the camera."""
    url = f"http://{ip}{FRAME_ENDPOINT}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()

        if len(r.content) < 1000:
            stats["errors"] += 1
            print(f"{RED}  Frame too small ({len(r.content)}B) — skipped{RESET}")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{reason}_{ts}.jpg"
        filepath = os.path.join(out_dir, filename)

        with open(filepath, "wb") as f:
            f.write(r.content)

        stats["captured"] += 1
        stats["bytes"] += len(r.content)
        size_kb = len(r.content) // 1024
        short_ts = datetime.now().strftime("%H:%M:%S")
        print(f"{GREEN}[{short_ts}] #{stats['captured']} {filename} ({size_kb}KB) [{reason}]{RESET}")
        return filepath

    except requests.RequestException as e:
        stats["errors"] += 1
        print(f"{RED}  HTTP error: {e}{RESET}")
        return None


def periodic_capture(ip, out_dir, interval):
    """Periodically capture frames."""
    global running
    while running:
        grab_frame(ip, out_dir, "periodic")
        # Sleep in small chunks so we can stop quickly
        for _ in range(int(interval * 10)):
            if not running:
                return
            time.sleep(0.1)


def db_motion_capture(ip, out_dir, db_path, cooldown):
    """Watch telemetry SQLite DB for motion/person events and capture frames."""
    global running
    last_capture = 0
    last_motion_id = 0
    last_person_id = 0

    # Get current max IDs so we only react to NEW events
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute("SELECT MAX(id) FROM motion_events").fetchone()
        last_motion_id = row[0] or 0
        row = conn.execute("SELECT MAX(id) FROM person_events").fetchone()
        last_person_id = row[0] or 0
        conn.close()
    except sqlite3.Error:
        pass

    print(f"{CYAN}[DB] Watching {db_path} for motion/person events...{RESET}")
    print(f"{DIM}  Starting after motion#{last_motion_id}, person#{last_person_id}{RESET}")

    while running:
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row

            # Check for new motion events
            rows = conn.execute(
                "SELECT id, event_type, blocks_changed, motion_pct, brightness "
                "FROM motion_events WHERE id > ? AND event_type = 'detected' "
                "ORDER BY id", (last_motion_id,)
            ).fetchall()

            for row in rows:
                last_motion_id = row["id"]
                stats["motion"] += 1
                now = time.time()
                if now - last_capture >= cooldown:
                    last_capture = now
                    pct = row["motion_pct"] or 0
                    grab_frame(ip, out_dir, f"motion_{pct:.0f}pct")

            # Check for new person events
            rows = conn.execute(
                "SELECT id, event_type, confidence "
                "FROM person_events WHERE id > ? AND event_type = 'detected' "
                "ORDER BY id", (last_person_id,)
            ).fetchall()

            for row in rows:
                last_person_id = row["id"]
                stats["person"] += 1
                now = time.time()
                if now - last_capture >= cooldown:
                    last_capture = now
                    conf = row["confidence"] or 0
                    grab_frame(ip, out_dir, f"person_{conf:.0f}pct")

            conn.close()

        except sqlite3.Error as e:
            print(f"{YELLOW}[DB] Read error: {e}{RESET}")

        # Poll DB every 1s
        for _ in range(10):
            if not running:
                return
            time.sleep(0.1)


def print_stats_loop():
    """Print stats every 5 minutes."""
    global running
    while running:
        for _ in range(300):
            if not running:
                return
            time.sleep(1)
        mb = stats["bytes"] / (1024 * 1024)
        print(f"\n{BOLD}[STATS] Captured: {stats['captured']} ({mb:.1f}MB) | "
              f"Motion: {stats['motion']} | Person: {stats['person']} | "
              f"Errors: {stats['errors']}{RESET}\n")


def main():
    global running

    parser = argparse.ArgumentParser(
        description="ESP32 Camera Photo Collector — HTTP snapshots"
    )
    parser.add_argument("--ip", default=CAMERA_IP,
                        help=f"Camera IP (default: {CAMERA_IP})")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "camera_photos"),
        help="Output directory (default: tools/camera_photos/)")
    parser.add_argument("--interval", type=int, default=0,
                        help="Periodic capture interval in seconds (0=off)")
    parser.add_argument("--on-motion", action="store_true",
                        help="Capture on motion/person detection (reads telemetry DB)")
    parser.add_argument("--cooldown", type=int, default=5,
                        help="Min seconds between motion captures (default: 5)")
    parser.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "camera.db"),
                        help="Telemetry DB path for --on-motion")
    args = parser.parse_args()

    # Default: periodic 60s if nothing specified
    if not args.on_motion and args.interval == 0:
        args.interval = DEFAULT_INTERVAL

    os.makedirs(args.out, exist_ok=True)

    print(f"{BOLD}ESP32 Camera Photo Collector{RESET}")
    print(f"Camera: http://{args.ip}{FRAME_ENDPOINT}")
    print(f"Output: {args.out}")
    if args.interval:
        print(f"Periodic: every {args.interval}s")
    if args.on_motion:
        print(f"Motion:  via DB {args.db} (cooldown {args.cooldown}s)")
    print(f"Press Ctrl+C to stop\n")

    # Test camera connection
    print(f"Testing camera connection...", end=" ", flush=True)
    try:
        r = requests.get(f"http://{args.ip}{FRAME_ENDPOINT}", timeout=5)
        if r.status_code == 200 and len(r.content) > 1000:
            print(f"{GREEN}OK ({len(r.content)//1024}KB){RESET}\n")
        else:
            print(f"{YELLOW}Warning: status={r.status_code}, size={len(r.content)}{RESET}\n")
    except requests.RequestException as e:
        print(f"{RED}FAIL: {e}{RESET}")
        print(f"Zkontroluj IP kamery a zkus znovu.")
        sys.exit(1)

    def shutdown(signum=None, frame=None):
        global running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threads = []

    # Periodic capture thread
    if args.interval:
        t = threading.Thread(
            target=periodic_capture,
            args=(args.ip, args.out, args.interval),
            daemon=True
        )
        t.start()
        threads.append(t)

    # Motion capture thread (reads from telemetry DB)
    if args.on_motion:
        if not os.path.exists(args.db):
            print(f"{YELLOW}Warning: DB {args.db} not found — motion capture "
                  f"will start when telemetry creates it{RESET}")
        t = threading.Thread(
            target=db_motion_capture,
            args=(args.ip, args.out, args.db, args.cooldown),
            daemon=True
        )
        t.start()
        threads.append(t)

    # Stats thread
    t = threading.Thread(target=print_stats_loop, daemon=True)
    t.start()

    # Wait
    try:
        while running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        running = False

    # Final stats
    mb = stats["bytes"] / (1024 * 1024)
    print(f"\n{BOLD}Session ukončena.{RESET}")
    print(f"  Fotky:  {stats['captured']} ({mb:.1f}MB)")
    print(f"  Motion: {stats['motion']} | Person: {stats['person']}")
    print(f"  Chyby:  {stats['errors']}")
    print(f"  Složka: {args.out}")


if __name__ == "__main__":
    main()
