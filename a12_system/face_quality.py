"""Face-only quality measurements and an explicit, opt-in filter policy.

No production defaults are inferred here. Thresholds need calibration on the
camera's own images; a whole person crop is not a face-quality measurement.
"""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class FaceQuality:
    size: float
    visible_fraction: float
    sharpness: float
    dark_fraction: float
    bright_fraction: float


@dataclass(frozen=True)
class QualityPolicy:
    min_size: float = 0.0
    min_sharpness: float = 0.0
    max_dark_fraction: float = 1.0
    max_bright_fraction: float = 1.0
    min_visible_fraction: float = 0.9


def measure_face_quality(frame: np.ndarray, face) -> FaceQuality | None:
    """Measure the actual clipped face; normalise blur measurement to 64px."""
    try:
        x, y, width, height = map(float, face[:4])
        if not np.isfinite([x, y, width, height]).all() or min(width, height) <= 0:
            return None
        fh, fw = frame.shape[:2]
        x1, y1 = max(0, int(np.floor(x))), max(0, int(np.floor(y)))
        x2, y2 = min(fw, int(np.ceil(x + width))), min(fh, int(np.ceil(y + height)))
        if x2 <= x1 or y2 <= y1:
            return None
        crop = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        normalised = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
        # Geometry uses floats: integer rounding must not make a clipped face
        # appear more than fully visible.
        visible = max(0.0, min(fw, x + width) - max(0.0, x)) * max(
            0.0, min(fh, y + height) - max(0.0, y)
        ) / (width * height)
        return FaceQuality(
            min(width, height), visible,
            float(cv2.Laplacian(normalised, cv2.CV_64F).var()),
            float(np.mean(gray <= 5)), float(np.mean(gray >= 250)),
        )
    except (TypeError, ValueError, AttributeError, cv2.error):
        return None


def quality_reasons(quality: FaceQuality | None, policy: QualityPolicy) -> tuple[str, ...]:
    if quality is None:
        return ("invalid_face",)
    reasons = []
    for failed, reason in (
        (quality.size < policy.min_size, "small"),
        (quality.visible_fraction < policy.min_visible_fraction, "clipped"),
        (quality.sharpness < policy.min_sharpness, "blurred"),
        (quality.dark_fraction > policy.max_dark_fraction, "dark"),
        (quality.bright_fraction > policy.max_bright_fraction, "overexposed"),
    ):
        if failed:
            reasons.append(reason)
    return tuple(reasons)
