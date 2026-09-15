#!/usr/bin/env bash
# Persistent serial console capture for the bench DFR1154 on USB.
#
# A panic reboots the chip, and because the console is the native USB-CDC
# (ARDUINO_USB_CDC_ON_BOOT=1) the device re-enumerates: /dev/ttyACM0 vanishes
# for a second and comes back. A plain `cat` dies there and loses exactly the
# boot that follows the backtrace, so reopen in a loop.
PORT="${1:-/dev/ttyACM0}"
OUT="${2:-$(dirname "$0")/../logs/bench_serial.log}"

while true; do
    if [ -e "$PORT" ]; then
        stty -F "$PORT" 115200 raw -echo -hupcl 2>/dev/null
        # ts prefixes each line with a timestamp; without moreutils fall back to cat.
        if command -v ts >/dev/null 2>&1; then
            cat "$PORT" | ts '%Y-%m-%d %H:%M:%S' >> "$OUT"
        else
            cat "$PORT" | while IFS= read -r line; do
                printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
            done >> "$OUT"
        fi
        printf '%s --- port closed, reopening ---\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$OUT"
    fi
    sleep 1
done
