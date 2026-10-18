"""Experimental lock confirmation window; not used by the live pipeline.

A real tracker must supply track_id. An episode identifier or a name alone
does not establish that two observations belong to the same physical person.
"""

from collections import deque
import math
from typing import Optional

from .face_liveness import check_liveness_pair
from .face_result import FaceResult


class FaceUnlockWindow:
    def __init__(self, *, allowed_names, min_score=0.45, confirmations=2, seconds=5.0):
        if confirmations < 2 or seconds <= 0 or not math.isfinite(seconds):
            raise ValueError("require at least two confirmations and a finite positive window")
        if not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise ValueError("require a finite score threshold between zero and one")
        self.allowed_names = frozenset(allowed_names)
        self.min_score = min_score
        self.confirmations = confirmations
        self.seconds = seconds
        self.reset()

    def reset(self):
        self.track_id = None
        self.hits = deque(maxlen=self.confirmations)
        self.name = None
        self.last_at = None
        self.last_face_row = None

    def record(
        self,
        face: FaceResult,
        *,
        at: float,
        track_id: str | None,
        quality_ok: bool,
        face_row: Optional[object] = None,
    ):
        """Return (confirmed, reason); contradictions clear older confirmations."""
        if face_row is None:
            face_row = getattr(face, "face_row", None)
        if not math.isfinite(at) or not track_id:
            self.reset()
            return False, "no_track"
        if self.track_id != track_id or (self.last_at is not None and at <= self.last_at):
            self.reset()
        self.track_id, self.last_at = track_id, at
        while self.hits and at - self.hits[0] > self.seconds:
            self.hits.popleft()
        reason = None
        if not quality_ok:
            reason = "quality"
        elif face.faces != 1:
            reason = "ambiguous_faces"
        elif not face.is_resident or face.lead_name not in self.allowed_names:
            reason = "identity"
        elif face.lead_score is None or not math.isfinite(face.lead_score) or face.lead_score < self.min_score:
            reason = "score"
        if reason:
            self.hits.clear()
            self.last_face_row = None
            return False, reason
        # Even if more than one identity is permitted, hits cannot be shared.
        if getattr(self, "name", None) != face.lead_name:
            self.hits.clear()
            self.last_face_row = None
        self.name = face.lead_name
        self.hits.append(at)

        if len(self.hits) >= self.confirmations:
            prev_row = self.last_face_row
            if face_row is not None and prev_row is not None:
                is_live, liveness_reason = check_liveness_pair(prev_row, face_row)
                if not is_live:
                    self.last_face_row = face_row
                    return False, f"awaiting:{liveness_reason}"
            self.last_face_row = face_row
            return True, "confirmed"

        self.last_face_row = face_row
        return False, "awaiting"
