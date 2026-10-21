"""The door must open only on a strong, listed, unrepeated resident verdict,
and must never be reported open unless Home Assistant says the lock moved."""

import os
import threading
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.door_unlock import ha_lock, ha_unlock, unlock_decision
from a12_system.face_result import FaceEpisode, FaceOutcome, FaceResult

RESIDENT = FaceResult(FaceOutcome.RESIDENT, "alice", 0.70, faces=1,
                      lead_name="alice", lead_score=0.70)


def res(name="alice", score=0.70, lead_name="alice", lead_score=0.70, faces=1):
    return FaceResult(FaceOutcome.RESIDENT, name, score, faces=faces,
                      lead_name=lead_name, lead_score=lead_score)


def decide(face=RESIDENT, **over):
    args = dict(
        enabled=True, allowed_names=["alice"], min_score=0.60,
        now=1000.0, last_unlock_at=0.0, cooldown=60.0,
    )
    args.update(over)
    return unlock_decision(face, **args)


def test_strong_listed_resident_is_allowed():
    assert decide() == (True, "ok")


def test_every_missing_condition_fails_closed():
    assert decide(enabled=False)[0] is False
    assert decide(allowed_names=[])[0] is False
    assert decide(allowed_names=["bob"])[0] is False
    assert decide(res(lead_score=None))[0] is False
    assert decide(res(lead_score=0.59))[0] is False
    assert decide(last_unlock_at=990.0)[0] is False
    for outcome in (FaceOutcome.STRANGER, FaceOutcome.UNDECIDED, FaceOutcome.NO_FACE,
                    FaceOutcome.UNAVAILABLE, FaceOutcome.ERROR):
        assert decide(FaceResult(outcome, None, 0.9))[0] is False


def test_score_exactly_at_minimum_is_allowed_and_cooldown_expires():
    assert decide(res(lead_score=0.60))[0] is True
    assert decide(last_unlock_at=940.0)[0] is True


def test_episode_reports_the_weakest_agreeing_sighting():
    ep = FaceEpisode(required_confirmations=2)
    ep.record(res(score=0.90, lead_score=0.90))
    ep.record(res(score=0.55, lead_score=0.55))
    verdict = ep.verdict()
    assert verdict.is_resident and verdict.score == 0.55 and verdict.lead_score == 0.55
    # ...so one flattering frame cannot carry a marginal match past the gate.
    assert decide(verdict)[0] is False


class _Resp:
    def __init__(self, status=200, body=None):
        self.status, self.body = status, body or {}

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.body


def run(post_status=200, states=("locked", "unlocked")):
    seq = iter(states)
    last = {"v": states[-1]}

    def get(url, **kw):
        try:
            last["v"] = next(seq)
        except StopIteration:
            pass
        return _Resp(200, {"state": last["v"]})

    return ha_unlock(
        "http://ha:8123/", "tok", "lock.x",
        post=lambda url, **kw: _Resp(post_status),
        get=get, sleep=lambda s: None, settle_seconds=3,
    )


def test_confirmed_unlock():
    assert run(states=("locked", "unlocked")) == (True, "unlocked")
    assert run(states=("locked", "unlocking")) == (True, "unlocking")
    assert run(states=("unlocked", "opening")) == (True, "opening")


def test_http_error_is_not_reported_as_unlocked():
    ok, detail = run(post_status=401)
    assert ok is False and "401" in detail


def test_lock_that_never_moves_is_not_reported_as_unlocked():
    ok, detail = run(states=("locked",))
    assert ok is False and detail == "open_not_confirmed:locked"


def test_already_unlocked_without_a_transition_is_not_success():
    assert run(states=("unlocked",)) == (False, "open_not_confirmed:unlocked")


def test_open_uses_unlatch_service_and_accepts_a_completed_transition():
    calls = []
    states = iter([
        {"state": "unlocked", "last_changed": "before"},
        {"state": "unlocked", "last_changed": "after"},
    ])
    result = ha_unlock(
        "http://ha:8123/", "tok", "lock.x",
        post=lambda url, **kw: calls.append((url, kw)) or _Resp(),
        get=lambda *a, **k: _Resp(body=next(states)),
    )
    assert result == (True, "unlocked")
    assert calls[0][0] == "http://ha:8123/api/services/lock/open"
    assert calls[0][1]["json"] == {"entity_id": "lock.x"}


