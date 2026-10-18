"""3D parallax and motion liveness detection for YuNet detections.

Distinguishes physical human presence from static printed photos, replayed screens,
and planar moving cutouts by checking inter-frame motion dynamics and 3D out-of-plane
facial parallax.
"""

from dataclasses import dataclass
import math
from typing import NamedTuple, Optional

import cv2
import numpy as np


@dataclass(frozen=True)
class LivenessPolicy:
    min_disp_px: float = 1.2
    min_scale_change: float = 0.015
    min_affine_residual: float = 0.008
    min_parallax_shift: float = 0.018


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


def check_liveness_pair(
    face1_row,
    face2_row,
    policy: Optional[LivenessPolicy] = None,
) -> tuple[bool, str]:
    """Establish whether two consecutive face observations demonstrate 3D physical presence.

    Returns (is_live: bool, reason: str).
    - If mean landmark displacement < min_disp_px and scale change < min_scale_change:
      returns (False, "spoof_static")
    - Estimates 2D affine transform using cv2.estimateAffine2D(lm1, lm2):
      if normalized affine error < min_affine_residual and parallax shift < min_parallax_shift:
      returns (False, "spoof_planar")
    - If 3D parallax / natural non-planar motion is present:
      returns (True, "live_confirmed")
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

    M, _inliers = cv2.estimateAffine2D(lm1, lm2)
    if M is None:
        norm_affine_error = 1.0
    else:
        pred_lm2 = (lm1 @ M[:, :2].T) + M[:, 2]
        res_px = np.linalg.norm(pred_lm2 - lm2, axis=1)
        norm_affine_error = float(np.mean(res_px) / max(geom2.d_eyes, 1e-6))

    bary_shift = math.hypot(geom2.xi - geom1.xi, geom2.eta - geom1.eta)
    yaw_shift = abs(geom2.r_yaw - geom1.r_yaw)
    parallax_shift = max(bary_shift, yaw_shift)

    if (
        norm_affine_error < policy.min_affine_residual
        and parallax_shift < policy.min_parallax_shift
    ):
        return False, "spoof_planar"

    return True, "live_confirmed"
