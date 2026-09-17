#!/usr/bin/env bash
#
# One CSV row per minute: what the camera and A12 look like right now.
#
# Written for the soak after 2026-09-16's four changes (freeze-ladder wiring,
# audio_stats removal, firmware CI, inference latency) and for the two open
# measurements: the stall rate on a clean day (TODO 14) and how the flat-frame
# ladder behaves across a whole night.
set -uo pipefail

# Both are overridable; the defaults are the production camera's mDNS name and
# the data directory the container mounts. Point A12_DATA_DIR at the host copy
# when running this outside the container.
CAM="${CAM:-ESP32-Cam-Test.local}"
OUT="${OUT:-logs/soak.csv}"
INTERVAL="${INTERVAL:-60}"
# A12 only ever watches one camera per process, so the detection-side counters
# below belong to the production board alone. Set A12=0 for a board nothing is
# streaming from — otherwise its rows would carry another camera's numbers.
A12="${A12:-1}"

HEADER="ts,uptime_s,free_heap,max_alloc,restarts_1h,restarts_24h,profile,aec,agc,lux,mqtt_up,mqtt_connects,mqtt_fails,mqtt_link_s,rssi,heap_health,power_health"
if [ "$A12" = "1" ]; then
    HEADER="${HEADER},dark_1m,flat_1m,unwedge_1m,stall_1m,reboot_1m,audit_rows,timed_rows"
fi
if [ ! -s "$OUT" ]; then
    echo "$HEADER" >> "$OUT"
fi

waiting=0

while true; do
    now="$(date +%s)"

    status="$(curl -s --max-time 10 "http://${CAM}/status" || echo '{}')"
    health="$(curl -s --max-time 10 "http://${CAM}/health" || echo '{}')"

    # A board that is unplugged writes no rows at all. A file of empty rows
    # reads like a camera that answered with nothing, which is a different
    # fault and would be indistinguishable later.
    if ! printf '%s' "$status" | grep -q "free_heap"; then
        if [ "$waiting" -eq 0 ]; then
            echo "$(date '+%F %T') ${CAM} not answering — waiting, no rows written" >&2
            waiting=1
        fi
        sleep "$INTERVAL"
        continue
    fi
    if [ "$waiting" -eq 1 ]; then
        echo "$(date '+%F %T') ${CAM} is back — sampling" >&2
        waiting=0
    fi

    # One python pass over both documents; missing keys become empty fields
    # rather than shifting the columns.
    device="$(STATUS="$status" HEALTH="$health" python3 - <<'PY'
import json, os
def load(name):
    try:
        v = json.loads(os.environ.get(name) or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}
s, h = load("STATUS"), load("HEALTH")
keys = [
    (h, "uptime_seconds"), (s, "free_heap"), (s, "max_alloc_heap"),
    (h, "restarts_1h"), (h, "restarts_24h"),
    (s, "camera_profile"), (s, "aec"), (s, "agc"), (s, "ambient_light_lux"),
    (s, "mqtt_connected"), (s, "mqtt_connects"), (s, "mqtt_failures"),
    (s, "mqtt_link_uptime_s"), (h, "wifi_rssi"), (h, "heap_health"),
    (h, "power_health"),
]
print(",".join(str(d.get(k, "")).replace(",", ";") for d, k in keys))
PY
)"

    if [ "$A12" != "1" ]; then
        echo "${now},${device}" >> "$OUT"
        sleep "$INTERVAL"
        continue
    fi

    # What A12 did in the last minute. Counted from the log, because these are
    # the events that have no counter anywhere else.
    a12log="$(docker compose -p a12_system logs --since 1m a12 2>/dev/null | sed -e 's/\x1b\[[0-9;]*m//g')"
    # Two distinct log lines, and counting only one of them hid a whole night:
    # on 2026-09-16 flat_1m read 0 for 15 hours while 20 exposure rewrites fired,
    # because the watchdog was logging "Dark frame detected" the whole time.
    dark="$(printf '%s' "$a12log" | grep -c "Dark frame detected")"
    flat="$(printf '%s' "$a12log" | grep -c "Flat frame detected")"
    unwedge="$(printf '%s' "$a12log" | grep -c "rewriting AEC/AGC")"
    stall="$(printf '%s' "$a12log" | grep -cE "Stream frozen|stream_ended|Stream stall")"
    reboot="$(printf '%s' "$a12log" | grep -c "reboot")"

    rows="$(python3 - <<'PY'
import os, sqlite3
path = os.path.join(os.environ.get("A12_DATA_DIR", "/opt/a12-data"), "events.db")
try:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    total = c.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0]
    timed = c.execute(
        "SELECT COUNT(*) FROM decision_audit WHERE inference_seconds IS NOT NULL"
    ).fetchone()[0]
    print(f"{total},{timed}")
except Exception:
    print(",")
PY
)"

    echo "${now},${device},${dark},${flat},${unwedge},${stall},${reboot},${rows}" >> "$OUT"
    sleep "$INTERVAL"
done
