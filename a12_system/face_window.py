"""The confirmation window that stands between face checks and the latch.

Fed ONLY fresh per-frame checks, one call per face check actually run. The
episode verdict is an aggregate that is handed back on every frame; feeding it
here counted one sighting again and again. `at` must strictly increase, so the
same check offered twice cannot count twice either.

A real tracker must supply track_id. An episode identifier or a name alone
does not establish that two observations belong to the same physical person.

Fails closed throughout: no track, no landmarks, an unmeasured quality, more
than one face or a weak score never count towards an opening.
"""

from collections import deque
import math
from typing import Optional

from .face_liveness import LivenessPolicy, check_liveness_sequence, face_pose, is_frontal
from .face_result import FaceOutcome, FaceResult

# Checks that resolved no face at all. They say nothing about who is at the
# door (78 % of person frames have no resolvable face), so they neither add a
# confirmation nor erase one; the time window still bounds how old a
# confirmation may be.
_NO_EVIDENCE = frozenset({FaceOutcome.NO_FACE, FaceOutcome.UNAVAILABLE, FaceOutcome.ERROR})


class FaceUnlockWindow:
    def __init__(
        self,
        *,
        allowed_names,
        min_score=0.45,
        confirmations=2,
        seconds=5.0,
        liveness_policy: Optional[LivenessPolicy] = None,
        min_look_seconds=0.0,
        look_gap_seconds=1.5,
        min_eye_ratio=0.35,
        max_nose_offset=0.30,
    ):
        if confirmations < 2 or seconds <= 0 or not math.isfinite(seconds):
            raise ValueError("require at least two confirmations and a finite positive window")
        if not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise ValueError("require a finite score threshold between zero and one")
        if not (math.isfinite(min_look_seconds) and 0 <= min_look_seconds < seconds):
            raise ValueError("require a look time from zero up to the window length")
        if not (math.isfinite(look_gap_seconds) and look_gap_seconds > 0):
            raise ValueError("require a finite positive look gap")
        if not (math.isfinite(min_eye_ratio) and math.isfinite(max_nose_offset)):
            raise ValueError("require finite frontal limits")
        self.allowed_names = frozenset(allowed_names)
        self.min_score = min_score
        self.confirmations = confirmations
        self.seconds = seconds
        self.liveness_policy = liveness_policy or LivenessPolicy()
        # Walking past or out of the door the camera sees a profile; walking
        # up to it, a face turned to the lens. Only frontal checks count, and
        # they must span min_look_seconds before the door opens.
        self.min_look_seconds = float(min_look_seconds)
        self.look_gap_seconds = float(look_gap_seconds)
        self.min_eye_ratio = float(min_eye_ratio)
        self.max_nose_offset = float(max_nose_offset)
        self.reset()

    def reset(self):
        self.track_id = None
        # (at, face_row, lead_score) of each agreeing check, oldest first.
        self.hits = deque(maxlen=self.confirmations)
        self.name = None
        self.last_at = None
        # Weakest score of the hits that produced the last confirmation.
        self.confirmed_score = None
        # First and latest frontal hit of the current unbroken look.
        self.look_started_at = None
        self.look_last_at = None
        # Look time of the last check and pose of its face, for the log.
        self.look_seconds = 0.0
        self.pose = None

    def _end_look(self):
        self.look_started_at = self.look_last_at = None
        self.look_seconds = 0.0

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
        while self.hits and at - self.hits[0][0] > self.seconds:
            self.hits.popleft()
        self.pose = face_pose(face_row) if face_row is not None else None
        if self.look_last_at is not None and at - self.look_last_at > self.look_gap_seconds:
            self._end_look()
        if face.outcome in _NO_EVIDENCE:
            # A blink or a lost frame neither ends a look nor lengthens it:
            # only frontal checks count, the gap bounds how far apart.
            return False, "no_face"
        reason = None
        if face.faces != 1:
            reason = "ambiguous_faces"
        elif not face.is_resident or face.lead_name not in self.allowed_names:
            reason = "identity"
        elif face.lead_score is None or not math.isfinite(face.lead_score) or face.lead_score < self.min_score:
            reason = "score"
        elif not quality_ok:
            reason = "quality"
        elif face_row is None:
            # Without landmarks nothing can be said about liveness, and a
            # check that cannot be made is a "no".
            reason = "no_landmarks"
        elif not is_frontal(self.pose, self.min_eye_ratio, self.max_nose_offset):
            reason = "not_frontal"
        if reason:
            self.hits.clear()
            self._end_look()
            return False, reason
        # Even if more than one identity is permitted, hits cannot be shared.
        if self.name != face.lead_name:
            self.hits.clear()
            self._end_look()
        self.name = face.lead_name
        self.hits.append((at, face_row, float(face.lead_score)))
        if self.look_started_at is None:
            self.look_started_at = at
        self.look_last_at = at
        self.look_seconds = at - self.look_started_at

        if len(self.hits) < self.confirmations:
            return False, "awaiting"
        if self.look_seconds < self.min_look_seconds:
            return False, "looking"
        is_live, liveness_reason = check_liveness_sequence(
            [row for _, row, _ in self.hits], self.liveness_policy
        )
        if not is_live:
            # The hits stay: the oldest drops out as the next one arrives, so
            # a person who moves still confirms without starting over.
            return False, f"awaiting:{liveness_reason}"
        self.confirmed_score = min(score for _, _, score in self.hits)
        # One confirmation opens once; the next needs fresh sightings and a
        # fresh look. look_seconds keeps the value that opened, for the log.
        self.hits.clear()
        self.look_started_at = self.look_last_at = None
        return True, "confirmed"
