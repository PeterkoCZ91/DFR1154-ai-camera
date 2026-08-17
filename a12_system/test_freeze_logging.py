"""Tests for stream-freeze diagnostics.

The 20s freeze watchdog used to log a bare "Stream frozen" line, which cannot
distinguish the three places a stall can live:

* the camera stopped sending bytes (network / camera-side drop),
* bytes keep arriving but no complete JPEG parses (corrupt stream),
* raw frames keep arriving but nothing decoded reaches the callback.

These tests pin the diagnostic contract: process_stream reports why it
returned, the freeze line carries window telemetry naming the likely layer,
teardown noise is not logged as an error, and the post-freeze camera health
snapshot surfaces the camera's own stream_health counters.
"""

import logging
import os
import sys
import time

import cv2
import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera
from a12_system.pipeline import DetectionPipeline

BOUNDARY = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"


def _jpeg_bytes():
    img = np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", img)
    assert ok
    return enc.tobytes()


def _cam():
    cam = object.__new__(Camera)
    cam.log_prefix = "[test:cam]"
    return cam


class _LoopingResponse:
    """Streams frames until the consumer closes us."""

    def __init__(self, jpeg: bytes):
        self._payload = BOUNDARY + jpeg + b"\r\n"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        while not self.closed:
            yield self._payload

    def close(self):
        self.closed = True


class _StallingResponse:
    """Sends one frame, then goes silent until closed — a camera-side stall."""

    def __init__(self, jpeg: bytes):
        self._payload = BOUNDARY + jpeg + b"\r\n"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self._payload
        while not self.closed:
            time.sleep(0.02)

    def close(self):
        self.closed = True


class _RaisesAfterCloseResponse:
    """Raises the urllib3 teardown artifact once the consumer closes us."""

    def __init__(self, jpeg: bytes):
        self._payload = BOUNDARY + jpeg + b"\r\n"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        while True:
            if self.closed:
                raise AttributeError("'NoneType' object has no attribute 'read'")
            yield self._payload

    def close(self):
        self.closed = True


class _DiesResponse:
    """Camera closes the connection mid-stream after one frame."""

    def __init__(self, jpeg: bytes):
        self._payload = BOUNDARY + jpeg + b"\r\n"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self._payload
        raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")

    def close(self):
        self.closed = True


# ── freeze telemetry ─────────────────────────────────────────────────────────

def test_freeze_returns_reason_and_names_the_silent_camera(caplog):
    cam = _cam()
    resp = _StallingResponse(_jpeg_bytes())
    with caplog.at_level(logging.INFO):
        reason = cam.process_stream(resp, lambda frame: None, freeze_timeout=1.0)
    assert reason == "frozen"
    frozen = [r for r in caplog.records if "Stream frozen" in r.getMessage()]
    assert frozen, "freeze must still be logged"
    msg = frozen[0].getMessage()
    assert "bytes=0" in msg
    assert "raw=0" in msg
    assert "likely=no_bytes_from_camera" in msg


def test_forced_reconnect_reports_its_reason():
    cam = _cam()
    resp = _LoopingResponse(_jpeg_bytes())
    seen = []
    reason = cam.process_stream(
        resp, seen.append, reconnect_requested=lambda: len(seen) >= 1
    )
    assert reason == "forced_reconnect"


# ── drain-thread exit classification ─────────────────────────────────────────

def test_teardown_artifact_is_not_logged_as_error(caplog):
    cam = _cam()
    resp = _RaisesAfterCloseResponse(_jpeg_bytes())
    seen = []
    with caplog.at_level(logging.DEBUG):
        cam.process_stream(
            resp, seen.append, reconnect_requested=lambda: len(seen) >= 1
        )
    errors = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "Stream drain error" in r.getMessage()
    ]
    assert not errors, "our own teardown must not masquerade as a stream error"


def test_camera_dropping_us_is_still_an_error_and_ends_the_stream(caplog):
    cam = _cam()
    resp = _DiesResponse(_jpeg_bytes())
    with caplog.at_level(logging.INFO):
        reason = cam.process_stream(resp, lambda frame: None)
    assert reason == "stream_ended"
    errors = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "Stream drain error" in r.getMessage()
    ]
    assert errors, "a genuine camera-side drop must stay an ERROR"


# ── post-freeze camera health snapshot ───────────────────────────────────────

def test_health_snapshot_surfaces_camera_stream_counters(caplog):
    cam = _cam()
    cam.get_health = lambda: {
        "wifi_rssi": -45,
        "free_heap": 82756,
        "uptime_seconds": 2838477,
        "stream_health": {
            "active_detection_clients": 1,
            "send_fail_count": 2114,
            "no_frame_count": 5,
            "last_drop_reason": "send-fail",
            "last_errno": 104,
        },
    }
    with caplog.at_level(logging.WARNING):
        cam.log_health_snapshot("frozen")
    lines = [r.getMessage() for r in caplog.records if "health after frozen" in r.getMessage()]
    assert lines, "snapshot line missing"
    msg = lines[0]
    assert "send_fail_count=2114" in msg
    assert "last_drop_reason=send-fail" in msg
    assert "last_errno=104" in msg
    assert "wifi_rssi=-45" in msg


def test_health_snapshot_reports_unreachable_camera(caplog):
    cam = _cam()
    cam.get_health = lambda: None
    with caplog.at_level(logging.WARNING):
        cam.log_health_snapshot("frozen")
    assert any(
        "health after frozen unavailable" in r.getMessage() for r in caplog.records
    )


# ── local-only clip log wording ──────────────────────────────────────────────

def test_local_only_clip_message_names_policy_when_unscored():
    p = object.__new__(DetectionPipeline)
    msg = p._local_only_clip_message(None, "/data/x.mp4")
    assert "without confirmed person" in msg
    assert "score" not in msg.split("(")[0].lower()


def test_local_only_clip_message_keeps_score_when_scored():
    p = object.__new__(DetectionPipeline)
    msg = p._local_only_clip_message(42, "/data/x.mp4")
    assert "score=42" in msg
