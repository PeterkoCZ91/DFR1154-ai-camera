#!/usr/bin/env python3
"""Look for real values sitting in the web UI where an example belongs.

The settings page shipped the owner's actual Telegram chat ID as the
placeholder of its Chat ID field. It rode along inside the gzip array compiled
into camera_server.cpp for four months and reached a public repository, and the
project's own secret scan could not see it: a gzip array is not text, so
nothing that greps the tree was ever going to find it.

Two things now stand between that and a repeat. The UI source is committed as
plain HTML, so an ordinary scan reaches it — and `build_web_assets.py --check`
proves the compiled array still matches that source, which is what makes
scanning the source mean anything. This script is the scan.

What it looks for is the specific shape of that mistake: a form field offering
a long digit run or a credential-shaped string as its example. Generic examples
are listed below; anything else is reported.

Usage:  tools/scan_ui_secrets.py [files...]   (default: firmware/web/*.html)
"""

import glob
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Examples a reader is meant to see. Deliberately short: every addition here is
# a value somebody decided is not real, and that decision should be visible.
GENERIC = {
    "123456789",
    "123456789:ABCdef...",
    "192.168.1.100",
    "192.168.4.1",
    "0.0.0.0",
}

# An attribute that shows the user a sample value.
EXAMPLE_ATTR = re.compile(r'(placeholder|value)\s*=\s*"([^"]*)"', re.I)

# Things that are never an example, wherever they appear.
CREDENTIAL = [
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"), "Telegram bot token"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "Groq key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"BEGIN (?:RSA|EC|OPENSSH) PRIVATE"), "private key"),
]

DIGIT_RUN = re.compile(r"^\d{6,}$")


def findings(path: str) -> list[str]:
    if not os.path.isfile(path):
        # Reported, not raised: a scan that cannot read its input must fail
        # loudly rather than crash in a way a caller might mistake for a bug.
        return [f"{path}: cannot read — nothing was scanned"]
    out = []
    for number, line in enumerate(open(path, encoding="utf-8").read().splitlines(), 1):
        for pattern, what in CREDENTIAL:
            if pattern.search(line):
                out.append(f"{path}:{number}: {what}")
        for attribute, value in EXAMPLE_ATTR.findall(line):
            value = value.strip()
            if not value or value in GENERIC:
                continue
            if DIGIT_RUN.match(value):
                out.append(
                    f"{path}:{number}: {attribute}=\"{value}\" — a bare "
                    f"{len(value)}-digit number as an example. If it is a real "
                    f"id, replace it; if not, add it to GENERIC in this script."
                )
    return out


def main(argv: list[str]) -> int:
    paths = argv[1:] or sorted(glob.glob(os.path.join(REPO, "firmware", "web", "*.html")))
    if not paths:
        print("no files to scan — did firmware/web/ move?", file=sys.stderr)
        return 1
    hits = [hit for path in paths for hit in findings(path)]
    for hit in hits:
        print(f"FAIL  {hit}", file=sys.stderr)
    if hits:
        return 1
    print(f"ok    scanned {len(paths)} UI source file(s), no real values as examples")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
