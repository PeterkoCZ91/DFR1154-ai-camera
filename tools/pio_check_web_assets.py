"""PlatformIO pre-build hook: refuse to build a stale web UI.

The dashboard and settings page ship as gzip arrays inside camera_server.cpp.
Editing firmware/web/*.html without re-embedding used to compile perfectly and
serve the old page, which is the failure that let the UI source drift out of the
repository entirely — for months nobody noticed the arrays were the only copy.

A check in CI catches it before release; this catches it before flashing.
"""

import subprocess
import sys
import os

Import("env")  # noqa: F821 — injected by PlatformIO/SCons

# PlatformIO exec()s this script, so __file__ does not exist here. PROJECT_DIR
# is firmware/, and the repository is its parent.
_REPO = os.path.dirname(env["PROJECT_DIR"])  # noqa: F821 — injected by PlatformIO
_CHECK = os.path.join(_REPO, "tools", "build_web_assets.py")

result = subprocess.run(
    [sys.executable, _CHECK, "--check"], capture_output=True, text=True
)
if result.returncode != 0:
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    print(
        "\nThe compiled web UI does not match firmware/web/. "
        "Run tools/build_web_assets.py and build again.",
        file=sys.stderr,
    )
    Exit(1)  # noqa: F821 — injected by PlatformIO/SCons