def _unlock_with(bodies, settle_seconds=3):
    seq = iter(bodies)
    last = {"v": bodies[-1]}

    def get(*a, **k):
        try:
            last["v"] = next(seq)
        except StopIteration:
            pass
        return _Resp(body=last["v"])

    return ha_unlock(
        "http://ha:8123", "tok", "lock.x",
        post=lambda *a, **k: _Resp(), get=get,
        sleep=lambda s: None, settle_seconds=settle_seconds,
    )


def test_unlatch_from_unlocked_confirmed_by_last_updated():
    # Prod: Nuki unlatched an already-unlocked door, the state never left
    # `unlocked` and last_changed did not move, but HA did record the update.
    assert _unlock_with([
        {"state": "unlocked", "last_changed": "same", "last_updated": "before"},
        {"state": "unlocked", "last_changed": "same", "last_updated": "after"},
    ]) == (True, "unlocked")


def test_unlatch_from_unlocked_confirmed_by_a_transient_state():
    assert _unlock_with([
        {"state": "unlocked", "last_changed": "t0", "last_updated": "t0"},
        {"state": "unlocked", "last_changed": "t0", "last_updated": "t0"},
        {"state": "unlatching", "last_changed": "t1", "last_updated": "t1"},
    ]) == (True, "unlatching")
    assert _unlock_with([
        {"state": "unlocked", "last_changed": "t0", "last_updated": "t0"},
        {"state": "unlatched", "last_changed": "t1", "last_updated": "t1"},
    ]) == (True, "unlatched")


def test_stale_unlocked_state_with_unchanged_timestamps_is_not_success():
    stale = {"state": "unlocked", "last_changed": "t0", "last_updated": "t0"}
    assert _unlock_with([stale, stale]) == (False, "open_not_confirmed:unlocked")


def test_timestamp_advance_into_a_locked_state_is_not_an_opening():
    assert _unlock_with([
        {"state": "unlocked", "last_changed": "t0", "last_updated": "t0"},
        {"state": "locked", "last_changed": "t1", "last_updated": "t1"},
    ]) == (False, "open_not_confirmed:locked")


def test_missing_timestamps_are_not_evidence():
    assert _unlock_with([
        {"state": "unlocked"},
        {"state": "unlocked", "last_updated": "t1"},
    ]) == (False, "open_not_confirmed:unlocked")


def test_jammed_lock_fails_the_opening_at_once():
    polls = []
    bodies = iter([{"state": "locked"}, {"state": "jammed"}, {"state": "unlocked"}])

    def get(*a, **k):
        polls.append(1)
        return _Resp(body=next(bodies))

    assert ha_unlock(
        "http://ha:8123", "tok", "lock.x",
        post=lambda *a, **k: _Resp(), get=get, sleep=lambda s: None,
    ) == (False, "open_failed:jammed")
    assert len(polls) == 2


def test_missing_ha_config_never_calls_out():
    called = []
    assert ha_unlock("", "", "lock.x", post=lambda *a, **k: called.append(1)) == (
        False, "no_ha_config")
    assert not called


def run_lock(post_status=200, states=("unlocked", "locked")):
    seq = iter(states)
    last = {"v": states[-1]}

    def get(url, **kw):
        try:
            last["v"] = next(seq)
        except StopIteration:
            pass
        return _Resp(200, {"state": last["v"]})

    return ha_lock(
        "http://ha:8123/", "tok", "lock.x",
        post=lambda url, **kw: _Resp(post_status),
        get=get, sleep=lambda s: None, settle_seconds=3,
    )


def test_confirmed_lock():
    assert run_lock(states=("unlocked", "locked")) == (True, "locked")
    assert run_lock(states=("locked", "locked")) == (True, "locked")


def test_lock_http_error_is_not_reported_as_locked():
    ok, detail = run_lock(post_status=401)
    assert ok is False and "401" in detail


