"""Tests for how quickly a stalled stream is noticed, and how it is classified.

Measured on the live camera over the 7 days to 2026-09-12: 447 `frozen` plus 74
`stream_ended` breaks, ~75/day, each costing ~23 s of blindness — ~26 minutes a
day. The 23 s was almost entirely the detection window, not the outage.

The cause was an ordering mistake between two timeouts. The socket read timeout
was 30 s while the freeze heuristic was 20 s, so the read timeout could never
fire first, and every trickling-but-dead stream was misreported as `frozen`
(an image-level guess) instead of `stream_ended` (a transport fact).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera


def _cam(**overrides):
    config = {"camera_url": "http://camera.invalid", "camera_http_user": ""}
    config.update(overrides)
    return Camera(config)


def test_socket_read_timeout_fires_before_the_freeze_heuristic():
    """The invariant the original bug violated.

    If the read timeout is not strictly shorter than the freeze timeout, a
    silent socket can only ever be caught by the heuristic, and a genuine
    transport break gets recorded as an image-quality event.
    """
    cam = _cam()
    assert cam.stream_read_timeout < cam.freeze_timeout


def test_detection_window_is_seconds_not_tens_of_seconds():
    cam = _cam()
    assert cam.freeze_timeout <= 5.0


def test_freeze_timeout_stays_above_the_idle_frame_gap():
    """Healthy idle traffic must not look like a stall.

    Idle decode pacing leaves ~0.5 s between delivered frames and the camera
    itself waits up to 2 s for a new frame, so anything at or below ~2.5 s
    would reconnect on a perfectly healthy stream.
    """
    cam = _cam()
    assert cam.freeze_timeout > 2.5


def test_freeze_timeout_is_configurable_without_a_rebuild():
    cam = _cam(stream_freeze_timeout=4.5)
    assert cam.freeze_timeout == 4.5


def test_read_timeout_is_configurable_without_a_rebuild():
    cam = _cam(stream_read_timeout=1.5)
    assert cam.stream_read_timeout == 1.5


def test_process_stream_defaults_to_the_configured_window():
    """__main__ does not pass freeze_timeout, so the instance value must win.

    Without this the config knob exists but nothing reads it in production.
    """
    import inspect

    sig = inspect.signature(Camera.process_stream)
    assert sig.parameters["freeze_timeout"].default is None, (
        "a hardcoded numeric default would silently override the configured value"
    )
