#!/usr/bin/env python3
"""
ESP32-S3 Camera Monitor v2.0 — Serial logger + HTTP health checker

Usage:
  python3 tools/esp_monitor.py                    # auto-detect IP from serial
  python3 tools/esp_monitor.py --ip 192.168.x.x   # manual IP
  python3 tools/esp_monitor.py --port /dev/ttyUSB0 # different serial port
  python3 tools/esp_monitor.py --filter motion     # only show lines matching 'motion'
  python3 tools/esp_monitor.py --no-dedup          # disable duplicate suppression

Logs to: tools/esp_monitor.log (rotated at 10MB)
"""

import serial
import threading
import time
import sys
import argparse
import re
import json
import os
from datetime import datetime

# Optional: HTTP health checks
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# --- Config ---
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esp_monitor.log")
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB rotation
HEALTH_INTERVAL = 30  # seconds between HTTP checks
HTTP_TIMEOUT = 5

# --- Colors ---
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Error patterns to highlight
ERROR_PATTERNS = [
    (re.compile(r"(E \(\d+\).*)"), RED),
    (re.compile(r"(❌.*)"), RED),
    (re.compile(r"(⚠️.*)"), YELLOW),
    (re.compile(r"(CRITICAL|FATAL|panic|abort|Guru Meditation)", re.I), RED + BOLD),
    (re.compile(r"(✅.*)"), GREEN),
    (re.compile(r"(📷.*)"), CYAN),
    (re.compile(r"(💾.*)"), CYAN),
    (re.compile(r"(📱.*)"), CYAN),
    (re.compile(r"(🧠.*)"), CYAN),
    (re.compile(r"(🔍.*)"), CYAN),
    (re.compile(r"(Motion DETECTED.*)"), YELLOW + BOLD),
    (re.compile(r"(Person detected.*)"), GREEN + BOLD),
    (re.compile(r"(AVI recording.*)"), CYAN + BOLD),
]

# Stats counters
stats = {
    "lines": 0,
    "errors": 0,
    "warnings": 0,
    "boot_count": 0,
    "last_ip": None,
    "last_heap": None,
    "last_psram": None,
    "min_heap": None,
    "heap_history": [],  # last N heap values for trend
    "wdt_errors": 0,
    "telegram_ok": 0,
    "telegram_fail": 0,
    "motion_events": 0,
    "person_events": 0,
    "camera_fails": 0,
    "ringbuf_busy": 0,
    "avi_recordings": 0,
    "profile_changes": 0,
    "panics": 0,
    "start_time": time.time(),
}

# Duplicate line suppression state
dedup = {
    "last_line": None,
    "repeat_count": 0,
}

# Runtime options (set from args in main)
options = {
    "filter": None,
    "dedup_enabled": True,
}


def colorize(line):
    """Apply color to known patterns."""
    for pattern, color in ERROR_PATTERNS:
        if pattern.search(line):
            return color + line + RESET
    return line


def parse_line(line):
    """Extract stats from serial line."""
    if "Camera Ready" in line or "Camera initialized" in line:
        stats["boot_count"] += 1
    if "E (" in line:
        stats["errors"] += 1
    if "task_wdt" in line:
        stats["wdt_errors"] += 1
    if "⚠️" in line:
        stats["warnings"] += 1
    if "Telegram photo sent" in line or "Telegram document sent" in line:
        stats["telegram_ok"] += 1
    if "Telegram" in line and ("failed" in line or "error" in line or "timeout" in line):
        stats["telegram_fail"] += 1
    if "Motion DETECTED" in line or "Detekovany pohyb" in line:
        stats["motion_events"] += 1
    if "Person detected" in line:
        stats["person_events"] += 1
    if "Camera capture failed" in line:
        stats["camera_fails"] += 1
    if "frame buffers busy" in line:
        stats["ringbuf_busy"] += 1
    if "AVI recording started" in line:
        stats["avi_recordings"] += 1
    if "Camera profile:" in line:
        stats["profile_changes"] += 1
    if "Guru Meditation" in line or "panic" in line.lower():
        stats["panics"] += 1

    # Extract IP
    m = re.search(r"http://(\d+\.\d+\.\d+\.\d+)", line)
    if m:
        stats["last_ip"] = m.group(1)

    # Extract heap/psram from serial
    m = re.search(r"Heap:\s*(\d+)\s*bytes", line)
    if m:
        heap_val = int(m.group(1))
        stats["last_heap"] = heap_val
        if stats["min_heap"] is None or heap_val < stats["min_heap"]:
            stats["min_heap"] = heap_val
    m = re.search(r"PSRAM:\s*(\d+)\s*bytes", line)
    if m:
        stats["last_psram"] = int(m.group(1))


