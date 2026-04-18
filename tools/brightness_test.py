#!/usr/bin/env python3
"""
ESP32 OV3660 Brightness Test Tool

Stáhne frame(y) z kamery, analyzuje jas a porovná s baseline.
Výsledky loguje do CSV pro sledování vývoje.

Použití:
  python3 tools/brightness_test.py                    # 1 snímek, výpis
  python3 tools/brightness_test.py -n 5               # průměr z 5 snímků
  python3 tools/brightness_test.py -n 5 --delay 2     # 5 snímků, 2s mezera
  python3 tools/brightness_test.py --save              # uloží frame do tools/brightness_frames/
  python3 tools/brightness_test.py --ip 192.168.1.100  # jiná IP
  python3 tools/brightness_test.py --history           # zobrazí historii měření
  python3 tools/brightness_test.py --baseline 73       # porovná s custom baseline
"""

import argparse
import csv
import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("CHYBA: pip install requests")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("CHYBA: pip install Pillow")
    sys.exit(1)

# --- Konfigurace ---
DEFAULT_IP = "192.168.1.100"
FRAME_ENDPOINT = "/frame"
STATUS_ENDPOINT = "/status"
REG_DEBUG_ENDPOINT = "/reg-debug"
HTTP_TIMEOUT = 10

# Baseline hodnoty pro porovnání
BASELINE_AVG = 59.0   # v3.9.0 při 23 lux
PREVIOUS_BEST = 73.2   # v3.9.1 při 23 lux

# Cesty
SCRIPT_DIR = Path(__file__).parent
FRAMES_DIR = SCRIPT_DIR / "brightness_frames"
CSV_LOG = SCRIPT_DIR / "brightness_log.csv"


def fetch_status(base_url):
    """Stáhne /status a vrátí dict."""
    try:
        r = requests.get(f"{base_url}{STATUS_ENDPOINT}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Varování: /status nedostupný: {e}")
        return {}


