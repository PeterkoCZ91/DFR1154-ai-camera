#!/usr/bin/env python3
"""Embed the web UI into the firmware, or check that the embedded copy matches.

The dashboard and the settings page are served from gzip byte arrays compiled
into `firmware/camera_server.cpp`. Until 2026-09-17 only those arrays existed:
the HTML they were generated from was in nobody's repository, so 59 KB of user
interface — including the full cz/en translation table — could not be edited by
anyone who cloned this project. The sources were recovered by decompressing the
arrays and now live in `firmware/web/`.

Committing the source is only half of it. What keeps the two from drifting apart
again is `--check`, which decompresses what is compiled in and compares it to
the source byte for byte. Run it in CI and an edit to one without the other
fails the build instead of quietly shipping the old page.

Usage:
    tools/build_web_assets.py            # regenerate the arrays from firmware/web/
    tools/build_web_assets.py --check    # verify they match; exit 1 if not
"""

import argparse
import gzip
import hashlib
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO, "firmware", "camera_server.cpp")
WEB = os.path.join(REPO, "firmware", "web")

# symbol in camera_server.cpp -> source file in firmware/web/
ASSETS = {
    "INDEX_HTML_GZ": "index_src.html",
    "SETTINGS_HTML_GZ": "settings_src.html",
}

ARRAY = r"const uint8_t {sym}\[\] PROGMEM = \{{(?P<body>.*?)\n\}};"


def _find(source: str, symbol: str) -> re.Match:
    match = re.search(ARRAY.format(sym=symbol), source, re.S)
    if not match:
        raise SystemExit(f"{TARGET}: no array named {symbol}")
    return match


def _embedded_bytes(source: str, symbol: str) -> bytes:
    body = _find(source, symbol).group("body")
    return bytes(int(v, 16) for v in re.findall(r"0x([0-9a-fA-F]{2})", body))


def _compress(raw: bytes) -> bytes:
    """Deterministic gzip: same input must give the same bytes, every time.

    The arrays that were in the tree carried a timestamp — and one of them an
    original filename — so regenerating produced a diff even when the HTML had
    not changed. mtime=0 and no filename make the output a pure function of the
    source, which is what lets --check mean something.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(raw)
    return buffer.getvalue()


def _format_array(data: bytes, per_line: int = 16) -> str:
    lines = []
    for start in range(0, len(data), per_line):
        chunk = data[start:start + per_line]
        lines.append("    " + ",".join(f"0x{b:02x}" for b in chunk))
    return ",\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the embedded pages match firmware/web/, change nothing")
    args = parser.parse_args()

    source = open(TARGET, encoding="utf-8").read()
    updated = source
    failures = []

    for symbol, filename in ASSETS.items():
        path = os.path.join(WEB, filename)
        if not os.path.isfile(path):
            failures.append(f"{filename}: missing — the source of {symbol} is not in the repo")
            continue
        raw = open(path, "rb").read()
        embedded = gzip.decompress(_embedded_bytes(source, symbol))

        if args.check:
            if embedded == raw:
                print(f"ok    {symbol:<18} {len(raw):>6} B  {hashlib.sha256(raw).hexdigest()[:12]}")
            else:
                failures.append(
                    f"{symbol}: the compiled page does not match {filename} "
                    f"(embedded {len(embedded)} B / source {len(raw)} B). "
                    f"Run tools/build_web_assets.py to re-embed it."
                )
            continue

        packed = _compress(raw)
        block = (
            f"const uint8_t {symbol}[] PROGMEM = {{\n"
            f"{_format_array(packed)}\n}};"
        )
        updated = re.sub(ARRAY.format(sym=symbol), lambda _m: block, updated, count=1, flags=re.S)
        # Keep the size comment honest rather than leaving a stale number.
        updated = re.sub(
            rf"// {symbol[:-3]} gzipped: \d+ -> \d+ bytes",
            f"// {symbol[:-3]} gzipped: {len(raw)} -> {len(packed)} bytes",
            updated, count=1,
        )
        print(f"embed {symbol:<18} {len(raw):>6} B -> {len(packed)} B")

    if failures:
        for line in failures:
            print(f"FAIL  {line}", file=sys.stderr)
        return 1

    if not args.check and updated != source:
        open(TARGET, "w", encoding="utf-8").write(updated)
        print(f"wrote {os.path.relpath(TARGET, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
