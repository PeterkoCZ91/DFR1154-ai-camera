"""What a face check established, and how that is written down.

Kept out of detection.py so the cheap consumers (stats, the daily summary) do
not have to import OpenCV just to classify a label.
"""

import enum
import re
import time
from typing import NamedTuple, Optional


class FaceOutcome(enum.Enum):
    """What the face check actually established — not merely known/unknown.

    Only STRANGER is evidence *about the person*. NO_FACE, UNAVAILABLE and
    ERROR are absences of evidence, and collapsing them into "unknown person"
    is what kept alert volume high: measured on 300 stored person frames, only
    22% contain a detectable face at all and only 6.7% carry one large enough
    to embed, so the no-evidence case is the overwhelmingly common one.

    The values are written to events.db and MQTT, so they are stable tokens.
    """

    RESIDENT = "resident"        # a face was seen and matched an enrolled person
    STRANGER = "stranger"        # a face was seen and matched nobody
    UNDECIDED = "undecided"      # matched a resident, but too few times to confirm
    NO_FACE = "no_face"          # no face was resolvable in this frame
    UNAVAILABLE = "unavailable"  # the check could not run (no backend, empty gallery)
    ERROR = "error"              # the backend raised


class FaceResult(NamedTuple):
    outcome: FaceOutcome
    name: Optional[str] = None
    # Similarity to the closest gallery entry, when one was computed. Kept so
    # the threshold can eventually be fitted to this camera instead of being
    # inherited from a library default — a near miss and a total mismatch are
    # very different facts and the outcome alone hides both.
    score: Optional[float] = None

    @property
    def is_resident(self) -> bool:
        return self.outcome is FaceOutcome.RESIDENT


# Rows written before 2026-09-12 carry the old bare sentinels. "unknown" meant
# a face WAS resolved and matched nobody, which is today's STRANGER; "No face"
# meant nothing was resolvable. Keeping the mapping here stops each reader from
# inventing its own set again.
_LEGACY_FACE_LABELS = {
    "unknown": FaceOutcome.STRANGER,
    "Unknown": FaceOutcome.STRANGER,
    "No face": FaceOutcome.NO_FACE,
    "Invalid frame": FaceOutcome.ERROR,
    "Error": FaceOutcome.ERROR,
}


def face_label_outcome(label: str) -> FaceOutcome:
    """Classify one events.db `face` label, old or new. A name means RESIDENT."""
    if label in _LEGACY_FACE_LABELS:
        return _LEGACY_FACE_LABELS[label]
    try:
        return FaceOutcome(label)
    except ValueError:
        return FaceOutcome.RESIDENT


def summarise_face_labels(counts: dict) -> dict:
    """Total the `face` rows of a {(type, label): count} map, by outcome.

    Reports strangers separately from frames nothing could be read from — the
    whole point of the split, and the number that says whether alerting on
    "unknown" is justified at all.
    """
    summary = {outcome.value: 0 for outcome in FaceOutcome}
    for (typ, label), value in counts.items():
        if typ != "face":
            continue
        summary[face_label_outcome(label).value] += value
    return summary


def notification_name(result: FaceResult) -> str:
    """The name to show a human, or "" when nothing may be claimed.

    Before 2026-09-12 the caller appended the raw sentinel, so a Telegram
    caption read "Person detected (Video) (No face)".
    """
    return result.name if result.is_resident and result.name else ""


# The minimum a person box may shrink to before it is not worth cropping. A
# face needs ~80px to embed; a box smaller than this cannot contain one.
_MIN_BOX_PIXELS = 16