def test_lock_initial_state_failure_is_not_reported_as_locked():
    res = ha_lock(
        "http://ha:8123/", "tok", "lock.x",
        get=lambda *a, **k: _Resp(401),
    )
    assert res[0] is False and "state_before_lock_failed" in res[1]


def test_lock_timeout_not_reaching_locked_state():
    ok, detail = run_lock(states=("unlocked",))
    assert ok is False and detail == "lock_not_confirmed:unlocked"


def test_locking_is_waited_out_until_locked():
    # Prod logged "Nuki locking confirmed (locking)": the motor had only started.
    assert run_lock(states=("unlocked", "locking", "locking", "locked")) == (True, "locked")


def test_lock_stuck_in_locking_is_not_reported_as_locked():
    assert run_lock(states=("unlocked", "locking")) == (False, "lock_not_confirmed:locking")


def test_jammed_or_lost_lock_fails_at_once():
    for bad in ("jammed", "unavailable", "unknown"):
        assert run_lock(states=("unlocked", "locking", bad, "locked")) == (
            False, f"lock_failed:{bad}")


def test_unavailable_lock_is_not_sent_a_command():
    called = []
    assert ha_lock(
        "http://ha:8123", "tok", "lock.x",
        post=lambda *a, **k: called.append(1) or _Resp(),
        get=lambda *a, **k: _Resp(body={"state": "unavailable"}),
        sleep=lambda s: None,
    ) == (False, "lock_failed:unavailable")
    assert not called


def test_lock_uses_lock_service():
    calls = []
    states = iter([
        {"state": "unlocked"},
        {"state": "locked"},
    ])
    result = ha_lock(
        "http://ha:8123/", "tok", "lock.x",
        post=lambda url, **kw: calls.append((url, kw)) or _Resp(),
        get=lambda *a, **k: _Resp(body=next(states)),
    )
    assert result == (True, "locked")
    assert calls[0][0] == "http://ha:8123/api/services/lock/lock"
    assert calls[0][1]["json"] == {"entity_id": "lock.x"}


def test_lock_missing_ha_config_never_calls_out():
    called = []
    assert ha_lock("", "", "lock.x", post=lambda *a, **k: called.append(1)) == (
        False, "no_ha_config"
    )
    assert not called


def test_lock_polling_network_error():
    call_count = {"n": 0}

    def flaky_get(url, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _Resp(200, {"state": "unlocked"})
        raise ConnectionResetError("network error")

    ok, detail = ha_lock(
        "http://ha:8123/", "tok", "lock.x",
        post=lambda *a, **k: _Resp(),
        get=flaky_get,
        sleep=lambda s: None,
        settle_seconds=0,
    )
    assert ok is False
    assert "lock_not_confirmed:unreadable" in detail


def test_resident_behind_a_stranger_does_not_open_the_door():
    """The verdict names a resident (right for muting an alert), but the nearest
    face matched nobody, so the door has no one to open for."""
    behind = res(lead_name=None, lead_score=0.15, faces=2)
    assert decide(behind) == (False, "lead_face_not_resident")


def test_lead_needs_the_same_confirmations_as_the_verdict():
    ep = FaceEpisode(required_confirmations=2)
    ep.record(res(faces=2))                                  # resident in front
    ep.record(res(lead_name=None, lead_score=0.1, faces=2))  # resident only behind
    verdict = ep.verdict()
    assert verdict.is_resident           # still mutes the alert, as before
    assert verdict.lead_name is None     # but only one sighting had them in front
    assert decide(verdict)[0] is False


def test_verdict_reports_the_most_faces_seen():
    ep = FaceEpisode(required_confirmations=2)
    ep.record(res(faces=1))
    ep.record(res(faces=3))
    assert ep.verdict().faces == 3


def test_confirmed_opening_notifies_with_name_and_captured_photo(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline
    from a12_system.messages import CZECH, translate_outgoing

    events, sent = [], []
    fake = SimpleNamespace(
        ha_url="http://ha", ha_token="test", nuki_entity_id="lock.x", log_prefix="[t]",
        db=SimpleNamespace(log_event=lambda *args: events.append(args)),
        notifier=SimpleNamespace(send_telegram=lambda text, **kw: sent.append((text, kw)) or True),
    )
    monkeypatch.setattr(pipeline, "ha_unlock", lambda *args: (True, "opening"))
    pipeline.DetectionPipeline._nuki_unlock_report(fake, "alice", 1, "/data/face_debug/match.jpg")
    assert events == [("unlock", "alice", 1.0)]
    text, options = sent[0]
    assert "alice" in text and "confirmed" in text
    assert options == {"media_path": "/data/face_debug/match.jpg", "bypass_cooldown": True, "critical": True}
    assert translate_outgoing(text, "cz") == CZECH[
        "Face recognised: {}. Latch opening confirmed."
    ].format("alice")

    monkeypatch.setattr(pipeline, "ha_unlock", lambda *args: (False, "open_not_confirmed:unlocked"))
    pipeline.DetectionPipeline._nuki_unlock_report(fake, "alice", 1)
    assert events[-1][0] == "unlock_failed"
    assert "NOT confirmed" in sent[-1][0]
    assert translate_outgoing(sent[-1][0], "cz") == CZECH[
        "Latch opening NOT confirmed for {} ({})"
    ].format("alice", "open_not_confirmed:unlocked")


def test_opening_thread_keeps_photo_from_the_recognition(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline

    jobs = []
    monkeypatch.setattr(
        pipeline.threading, "Thread",
        lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw)),
    )
    fake = SimpleNamespace(_last_face_crop_path="/data/face_debug/match.jpg", _nuki_unlock_worker=object())
    fake._snapshot_face_crop = lambda: fake._last_face_crop_path
    fake._door_busy = threading.Lock()
    fake.log_prefix = "[t]"
    pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "alice", 2)
    fake._last_face_crop_path = "/data/face_debug/next-person.jpg"
    assert jobs[0]["args"] == ("alice", 2, "/data/face_debug/match.jpg", False)