def rotate_log():
    """Rotate log if too big."""
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
        backup = LOG_FILE + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(LOG_FILE, backup)


def flush_dedup(ts):
    """Print suppressed duplicate count if any."""
    if dedup["repeat_count"] > 0:
        print(f"{DIM}[{ts}]{RESET} {YELLOW}  ... repeated {dedup['repeat_count']}x (suppressed){RESET}")
        dedup["repeat_count"] = 0


def serial_reader(port, baud, log_fp):
    """Read serial port and log + display."""
    while True:
        try:
            ser = serial.Serial(port, baud, timeout=1)
            print(f"{GREEN}[MONITOR] Connected to {port} @ {baud}{RESET}")
            while True:
                try:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    stats["lines"] += 1
                    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                    # Always log to file (no dedup in file)
                    log_fp.write(f"[{ts}] {line}\n")
                    log_fp.flush()

                    # Parse stats (always, even if display is suppressed)
                    parse_line(line)

                    # Duplicate suppression for display
                    if options["dedup_enabled"]:
                        # Strip timestamp-like prefix for comparison
                        stripped = re.sub(r"^\[\d+:\d+:\d+\.\d+\]\s*", "", line)
                        if stripped == dedup["last_line"]:
                            dedup["repeat_count"] += 1
                            # Rotate log periodically
                            if stats["lines"] % 1000 == 0:
                                rotate_log()
                            continue
                        else:
                            flush_dedup(ts)
                            dedup["last_line"] = stripped

                    # Filter display
                    if options["filter"]:
                        if options["filter"].lower() not in line.lower():
                            if stats["lines"] % 1000 == 0:
                                rotate_log()
                            continue

                    # Display with color
                    colored = colorize(line)
                    print(f"{DIM}[{ts}]{RESET} {colored}")

                    # Rotate log periodically
                    if stats["lines"] % 1000 == 0:
                        rotate_log()

                except serial.SerialException:
                    print(f"{RED}[MONITOR] Serial disconnected{RESET}")
                    break
                except UnicodeDecodeError:
                    continue

        except serial.SerialException as e:
            print(f"{YELLOW}[MONITOR] Waiting for {port}... ({e}){RESET}")
            time.sleep(2)
        except KeyboardInterrupt:
            return


