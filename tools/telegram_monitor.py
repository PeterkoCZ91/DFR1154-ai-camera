#!/usr/bin/env python3
"""
ESP32 Telegram Monitor — sleduje sériový výstup a loguje detekce + Telegram zprávy.
Použití: python3 telegram_monitor.py [--port /dev/ttyACM0] [--baud 115200] [--log detections.log]
"""

import serial
import time
import argparse
import sys
from datetime import datetime

KEYWORDS = [
    'Motion DETECTED',
    'Person detected',
    'No person',
    'inference',
    'Telegram',
    'telegram',
    'CORRUPT',
    'panic',
    'Backtrace',
    'photo sent',
    'notifikac',
]

def matches(line):
    return any(k in line for k in KEYWORDS)

def main():
    parser = argparse.ArgumentParser(description='ESP32 Telegram/Detection Monitor')
    parser.add_argument('--port', default='/dev/ttyACM0', help='Serial port')
    parser.add_argument('--baud', type=int, default=115200, help='Baud rate')
    parser.add_argument('--log', default=None, help='Log file (append)')
    parser.add_argument('--all', action='store_true', help='Show all serial output, not just detections')
    args = parser.parse_args()

    logfile = open(args.log, 'a') if args.log else None

    print(f"[*] Connecting to {args.port} @ {args.baud}...")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        print(f"[!] Cannot open {args.port}: {e}", file=sys.stderr)
        sys.exit(1)

    ser.read(ser.in_waiting or 1)  # flush
    print(f"[*] Monitoring started. Ctrl+C to stop.\n")

    motion_count = 0
    person_count = 0
    false_positive = 0
    start_time = time.time()

    try:
        buf = ''
        while True:
            data = ser.read(ser.in_waiting or 256)
            if data:
                buf += data.decode('utf-8', errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue

                    show = args.all or matches(line)

                    # Track stats
                    if 'Motion DETECTED' in line:
                        motion_count += 1
                    if 'Person detected' in line:
                        person_count += 1
                    if 'No person' in line:
                        false_positive += 1

                    if show:
                        ts = datetime.now().strftime('%H:%M:%S')
                        elapsed = time.time() - start_time

                        # Color coding
                        prefix = ''
                        if 'Motion DETECTED' in line:
                            prefix = '\033[33m[MOTION]\033[0m '
                        elif 'Person detected' in line:
                            prefix = '\033[32m[PERSON]\033[0m '
                        elif 'No person' in line:
                            prefix = '\033[31m[FALSE+]\033[0m '
                        elif 'photo sent' in line:
                            prefix = '\033[36m[PHOTO] \033[0m '
                        elif 'Telegram' in line or 'telegram' in line or 'notifikac' in line:
                            prefix = '\033[35m[TG]    \033[0m '
                        elif 'CORRUPT' in line or 'panic' in line:
                            prefix = '\033[91m[CRASH] \033[0m '

                        out = f"{ts} {prefix}{line}"
                        print(out)

                        if logfile:
                            logfile.write(f"{ts} {line}\n")
                            logfile.flush()

            time.sleep(0.1)

    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        mins = elapsed / 60
        print(f"\n{'='*50}")
        print(f"  Session: {mins:.1f} min")
        print(f"  Motion events:    {motion_count}")
        print(f"  Person confirmed: {person_count}")
        print(f"  False positives:  {false_positive}")
        if motion_count > 0:
            fp_rate = false_positive / motion_count * 100
            print(f"  FP rate:          {fp_rate:.1f}%")
        print(f"{'='*50}")
    finally:
        ser.close()
        if logfile:
            logfile.close()

if __name__ == '__main__':
    main()