def test_manual_opening_sends_no_stale_face_photo(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline
    from a12_system.messages import CZECH, translate_outgoing

    jobs = []
    monkeypatch.setattr(
        pipeline.threading, "Thread",
        lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw)),
    )
    fake = SimpleNamespace(_last_face_crop_path="/data/face_debug/old.jpg", _nuki_unlock_worker=object())
    fake._snapshot_face_crop = lambda: fake._last_face_crop_path
    fake._door_busy = threading.Lock()
    fake.log_prefix = "[t]"
    pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "Telegram Admin", manual=True)
    assert jobs[0]["args"] == ("Telegram Admin", None, None, True)

    sent = []
    worker = SimpleNamespace(
        ha_url="http://ha", ha_token="test", nuki_entity_id="lock.x", log_prefix="[t]",
        db=SimpleNamespace(log_event=lambda *args: None),
        notifier=SimpleNamespace(send_telegram=lambda text, **kw: sent.append((text, kw)) or True),
    )
    monkeypatch.setattr(pipeline, "ha_unlock", lambda *args: (True, "opened"))
    pipeline.DetectionPipeline._nuki_unlock_report(worker, "Telegram Admin", None, None, True)
    text, kw = sent[0]
    assert kw["media_path"] is None
    assert "Rozpozn" not in translate_outgoing(text, CZECH)


