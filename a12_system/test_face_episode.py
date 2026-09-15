"""When the face check may run, and how one occurrence decides.

Two changes that only make sense together.

GATE. The check used to run on every confirmed person detection, on the full
frame, always. That is what made it expensive enough to be blamed for the OOM
kills at a 2 GB limit (`~/.codex/memories/a12_system_v2.md`, 2026-05-06) and to
be switched off. But faces are only large enough to embed when somebody is
standing in the doorway — median 99px with a person deliberately facing the
camera against a median of 35px in ordinary passage — and the PIR already knows
when that is happening. Outside the PIR window the check costs a lot and can
answer almost nothing.

EPISODE. `identify_person` was called once, on one frame, and that single
answer decided the notification. The clip buffer holds dozens of frames of the
same occurrence. Deciding per episode instead of per frame is the largest gain
available without changing the model.

The aggregation rule is deliberately asymmetric: recognition is used as a
high-precision SUPPRESSOR ("this is certainly a resident, stay quiet") and
never as a stranger detector. So a resident verdict needs repeated agreement,
while a single unmatched face is enough to say a stranger was seen.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.detection import crop_person_box
from a12_system.face_result import (
    FaceEpisode,
    FaceOutcome,
    FaceResult,
    should_run_face_check,
)

RESIDENT = FaceResult(FaceOutcome.RESIDENT, "Resident")
OTHER = FaceResult(FaceOutcome.RESIDENT, "Other")
STRANGER = FaceResult(FaceOutcome.STRANGER)
NO_FACE = FaceResult(FaceOutcome.NO_FACE)
ERROR = FaceResult(FaceOutcome.ERROR)


def _gate(**overrides):
    kwargs = dict(
        enabled=True,
        pir_window_active=True,
        require_pir_window=True,
        have_person_box=True,
        checks_done=0,
        max_checks=5,
        now=100.0,
        last_check_at=0.0,
        min_interval=0.5,
    )
    kwargs.update(overrides)
    return should_run_face_check(**kwargs)


# --- the gate --------------------------------------------------------------


def test_the_check_runs_inside_the_pir_window():
    assert _gate() is True


def test_the_check_is_skipped_outside_the_pir_window():
    """The expensive case that bought almost nothing."""
    assert _gate(pir_window_active=False) is False


def test_the_pir_requirement_can_be_turned_off():
    """A deployment without a PIR must still be able to use recognition."""
    assert _gate(pir_window_active=False, require_pir_window=False) is True


def test_the_check_is_skipped_without_a_person_box():
    """Cropping to the box is the point; a whole frame is the old cost."""
    assert _gate(have_person_box=False) is False


def test_the_budget_per_episode_is_bounded():
    assert _gate(checks_done=4, max_checks=5) is True
    assert _gate(checks_done=5, max_checks=5) is False


def test_checks_are_spaced_out():
    """Consecutive decoded frames are ~0.1 s apart and nearly identical."""
    assert _gate(now=100.0, last_check_at=99.9, min_interval=0.5) is False
    assert _gate(now=100.0, last_check_at=99.4, min_interval=0.5) is True


def test_a_disabled_feature_never_runs():
    assert _gate(enabled=False) is False
    assert _gate(enabled=False, pir_window_active=True, checks_done=0) is False


# --- the episode verdict ---------------------------------------------------


def test_an_empty_episode_claims_nothing():
    assert FaceEpisode().verdict().outcome is FaceOutcome.UNAVAILABLE


def test_one_match_is_not_enough_to_suppress_an_alert():
    """A false accept here hides a real stranger, so require agreement."""
    ep = FaceEpisode(required_confirmations=2)
    ep.record(RESIDENT)
    assert ep.verdict().outcome is not FaceOutcome.RESIDENT


def test_repeated_agreement_makes_a_resident():
    ep = FaceEpisode(required_confirmations=2)
    ep.record(RESIDENT)
    ep.record(RESIDENT)
    verdict = ep.verdict()
    assert verdict.outcome is FaceOutcome.RESIDENT
    assert verdict.name == "Resident"


def test_confirmations_must_name_the_same_person():
    """Two different people once each is not two sightings of one person."""
    ep = FaceEpisode(required_confirmations=2)
    ep.record(RESIDENT)
    ep.record(OTHER)
    assert ep.verdict().outcome is not FaceOutcome.RESIDENT


def test_a_resident_outvotes_unreadable_frames():
    """Most frames of a real passage carry no usable face at all."""
    ep = FaceEpisode(required_confirmations=2)
    for _ in range(9):
        ep.record(NO_FACE)
    ep.record(RESIDENT)
    ep.record(RESIDENT)
    assert ep.verdict().outcome is FaceOutcome.RESIDENT


def test_one_unmatched_face_is_enough_to_report_a_stranger():
    ep = FaceEpisode(required_confirmations=2)
    ep.record(NO_FACE)
    ep.record(STRANGER)
    assert ep.verdict().outcome is FaceOutcome.STRANGER


def test_a_resident_still_wins_over_a_single_unmatched_frame():
    """One bad angle during an episode must not turn a resident into a stranger."""
    ep = FaceEpisode(required_confirmations=2)
    ep.record(STRANGER)
    ep.record(RESIDENT)
    ep.record(RESIDENT)
    assert ep.verdict().outcome is FaceOutcome.RESIDENT


def test_an_episode_of_unreadable_frames_says_no_face():
    ep = FaceEpisode()
    for _ in range(5):
        ep.record(NO_FACE)
    assert ep.verdict().outcome is FaceOutcome.NO_FACE


def test_errors_never_become_evidence():
    ep = FaceEpisode()
    for _ in range(5):
        ep.record(ERROR)
    assert ep.verdict().outcome is not FaceOutcome.STRANGER
    assert ep.verdict().outcome is not FaceOutcome.RESIDENT


def test_the_episode_counts_its_checks_for_the_budget():
    ep = FaceEpisode()
    assert ep.checks_done == 0
    ep.record(NO_FACE)
    ep.record(STRANGER)
    assert ep.checks_done == 2


def test_reset_forgets_the_previous_occurrence():
    """Two people a minute apart must not share a verdict."""
    ep = FaceEpisode(required_confirmations=2)
    ep.record(RESIDENT)
    ep.record(RESIDENT)
    ep.reset()
    assert ep.checks_done == 0
    assert ep.verdict().outcome is FaceOutcome.UNAVAILABLE


# --- cropping to the person box -------------------------------------------


def _frame(h=480, w=640):
    return np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)


def test_the_crop_follows_the_person_box():
    crop = crop_person_box(_frame(), (100, 50, 200, 250), margin=0.0)
    assert crop.shape[:2] == (200, 100)


def test_the_crop_adds_margin_because_the_box_cuts_the_head():
    """YOLO person boxes clip the crown; a face needs the whole head."""
    crop = crop_person_box(_frame(), (100, 50, 200, 250), margin=0.5)
    assert crop.shape[0] > 200 and crop.shape[1] > 100


def test_the_crop_is_clamped_to_the_frame():
    crop = crop_person_box(_frame(120, 160), (0, 0, 160, 120), margin=1.0)
    assert crop.shape[:2] == (120, 160)


def test_a_missing_box_falls_back_to_the_whole_frame():
    frame = _frame()
    assert crop_person_box(frame, None) is frame


def test_a_degenerate_box_falls_back_to_the_whole_frame():
    frame = _frame()
    for box in ((10, 10, 10, 10), (50, 50, 10, 10), (-5, -5, -1, -1)):
        assert crop_person_box(frame, box) is frame


def test_a_box_too_small_for_a_face_is_not_rescued_by_the_margin():
    """Padding can inflate a 10px box past the size guard without adding a face.

    A mutation that dropped the pre-margin size check survived the tests above,
    because at the default margin the post-margin check happened to catch the
    same cases. At a larger margin it does not.
    """
    frame = _frame()
    assert crop_person_box(frame, (100, 100, 110, 110), margin=1.0) is frame


# --- the wiring ------------------------------------------------------------
# A mutation that made the pipeline use the single frame's answer instead of
# the episode verdict survived, because nothing exercised the wiring at all.


class _StubDetector:
    def __init__(self, answers, box=(100, 50, 200, 250)):
        self.answers = list(answers)
        self.last_person_box = box
        self.calls = []

    def identify_person(self, frame):
        self.calls.append(frame)
        return self.answers.pop(0) if self.answers else NO_FACE


class _StubStats:
    def __init__(self):
        self.recorded = []

    def record_face_attempt(self, result):
        self.recorded.append(result)


def _pipeline(detector, *, pir=True, face_cfg=None, **overrides):
    from a12_system.pipeline import DetectionPipeline

    p = DetectionPipeline.__new__(DetectionPipeline)
    p.detector = detector
    p.stats = _StubStats()
    p.shared_state = {"external_yolo_until": 1e12 if pir else 0.0}
    p.ha_monitor = None
    # Call the real wiring rather than re-declaring its attributes here.
    cfg = {"min_check_interval_seconds": 0.0}
    cfg.update(face_cfg or {})
    p.configure_face_checks(cfg)
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def test_the_pipeline_decides_on_the_episode_not_on_one_frame():
    det = _StubDetector([RESIDENT])
    p = _pipeline(det)
    assert p._face_verdict(_frame()).outcome is not FaceOutcome.RESIDENT, (
        "one frame's answer must not decide the occurrence"
    )
    det.answers = [RESIDENT]
    assert p._face_verdict(_frame()).outcome is FaceOutcome.RESIDENT


def test_the_pipeline_does_not_call_the_detector_outside_the_pir_window():
    det = _StubDetector([RESIDENT])
    p = _pipeline(det, pir=False)
    p._face_verdict(_frame())
    assert det.calls == [], "the expensive check ran with nobody at the door"


def test_the_pipeline_hands_the_detector_a_crop_not_the_whole_frame():
    det = _StubDetector([NO_FACE])
    p = _pipeline(det)
    frame = _frame()
    p._face_verdict(frame)
    assert det.calls[0].shape[0] < frame.shape[0]


def test_the_pipeline_stops_checking_once_the_budget_is_spent():
    det = _StubDetector([NO_FACE] * 20)
    p = _pipeline(det, _face_max_checks=3)
    for _ in range(10):
        p._face_verdict(_frame())
    assert len(det.calls) == 3


def test_two_occurrences_apart_in_time_do_not_share_a_verdict(monkeypatch):
    """The second person must not inherit the first one's suppression.

    A mutation that never reset the episode survived: `FaceEpisode.reset` was
    tested directly, but nothing checked that the pipeline ever calls it.
    """
    from a12_system import pipeline as pipeline_module

    clock = {"t": 1000.0}
    monkeypatch.setattr(pipeline_module.time, "time", lambda: clock["t"])

    det = _StubDetector([RESIDENT, RESIDENT])
    p = _pipeline(det, _face_episode_gap=30.0)

    p._face_verdict(_frame())
    clock["t"] = 1100.0  # a quiet gap longer than the episode gap
    assert p._face_verdict(_frame()).outcome is not FaceOutcome.RESIDENT, (
        "the earlier sighting still counted towards a later occurrence"
    )


def test_sightings_within_one_occurrence_accumulate(monkeypatch):
    """The same test without the gap, so the assertion above means something."""
    from a12_system import pipeline as pipeline_module

    clock = {"t": 1000.0}
    monkeypatch.setattr(pipeline_module.time, "time", lambda: clock["t"])

    det = _StubDetector([RESIDENT, RESIDENT])
    p = _pipeline(det, _face_episode_gap=30.0)

    p._face_verdict(_frame())
    clock["t"] = 1002.0
    assert p._face_verdict(_frame()).outcome is FaceOutcome.RESIDENT


# --- keeping what the check saw -------------------------------------------


def test_nothing_is_written_when_debug_saving_is_off(tmp_path):
    det = _StubDetector([NO_FACE])
    p = _pipeline(det, _face_debug_dir="")
    p._face_verdict(_frame())
    assert list(tmp_path.iterdir()) == []


def test_the_crop_is_written_when_debug_saving_is_on(tmp_path):
    det = _StubDetector([NO_FACE])
    p = _pipeline(det, _face_debug_dir=str(tmp_path))
    p._face_verdict(_frame())
    written = list(tmp_path.iterdir())
    assert len(written) == 1
    assert "no_face" in written[0].name


def test_the_bound_survives_a_restart(tmp_path):
    """The counter used to start at zero in every Detector, so each A12 restart
    granted a fresh quota — and during a camera crash loop A12 restarts a lot.
    The cap has to be a property of the directory, not of the process."""
    # Through the real config wiring, not by setting the attribute afterwards:
    # the seeding happens while the pipeline is being configured.
    cfg = {"debug_crop_dir": str(tmp_path), "debug_crop_limit": 3}

    first = _pipeline(
        _StubDetector([NO_FACE] * 50), face_cfg=dict(cfg),
        _face_max_checks=50, _face_episode_gap=1e9,
    )
    for _ in range(5):
        first._face_verdict(_frame())
    assert len(list(tmp_path.iterdir())) == 3

    restarted = _pipeline(
        _StubDetector([NO_FACE] * 50), face_cfg=dict(cfg),
        _face_max_checks=50, _face_episode_gap=1e9,
    )
    for _ in range(5):
        restarted._face_verdict(_frame())
    assert len(list(tmp_path.iterdir())) == 3, "a restart re-armed the quota"


def test_saving_is_bounded_so_it_cannot_fill_the_disk(tmp_path):
    det = _StubDetector([NO_FACE] * 50)
    p = _pipeline(
        det, _face_debug_dir=str(tmp_path), _face_debug_limit=3,
        _face_max_checks=50, _face_episode_gap=1e9,
    )
    for _ in range(10):
        p._face_verdict(_frame())
    assert len(list(tmp_path.iterdir())) == 3


def test_a_failed_write_does_not_break_the_check(tmp_path):
    """Debug output is a convenience; it must never cost a detection."""
    det = _StubDetector([STRANGER])
    p = _pipeline(det, _face_debug_dir=str(tmp_path / "nope" / "\0bad"))
    assert p._face_verdict(_frame()).outcome is FaceOutcome.STRANGER
