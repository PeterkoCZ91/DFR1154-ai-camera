"""Unit tests for 3D parallax and motion liveness detection in YuNet face pipeline."""

import math
import numpy as np
import pytest

from a12_system.face_liveness import (
    FaceGeometry,
    LivenessPolicy,
    check_liveness_pair,
    check_liveness_sequence,
    compute_face_geometry,
    extract_landmarks,
    nose_parallax,
)
from a12_system.face_result import FaceOutcome, FaceResult
from a12_system.face_window import FaceUnlockWindow


def make_yunet_row(landmarks, bbox=(10.0, 10.0, 100.0, 100.0), score=0.95):
    """Build a 15-float YuNet row: [x, y, w, h, x_re, y_re, x_le, y_le, x_nt, y_nt, x_rc, y_rc, x_lc, y_lc, score]."""
    x, y, w, h = bbox
    row = [x, y, w, h]
    for pt in landmarks:
        row.extend([float(pt[0]), float(pt[1])])
    row.append(score)
    return np.array(row, dtype=np.float64)


# Eyes, nose tip (protruding 35 units towards the camera) and mouth corners.
FACE_3D = np.array([
    [-50.0, 0.0, 0.0],
    [50.0, 0.0, 0.0],
    [0.0, 40.0, 35.0],
    [-30.0, 70.0, 5.0],
    [30.0, 70.0, 5.0],
], dtype=np.float64)


def project_face(yaw=0.0, pitch=0.0, distance=600.0, focal=500.0, shift=(320.0, 240.0)):
    """Pinhole projection of FACE_3D after turning the head."""
    ry, rx = np.radians(yaw), np.radians(pitch)
    Ry = np.array([[np.cos(ry), 0, np.sin(ry)], [0, 1, 0], [-np.sin(ry), 0, np.cos(ry)]])
    Rx = np.array([[1, 0, 0], [0, np.cos(rx), -np.sin(rx)], [0, np.sin(rx), np.cos(rx)]])
    rotated = FACE_3D @ Rx.T @ Ry.T
    z = rotated[:, 2] + distance
    return np.column_stack([
        rotated[:, 0] * focal / z + shift[0], rotated[:, 1] * focal / z + shift[1],
    ])


def live_row(step):
    """Landmark row of a live head, turned a further 15 degrees each step."""
    return make_yunet_row(project_face(yaw=-15.0 + 15.0 * (step % 3), pitch=4.0 * (step % 2)))


@pytest.fixture
def base_landmarks():
    """Frontal face landmark coordinates."""
    return np.array([
        [100.0, 100.0],  # right eye
        [200.0, 100.0],  # left eye
        [150.0, 150.0],  # nose tip
        [120.0, 180.0],  # right mouth corner
        [180.0, 180.0],  # left mouth corner
    ], dtype=np.float64)


def test_extract_landmarks_valid(base_landmarks):
    row = make_yunet_row(base_landmarks)
    extracted = extract_landmarks(row)
    assert extracted.shape == (5, 2)
    assert np.allclose(extracted, base_landmarks)


def test_extract_landmarks_degenerate():
    with pytest.raises(ValueError, match="face_row is None"):
        extract_landmarks(None)

    with pytest.raises(ValueError, match="at least 14 elements"):
        extract_landmarks([1.0, 2.0, 3.0])

    nan_row = [0.0] * 15
    nan_row[4] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        extract_landmarks(nan_row)

    inf_row = [0.0] * 15
    inf_row[5] = float("inf")
    with pytest.raises(ValueError, match="non-finite"):
        extract_landmarks(inf_row)


def test_compute_face_geometry_standard(base_landmarks):
    geom = compute_face_geometry(base_landmarks)
    assert isinstance(geom, FaceGeometry)
    assert math.isclose(geom.d_eyes, 100.0, rel_tol=1e-5)
    assert math.isclose(geom.xi, 0.0, abs_tol=1e-5)
    assert math.isclose(geom.eta, 0.5, rel_tol=1e-5)
    assert math.isclose(geom.r_yaw, 1.0, rel_tol=1e-5)