def test_confirmed_locking_notifies_and_logs(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline
    from a12_system.messages import CZECH, translate_outgoing

    events, sent = [], []
    fake = SimpleNamespace(
        ha_url="http://ha", ha_token="test", nuki_entity_id="lock.x", log_prefix="[t]",
        db=SimpleNamespace(log_event=lambda *args: events.append(args)),
        notifier=SimpleNamespace(send_telegram=lambda text, **kw: sent.append((text, kw)) or True),
    )
    monkeypatch.setattr(pipeline, "ha_lock", lambda *args: (True, "locked"))
    pipeline.DetectionPipeline._nuki_lock_report(fake, "Telegram Admin")
    assert events == [("lock", "Telegram Admin", 1.0)]
    text, options = sent[0]
    assert "Telegram Admin" in text and "Door locked" in text
    assert options == {"bypass_cooldown": True, "critical": True}
    assert translate_outgoing(text, "cz") == CZECH["🔒 Door locked ({})."].format("Telegram Admin")

    monkeypatch.setattr(pipeline, "ha_lock", lambda *args: (False, "lock_not_confirmed:unlocked"))
    pipeline.DetectionPipeline._nuki_lock_report(fake, "Telegram Admin")
    assert events[-1] == ("lock_failed", "Telegram Admin", 0.0)
    fail_text = sent[-1][0]
    assert "Door locking NOT confirmed" in fail_text
    assert translate_outgoing(fail_text, "cz") == CZECH[
        "⚠️ Door locking NOT confirmed for {} ({})"
    ].format("Telegram Admin", "lock_not_confirmed:unlocked")


def test_locking_thread_starts(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline

    jobs = []
    monkeypatch.setattr(
        pipeline.threading, "Thread",
        lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw)),
    )
    fake = SimpleNamespace(_nuki_lock_worker=object(), _door_busy=threading.Lock(), log_prefix="[t]")
    pipeline.DetectionPipeline._trigger_nuki_lock(fake, "Telegram Admin")
    assert jobs[0]["args"] == ("Telegram Admin",)


def test_a_second_door_action_waits_for_the_first(monkeypatch):
    """Two opens racing each other: the second sees no state change and would
    report a false "not confirmed" while the door is in fact open."""
    from types import SimpleNamespace
    from a12_system import pipeline

    jobs = []
    monkeypatch.setattr(
        pipeline.threading, "Thread",
        lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw)),
    )
    fake = SimpleNamespace(
        _nuki_unlock_worker=object(), _nuki_lock_worker=object(),
        _door_busy=threading.Lock(), log_prefix="[t]",
    )
    assert pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "Telegram Admin", manual=True) is True
    assert pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "Telegram Admin", manual=True) is False
    assert pipeline.DetectionPipeline._trigger_nuki_lock(fake, "Telegram Admin") is False
    assert len(jobs) == 1


