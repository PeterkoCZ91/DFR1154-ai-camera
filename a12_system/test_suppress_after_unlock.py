"""Person alerts are muted briefly after a confirmed resident opening.

A stranger verdict after the opening must cancel the mute: tailgating is the
case this feature must never hide. Driven through the real
_handle_person_detections so the audit outcome is what production writes.
"""

from unittest.mock import Mock

import pytest

from a12_system.face_result import FaceOutcome, FaceResult
from a12_system.test_face_episode import _StubDetector, _frame, _pipeline

NO_FACE = FaceResult(FaceOutcome.NO_FACE)
STRANGER = FaceResult(FaceOutcome.STRANGER)
OPENED_AT = 1000.0


def make(monkeypatch, answers, now, window=120.0):
    clock = {"t": now}
    monkeypatch.setattr("a12_system.pipeline.time.time", lambda: clock["t"])
    p = _pipeline(_StubDetector(answers))
    p.person_track_ids = {"pir": "pir_1", "camera": None}
    p.runtime_config = {
        "face_recognition": {"enabled": True},
        "face_unlock.enabled": False,
        "require_sensor_for_recording": False,
        "detection_cooldown_seconds": 0,
        "occupant_suppress_after_unlock_seconds": window,
        "event_scoring.notify_threshold": 0,
    }
    p.groq_vision = None
    p.last_save_time = {}
    p._last_face_unlock_at = 0.0
    p._score_event = Mock(return_value=100)
    p._log_decision_audit = Mock(return_value=1)
    p._queue_notification = Mock()
    p.db = Mock()
    p.stats = Mock()
    p.screenshot_folder = "shots"
    p.camera_id = "cam"
    p._last_face_unlock_confirmed_at = OPENED_AT
    p._last_face_unlock_started_at = OPENED_AT - 5
    return p


def run(p):
    p._handle_person_detections(_frame(), [("person", .95)], "external_trigger_window", "pir", 1, {})
    outcomes = [c.args[5] for c in p._log_decision_audit.call_args_list]
    sent = [c.kwargs.get("send_telegram") for c in p._queue_notification.call_args_list]
    return outcomes, sent


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch):
    monkeypatch.setattr("a12_system.pipeline.cv2.imwrite", lambda *a, **k: True)
    monkeypatch.setattr("a12_system.pipeline.os.makedirs", lambda *a, **k: None)


def test_alert_muted_inside_window_and_audited(monkeypatch):
    p = make(monkeypatch, [NO_FACE], OPENED_AT + 60)
    outcomes, sent = run(p)
    assert outcomes == ["suppressed_after_unlock"]
    assert sent == [False]


def test_alert_sent_after_window(monkeypatch):
    p = make(monkeypatch, [NO_FACE], OPENED_AT + 121)
    outcomes, sent = run(p)
    assert outcomes == ["recorded_and_notified"]
    assert sent == [True]


def test_stranger_in_window_still_alerts(monkeypatch):
    p = make(monkeypatch, [STRANGER], OPENED_AT + 30)
    outcomes, sent = run(p)
    assert outcomes == ["recorded_and_notified"]
    assert sent == [True]


def test_stranger_seen_while_lock_was_confirming_still_alerts(monkeypatch):
    p = make(monkeypatch, [NO_FACE], OPENED_AT + 30)
    p._last_stranger_at = OPENED_AT - 2  # after the face matched, before confirmation
    outcomes, _ = run(p)
    assert outcomes == ["recorded_and_notified"]


def test_window_zero_disables(monkeypatch):
    p = make(monkeypatch, [NO_FACE], OPENED_AT + 10, window=0)
    outcomes, sent = run(p)
    assert outcomes == ["recorded_and_notified"]
    assert sent == [True]


def test_no_unlock_no_mute(monkeypatch):
    p = make(monkeypatch, [NO_FACE], OPENED_AT + 10)
    p._last_face_unlock_confirmed_at = 0.0
    assert run(p)[0] == ["recorded_and_notified"]


def test_confirmed_face_opening_arms_the_mute_but_manual_does_not(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("a12_system.pipeline.time.time", lambda: 2000.0)
    monkeypatch.setattr("a12_system.pipeline.ha_unlock", lambda *a, **k: (True, "ok"))
    for manual, armed in ((False, True), (True, False)):
        p = _pipeline(_StubDetector([]))
        p.runtime_config = {}
        p.ha_url = p.ha_token = p.nuki_entity_id = ""
        p._own_unlock_commands = __import__("collections").deque(maxlen=16)
        p._own_unlock_commands_lock = __import__("threading").Lock()
        p.notifier = SimpleNamespace(send_telegram=lambda *a, **k: True)
        p.db = Mock()
        p._last_face_unlock_confirmed_at = 0.0
        p._nuki_unlock_report("alice", manual=manual)
        assert (p._last_face_unlock_confirmed_at == 2000.0) is armed