def test_compute_face_geometry_degenerate():
    # All zeros
    geom_zero = compute_face_geometry(np.zeros((5, 2)))
    assert geom_zero.d_eyes == 0.0
    assert geom_zero.xi == 0.0
    assert geom_zero.eta == 0.0
    assert geom_zero.r_yaw == 1.0

    # NaNs
    geom_nan = compute_face_geometry(np.full((5, 2), float("nan")))
    assert geom_nan.d_eyes == 0.0
    assert geom_nan.r_yaw == 1.0

    # Coincident eyes
    coincident = np.array([
        [50.0, 50.0],
        [50.0, 50.0],
        [50.0, 70.0],
        [40.0, 90.0],
        [60.0, 90.0],
    ])
    geom_coincident = compute_face_geometry(coincident)
    assert geom_coincident.d_eyes == 0.0


def test_static_photo_rejection(base_landmarks):
    row1 = make_yunet_row(base_landmarks)
    row2 = make_yunet_row(base_landmarks)

    is_live, reason = check_liveness_pair(row1, row2)
    assert not is_live
    assert reason == "spoof_static"

    # Micro-jitter (< min_disp_px=1.2 and < min_scale_change=0.015)
    jittered = base_landmarks + 0.3
    row_jitter = make_yunet_row(jittered)
    is_live_j, reason_j = check_liveness_pair(row1, row_jitter)
    assert not is_live_j
    assert reason_j == "spoof_static"


def test_planar_moving_photo_rejection(base_landmarks):
    row1 = make_yunet_row(base_landmarks)

    # 1. Pure 2D translation (mean displacement >= 1.2 px)
    translated = base_landmarks + np.array([5.0, 3.0])
    row_trans = make_yunet_row(translated)
    is_live, reason = check_liveness_pair(row1, row_trans)
    assert not is_live
    assert reason == "spoof_planar"

    # 2. Pure 2D scaling (photo brought closer to camera)
    scaled = base_landmarks * 1.08
    row_scaled = make_yunet_row(scaled)
    is_live, reason = check_liveness_pair(row1, row_scaled)
    assert not is_live
    assert reason == "spoof_planar"

    # 3. 2D Affine rotation + scale + translation (moving photo in front of camera)
    theta = np.radians(12.0)
    scale = 1.06
    R = scale * np.array([
        [np.cos(theta), -np.sin(theta)],
        [np.sin(theta), np.cos(theta)],
    ])
    trans = np.array([15.0, -8.0])
    affine_lm = base_landmarks @ R.T + trans
    row_affine = make_yunet_row(affine_lm)
    is_live, reason = check_liveness_pair(row1, row_affine)
    assert not is_live
    assert reason == "spoof_planar"


def test_real_3d_head_movement_passes():
    # A 15 degree turn and a 12 degree nod move the nose ~0.08 eye distances
    # off the eyes+mouth plane, above the 0.06 default.
    row1 = make_yunet_row(project_face())
    is_live, reason = check_liveness_pair(row1, make_yunet_row(project_face(yaw=15.0)))
    assert is_live
    assert reason == "live_confirmed"

    is_live_pitch, reason_pitch = check_liveness_pair(row1, make_yunet_row(project_face(pitch=12.0)))
    assert is_live_pitch
    assert reason_pitch == "live_confirmed"


def test_small_turn_is_below_the_default_threshold():
    """Calibration fact, pinned: a 5 degree turn is ~0.027, under the 0.06 bar."""
    lm1, lm2 = project_face(), project_face(yaw=5.0)
    assert 0.02 < nose_parallax(lm1, lm2) < 0.035
    assert not check_liveness_pair(make_yunet_row(lm1), make_yunet_row(lm2))[0]


def test_tilted_photo_is_planar_under_perspective():
    """A flat photo turned 25 degrees moves by a homography, not an affine map.

    The old affine-residual test read this perspective as 3D motion; the
    homography of the eyes and mouth predicts the nose exactly.
    """
    flat = FACE_3D.copy()
    flat[:, 2] = 0.0
    def project_flat(yaw, distance=250.0):
        ry = np.radians(yaw)
        Ry = np.array([[np.cos(ry), 0, np.sin(ry)], [0, 1, 0], [-np.sin(ry), 0, np.cos(ry)]])
        r = flat @ Ry.T
        z = r[:, 2] + distance
        return np.column_stack([r[:, 0] * 500 / z + 320, r[:, 1] * 500 / z + 240])
    lm1, lm2 = project_flat(0.0), project_flat(25.0)
    assert nose_parallax(lm1, lm2) < 1e-6
    assert check_liveness_pair(make_yunet_row(lm1), make_yunet_row(lm2)) == (False, "spoof_planar")


