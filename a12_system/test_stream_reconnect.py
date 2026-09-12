import os
import sys
import threading

import cv2
import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera

BOUNDARY = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"


def _jpeg_bytes():
    img = np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", img)
    assert ok
    return enc.tobytes()


class _FakeResponse:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, jpeg: bytes):
        self._payload = BOUNDARY + jpeg + b"\r\n"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        # Emit frames until the consumer closes us (forced reconnect breaks the loop).
        while not self.closed:
            yield self._payload

    def close(self):
        self.closed = True


def test_process_stream_returns_on_forced_reconnect():
    cam = object.__new__(Camera)
    cam.configure_stall_detection({})
    cam.log_prefix = "[test:cam]"
    cam.configure_stall_detection({})

    resp = _FakeResponse(_jpeg_bytes())
    seen = []

    def callback(frame):
        seen.append(frame)

    # Ask for a reconnect as soon as one frame has been delivered.
    def reconnect_requested():
        return len(seen) >= 1

    done = threading.Event()

    def run():
        cam.process_stream(resp, callback, reconnect_requested=reconnect_requested)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Must return quickly (well under the 20s freeze timeout) once reconnect is requested.
    assert done.wait(timeout=10), "process_stream did not return on forced reconnect"
    assert len(seen) >= 1


def test_process_stream_stashes_last_raw_jpg():
    # Forensics: keep the last raw JPEG so a flat episode can dump the actual
    # bytes A12 received (root-cause evidence, can't attach a 2nd stream client).
    cam = object.__new__(Camera)
    cam.configure_stall_detection({})
    cam.log_prefix = "[test:cam]"
    cam.configure_stall_detection({})
    jpeg = _jpeg_bytes()
    resp = _FakeResponse(jpeg)
    seen = []
    cam.process_stream(resp, seen.append, reconnect_requested=lambda: len(seen) >= 1)
    assert cam.last_raw_jpg == jpeg


def test_reset_session_swaps_session_and_preserves_auth():
    # A hung stream survives in-process reconnects that reuse the pooled session;
    # get_stream must start from a clean session (2026-07-10 outage evidence).
    cam = object.__new__(Camera)
    cam.configure_stall_detection({})
    cam.log_prefix = "[test:cam]"
    cam.configure_stall_detection({})
    old = requests.Session()
    old.auth = ("user", "secret")
    cam.session = old
    cam.reset_session()
    assert cam.session is not old
    assert cam.session.auth == ("user", "secret")


class _ChunkResponse:
    """Deliver finite chunks, then keep the connection open until closed."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = threading.Event()

    def iter_content(self, chunk_size=8192):
        yield from self.chunks
        self.closed.wait(5)

    def close(self):
        self.closed.set()


def _test_camera():
    cam = object.__new__(Camera)
    cam.configure_stall_detection({})
    cam.log_prefix = "[test:cam]"
    cam.configure_stall_detection({})
    return cam


def _multipart(jpeg, declared=None):
    length = len(jpeg) if declared is None else declared
    return (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(length).encode() + b"\r\n\r\n" + jpeg + b"\r\n")


def test_split_multipart_payload_never_concatenates_truncated_jpeg(monkeypatch):
    # The declared payload and its headers must survive a wait for more bytes.
    good = _jpeg_bytes()
    broken = good[:-2]
    first = _multipart(broken, declared=len(good))
    second = _multipart(good)
    split = first.index(b"\xff\xd8") + 100
    response = _ChunkResponse([first[:split], first[split:] + second])
    decoded = []
    real_decode = cv2.imdecode

    def decode(data, flags):
        decoded.append(data.tobytes())
        return real_decode(data, flags)

    monkeypatch.setattr(cv2, "imdecode", decode)
    seen = []
    reason = _test_camera().process_stream(
        response, seen.append, reconnect_requested=lambda: bool(seen),
        freeze_timeout=0.5,
    )
    assert reason == "forced_reconnect"
    assert decoded == [good]


def test_bytewise_headers_keep_declared_length(monkeypatch):
    # Marker scanning would incorrectly emit this payload: EOI is present,
    # but Content-Length says there should be more bytes before the next part.
    good = _jpeg_bytes()
    first = _multipart(good, declared=len(good) + 100)
    second = _multipart(good)
    response = _ChunkResponse([bytes([b]) for b in first + second])
    decoded = []
    real_decode = cv2.imdecode

    def decode(data, flags):
        decoded.append(data.tobytes())
        return real_decode(data, flags)

    monkeypatch.setattr(cv2, "imdecode", decode)
    seen = []
    reason = _test_camera().process_stream(
        response, seen.append, reconnect_requested=lambda: bool(seen),
        freeze_timeout=0.5,
    )
    assert reason == "forced_reconnect"
    assert decoded == [good]


def test_slow_callback_does_not_freeze_healthy_stream():
    import time

    response = _FakeResponse(_jpeg_bytes())
    seen = []

    def callback(frame):
        seen.append(frame)
        if len(seen) == 1:
            time.sleep(0.3)

    cam = _test_camera()
    reason = cam.process_stream(
        response, callback, reconnect_requested=lambda: len(seen) >= 2,
        freeze_timeout=0.1,
    )
    assert reason == "forced_reconnect"
    assert len(seen) == 2
    assert cam.last_freeze_summary is None


def test_actual_silent_stream_still_freezes():
    response = _ChunkResponse([])
    cam = _test_camera()
    reason = cam.process_stream(response, lambda frame: None, freeze_timeout=0.1)
    assert reason == "frozen"
    assert "likely=no_bytes_from_camera" in cam.last_freeze_summary
    assert response.closed.is_set()
