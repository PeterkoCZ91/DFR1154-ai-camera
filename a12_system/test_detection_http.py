import os
import sys
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system import detection, scorer_client
from a12_system.pipeline import box_iou


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
    assert calls == [("http://scorer.example:8766/score", b"jpeg-bytes", 2.0)]


def test_detect_objects_http_keeps_person_box(monkeypatch):
    _jpeg(monkeypatch)
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(
            score_image=lambda *args, **kwargs: {"classes": {"person": 0.72}, "box": [1, 2, 30, 40]}
        ),
        raising=False,
    )
    det = _detector()

    assert det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8)) == [("person", 0.72)]
    assert det.last_person_box == (1.0, 2.0, 30.0, 40.0)


def test_detect_objects_http_keeps_pir_person_candidate_below_base_threshold(monkeypatch):
    _jpeg(monkeypatch)
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(score_image=lambda *args, **kwargs: {"classes": {"person": 0.50}}),
        raising=False,
    )
    det = _detector()
    det.config["yolo"]["pir_notify_confidence_threshold"] = 0.45

    assert det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8)) == [("person", 0.50)]


def test_detect_objects_http_rejects_invalid_confidence(monkeypatch):
    _jpeg(monkeypatch)
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(score_image=lambda *args, **kwargs: {"classes": {"person": float("nan")}}),
        raising=False,
    )

    assert _detector().detect_objects(np.zeros((4, 4, 3), dtype=np.uint8)) == []


def test_validate_response_normalizes_and_rejects_malformed_scorer_data():
    result = scorer_client.validate_response(
        {"classes": {"person": "0.72", "dog": 0.4}, "box": [1, 2, 30, 40]}
    )

    assert result["classes"] == {"person": 0.72, "dog": 0.4}
    assert result["box"] == [1.0, 2.0, 30.0, 40.0]
    assert scorer_client.validate_response({"classes": {"person": float("nan")}}) is None
    assert scorer_client.validate_response({"classes": {}, "box": [1, 2, 1, 3]}) is None

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


def test_detect_objects_remote_failure_opens_circuit(monkeypatch):
    _jpeg(monkeypatch)
    calls = []
    monkeypatch.setattr(
        detection,
        "scorer_client",
        types.SimpleNamespace(score_image=lambda *args, **kwargs: calls.append(1) or None),
        raising=False,
    )
    det = _detector()
    monkeypatch.setattr(det, "_detect_objects_local", lambda frame: [], raising=False)

    det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8))
    det.detect_objects(np.zeros((4, 4, 3), dtype=np.uint8))

    assert calls == [1]


def test_motion_requires_configured_consecutive_changes():
    det = detection.Detector.__new__(detection.Detector)
    det.config = {
        "motion": {
            "enabled": True,
            "threshold": 10,
            "min_contour_area": 100,
            "min_consecutive_frames": 2,
        }
    }
    det.previous_frame_gray = None
    det.motion_streak = 0
    baseline = np.zeros((120, 120, 3), dtype=np.uint8)
    moving_once = baseline.copy()
    moving_once[30:90, 10:70] = 255
    moving_twice = baseline.copy()
    moving_twice[30:90, 30:90] = 255

    assert det.detect_motion(baseline) is False
    assert det.detect_motion(moving_once) is False
    assert det.detect_motion(moving_twice) is True


def test_box_iou_requires_overlap_and_handles_missing_boxes():
    assert box_iou(None, (0, 0, 2, 2)) is None
    assert box_iou((0, 0, 2, 2), (3, 3, 5, 5)) == 0.0
    assert box_iou((0, 0, 4, 4), (2, 2, 6, 6)) == 4 / 28


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