def fetch_reg_debug(base_url):
    """Stáhne /reg-debug a vrátí dict."""
    try:
        r = requests.get(f"{base_url}{REG_DEBUG_ENDPOINT}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def fetch_frame(base_url):
    """Stáhne JPEG frame, vrátí (bytes, doba_ms)."""
    start = time.time()
    r = requests.get(f"{base_url}{FRAME_ENDPOINT}", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    elapsed = (time.time() - start) * 1000
    if len(r.content) < 1000:
        raise ValueError(f"Frame příliš malý: {len(r.content)} bytes")
    return r.content, elapsed


def analyze_brightness(jpeg_data):
    """Analyzuje jas JPEG dat, vrátí dict s metrikami."""
    img = Image.open(io.BytesIO(jpeg_data))
    gray = img.convert("L")
    pixels = list(gray.getdata())
    w, h = img.size

    sorted_p = sorted(pixels)
    n = len(sorted_p)

    return {
        "width": w,
        "height": h,
        "avg": sum(pixels) / n,
        "median": sorted_p[n // 2],
        "p10": sorted_p[n // 10],
        "p90": sorted_p[9 * n // 10],
        "min": min(pixels),
        "max": max(pixels),
        "size_bytes": len(jpeg_data),
    }


def print_result(result, status, baseline, sample_num=None):
    """Vytiskne výsledek analýzy."""
    prefix = f"  Vzorek {sample_num}: " if sample_num else "  "
    avg = result["avg"]
    delta_base = avg - baseline
    delta_pct = (delta_base / baseline) * 100 if baseline > 0 else 0

    sign = "+" if delta_base >= 0 else ""
    print(f"{prefix}avg={avg:.1f}/255  P10={result['p10']}  P50={result['median']}  "
          f"P90={result['p90']}  [{sign}{delta_base:.1f} vs baseline, {sign}{delta_pct:.0f}%]  "
          f"({result['size_bytes'] // 1024}KB)")


def log_to_csv(avg, lux, version, brightness, contrast, ae_level, note=""):
    """Zapíše řádek do CSV logu."""
    CSV_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_LOG.exists()
    with open(CSV_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "avg_brightness", "lux", "version",
                             "brightness", "contrast", "ae_level", "note"])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"{avg:.1f}", f"{lux:.1f}" if lux else "",
            version, brightness, contrast, ae_level, note
        ])


def show_history():
    """Zobrazí historii měření z CSV."""
    if not CSV_LOG.exists():
        print("Žádná historie měření.")
        return

    print(f"\nHistorie měření ({CSV_LOG}):")
    print(f"{'Čas':<20} {'Avg':>6} {'Lux':>6} {'Verze':<35} {'B':>3} {'C':>3} {'AE':>3}  Pozn.")
    print("-" * 100)

    with open(CSV_LOG) as f:
        reader = csv.DictReader(f)
        for row in reader:
            print(f"{row['timestamp']:<20} {row['avg_brightness']:>6} {row.get('lux', ''):>6} "
                  f"{row.get('version', ''):.<35} {row.get('brightness', ''):>3} "
                  f"{row.get('contrast', ''):>3} {row.get('ae_level', ''):>3}  "
                  f"{row.get('note', '')}")


def main():
    parser = argparse.ArgumentParser(description="ESP32 OV3660 Brightness Test")
    parser.add_argument("--ip", default=DEFAULT_IP, help=f"IP kamery (default: {DEFAULT_IP})")
    parser.add_argument("-n", "--samples", type=int, default=1, help="Počet snímků k průměrování")
    parser.add_argument("--delay", type=float, default=1.0, help="Prodleva mezi snímky (s)")
    parser.add_argument("--save", action="store_true", help="Uložit frame(y) do tools/brightness_frames/")
    parser.add_argument("--baseline", type=float, default=BASELINE_AVG, help=f"Baseline avg (default: {BASELINE_AVG})")
    parser.add_argument("--history", action="store_true", help="Zobrazit historii měření")
    parser.add_argument("--note", default="", help="Poznámka k měření (uloží se do CSV)")
    parser.add_argument("--reg", action="store_true", help="Zobrazit /reg-debug registrový dump")
    args = parser.parse_args()

    if args.history:
        show_history()
        return

    base_url = f"http://{args.ip}"
    print(f"ESP32 OV3660 Brightness Test")
    print(f"Kamera: {args.ip}  |  Snímků: {args.samples}  |  Baseline: {args.baseline}")
    print()

    # Stáhni status
    print("Stav kamery:")
    status = fetch_status(base_url)
    if status:
        lux = status.get("ambient_light_lux", 0)
        version = status.get("version", "?")
        bri = status.get("brightness", "?")
        con = status.get("contrast", "?")
        ae = status.get("ae_level", "?")
        sat = status.get("saturation", "?")
        quality = status.get("jpeg_quality", "?")
        heap = status.get("free_heap", 0)
        psram = status.get("free_psram", 0)
        uptime = status.get("uptime", "?")
        rssi = status.get("wifi_rssi", "?")

        print(f"  Verze: {version}")
        print(f"  Lux: {lux:.1f}  |  Brightness: {bri}  |  Contrast: {con}  |  AE level: {ae}  |  Saturation: {sat}")
        print(f"  JPEG quality: {quality}  |  Heap: {heap // 1024}KB  |  PSRAM: {psram // 1024}KB")
        print(f"  Uptime: {uptime}  |  RSSI: {rssi} dBm")
    else:
        lux = 0
        version = "?"
        bri = con = ae = "?"
    print()

    # Registrový dump
    if args.reg:
        print("Registry (/reg-debug):")
        regs = fetch_reg_debug(base_url)
        if regs:
            for k, v in regs.items():
                print(f"  {k}: {v}")
        print()

    # Stáhni a analyzuj frame(y)
    results = []
    for i in range(args.samples):
        try:
            jpeg_data, fetch_ms = fetch_frame(base_url)
            result = analyze_brightness(jpeg_data)
            results.append(result)

            if args.samples > 1:
                print_result(result, status, args.baseline, i + 1)

            if args.save:
                FRAMES_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = FRAMES_DIR / f"frame_{ts}_{result['avg']:.0f}avg.jpg"
                fname.write_bytes(jpeg_data)
                print(f"  -> Uloženo: {fname}")

            if i < args.samples - 1:
                time.sleep(args.delay)

        except Exception as e:
            print(f"  CHYBA při stahování snímku {i + 1}: {e}")

    if not results:
        print("Žádný snímek se nepodařilo stáhnout.")
        sys.exit(1)

    # Výsledek
    overall_avg = sum(r["avg"] for r in results) / len(results)
    overall_median = sum(r["median"] for r in results) / len(results)
    overall_p10 = sum(r["p10"] for r in results) / len(results)
    overall_p90 = sum(r["p90"] for r in results) / len(results)

    print()
    print("=" * 65)
    delta = overall_avg - args.baseline
    delta_pct = (delta / args.baseline) * 100 if args.baseline > 0 else 0
    sign = "+" if delta >= 0 else ""

    delta_prev = overall_avg - PREVIOUS_BEST
    sign_prev = "+" if delta_prev >= 0 else ""

    if args.samples > 1:
        print(f"PRŮMĚR ({args.samples} snímků):")
    else:
        print("VÝSLEDEK:")
    print(f"  Avg brightness:  {overall_avg:.1f}/255")
    print(f"  P10/P50/P90:     {overall_p10:.0f} / {overall_median:.0f} / {overall_p90:.0f}")
    print(f"  Resolution:      {results[0]['width']}x{results[0]['height']}")
    if lux:
        print(f"  Ambient light:   {lux:.1f} lux")
    print(f"  vs Baseline ({args.baseline:.0f}): {sign}{delta:.1f} ({sign}{delta_pct:.0f}%)")
    print(f"  vs Prev best ({PREVIOUS_BEST:.0f}): {sign_prev}{delta_prev:.1f}")

    verdict = "PASS" if overall_avg > args.baseline else "FAIL"
    icon = "OK" if verdict == "PASS" else "!!"
    print(f"  Verdikt:         [{icon}] {verdict}")
    print("=" * 65)

    # Loguj do CSV
    log_to_csv(overall_avg, lux, version, bri, con, ae, args.note)
    print(f"\nZalogováno do {CSV_LOG}")

    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
