#!/usr/bin/env bash
#
# Build the ESP32 firmware, retrying once and ONLY for a GCC internal compiler
# error.
#
# Full rebuilds — which a changed -DFIRMWARE_VERSION forces — intermittently hit
# `internal compiler error: in ggc_set_mark, at ggc-page.cc:1551` inside the Edge
# Impulse SDK. It was seen three times on 2026-09-15, every time green on an
# immediate retry with no change. A blanket `retry twice` would also paper over
# a genuine compile error, so the retry is gated on that string: anything else
# fails on the first attempt, at full speed.
#
# PIO is overridable so the retry logic itself can be exercised without a
# 10-minute toolchain download.
set -uo pipefail

ENV_NAME="${1:-esp32-s3-devkitc-1}"
PIO="${PIO:-pio}"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT

for attempt in 1 2; do
    if "$PIO" run -e "$ENV_NAME" 2>&1 | tee "$log"; then
        exit 0
    fi

    if ! grep -q "internal compiler error" "$log"; then
        echo "::error::Firmware build failed on attempt ${attempt} — not a compiler ICE, not retrying."
        exit 1
    fi

    echo "::warning::GCC internal compiler error on attempt ${attempt}."
    if [ "$attempt" -eq 1 ]; then
        echo "Cleaning and retrying once."
        "$PIO" run -e "$ENV_NAME" -t clean >/dev/null 2>&1 || true
    fi
done

echo "::error::Firmware build hit an internal compiler error twice in a row."
exit 1
