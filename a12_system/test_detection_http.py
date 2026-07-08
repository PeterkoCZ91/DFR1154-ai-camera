import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system import detection


def _config(backend="http"):
    return {
        "yolo": {
            "enabled": True,
            "backend": backend,
            "scorer_url": "http://scorer.example:8766/score",
            "classes": ["person", "dog"],
            "confidence_threshold": 0.55,
        },
        "motion": {"enabled": False, "threshold": 50, "min_contour_area": 500},
    }


def _detector(config=None):
    det = detection.Detector.__new__(detection.Detector)
    det.config = config or _config()
    det.script_dir = "."
    det.net = None
    det.coco_classes = ["person", "dog"]
    det.known_face_encodings = []
    det.known_face_names = []
    det.previous_frame_gray = None
    det.is_ultralytics_v8 = False
    return det


def _jpeg(monkeypatch):
    monkeypatch.setattr(
        detection.cv2,
        "imencode",
        lambda ext, frame: (True, np.frombuffer(b"jpeg-bytes", dtype=np.uint8)),
    )


def test_detect_objects_http_filters_classes(monkeypatch):
    _jpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(
            score_image=lambda url, body, timeout=10: calls.append((url, body, timeout))
            or {"classes": {"person": 0.72, "dog": 0.40, "car": 0.99}}
        ),
        raising=False,
    )

    out = _detector().detect_objects(np.zeros((4, 4, 3), dtype=np.uint8))

    assert out == [("person", 0.72)]
    assert calls == [("http://scorer.example:8766/score", b"jpeg-bytes", 10)]


def test_detect_objects_http_failure_falls_back_to_local(monkeypatch):
    _jpeg(monkeypatch)
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(score_image=lambda url, body, timeout=10: None),
        raising=False,
    )
    det = _detector()
    monkeypatch.setattr(det, "_detect_objects_local", lambda frame: [("dog", 0.88)], raising=False)

    assert det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8)) == [("dog", 0.88)]


def test_detect_objects_local_backend_does_not_call_http(monkeypatch):
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(score_image=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError)),
        raising=False,
    )
    det = _detector(_config(backend="local"))
    monkeypatch.setattr(det, "_detect_objects_local", lambda frame: [("person", 0.61)], raising=False)

    assert det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8)) == [("person", 0.61)]
