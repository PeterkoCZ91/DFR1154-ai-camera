"""The door must open only on a strong, listed, unrepeated resident verdict,
and must never be reported open unless Home Assistant says the lock moved."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.door_unlock import ha_unlock, unlock_decision
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


def run(post_status=200, states=("unlocked",)):
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
    assert run(states=("unlocked",)) == (True, "unlocked")
    assert run(states=("locked", "unlocking")) == (True, "unlocking")


def test_http_error_is_not_reported_as_unlocked():
    ok, detail = run(post_status=401)
    assert ok is False and "401" in detail


def test_lock_that_never_moves_is_not_reported_as_unlocked():
    ok, detail = run(states=("locked",))
    assert ok is False and detail == "state_after_unlock:locked"


def test_missing_ha_config_never_calls_out():
    called = []
    assert ha_unlock("", "", "lock.x", post=lambda *a, **k: called.append(1)) == (
        False, "no_ha_config")
    assert not called


class _Cfg(dict):
    pass


def _pipeline(dry_run):
    from types import SimpleNamespace

    from a12_system.pipeline import DetectionPipeline as P

    events, sent, opened = [], [], []
    cfg = {
        "face_unlock.enabled": True, "face_unlock.dry_run": dry_run,
        "face_unlock.names": ["alice"], "face_unlock.min_score": 0.6,
        "face_unlock.cooldown_seconds": 60.0,
    }
    fake = SimpleNamespace(
        runtime_config=SimpleNamespace(get=lambda k, d=None: cfg.get(k, d)),
        _last_face_unlock_at=0.0, log_prefix="[t]",
        db=SimpleNamespace(log_event=lambda *a, **k: events.append(a)),
        notifier=SimpleNamespace(send_telegram=lambda m, **k: sent.append(m)),
        _trigger_nuki_unlock=lambda name, faces=None: opened.append((name, faces)),
    )
    return P._maybe_unlock_for_face, fake, events, sent, opened


def test_dry_run_reports_but_never_opens_and_respects_cooldown():
    fn, fake, events, sent, opened = _pipeline(dry_run=True)
    fn(fake, RESIDENT)
    fn(fake, RESIDENT)  # second call falls inside the cooldown
    assert opened == [] and len(sent) == 1 and events[0][0] == "unlock_dry_run"
    assert "faces in view: 1" in sent[0]


def test_dry_run_message_carries_the_face_count():
    fn, fake, events, sent, opened = _pipeline(dry_run=True)
    fn(fake, res(faces=3))
    assert "faces in view: 3" in sent[0]


def test_live_mode_opens_once_for_the_listed_resident():
    fn, fake, events, sent, opened = _pipeline(dry_run=False)
    fn(fake, res(faces=2))
    fn(fake, FaceResult(FaceOutcome.STRANGER))
    fn(fake, RESIDENT)
    assert opened == [("alice", 2)]


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


def test_dry_run_message_attaches_the_latest_face_crop():
    fn, fake, events, sent, opened = _pipeline(dry_run=True)
    attached = []
    fake.notifier.send_telegram = lambda m, **k: attached.append(k.get("media_path"))
    fake._last_face_crop_path = "/data/face_debug/x.jpg"
    fn(fake, RESIDENT)
    assert attached == ["/data/face_debug/x.jpg"]
