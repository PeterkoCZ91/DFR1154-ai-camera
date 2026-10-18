"""Quality rejection and strict identity/time boundaries of the offline proposal."""

import cv2
import numpy as np
import pytest

from a12_system.face_quality import QualityPolicy, measure_face_quality, quality_reasons
from a12_system.face_result import FaceOutcome, FaceResult
from a12_system.face_window import FaceUnlockWindow
from a12_system.test_face_liveness import live_row

_step = {"n": 0}


def match(name="alice", score=.8, faces=1, face_row=...):
    """A matching check whose landmarks show a live head, a new pose each call."""
    if face_row is ...:
        _step["n"] += 1
        face_row = live_row(_step["n"])
    return FaceResult(FaceOutcome.RESIDENT, name, score, faces, name, score, face_row=face_row)


def test_quality_measures_face_not_detailed_background():
    frame = np.random.default_rng(0).integers(0, 256, (160, 160, 3), dtype=np.uint8)
    frame[40:120, 40:120] = 128
    q = measure_face_quality(frame, [40, 40, 80, 80])
    assert q.sharpness == 0
    assert quality_reasons(q, QualityPolicy(min_sharpness=10)) == ("blurred",)


def test_blur_and_exposure_are_separate_measurements():
    image = np.random.default_rng(1).integers(30, 225, (100, 100), dtype=np.uint8)
    sharp = measure_face_quality(image, [0, 0, 100, 100])
    blurry = measure_face_quality(cv2.GaussianBlur(image, (15, 15), 5), [0, 0, 100, 100])
    assert sharp.sharpness > blurry.sharpness
    assert blurry.dark_fraction == blurry.bright_fraction == 0
    assert quality_reasons(measure_face_quality(np.zeros_like(image), [0, 0, 100, 100]),
                           QualityPolicy(max_dark_fraction=.5)) == ("dark",)
    assert quality_reasons(measure_face_quality(np.full_like(image, 255), [0, 0, 100, 100]),
                           QualityPolicy(max_bright_fraction=.5)) == ("overexposed",)


def test_clipped_small_and_invalid_faces_are_detected():
    image = np.full((100, 100, 3), 128, dtype=np.uint8)
    q = measure_face_quality(image, [-20, 0, 40, 40])
    assert q.visible_fraction == .5
    assert quality_reasons(q, QualityPolicy(min_size=60)) == ("small", "clipped")
    for box in ([0, 0, 0, 10], [200, 200, 20, 20], [0, 0, float("nan"), 20]):
        assert quality_reasons(measure_face_quality(image, box), QualityPolicy()) == ("invalid_face",)


def test_weak_then_two_strong_matches_can_recover_in_one_track():
    w = FaceUnlockWindow(allowed_names=["alice"])
    assert not w.record(match(score=.38), at=1, track_id="track", quality_ok=True)[0]
    assert not w.record(match(score=.77), at=3, track_id="track", quality_ok=True)[0]
    assert w.record(match(score=.78), at=5, track_id="track", quality_ok=True)[0]


@pytest.mark.parametrize("middle,quality,track", [
    (match("bob"), True, "track"),
    (FaceResult(FaceOutcome.STRANGER, faces=1), True, "track"),
    (match(), False, "track"),
    (match(faces=2), True, "track"),
    (match(score=float("nan")), True, "track"),
    (match(), True, "different"),
    (match(), True, None),
])
def test_conflicting_or_unusable_observation_cannot_bridge_two_good_matches(middle, quality, track):
    w = FaceUnlockWindow(allowed_names=["alice"])
    assert not w.record(match(), at=1, track_id="track", quality_ok=True)[0]
    assert not w.record(middle, at=2, track_id=track, quality_ok=quality)[0]
    assert not w.record(match(), at=3, track_id="track", quality_ok=True)[0]


def test_old_or_repeated_frame_cannot_supply_a_second_confirmation():
    w = FaceUnlockWindow(allowed_names=["alice"], seconds=5)
    for at in (1, 7, 7, 6):
        assert not w.record(match(), at=at, track_id="track", quality_ok=True)[0]


def test_different_allowed_names_do_not_share_confirmations():
    w = FaceUnlockWindow(allowed_names=["alice", "bob"])
    assert not w.record(match(), at=1, track_id="track", quality_ok=True)[0]
    assert not w.record(match("bob"), at=2, track_id="track", quality_ok=True)[0]
    assert w.record(match("bob"), at=3, track_id="track", quality_ok=True)[0]


def test_check_without_landmarks_never_confirms():
    """Fail closed: no face row means no liveness evidence, means no opening."""
    w = FaceUnlockWindow(allowed_names=["alice"])
    for at in range(1, 8):
        assert w.record(match(face_row=None), at=at, track_id="track", quality_ok=True) == (
            False, "no_landmarks"
        )
    # One row-less check also erases the confirmation before it.
    assert not w.record(match(), at=10, track_id="track", quality_ok=True)[0]
    assert not w.record(match(face_row=None), at=11, track_id="track", quality_ok=True)[0]
    assert not w.record(match(), at=12, track_id="track", quality_ok=True)[0]


def test_no_face_frames_neither_count_nor_erase():
    """78% of person frames hold no face; they are not evidence either way."""
    w = FaceUnlockWindow(allowed_names=["alice"], seconds=5)
    assert not w.record(match(), at=1, track_id="track", quality_ok=True)[0]
    assert w.record(FaceResult(FaceOutcome.NO_FACE), at=2, track_id="track", quality_ok=False) == (
        False, "no_face"
    )
    assert w.record(match(), at=3, track_id="track", quality_ok=True)[0]
    # ...but the window still ages them out.
    assert not w.record(match(), at=10, track_id="track", quality_ok=True)[0]
    assert not w.record(FaceResult(FaceOutcome.NO_FACE), at=12, track_id="track", quality_ok=True)[0]
    assert not w.record(match(), at=16, track_id="track", quality_ok=True)[0]


def test_one_confirmation_opens_once():
    w = FaceUnlockWindow(allowed_names=["alice"])
    assert not w.record(match(), at=1, track_id="track", quality_ok=True)[0]
    assert w.record(match(score=.7), at=2, track_id="track", quality_ok=True)[0]
    assert w.confirmed_score == .7
    assert not w.record(match(), at=3, track_id="track", quality_ok=True)[0]
