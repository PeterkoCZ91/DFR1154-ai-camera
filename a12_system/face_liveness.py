"""Landmark motion and parallax liveness for YuNet detections.

Distinguishes a face that moved in 3D from a frozen frame, a still photo and a
photo or screen moved in front of the camera, using only YuNet's five
landmarks of two or more observations.

Planar test: anything flat (print, phone, tablet) moves between two views by a
homography, and a homography is fixed exactly by four points. The eyes and
mouth corners are close to one plane, so the homography they define predicts
where the nose of a flat picture must land. A real nose sits ~2-4 cm in front
of that plane and leaves it when the head turns or nods. The miss, divided by
the eye distance, is the parallax. An affine fit is not enough: a tilted photo
moves by a perspective map, which the affine model reads as non-planar.

Limits, measured on this camera's own crops (2026-10-18): YuNet's landmarks
jitter by roughly 10-15 % of the eye distance between two views of the very
same image, which is as large as the parallax of a real head turn. With the
0.06 default, replayed through FaceUnlockWindow: a frozen (identical) frame
never passes, a photo held still passed 27 % of synthetic attempts, a photo
moved or tilted 61-80 %. It does NOT reliably tell a moved photo from a live
face and must not be the only factor between a photo and the latch.

Coordinates must be in one frame: rows from different person crops have to be
moved to frame pixels first (detection.face_row_in_frame), or the crop moving
reads as the face moving.
"""

from dataclasses import dataclass
import math
from typing import NamedTuple, Optional, Sequence

import cv2
import numpy as np


@dataclass(frozen=True)
class LivenessPolicy:
    # Frame pixels / relative eye-distance change below which nothing moved.
    min_disp_px: float = 1.2
    min_scale_change: float = 0.015
    # Nose departure from the eyes+mouth homography, in eye distances.
    min_parallax: float = 0.06


class FaceGeometry(NamedTuple):
    d_eyes: float
    xi: float
    eta: float
    r_yaw: float


def extract_landmarks(face_row) -> np.ndarray:
    """Extract the 5 landmarks (right eye, left eye, nose tip, right mouth corner, left mouth corner)
    from YuNet's 15-float detection row.
    """
    if face_row is None:
        raise ValueError("face_row is None")
    arr = np.asarray(face_row, dtype=np.float64).ravel()
    if len(arr) < 14:
        raise ValueError(f"face_row must have at least 14 elements, got {len(arr)}")
    lm = np.array([
        [arr[4], arr[5]],    # right eye
        [arr[6], arr[7]],    # left eye
        [arr[8], arr[9]],    # nose tip
        [arr[10], arr[11]],  # right mouth corner
        [arr[12], arr[13]],  # left mouth corner
    ], dtype=np.float64)
    if not np.isfinite(lm).all():
        raise ValueError("face_row landmarks contain non-finite numbers")
    return lm


def compute_face_geometry(landmarks) -> FaceGeometry:
    """Compute scale-normalized facial barycentric coordinates (d_eyes, xi, eta, r_yaw)."""
    try:
        pts = np.asarray(landmarks, dtype=np.float64)
        if pts.shape != (5, 2) or not np.isfinite(pts).all():
            return FaceGeometry(0.0, 0.0, 0.0, 1.0)
        re, le, nt = pts[0], pts[1], pts[2]
        d_eyes = float(np.linalg.norm(le - re))
        if d_eyes <= 1e-6 or not math.isfinite(d_eyes):
            return FaceGeometry(0.0, 0.0, 0.0, 1.0)
        u = (le - re) / d_eyes
        v = np.array([-u[1], u[0]], dtype=np.float64)
        m = (re + le) / 2.0
        d_nt = nt - m
        xi = float(np.dot(d_nt, u) / d_eyes)
        eta = float(np.dot(d_nt, v) / d_eyes)
        d_r = float(np.linalg.norm(nt - re))
        d_l = float(np.linalg.norm(nt - le))
        r_yaw = float(d_r / d_l) if d_l > 1e-6 else 1.0
        if not math.isfinite(r_yaw):
            r_yaw = 1.0
        return FaceGeometry(d_eyes, xi, eta, r_yaw)
    except Exception:
        return FaceGeometry(0.0, 0.0, 0.0, 1.0)


class FacePose(NamedTuple):
    # Eye distance over face box width: ~0.45-0.55 looking at the lens,
    # 0.10-0.20 in profile (door crops, 2026-10-21).
    eye_ratio: float
    # Nose tip off the eye midpoint along the eye line, in eye distances;
    # 0 is straight on, |x| > ~0.3 a turned head.
    nose_offset: float


def face_pose(face_row) -> Optional[FacePose]:
    """How squarely a YuNet face looks at the camera; None when unmeasurable."""
    try:
        lm = extract_landmarks(face_row)
        width = float(np.asarray(face_row, dtype=np.float64).ravel()[2])
    except (TypeError, ValueError, IndexError):
        return None
    geometry = compute_face_geometry(lm)
    if geometry.d_eyes <= 0 or not math.isfinite(width) or width <= 0:
        return None
    return FacePose(geometry.d_eyes / width, geometry.xi)


