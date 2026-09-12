"""What the face check is allowed to claim, and what it must stay silent about.

The product problem this encodes: "no face was resolvable" and "a face was
resolvable and it is nobody we know" are completely different facts, and only
the second one says anything about the person at the door. Measured on 300
stored person frames, only 22% contain a detectable face at all and only 6.7%
carry one at the >=80px an embedding needs — so the first case is the common
one, and treating it as "unknown person" is what keeps alert volume high.

Until 2026-09-12 both collapsed into the bare string "Unknown"/"No face" in a
`tuple[bool, str]`, and four separate places re-hardcoded the sentinel list
(stats.py, status_monitor.py twice, pipeline.py). One of them didn't: the
notification caption appended the sentinel verbatim, so Telegram read
"Person detected (Video) (No face)".
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system import detection
from a12_system.detection import Detector
from a12_system.face_result import (
    FaceOutcome,
    FaceResult,
    notification_name,
    summarise_face_labels,
)


class _FakeFaceRecognition:
    """Stands in for the dlib-backed `face_recognition` module.

    dlib is deliberately not installed in the image (it is a lazy import), so
    the real library is not available to test against even if we wanted it.
    """

    def __init__(self, locations, distances):
        self._locations = locations
        self._distances = distances

    def face_locations(self, rgb, model="hog"):
        return self._locations

    def face_encodings(self, rgb, locations):
        return [np.zeros(128) for _ in locations]

    def face_distance(self, known, encoding):
        return np.array(self._distances)


def _detector(monkeypatch, *, gallery=("Resident",), locations=(), distances=(),
              available=True, raises=False):
    det = Detector.__new__(Detector)
    det.config = {"face_recognition": {"tolerance": 0.6}}
    det.known_face_names = list(gallery)
    det.known_face_encodings = [np.zeros(128) for _ in gallery]

    fake = _FakeFaceRecognition(list(locations), list(distances))
    if raises:
        def boom(*a, **kw):
            raise RuntimeError("dlib exploded")
        fake.face_locations = boom

    monkeypatch.setattr(detection, "face_recognition", fake)
    monkeypatch.setattr(detection, "FACE_RECOGNITION_AVAILABLE", available)
    return det


def _frame():
    return np.zeros((120, 160, 3), dtype=np.uint8)


def test_an_empty_gallery_is_unavailable_not_a_stranger(monkeypatch):
    """Nothing was compared, so nothing may be claimed about the person.

    This is also the state a runtime MQTT toggle lands in: `enabled` is read
    from runtime config but the gallery only loads in Detector.__init__, so
    enabling the feature after boot leaves the encodings empty forever.
    """
    det = _detector(monkeypatch, gallery=())
    assert det.identify_person(_frame()).outcome is FaceOutcome.UNAVAILABLE


def test_a_missing_library_is_unavailable(monkeypatch):
    det = _detector(monkeypatch, available=False)
    assert det.identify_person(_frame()).outcome is FaceOutcome.UNAVAILABLE


def test_no_resolvable_face_is_not_a_stranger(monkeypatch):
    """The 78% case. It must not read as evidence about who is there."""
    det = _detector(monkeypatch, locations=[])
    assert det.identify_person(_frame()).outcome is FaceOutcome.NO_FACE


def test_a_face_matching_nobody_is_a_stranger(monkeypatch):
    det = _detector(monkeypatch, locations=[(0, 10, 10, 0)], distances=[0.9])
    assert det.identify_person(_frame()).outcome is FaceOutcome.STRANGER


def test_a_face_under_tolerance_is_a_resident(monkeypatch):
    det = _detector(monkeypatch, gallery=("Resident",),
                    locations=[(0, 10, 10, 0)], distances=[0.4])
    result = det.identify_person(_frame())
    assert result.outcome is FaceOutcome.RESIDENT
    assert result.name == "Resident"


def test_a_raising_backend_is_an_error_not_a_stranger(monkeypatch):
    det = _detector(monkeypatch, raises=True)
    assert det.identify_person(_frame()).outcome is FaceOutcome.ERROR


def test_only_a_resident_carries_a_name(monkeypatch):
    """No outcome but RESIDENT may put a name into the rest of the system."""
    for kwargs in (
        {"gallery": ()},
        {"available": False},
        {"locations": []},
        {"locations": [(0, 10, 10, 0)], "distances": [0.9]},
        {"raises": True},
    ):
        det = _detector(monkeypatch, **kwargs)
        result = det.identify_person(_frame())
        assert result.outcome is not FaceOutcome.RESIDENT
        assert result.name is None, f"{result.outcome} leaked a name"


def test_the_best_match_wins_not_the_first_one_under_tolerance(monkeypatch):
    """argmin picks the closest encoding; the gallery holds many per person."""
    det = _detector(monkeypatch, gallery=("Other", "Resident"),
                    locations=[(0, 10, 10, 0)], distances=[0.55, 0.2])
    assert det.identify_person(_frame()).name == "Resident"


def test_a_caption_never_shows_a_sentinel(monkeypatch):
    """The bug this replaces: Telegram literally read "(No face)"."""
    for kwargs in (
        {"gallery": ()},
        {"locations": []},
        {"locations": [(0, 10, 10, 0)], "distances": [0.9]},
        {"raises": True},
    ):
        det = _detector(monkeypatch, **kwargs)
        assert notification_name(det.identify_person(_frame())) == ""


def test_a_caption_shows_the_resident(monkeypatch):
    det = _detector(monkeypatch, locations=[(0, 10, 10, 0)], distances=[0.4])
    assert notification_name(det.identify_person(_frame())) == "Resident"


def test_every_outcome_has_a_distinct_stable_token():
    """The token is written to events.db and MQTT, so it must not drift."""
    tokens = {o.value for o in FaceOutcome}
    assert tokens == {"resident", "stranger", "no_face", "unavailable", "error"}


# --- consumers -------------------------------------------------------------
# Four places used to re-hardcode the sentinel list independently:
# stats.py:60, stats.py:62-64, status_monitor.py:505 and status_monitor.py:510.
# Each one is a place the distinction could be lost again.

from a12_system.stats import Statistics  # noqa: E402


def _stats():
    s = Statistics.__new__(Statistics)
    import threading

    s.lock = threading.Lock()
    s.face_attempts = 0
    s.face_recognized = 0
    s.face_unknown = 0
    s.face_no_face = 0
    return s


def test_stats_do_not_count_an_unresolvable_face_as_an_unknown_person():
    s = _stats()
    s.record_face_attempt(FaceResult(FaceOutcome.NO_FACE))
    assert s.face_no_face == 1
    assert s.face_unknown == 0, "a frame with no face is not an unknown person"


def test_stats_count_a_stranger_as_unknown():
    s = _stats()
    s.record_face_attempt(FaceResult(FaceOutcome.STRANGER))
    assert s.face_unknown == 1
    assert s.face_no_face == 0


def test_stats_count_a_resident_as_recognised():
    s = _stats()
    s.record_face_attempt(FaceResult(FaceOutcome.RESIDENT, "Resident"))
    assert s.face_recognized == 1


def test_stats_never_credit_a_backend_failure_to_anyone():
    for outcome in (FaceOutcome.ERROR, FaceOutcome.UNAVAILABLE):
        s = _stats()
        s.record_face_attempt(FaceResult(outcome))
        assert (s.face_recognized, s.face_unknown, s.face_no_face) == (0, 0, 0)
        assert s.face_attempts == 1


def test_daily_summary_separates_strangers_from_unreadable_frames():
    counts = {
        ("face", "Resident"): 4,
        ("face", "stranger"): 2,
        ("face", "no_face"): 30,
        ("face", "error"): 1,
        ("motion", "person"): 99,
    }
    summary = summarise_face_labels(counts)
    assert summary["resident"] == 4
    assert summary["stranger"] == 2
    assert summary["no_face"] == 30


def test_daily_summary_still_reads_rows_written_before_the_split():
    """events.db keeps history; old rows used "unknown"/"Unknown"/"No face"."""
    counts = {
        ("face", "Resident"): 1,
        ("face", "unknown"): 5,
        ("face", "Unknown"): 2,
        ("face", "No face"): 7,
        ("face", "Invalid frame"): 1,
    }
    summary = summarise_face_labels(counts)
    assert summary["resident"] == 1
    assert summary["stranger"] == 7, "legacy 'unknown' meant a face matched nobody"
    assert summary["no_face"] == 7
