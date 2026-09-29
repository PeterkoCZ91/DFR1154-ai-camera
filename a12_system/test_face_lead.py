"""The door follows the largest face, not the best match in the frame."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.detection import Detector
from a12_system.face_result import FaceOutcome

_RESIDENT = np.zeros(128, dtype=np.float32)
_RESIDENT[0] = 1.0
_OTHER = np.zeros(128, dtype=np.float32)
_OTHER[1] = 1.0


def _row(width, height):
    return np.array([10, 10, width, height, 0.9], dtype=np.float32)


class _Backend:
    def __init__(self, pairs):
        self._pairs = pairs

    def detect_and_embed(self, frame):
        return list(self._pairs)


class _NoBoxBackend:
    def __init__(self, embeddings):
        self._embeddings = embeddings

    def embed(self, frame):
        return list(self._embeddings)


def _detector(backend):
    det = Detector.__new__(Detector)
    det.config = {}
    det.known_face_names = ["alice"]
    det.known_face_encodings = [_RESIDENT]
    det.face_cosine_threshold = 0.363
    det.face_backend = backend
    return det


def _frame():
    return np.zeros((120, 160, 3), dtype=np.uint8)


def test_resident_in_front_of_a_stranger_leads():
    det = _detector(_Backend([(_row(40, 40), _OTHER), (_row(90, 90), _RESIDENT)]))
    r = det.identify_person(_frame())
    assert r.outcome is FaceOutcome.RESIDENT
    assert (r.faces, r.lead_name) == (2, "alice")


def test_resident_behind_a_stranger_is_found_but_does_not_lead():
    det = _detector(_Backend([(_row(90, 90), _OTHER), (_row(40, 40), _RESIDENT)]))
    r = det.identify_person(_frame())
    assert r.outcome is FaceOutcome.RESIDENT and r.name == "alice"  # alert still muted
    assert r.faces == 2 and r.lead_name is None


def test_a_single_face_is_the_lead_even_without_boxes():
    det = _detector(_NoBoxBackend([_RESIDENT]))
    r = det.identify_person(_frame())
    assert (r.faces, r.lead_name) == (1, "alice")


def test_several_faces_without_boxes_cannot_be_ranked_so_nobody_leads():
    det = _detector(_NoBoxBackend([_OTHER, _RESIDENT]))
    r = det.identify_person(_frame())
    assert r.outcome is FaceOutcome.RESIDENT
    assert r.faces == 2 and r.lead_name is None


def test_stranger_verdict_still_reports_how_many_faces():
    det = _detector(_Backend([(_row(50, 50), _OTHER), (_row(60, 60), _OTHER)]))
    r = det.identify_person(_frame())
    assert r.outcome is FaceOutcome.STRANGER and r.faces == 2