def should_run_face_check(
    *,
    enabled: bool,
    pir_window_active: bool,
    require_pir_window: bool,
    have_person_box: bool,
    checks_done: int,
    max_checks: int,
    now: float,
    last_check_at: float,
    min_interval: float,
) -> bool:
    """Decide whether to spend one face check on this frame.

    The check is expensive and can only answer when the face is large, which
    is exactly when the PIR says somebody is standing in the doorway. Outside
    that window it costs the same and answers almost nothing, so it is skipped.
    The remaining guards bound one occurrence to a handful of checks spaced far
    enough apart to be different frames rather than the same pose twice.
    """
    if not enabled:
        return False
    if require_pir_window and not pir_window_active:
        return False
    if not have_person_box:
        return False
    if checks_done >= max_checks:
        return False
    if now - last_check_at < min_interval:
        return False
    return True


class FaceEpisode:
    """Accumulates the face checks of one occurrence into a single verdict.

    Deliberately asymmetric. A RESIDENT verdict suppresses the alert, so a
    false accept hides a real stranger — it needs `required_confirmations`
    sightings that agree on the same name. A STRANGER verdict only says a face
    was resolved and matched nobody, which one frame is enough to establish.

    Frames that resolved no face at all are the majority (78% of person frames
    measured) and never outvote anything; they are only the answer when there
    was nothing else.

    Short of the bar the answer is UNDECIDED, not UNAVAILABLE: a face was
    resolved and it did match an enrolled person, which is a real observation —
    it simply is not enough to suppress an alert. Collapsing it into "the check
    could not run" is the same two-facts-in-one-value mistake the RESIDENT /
    STRANGER / NO_FACE split was made to fix.
    """

    def __init__(self, required_confirmations: int = 2):
        self.required_confirmations = max(1, int(required_confirmations))
        self.reset()

    def reset(self) -> None:
        self._name_hits: dict = {}
        self._stranger = 0
        self._no_face = 0
        self._error = 0
        self.checks_done = 0

    def record(self, result: FaceResult) -> None:
        self.checks_done += 1
        if result.outcome is FaceOutcome.RESIDENT and result.name:
            self._name_hits[result.name] = self._name_hits.get(result.name, 0) + 1
        elif result.outcome is FaceOutcome.STRANGER:
            self._stranger += 1
        elif result.outcome is FaceOutcome.NO_FACE:
            self._no_face += 1
        elif result.outcome is FaceOutcome.ERROR:
            self._error += 1

    def verdict(self) -> FaceResult:
        if self._name_hits:
            name, hits = max(self._name_hits.items(), key=lambda kv: kv[1])
            if hits >= self.required_confirmations:
                return FaceResult(FaceOutcome.RESIDENT, name)
        if self._stranger:
            return FaceResult(FaceOutcome.STRANGER)
        if self._name_hits:
            # Seen, matched, and short of the bar — a different fact from "the
            # check could not run", which is what this used to report. No name
            # travels with it: only RESIDENT may name somebody.
            return FaceResult(FaceOutcome.UNDECIDED)
        if self._no_face:
            return FaceResult(FaceOutcome.NO_FACE)
        if self._error:
            return FaceResult(FaceOutcome.ERROR)
        return FaceResult(FaceOutcome.UNAVAILABLE)


# Gallery names come from directory names, so they are user-controlled and must
# never be able to steer a write outside the debug directory.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def debug_crop_name(when: float, result: FaceResult, seq: Optional[int] = None) -> str:
    """Filename encoding what the check saw, so a directory listing is the report.

    The score comes before the name so files sort by how close the match was,
    which is what you scan when deciding whether the threshold is wrong.

    `seq` is what actually makes the name unique. The timestamp alone does not:
    it carries milliseconds, and two saves inside one millisecond produced the
    same name and silently overwrote each other — three writes, two files.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    parts = [stamp, f"{when % 1:.3f}".split(".")[1]]
    if seq is not None:
        parts.append(f"{seq:04d}")
    parts.append(result.outcome.value)
    if result.score is not None:
        parts.append(f"{result.score:.3f}")
    if result.name:
        safe = _UNSAFE_IN_NAME.sub("_", result.name).strip("._") or "unnamed"
        parts.append(safe)
    return "-".join(parts) + ".jpg"
