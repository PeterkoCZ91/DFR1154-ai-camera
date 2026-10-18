"""Quality rejection and strict identity/time boundaries of the offline proposal."""

import cv2
import numpy as np
import pytest

from a12_system.face_quality import QualityPolicy, measure_face_quality, quality_reasons
from a12_system.face_result import FaceOutcome, FaceResult
from a12_system.face_window import FaceUnlockWindow


def match(name="alice", score=.8, faces=1):
    return FaceResult(FaceOutcome.RESIDENT, name, score, faces, name, score)


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
