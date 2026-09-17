#!/usr/bin/env bash
#
# Project checks that run before a push, chained from the account-wide hook.
#
# The global hook at ~/.config/git/hooks/pre-push guards identity and
# credentials across every repository, and it hands control to a repo-local
# .git/hooks/pre-push first. This script is what that local hook runs, so the
# account-wide protection stays in place and the project adds to it rather than
# replacing it. Do NOT point core.hooksPath at this repository: that disables
# the global scan for this repo, which is the one thing it exists to prevent.
#
# Install:  ln -sf ../../tools/pre_push_checks.sh .git/hooks/pre-push
#
# What it checks, and why each one is here rather than only in CI: both catch a
# mistake that already reached a public repository once.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
failed=0

# The compiled web UI must match firmware/web/. Without this the arrays are the
# only copy that matters, which is how the UI source went missing for months.
python3 tools/build_web_assets.py --check || failed=1

# No real values posing as examples in that UI. The settings page shipped a real
# Telegram chat ID as a placeholder, and the project's own scan could not see it
# because it lived inside a gzip array rather than in text.
python3 tools/scan_ui_secrets.py || failed=1

if [ "$failed" -ne 0 ]; then
    echo "" >&2
    echo "Project pre-push checks failed — push aborted." >&2
    exit 1
fi
