"""The pipeline opens the door only from fresh, live, agreeing face checks.

Until 2026-10-18 the confirmation window was never built: the latch followed
the EPISODE verdict, so two sightings of a photo, minutes apart, opened the
door, and nothing ever looked at the landmarks. These tests drive the real
pipeline methods (_face_check -> _maybe_unlock_for_face) with stub checks.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from a12_system.face_quality import FaceQuality
from a12_system.face_result import FaceOutcome, FaceResult
from a12_system.test_face_episode import _StubDetector, _frame, _pipeline
from a12_system.test_face_liveness import live_row, make_yunet_row, project_face

GOOD = FaceQuality(100.0, 1.0, 100.0, 0.0, 0.0)


def check(row, name="alice", score=0.8, faces=1, quality=GOOD):
    return FaceResult(
        FaceOutcome.RESIDENT, name, score, faces=faces, lead_name=name, lead_score=score,
        quality=quality, face_row=row,
    )


def unlock_pipeline(monkeypatch, answers, *, dry_run=False, box=(100, 50, 200, 250), **cfg):
    clock = {"t": 1000.0}
    monkeypatch.setattr("a12_system.pipeline.time.time", lambda: clock["t"])
    p = _pipeline(_StubDetector(answers, box=box))
    p.runtime_config = {
        "face_recognition": {"enabled": True},
        "face_unlock.enabled": True, "face_unlock.dry_run": dry_run,
        "face_unlock.names": ["alice"], "face_unlock.min_score": 0.45,
        "face_unlock.cooldown_seconds": 60.0, **cfg,
    }
    p.person_track_ids = {"pir": "pir_1", "camera": None}
    p._last_face_unlock_at = 0.0
    p._last_face_crop_path = "/data/face_debug/x.jpg"
    p.events = []
    p.sent = []
    p.opened = []
    p.notifier = SimpleNamespace(send_telegram=lambda m, **k: p.sent.append((m, k)))
    p._trigger_nuki_unlock = lambda name, faces=None: p.opened.append((name, faces))
    p.clock = clock
    return p


def run_checks(p, count, step=0.5):
    for _ in range(count):
        face, fresh = p._face_check(_frame())
        p._maybe_unlock_for_face(face, fresh)
        p.clock["t"] += step


def test_live_moving_resident_opens_once(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(4)])
    run_checks(p, 4)
    assert p.opened == [("alice", 1)]


def test_dry_run_reports_with_weakest_score_and_crop_and_respects_cooldown(monkeypatch):
    rows = [check(live_row(0), score=0.9), check(live_row(1), score=0.7),
            check(live_row(2)), check(live_row(3))]
    p = unlock_pipeline(monkeypatch, rows, dry_run=True)
    run_checks(p, 4)
    assert p.opened == []
    assert [e for e in p.db.events if e[0] == "unlock_dry_run"] == [("unlock_dry_run", "alice", 0.7)]
    assert len(p.sent) == 1
    text, options = p.sent[0]
    assert "alice" in text and "0.70" in text and "faces in view: 1" in text
    assert options["media_path"] == "/data/face_debug/x.jpg"


def test_photo_held_still_never_opens(monkeypatch):
    row = make_yunet_row(project_face())
    p = unlock_pipeline(monkeypatch, [check(row.copy()) for _ in range(12)])
    run_checks(p, 12)
    assert p.opened == []


def test_photo_moved_in_the_image_plane_never_opens(monkeypatch):
    """Shifted, scaled and rotated copies of one flat set of landmarks."""
    base = project_face()
    rows = []
    for i in range(12):
        theta = np.radians(3.0 * i)
        rot = (1.0 + 0.02 * i) * np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
        rows.append(check(make_yunet_row(base @ rot.T + [4.0 * i, -2.0 * i])))
    p = unlock_pipeline(monkeypatch, rows)
    run_checks(p, 12)
    assert p.opened == []


def test_photo_on_a_steady_crop_reads_as_static_once_in_frame_pixels(monkeypatch):
    """The person box moves between checks; the photo does not.

    In crop pixels the landmarks shift by the box shift and would look like
    motion. Moved back to frame pixels they coincide: a static photo.
    """
    from a12_system.detection import face_row_in_frame
    from a12_system.face_liveness import check_liveness_pair

    lm = project_face()
    in_first_crop = make_yunet_row(lm - [75.0, 0.0])     # box (100,50,..) margin .25 -> origin (75, 0)
    in_second_crop = make_yunet_row(lm - [95.0, 10.0])   # box moved by (20, 10)
    assert check_liveness_pair(in_first_crop, in_second_crop)[1] != "spoof_static"
    assert check_liveness_pair(
        face_row_in_frame(in_first_crop, (75, 0)), face_row_in_frame(in_second_crop, (95, 10)),
    ) == (False, "spoof_static")


def test_check_rows_are_moved_to_frame_pixels(monkeypatch):
    row = live_row(0)
    p = unlock_pipeline(monkeypatch, [check(row)])
    _verdict, fresh = p._face_check(_frame())
    # Box (100, 50, 200, 250) with margin 0.25 starts the crop at (75, 0).
    assert fresh.face_row[0] == pytest.approx(row[0] + 75.0)
    assert fresh.face_row[5] == pytest.approx(row[5] + 0.0)
    assert fresh.face_row[2] == row[2] and fresh.face_row[14] == row[14]


def test_missing_landmarks_never_open(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(None) for _ in range(10)])
    run_checks(p, 10)
    assert p.opened == []


def test_unmeasured_quality_never_opens(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i), quality=None) for i in range(6)])
    run_checks(p, 6)
    assert p.opened == []


@pytest.mark.parametrize("bad", [
    dict(score=0.40), dict(faces=2), dict(name="bob"),
    dict(quality=FaceQuality(40.0, 1.0, 100.0, 0.0, 0.0)),   # below min_face_size 60
    dict(quality=FaceQuality(100.0, 0.5, 100.0, 0.0, 0.0)),  # face cut by the crop edge
])
def test_each_per_frame_gate_blocks(monkeypatch, bad):
    p = unlock_pipeline(monkeypatch, [check(live_row(i), **bad) for i in range(6)])
    run_checks(p, 6)
    assert p.opened == []


def test_repeated_episode_verdict_cannot_produce_confirmations(monkeypatch):
    """The verdict repeats every frame; only a check that actually ran counts."""
    p = unlock_pipeline(monkeypatch, [check(live_row(0))])
    verdict, fresh = p._face_check(_frame())
    p._maybe_unlock_for_face(verdict, fresh)
    for _ in range(20):
        p.clock["t"] += 0.5
        p._maybe_unlock_for_face(verdict, None)
        p._maybe_unlock_for_face(verdict)
    assert p.opened == []
    assert len(p.face_window.hits) == 1


def test_no_check_this_frame_returns_none(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(0)), check(live_row(1))])
    p._face_min_check_interval = 5.0
    _verdict, first = p._face_check(_frame())
    p.clock["t"] += 0.5
    verdict, second = p._face_check(_frame())
    assert first is not None and second is None


def test_no_person_track_never_opens(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(6)])
    p.person_track_ids = {"pir": None, "camera": None}
    run_checks(p, 6)
    assert p.opened == []


def test_confirmations_do_not_cross_person_tracks(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(2)])
    run_checks(p, 1)
    p.person_track_ids = {"pir": "pir_2", "camera": None}
    run_checks(p, 1)
    assert p.opened == []


def test_hits_outside_the_window_do_not_combine(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(2)],
                        **{"face_unlock.window_seconds": 5.0})
    run_checks(p, 2, step=6.0)
    assert p.opened == []


def test_closing_the_episode_drops_pending_confirmations(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(2)])
    run_checks(p, 1)
    assert len(p.face_window.hits) == 1
    p.close_face_episode(reason="quiet")
    assert len(p.face_window.hits) == 0


def test_disabled_does_nothing(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(4)],
                        **{"face_unlock.enabled": False})
    run_checks(p, 4)
    assert p.opened == [] and p.sent == []


def test_invalid_settings_fail_closed(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(4)],
                        **{"face_unlock.confirmations": 1})
    run_checks(p, 4)
    assert p.face_window is None and p.opened == []


def test_runtime_setting_change_rebuilds_the_window(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(4)])
    run_checks(p, 1)
    first = p.face_window
    p.runtime_config["face_unlock.min_score"] = 0.5
    run_checks(p, 1)
    assert p.face_window is not first
    assert p.opened == []  # the hit made under the old threshold did not carry over
    run_checks(p, 1)
    assert p.opened == [("alice", 1)]


class _Gate:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def allows(self):
        self.calls += 1
        return self.answer


def test_presence_gate_denial_blocks_and_does_not_spend_the_cooldown(monkeypatch):
    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(4)],
                        **{"face_unlock.presence_entity": "person.alice"})
    p._face_unlock_window()
    p.presence_gate = gate = _Gate((False, "presence_state:not_home"))
    run_checks(p, 2)
    assert gate.calls == 1 and p.opened == [] and p._last_face_unlock_at == 0.0
    gate.answer = (True, "presence:home")
    run_checks(p, 2)
    assert p.opened == [("alice", 1)]


def test_presence_gate_is_built_from_settings(monkeypatch):
    p = unlock_pipeline(monkeypatch, [], **{"face_unlock.presence_entity": "person.alice",
                                            "face_unlock.presence_states": ["home", "Doma"]})
    p.ha_url, p.ha_token = "http://ha:8123", "tok"
    p._face_unlock_window()
    assert p.presence_gate.entity_id == "person.alice"
    assert p.presence_gate.allowed_states == {"home", "doma"}
    p.runtime_config["face_unlock.presence_entity"] = ""
    p._face_unlock_window()
    assert p.presence_gate is None


def test_names_given_as_text_are_split_not_spelled(monkeypatch):
    p = unlock_pipeline(monkeypatch, [], **{"face_unlock.names": "alice, bob"})
    assert p._face_unlock_window().allowed_names == {"alice", "bob"}
