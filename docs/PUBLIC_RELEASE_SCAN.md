# Public Release Scan

Use this checklist before pushing public changes from the firmware/A12 repository.
It is written for Codex, Claude, or a human reviewer doing a fast release audit.

## Scope

This repository may contain three different kinds of data:

- ESP32 firmware source and public documentation, which are safe to publish.
- A12 companion source and example configs, which are safe only with placeholders.
- Runtime data, screenshots, local configs, face images, logs, databases and model files,
  which must stay out of git.

The paired Tapo monitor repository is separate:
<https://github.com/PeterkoCZ91/tapo-monitoring>. This repo may link to it, but should not
copy Tapo runtime logs, camera screenshots, LAN addresses, or deployment secrets.

## Required Checks

Run from the repository root:

```bash
git status --short --branch
git diff --check
ruff check .
pytest -q
```

`ruff check .` and `pytest -q` are the first CI job in
`.github/workflows/ci.yml`, with the linter pinned in
`a12_system/requirements-dev.txt` so a new ruff release cannot fail the build on
unchanged code. A green local run is what CI will report.

Since 2026-09-16 CI has a second job that compiles the firmware, so a change
under `firmware/` can no longer reach `main` uncompiled. Reproduce it locally
when firmware code changed:

```bash
cd firmware && ../.github/scripts/build-firmware.sh esp32-s3-devkitc-1
```

That script is what CI runs. It retries once, and only when the output contains
`internal compiler error` — full rebuilds intermittently hit one inside the Edge
Impulse SDK, always green on retry, while a genuine compile error still fails on
the first attempt. Only `env:esp32-s3-devkitc-1` is built: `env:ota` extends it
and overrides nothing but the upload settings, so it compiles identical objects.
Build it too the day it gains a `build_flag`.

## Secret and PII Scan

This scan intentionally excludes `.git`, vendored Edge Impulse sources, binary media and
known placeholder examples. Review every remaining hit manually.

```bash
rg -n \
  "(bot[0-9]+:|gsk_|sk-[A-Za-z0-9]|Bearer +[A-Za-z0-9._-]+|chat_id|api[_-]?key|token|password|secret|known_faces|events\.db|a12\.log)" \
  -g '!**/.git/**' \
  -g '!firmware/lib/ei-person-detection-fomo/**' \
  -g '!**/*.png' -g '!**/*.jpg' -g '!**/*.jpeg' -g '!**/*.webp' \
  .
```

Network examples such as `192.168.1.100`, `192.168.4.1`, and `192.168.x.x` are allowed
only when they are clearly generic examples or firmware default setup addresses. Replace
real deployment addresses with `<camera-ip>`, `<mqtt-ip>`, `<ha-url>`, or TEST-NET examples
such as `192.0.2.10`.

### Vendored Edge Impulse sources

The Edge Impulse exclusion is a blind spot, not a safe area — it hides a whole
directory from the scan above. An export embeds Edge Impulse account metadata in
`model-parameters/model_metadata.h` and `model-parameters/model_variables.h`
(`EI_CLASSIFIER_PROJECT_OWNER`, `EI_CLASSIFIER_PROJECT_ID`, `.project_owner`, plus
the project name and dataset labels). The values currently committed were reviewed
and cleared: the owner handle is a nickname that identifies nobody, and the project
ID appears in generated file and symbol names (`tflite_learn_<id>_<ver>.h`), so it
could not be search-and-replaced anyway.

Re-check after every model re-export, since a new export can carry a different
project name, dataset labels or collaborator handles:

```bash
rg -n "PROJECT_OWNER|project_owner|PROJECT_ID|project_id" \
  firmware/lib/ei-person-detection-fomo/src/model-parameters/
```

## Web UI

The dashboard and the settings page are served from gzip arrays compiled into
`firmware/camera_server.cpp`. Their HTML lives in `firmware/web/`, and
`tools/build_web_assets.py` embeds it:

```bash
tools/build_web_assets.py            # re-embed after editing firmware/web/
tools/build_web_assets.py --check    # verify the compiled page matches the source
```

Until 2026-09-17 only the arrays existed and the HTML was in nobody's
repository, so 59 KB of interface — including the complete cz/en translation
table — could not be edited by anyone who cloned this. The sources were
recovered by decompressing the arrays, and confirmed against the live camera:
both pages hash identically to what the running firmware serves.

`--check` runs in CI before the firmware build, because the array is what ships:
an edit to `firmware/web/` that is not re-embedded would otherwise compile green
and serve the old page.

## Language

The repository is public and its own voice is **English**: code comments,
docstrings, documentation, the changelog, commit messages, branch names and test
names.

**Czech is reserved for what the system says to its operator** — the daily
Telegram summary and the dashboard's own button labels — and for the places that
quote those strings verbatim: the test that asserts on the summary, and the two
documents that cite the labels.

The split had always been followed in practice and written down nowhere, so new
code kept it by accident rather than by rule; on 2026-09-17 there were 86 Czech
lines on the wrong side of it, none of them user-facing — docstrings and prints
in four tools, ring-buffer comments in `firmware/camera_capture.h`, and four
changelog entries in an otherwise English file.

`a12_system/test_language_split.py` now enforces it. The allowlist there records
a reason per file, and a second test fails if an exemption stops being needed,
so the list cannot quietly grow. In Python the check is exact — the Czech has to
sit inside a string token, so a Czech comment in an allowlisted file still
fails. In Markdown the exemption is per file and it is review, not the test,
that keeps those lines quotations.

`.claude/` is outside the scan. Those are the owner's working instructions for
their own tooling, on the same side of the line as the daily summary: written
for the person who runs this, not for someone reading the repository to
understand it.

## Must Not Be Committed

Reject the commit if any of these appear as tracked files:

- `.env`, `config.env`, Home Assistant tokens, Telegram tokens, MQTT passwords.
- `known_faces/`, face encodings, private snapshots, clips, SD-card captures.
- `events.db`, logs, runtime databases, local deployment notes.
- YOLO or other model weights: `*.onnx`, `*.pt`, `*.weights`, `*.pkl`.
- Local-only handoff files such as `LOCAL_*.md` or `*_LOCAL.md`.
- Identifying project names, dataset labels or collaborator handles in a
  re-exported Edge Impulse model (see the note above).

Useful commands:

```bash
git ls-files | rg "(^|/)(config\.env|\.env|known_faces|events\.db|a12\.log|LOCAL_|_LOCAL\.md|.*\.(onnx|pt|weights|pkl))$"
git ls-files | rg "\.(png|jpg|jpeg|webp|mp4|avi)$"
```

Media files are not automatically forbidden because documentation screenshots can be valid,
but every hit must be intentionally public and non-identifying.

## Documentation Expectations

Before pushing, confirm public docs explain:

- Standalone firmware vs Enhanced A12 mode.
- How A12 connects to the paired Tapo shared scorer over HTTP.
- That A12 and Tapo keep separate thresholds and alert rules.
- How to disable ESP32 Telegram when A12 owns notifications.
- Where runtime configuration lives and why it is gitignored.

The main A12 runtime entry point is [`A12_COMPANION.md`](A12_COMPANION.md). Privacy and
retention details are in [`DATA_PRIVACY.md`](DATA_PRIVACY.md).