def test_the_door_is_free_again_after_the_worker_even_if_it_fails(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline

    def boom(*args):
        raise RuntimeError("ha down")

    fake = SimpleNamespace(_door_busy=threading.Lock(), _nuki_unlock_report=boom)
    fake._door_busy.acquire()
    try:
        pipeline.DetectionPipeline._nuki_unlock_worker(fake, "x")
    except RuntimeError:
        pass
    assert fake._door_busy.acquire(blocking=False)


def test_yolo_keeps_running_while_somebody_stands_still_at_the_door():
    """18:28 on 2026-10-19: the 25 s window closed while the PIR stayed on, so
    no person box and no face check for the 50 s the resident stood there."""
    from types import SimpleNamespace
    from a12_system import pipeline

    sensor = {"on": True}
    fake = SimpleNamespace(
        shared_state={"external_yolo_until": 1025.0, "last_sensor_activity": 1000.0},
        ha_monitor=SimpleNamespace(is_any_sensor_active=lambda: sensor["on"]),
        runtime_config={"external_trigger_yolo_hold_seconds": 120.0},
        log_prefix="[t]",
    )
    def is_open(t):
        return pipeline.DetectionPipeline._external_yolo_window_open(fake, t)

    assert is_open(1010.0)
    assert is_open(1060.0), "sensor still on, window must stay open"
    assert not is_open(1121.0), "a stuck sensor must not run YOLO forever"
    sensor["on"] = False
    assert not is_open(1060.0)


def test_a_critical_message_survives_a_failed_photo_upload(tmp_path):
    from types import SimpleNamespace
    from a12_system.notifier import Notifier

    photo = tmp_path / "x.jpg"
    photo.write_bytes(b"jpg")
    texts = []

    def fail(*args, **kwargs):
        raise OSError("upload timed out")

    n = Notifier.__new__(Notifier)
    n.bot = SimpleNamespace(send_photo=fail, send_message=lambda chat, text, **kw: texts.append(text))
    n.chat_id = "1"
    n.config = {}
    n.last_telegram_time = 0.0
    n._rate_limited_until = 0.0
    n._muted_until = 0.0
    n._send_lock = threading.Lock()
    n.language = lambda: "en"
    assert n.send_telegram("Door opened", media_path=str(photo), critical=True) is True
    assert texts == ["Door opened"]


def _track_pipeline(sensor_on=True):
    from types import SimpleNamespace
    from a12_system import pipeline

    p = pipeline.DetectionPipeline.__new__(pipeline.DetectionPipeline)
    p.runtime_config = {}
    p.detector = SimpleNamespace(last_person_box=(0, 0, 10, 10))
    p._pir_window_active = lambda now: sensor_on
    return p


def test_one_weak_yolo_frame_does_not_restart_the_person_track():
    """A rejected YOLO run used to drop the track id, and a new id resets the
    unlock window: one weak frame between two good checks lost the first."""
    p = _track_pipeline()
    assert p._update_face_track(100.0, 0.90)
    first = p._face_track_id
    p.detector.last_person_box = None          # YOLO saw nobody this time
    assert not p._update_face_track(102.0, 0.0)
    p.detector.last_person_box = (0, 0, 10, 10)
    assert p._update_face_track(104.0, 0.55)   # weak, but a person: still checkable
    assert p._face_track_id == first
    assert p._update_face_track(109.0, 0.90)   # quiet longer than the gap: new person
    assert p._face_track_id != first


def test_no_face_track_without_the_sensor_or_below_the_floor():
    p = _track_pipeline(sensor_on=False)
    assert not p._update_face_track(100.0, 0.95)
    p = _track_pipeline()
    assert not p._update_face_track(100.0, 0.30)


def test_the_face_budget_refills_while_somebody_keeps_standing_there():
    from types import SimpleNamespace

    p = _track_pipeline()
    episode = SimpleNamespace(checks_done=0, usable_checks=0)
    assert p._face_budget_used(100.0, episode) == (0, 0)
    episode.checks_done, episode.usable_checks = 30, 12
    assert p._face_budget_used(120.0, episode) == (30, 12)
    assert p._face_budget_used(131.0, episode) == (0, 0), "budget refilled after 30 s"
    p._pir_window_active = lambda now: False
    episode.checks_done = 60
    assert p._face_budget_used(170.0, episode) == (30, 0), "no refill once they left"


def test_a_lock_home_assistant_cannot_see_is_not_asked_to_open():
    """A queued command that fires later, plus the lock's return to "unlocked",
    would read as a confirmed opening that never happened."""
    posts = []

    def attempt(state):
        return ha_unlock(
            "http://ha:8123/", "tok", "lock.x",
            post=lambda url, **kw: posts.append(url) or _Resp(200),
            get=lambda url, **kw: _Resp(200, {"state": state}),
            sleep=lambda s: None, settle_seconds=2,
        )

    for state in ("unavailable", "unknown", "jammed"):
        ok, detail = attempt(state)
        assert ok is False and state in detail
    assert posts == []


def test_a_door_action_that_cannot_start_a_thread_frees_the_door(monkeypatch):
    from types import SimpleNamespace
    from a12_system import pipeline

    def no_thread(**kw):
        return SimpleNamespace(start=lambda: (_ for _ in ()).throw(RuntimeError("can't start new thread")))

    monkeypatch.setattr(pipeline.threading, "Thread", no_thread)
    fake = SimpleNamespace(
        _nuki_unlock_worker=object(), _nuki_lock_worker=object(),
        _door_busy=threading.Lock(), log_prefix="[t]",
    )
    for call in (
        lambda: pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "x", manual=True),
        lambda: pipeline.DetectionPipeline._trigger_nuki_lock(fake, "x"),
    ):
        try:
            call()
        except RuntimeError:
            pass
        assert fake._door_busy.acquire(blocking=False), "the door stayed 'busy' forever"
        fake._door_busy.release()


def test_a_snapshot_failure_frees_the_door():
    from types import SimpleNamespace
    from a12_system import pipeline

    def boom():
        raise ValueError("disk")

    fake = SimpleNamespace(_snapshot_face_crop=boom, _door_busy=threading.Lock(), log_prefix="[t]")
    try:
        pipeline.DetectionPipeline._trigger_nuki_unlock(fake, "x")
    except ValueError:
        pass
    assert fake._door_busy.acquire(blocking=False)