def test_sequence_fails_closed_on_missing_or_static_rows(base_landmarks):
    moving = [live_row(0), live_row(1)]
    assert check_liveness_sequence(moving) == (True, "live_confirmed")
    assert check_liveness_sequence([live_row(0)]) == (False, "too_few_observations")
    assert check_liveness_sequence([live_row(0), None]) == (False, "invalid_landmarks")
    assert check_liveness_sequence([None, None]) == (False, "invalid_landmarks")
    # A frozen pair anywhere in the sequence refuses, even if the ends moved.
    frozen = [live_row(0), live_row(1), live_row(1)]
    assert check_liveness_sequence(frozen) == (False, "spoof_static")


def test_sequence_judges_first_against_last_not_only_the_newest_pair():
    """Turned, frontal, turned again and shifted: the newest pair shows parallax,
    but the first and last are the same pose moved in the image plane."""
    rows = [
        make_yunet_row(project_face(yaw=-15.0)),
        make_yunet_row(project_face()),
        make_yunet_row(project_face(yaw=-15.0, shift=(330.0, 245.0))),
    ]
    assert check_liveness_pair(rows[1], rows[2])[0]
    assert check_liveness_sequence(rows) == (False, "spoof_planar")


def test_face_unlock_window_graceful_awaiting(base_landmarks):
    w = FaceUnlockWindow(allowed_names=["alice"], confirmations=2, seconds=5.0)

    res = FaceResult(
        FaceOutcome.RESIDENT,
        name="alice",
        score=0.85,
        faces=1,
        lead_name="alice",
        lead_score=0.85,
    )

    row_static1 = make_yunet_row(base_landmarks)
    row_static2 = make_yunet_row(base_landmarks)  # exactly identical

    # Frame 1: first confirmation recorded
    allowed1, reason1 = w.record(res, at=1.0, track_id="track1", quality_ok=True, face_row=row_static1)
    assert not allowed1
    assert reason1 == "awaiting"
    assert len(w.hits) == 1

    # Frame 2: second confirmation reached, but photo is static -> awaiting:spoof_static
    allowed2, reason2 = w.record(res, at=1.5, track_id="track1", quality_ok=True, face_row=row_static2)
    assert not allowed2
    assert reason2 == "awaiting:spoof_static"
    # Crucial check: hits are NOT cleared and track_id is NOT dropped
    assert len(w.hits) == 2
    assert w.track_id == "track1"

    # Frame 3: planar moving photo presented -> awaiting:spoof_planar
    translated = base_landmarks + np.array([8.0, 4.0])
    row_planar = make_yunet_row(translated)
    allowed3, reason3 = w.record(res, at=2.0, track_id="track1", quality_ok=True, face_row=row_planar)
    assert not allowed3
    assert reason3 == "awaiting:spoof_planar"
    assert len(w.hits) == 2

    # Frame 4: natural 3D head motion occurs with out-of-plane parallax -> confirmed!
    row_3d = make_yunet_row(project_face(yaw=15.0))
    allowed4, reason4 = w.record(res, at=2.5, track_id="track1", quality_ok=True, face_row=row_3d)
    assert allowed4
    assert reason4 == "confirmed"


def test_check_liveness_pair_degenerate_inputs(base_landmarks):
    row = make_yunet_row(base_landmarks)

    assert check_liveness_pair(None, row) == (False, "invalid_landmarks")
    assert check_liveness_pair(row, None) == (False, "invalid_landmarks")
    assert check_liveness_pair([1.0, 2.0], row) == (False, "invalid_landmarks")

    nan_row = make_yunet_row(base_landmarks)
    nan_row[4] = float("nan")
    assert check_liveness_pair(row, nan_row) == (False, "invalid_landmarks")

    zero_eye_row = make_yunet_row(base_landmarks)
    zero_eye_row[6] = zero_eye_row[4]
    zero_eye_row[7] = zero_eye_row[5]
    assert check_liveness_pair(row, zero_eye_row) == (False, "invalid_landmarks")


def test_custom_policy_thresholds(base_landmarks):
    row1 = make_yunet_row(base_landmarks)
    row2 = make_yunet_row(base_landmarks + 2.0)  # 2px displacement

    # Default policy (min_disp_px=1.2) passes static check and hits planar check
    _, reason_default = check_liveness_pair(row1, row2)
    assert reason_default == "spoof_planar"

    # Stricter static policy (min_disp_px=5.0) flags it as spoof_static
    strict_policy = LivenessPolicy(min_disp_px=5.0)
    is_live_strict, reason_strict = check_liveness_pair(row1, row2, policy=strict_policy)
    assert not is_live_strict
    assert reason_strict == "spoof_static"