def http_health_check(ip):
    """Periodic HTTP health check."""
    if not HAS_REQUESTS:
        return

    base = f"http://{ip}"
    checks = [
        ("/telemetry", "Telemetry"),
        ("/ir-status", "IR/Lux"),
    ]

    while True:
        time.sleep(HEALTH_INTERVAL)
        if not ip and stats["last_ip"]:
            ip = stats["last_ip"]
        if not ip:
            continue

        base = f"http://{ip}"
        results = []

        for endpoint, name in checks:
            try:
                r = requests.get(base + endpoint, timeout=HTTP_TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    results.append((name, data))
                else:
                    print(f"{RED}[HTTP] {name}: {r.status_code}{RESET}")
            except requests.exceptions.RequestException as e:
                print(f"{RED}[HTTP] {name}: {e}{RESET}")

        # Display summary
        if results:
            ts = datetime.now().strftime("%H:%M:%S")
            parts = []
            for name, data in results:
                if name == "Telemetry":
                    heap = data.get("free_heap", 0)
                    psram = data.get("free_psram", 0)
                    rssi = data.get("wifi_rssi", 0)
                    uptime = data.get("uptime_seconds", 0)
                    h, m = divmod(uptime // 60, 60)
                    stats["last_heap"] = heap
                    stats["last_psram"] = psram
                    if stats["min_heap"] is None or heap < stats["min_heap"]:
                        stats["min_heap"] = heap
                    # Keep last 10 readings for trend
                    stats["heap_history"].append(heap)
                    if len(stats["heap_history"]) > 10:
                        stats["heap_history"] = stats["heap_history"][-10:]
                    trend = heap_trend_arrow()
                    parts.append(f"heap={heap//1024}KB{trend} psram={psram//1024}KB rssi={rssi}dBm up={h}h{m}m")
                elif name == "IR/Lux":
                    lux = data.get("ambient_light_lux", -1)
                    ir = data.get("ir_led_state", False)
                    parts.append(f"lux={lux:.0f} ir={'ON' if ir else 'OFF'}")

            summary = " | ".join(parts)
            print(f"{CYAN}[HEALTH {ts}] {summary}{RESET}")

        # Also try a snapshot to verify camera works
        try:
            r = requests.get(base + "/capture", timeout=HTTP_TIMEOUT, stream=True)
            jpg_size = len(r.content)
            if r.status_code == 200 and jpg_size > 1000:
                print(f"{GREEN}[HEALTH] Snapshot OK: {jpg_size//1024}KB{RESET}")
            else:
                print(f"{RED}[HEALTH] Snapshot FAIL: status={r.status_code} size={jpg_size}{RESET}")
        except requests.exceptions.RequestException as e:
            print(f"{RED}[HEALTH] Snapshot: {e}{RESET}")


def heap_trend_arrow():
    """Return trend arrow based on recent heap history."""
    history = stats["heap_history"]
    if len(history) < 2:
        return ""
    diff = history[-1] - history[0]
    if diff < -10240:  # dropping >10KB
        return RED + " ↓↓" + RESET
    elif diff < -2048:
        return YELLOW + " ↓" + RESET
    elif diff > 2048:
        return GREEN + " ↑" + RESET
    return " →"


def print_stats():
    """Print periodic stats summary."""
    while True:
        time.sleep(60)
        elapsed = time.time() - stats["start_time"]
        mins = int(elapsed // 60)
        print(f"\n{BOLD}{'='*65}")
        print(f"[STATS] After {mins} min monitoring:")
        print(f"  Lines: {stats['lines']} | Errors: {stats['errors']} | Warnings: {stats['warnings']}")
        print(f"  WDT: {stats['wdt_errors']} | Panics: {stats['panics']} | CamFail: {stats['camera_fails']} | RingBufBusy: {stats['ringbuf_busy']}")
        print(f"  Telegram OK: {stats['telegram_ok']} | FAIL: {stats['telegram_fail']}")
        print(f"  Motion: {stats['motion_events']} | Person: {stats['person_events']} | AVI: {stats['avi_recordings']} | Boots: {stats['boot_count']}")
        if stats["last_heap"]:
            trend = heap_trend_arrow()
            min_heap = stats["min_heap"] or 0
            print(f"  Heap: {stats['last_heap']//1024}KB{trend} (min: {min_heap//1024}KB) | PSRAM: {(stats['last_psram'] or 0)//1024}KB")
        print(f"{'='*65}{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="ESP32 Camera Monitor")
    parser.add_argument("--port", default=SERIAL_PORT, help="Serial port")
    parser.add_argument("--baud", type=int, default=BAUD_RATE, help="Baud rate")
    parser.add_argument("--ip", default=None, help="ESP32 IP (auto-detect from serial if not set)")
    parser.add_argument("--filter", default=None, help="Only display lines containing this keyword")
    parser.add_argument("--no-dedup", action="store_true", help="Disable duplicate line suppression")
    args = parser.parse_args()

    options["filter"] = args.filter
    options["dedup_enabled"] = not args.no_dedup

    print(f"{BOLD}ESP32-S3 Camera Monitor v2.0{RESET}")
    print(f"Serial: {args.port} @ {args.baud}")
    print(f"Log: {LOG_FILE}")
    print(f"IP: {args.ip or 'auto-detect'}")
    if args.filter:
        print(f"Filter: '{args.filter}'")
    if args.no_dedup:
        print(f"Dedup: disabled")
    if not HAS_REQUESTS:
        print(f"{YELLOW}Note: install 'requests' for HTTP health checks{RESET}")
    print(f"Press Ctrl+C to stop\n")

    # Open log file
    rotate_log()
    log_fp = open(LOG_FILE, "a")
    log_fp.write(f"\n{'='*60}\n")
    log_fp.write(f"Monitor started: {datetime.now().isoformat()}\n")
    log_fp.write(f"{'='*60}\n")

    # Start threads
    threads = []

    # Serial reader (main work)
    t = threading.Thread(target=serial_reader, args=(args.port, args.baud, log_fp), daemon=True)
    t.start()
    threads.append(t)

    # HTTP health checker
    if HAS_REQUESTS:
        t = threading.Thread(target=http_health_check, args=(args.ip,), daemon=True)
        t.start()
        threads.append(t)

    # Stats printer
    t = threading.Thread(target=print_stats, daemon=True)
    t.start()
    threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        elapsed = time.time() - stats["start_time"]
        print(f"\n{BOLD}Monitor stopped after {int(elapsed)}s. Log: {LOG_FILE}{RESET}")
        log_fp.close()


if __name__ == "__main__":
    main()
