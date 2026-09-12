"""Tests for how quickly a stalled stream is noticed, and how it is classified.

Two separate concerns, and only one of them wants a small number.

CLASSIFICATION. The socket read timeout must be strictly shorter than the freeze
heuristic. Until 2026-09-12 it was inverted (30 s read against a 20 s
heuristic), so silence could only ever be caught by the heuristic and a genuine
transport break was recorded as `frozen` — an image-level guess — instead of
`stream_ended`, a transport fact. That ordering fix stands.

DETECTION WINDOW. Narrowing this pair to 2 s / 3 s on 2026-09-12 17:43 was a
mistake, measured and reverted at 18:53 the same evening. The floor is set by
the camera's OWN send timeout: the firmware gives up on a frame after
`detection_send_timeout_ms` (15 s) and then carries on with the next one. A
client window below that turns every camera-side hiccup into a teardown of a
stream the camera was about to resume by itself — and a teardown is expensive,
because reconnecting took a median 14 s.

The stalls that prompted all this are NOT an A12 problem. Measured the same
evening on the production camera: a reader doing nothing but recv() saw gaps of
exactly 15.0 s, and a simultaneous ping of both cameras showed 16 % loss with
12-13 s blackouts on the production board against 0 % on a bench board on the
same AP, channel and RSSI. The link, not the software, goes away.
"""


import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera

# The camera's own per-frame send timeout (firmware `detection_send_timeout_ms`,
# reported by /health). It gives up on one frame and continues; a client that
# tears down sooner turns a recoverable hiccup into a median-14 s reconnect.
CAMERA_SEND_TIMEOUT_SECONDS = 15.0


def _cam(**overrides):
    config = {"camera_url": "http://camera.invalid", "camera_http_user": ""}
    config.update(overrides)
    return Camera(config)


def test_socket_read_timeout_fires_before_the_freeze_heuristic():
    """The invariant the original 30 s/20 s bug violated.

    If the read timeout is not strictly shorter than the freeze timeout, a
    silent socket can only ever be caught by the heuristic, and a genuine
    transport break gets recorded as an image-quality event.
    """
    cam = _cam()
    assert cam.stream_read_timeout < cam.freeze_timeout


def test_detection_window_outlasts_the_cameras_own_send_timeout():
    """The invariant the 2 s/3 s 'fix' violated.

    The camera drops a frame it could not send within 15 s and then resumes on
    its own. Reconnecting inside that window does not shorten the outage — it
    replaces a self-healing gap with a median-14 s reconnect.
    """
    cam = _cam()
    assert cam.freeze_timeout >= CAMERA_SEND_TIMEOUT_SECONDS


def test_read_timeout_also_exceeds_the_healthy_idle_gap():
    """Healthy idle traffic must not look like a dead socket.

    Observed delivery runs 0.8-1.7 fps against a target of 2, and the camera
    itself waits up to 2 s for a new frame, so a 2 s read timeout sat on top of
    the normal inter-frame gap and fired on jitter alone.
    """
    cam = _cam()
    assert cam.stream_read_timeout > 5.0


def test_freeze_timeout_is_configurable_without_a_rebuild():
    cam = _cam(stream_freeze_timeout=25.0)
    assert cam.freeze_timeout == 25.0


def test_read_timeout_is_configurable_without_a_rebuild():
    cam = _cam(stream_read_timeout=12.0)
    assert cam.stream_read_timeout == 12.0


def test_process_stream_defaults_to_the_configured_window():
    """__main__ does not pass freeze_timeout, so the instance value must win.

    Without this the config knob exists but nothing reads it in production.
    """
    import inspect

    sig = inspect.signature(Camera.process_stream)
    assert sig.parameters["freeze_timeout"].default is None, (
        "a hardcoded numeric default would silently override the configured value"
    )
