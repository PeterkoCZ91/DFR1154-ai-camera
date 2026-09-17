"""The camera owns the language, and A12 follows it.

Both web pages carry a full cz/en table and a toggle, but until 2026-09-17 the
choice lived in the browser's localStorage: nothing outside that one tab could
act on it. The firmware now persists it in /config.json and reports it on
/health, which A12 already polls every cycle — so the alerts can answer in the
language the operator picked in the UI, without a second request or a second
place to configure it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera


def _camera():
    return Camera({"camera_url": "http://camera.invalid", "camera_id": "test"})


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _health(cam, payload, status=200):
    cam.session.get = lambda *a, **k: _Response(payload, status)
    return cam.get_health()


def test_language_defaults_to_czech_before_any_camera_answers():
    assert _camera().ui_language == "cz"


def test_health_poll_adopts_the_camera_language():
    cam = _camera()
    _health(cam, {"status": "ok", "ui_language": "en"})
    assert cam.ui_language == "en"


def test_an_unknown_language_is_ignored_rather_than_adopted():
    """A garbled field must not silently switch every alert to nothing."""
    cam = _camera()
    _health(cam, {"status": "ok", "ui_language": "en"})
    _health(cam, {"status": "ok", "ui_language": "klingon"})
    assert cam.ui_language == "en"


def test_a_health_response_without_the_field_keeps_the_last_value():
    """Older firmware does not report it; that is not a reason to flip back."""
    cam = _camera()
    _health(cam, {"status": "ok", "ui_language": "en"})
    _health(cam, {"status": "ok"})
    assert cam.ui_language == "en"


def test_a_failed_health_poll_keeps_the_last_value():
    cam = _camera()
    _health(cam, {"status": "ok", "ui_language": "en"})

    def boom(*a, **k):
        raise OSError("camera unreachable")

    cam.session.get = boom
    assert cam.get_health() is None
    assert cam.ui_language == "en"


def test_a_non_200_response_keeps_the_last_value():
    cam = _camera()
    _health(cam, {"status": "ok", "ui_language": "en"})
    _health(cam, {"status": "ok", "ui_language": "cz"}, status=503)
    assert cam.ui_language == "en"
