import logging
import pickle

import numpy as np
import pytest

from a12_system.face_shadow import ARCFACE_BACKEND, ArcFaceShadow


class _Net:
    """Stands in for cv2.dnn: answers every face with one fixed embedding."""

    def __init__(self, vector):
        self.vector = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        self.inputs = []

    def setInput(self, blob):
        self.inputs.append(blob)

    def forward(self):
        return self.vector


# A YuNet row: box, five landmarks (eyes, nose, mouth corners), score.
ROW = np.array([40, 30, 60, 70, 55, 55, 85, 55, 70, 70, 58, 85, 82, 85, 0.9], dtype=np.float32)
IMAGE = np.full((160, 160, 3), 128, dtype=np.uint8)


def test_the_best_match_is_the_closest_person():
    gallery = {"alice": np.array([[1.0, 0.0, 0.0]]), "bob": np.array([[0.0, 1.0, 0.0], [0.6, 0.8, 0.0]])}
    shadow = ArcFaceShadow(_Net([0.6, 0.8, 0.0]), gallery)
    name, score = shadow.best(IMAGE, ROW)
    assert name == "bob" and score == pytest.approx(1.0)
    assert shadow.net.inputs[0].shape == (1, 3, 112, 112)  # aligned to the ArcFace template


def test_a_row_without_landmarks_gives_no_opinion():
    shadow = ArcFaceShadow(_Net([1.0, 0.0]), {"alice": np.array([[1.0, 0.0]])})
    assert shadow.best(IMAGE, ROW[:4]) is None
    assert shadow.net.inputs == []


def test_a_gallery_from_another_model_is_refused(tmp_path, monkeypatch):
    import cv2

    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", lambda path: _Net([1.0]))
    sface = tmp_path / "sface.pkl"
    sface.write_bytes(pickle.dumps({"backend": "sface", "embeddings": {"alice": [[1.0]]}}))
    with pytest.raises(ValueError):
        ArcFaceShadow.from_paths("model.onnx", str(sface))
    arc = tmp_path / "arc.pkl"
    arc.write_bytes(pickle.dumps({"backend": ARCFACE_BACKEND, "embeddings": {"alice": [[3.0, 4.0]]}}))
    shadow = ArcFaceShadow.from_paths("model.onnx", str(arc))
    assert np.allclose(shadow.gallery["alice"], [[0.6, 0.8]])  # stored as unit rows


def test_the_face_check_line_carries_the_shadow_score(monkeypatch, caplog):
    from a12_system.test_pipeline_face_unlock import check, live_row, run_checks, unlock_pipeline

    p = unlock_pipeline(monkeypatch, [check(live_row(i)) for i in range(2)])
    assert p.face_shadow is None and p._arc_text() == ""
    p.face_shadow = ArcFaceShadow(_Net([1.0, 0.0]), {"alice": np.array([[1.0, 0.0]])})
    with caplog.at_level(logging.INFO):
        run_checks(p, 2)
    lines = [r.getMessage() for r in caplog.records if "Face check:" in r.getMessage()]
    assert lines and all(line.endswith(" arc=alice:1.000") for line in lines)
    # Log only: the real decision is the same as without the shadow.
    assert p.opened == [("alice", 1)]


def test_a_shadow_that_does_not_load_stays_off(tmp_path, caplog):
    from a12_system.pipeline import DetectionPipeline

    with caplog.at_level(logging.WARNING):
        shadow = DetectionPipeline._load_face_shadow(
            None, {"shadow_arcface_model": str(tmp_path / "no.onnx"),
                   "shadow_arcface_gallery": str(tmp_path / "no.pkl")},
        )
    assert shadow is None
    assert any("ArcFace shadow off" in r.getMessage() for r in caplog.records)
    assert DetectionPipeline._load_face_shadow(None, {}) is None
