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
    cam.log_prefix = "[test:cam]"

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
    cam.log_prefix = "[test:cam]"
    jpeg = _jpeg_bytes()
    resp = _FakeResponse(jpeg)
    seen = []
    cam.process_stream(resp, seen.append, reconnect_requested=lambda: len(seen) >= 1)
    assert cam.last_raw_jpg == jpeg


def test_reset_session_swaps_session_and_preserves_auth():
    # A hung stream survives in-process reconnects that reuse the pooled session;
    # get_stream must start from a clean session (2026-07-10 outage evidence).
    cam = object.__new__(Camera)
    cam.log_prefix = "[test:cam]"
    old = requests.Session()
    old.auth = ("user", "secret")
    cam.session = old
    cam.reset_session()
    assert cam.session is not old
    assert cam.session.auth == ("user", "secret")