def is_frontal(pose: Optional[FacePose], min_eye_ratio: float, max_nose_offset: float) -> bool:
    return (
        pose is not None
        and pose.eye_ratio >= min_eye_ratio
        and abs(pose.nose_offset) <= max_nose_offset
    )


# Eyes and mouth corners: the four points taken as the face plane.
_PLANE = [0, 1, 3, 4]
_NOSE = 2


def nose_parallax(lm1, lm2) -> Optional[float]:
    """How far the nose leaves the eyes+mouth plane between two views.

    Symmetric (both directions, averaged) and divided by the mean eye distance.
    None when the four plane points are degenerate, which the callers treat as
    "no evidence", never as "live".
    """
    a = np.asarray(lm1, dtype=np.float64)
    b = np.asarray(lm2, dtype=np.float64)
    if a.shape != (5, 2) or b.shape != (5, 2) or not (np.isfinite(a).all() and np.isfinite(b).all()):
        return None
    d = 0.5 * (np.linalg.norm(a[1] - a[0]) + np.linalg.norm(b[1] - b[0]))
    if d <= 1e-6:
        return None
    for pts in (a, b):
        # The quadrilateral right eye, left eye, left mouth, right mouth must
        # enclose real area, or the homography it defines is meaningless.
        quad = pts[[0, 1, 4, 3]]
        area = 0.5 * abs(np.dot(quad[:, 0], np.roll(quad[:, 1], -1)) - np.dot(quad[:, 1], np.roll(quad[:, 0], -1)))
        if area < 0.1 * d * d:
            return None
    misses = []
    for src, dst in ((a, b), (b, a)):
        try:
            H = cv2.getPerspectiveTransform(src[_PLANE].astype(np.float32), dst[_PLANE].astype(np.float32))
        except cv2.error:
            return None
        if H is None or not np.isfinite(H).all():
            return None
        p = H @ np.array([src[_NOSE, 0], src[_NOSE, 1], 1.0])
        if abs(p[2]) < 1e-9:
            return None
        misses.append(float(np.linalg.norm(p[:2] / p[2] - dst[_NOSE])))
    value = float(np.mean(misses) / d)
    return value if math.isfinite(value) else None


def check_liveness_pair(
    face1_row,
    face2_row,
    policy: Optional[LivenessPolicy] = None,
) -> tuple[bool, str]:
    """Whether two observations of a face show it moving in 3D.

    Returns (is_live, reason):
    - (False, "invalid_landmarks") when either row is missing or degenerate
    - (False, "spoof_static") when the landmarks did not move or rescale
    - (False, "spoof_planar") when the nose stayed on the plane of the other
      four points (nose_parallax below policy.min_parallax)
    - (True, "live_confirmed") otherwise
    """
    if policy is None:
        policy = LivenessPolicy()

    try:
        lm1 = extract_landmarks(face1_row)
        lm2 = extract_landmarks(face2_row)
    except (ValueError, TypeError, IndexError):
        return False, "invalid_landmarks"

    geom1 = compute_face_geometry(lm1)
    geom2 = compute_face_geometry(lm2)
    if geom1.d_eyes <= 1e-6 or geom2.d_eyes <= 1e-6:
        return False, "invalid_landmarks"

    mean_disp = float(np.mean(np.linalg.norm(lm2 - lm1, axis=1)))
    scale_change = abs(geom2.d_eyes - geom1.d_eyes) / max(geom1.d_eyes, 1e-6)
    if mean_disp < policy.min_disp_px and scale_change < policy.min_scale_change:
        return False, "spoof_static"

    parallax = nose_parallax(lm1, lm2)
    if parallax is None:
        return False, "invalid_landmarks"
    if parallax < policy.min_parallax:
        return False, "spoof_planar"
    return True, "live_confirmed"


def check_liveness_sequence(
    face_rows: Sequence,
    policy: Optional[LivenessPolicy] = None,
) -> tuple[bool, str]:
    """Liveness over every observation that confirms one unlock. Fails closed.

    Every row must carry landmarks; any consecutive pair that did not move at
    all (a frozen stream, a picture held perfectly still) refuses; and the
    first and last observation, the widest baseline the sequence has, must
    show parallax. Judging only the newest pair let one lucky pair decide.
    """
    if policy is None:
        policy = LivenessPolicy()
    rows = list(face_rows)
    if len(rows) < 2:
        return False, "too_few_observations"
    if any(row is None for row in rows):
        return False, "invalid_landmarks"
    for earlier, later in zip(rows, rows[1:]):
        live, reason = check_liveness_pair(earlier, later, policy)
        if reason in ("spoof_static", "invalid_landmarks"):
            return False, reason
    return check_liveness_pair(rows[0], rows[-1], policy)
