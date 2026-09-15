"""YOLO object detection, motion detection, and face recognition."""

import logging
import math
import os
import pickle
import time
from typing import Optional

import cv2
import numpy as np

from . import scorer_client
from .face_backend import (
    DEFAULT_COSINE_THRESHOLD,
    SFACE_BACKEND,
    SFaceBackend,
    best_match,
    read_gallery,
)
from .face_result import _MIN_BOX_PIXELS, FaceOutcome, FaceResult

def crop_person_box(frame: np.ndarray, box, margin: float = 0.25) -> np.ndarray:
    """Narrow a frame to the detected person, with room for the head.

    YOLO person boxes clip the crown, and a face detector given a headless
    crop finds nothing — hence the margin. Falls back to the whole frame
    rather than to nothing, so a missing or degenerate box degrades to the old
    behaviour instead of silently skipping the check.
    """
    if box is None:
        return frame
    try:
        x1, y1, x2, y2 = (int(v) for v in box)
    except (TypeError, ValueError):
        return frame
    width, height = x2 - x1, y2 - y1
    if width < _MIN_BOX_PIXELS or height < _MIN_BOX_PIXELS:
        return frame

    pad_x, pad_y = int(width * margin), int(height * margin)
    frame_h, frame_w = frame.shape[:2]
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(frame_w, x2 + pad_x)
    y2 = min(frame_h, y2 + pad_y)
    if x2 - x1 < _MIN_BOX_PIXELS or y2 - y1 < _MIN_BOX_PIXELS:
        return frame
    return frame[y1:y2, x1:x2]


class Detector:
    def __init__(self, config: dict, script_dir: str):
        self.config = config
        self.script_dir = script_dir
        self.net = None
        self.coco_classes: list[str] = []
        self.known_face_encodings: list = []
        self.known_face_names: list[str] = []
        self.previous_frame_gray: Optional[np.ndarray] = None
        self.motion_streak = 0
        self.is_ultralytics_v8 = False
        self.last_person_box = None
        self.face_backend = None
        self.face_cosine_threshold = DEFAULT_COSINE_THRESHOLD
        self._remote_failure_until = 0.0

        self._init_yolo()
        self._init_face_recognition()

    def _init_yolo(self) -> None:
        if not self.config["yolo"]["enabled"]:
            return

        try:
            weights = os.path.join(self.script_dir, self.config["yolo"]["weights"])
            names = os.path.join(self.script_dir, self.config["yolo"]["names"])

            basename = os.path.basename(weights).lower()
            self.is_ultralytics_v8 = any(
                tag in basename for tag in ["yolov8", "yolov9", "yolov10", "yolov11", "yolo11"]
            )
            model_type = "YOLOv11/v8 Ultralytics" if self.is_ultralytics_v8 else "YOLOv5"
            logging.info(f"Loading {model_type} ONNX model: {os.path.basename(weights)}")

            self.net = cv2.dnn.readNetFromONNX(weights)
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

            with open(names, "r") as f:
                self.coco_classes = [line.strip() for line in f.readlines()]

            logging.info("YOLO model loaded successfully")
        except Exception as e:
            logging.error(f"YOLO load failed: {e}")
            self.net = None

    def _init_face_backend(self) -> bool:
        """Wire OpenCV's YuNet + SFace, if the two model files are present.

        Returns False when the models are missing or the backend refuses to
        build, so recognition stays off rather than half-configured.
        """
        cfg = self.config.get("face_recognition", {})
        if cfg.get("backend", SFACE_BACKEND) != SFACE_BACKEND:
            return False

        models_dir = cfg.get(
            "known_faces_dir", os.environ.get("A12_DATA_DIR", self.script_dir)
        )
        detector_path = os.path.join(
            models_dir, cfg.get("yunet_model", "face_detection_yunet_2023mar.onnx")
        )
        recognizer_path = os.path.join(
            models_dir, cfg.get("sface_model", "face_recognition_sface_2021dec.onnx")
        )
        missing = [p for p in (detector_path, recognizer_path) if not os.path.exists(p)]
        if missing:
            logging.warning(f"SFace backend unavailable, missing: {', '.join(missing)}")
            return False

        self.face_cosine_threshold = float(
            cfg.get("cosine_threshold", DEFAULT_COSINE_THRESHOLD)
        )
        try:
            self.face_backend = SFaceBackend.from_paths(
                detector_path,
                recognizer_path,
                self.face_cosine_threshold,
                float(cfg.get("detector_score_threshold", 0.6)),
            )
        except Exception as e:
            logging.error(f"SFace backend init failed: {e}")
            self.face_backend = None
            return False

        for pkl_path in self._gallery_paths(cfg, models_dir):
            try:
                with open(pkl_path, "rb") as handle:
                    encodings, names = read_gallery(pickle.load(handle), SFACE_BACKEND)
            except Exception as e:
                logging.error(f"Could not read face gallery {pkl_path}: {e}")
                continue
            self.known_face_encodings.extend(encodings)
            self.known_face_names.extend(names)

        logging.info(
            f"SFace backend ready, {len(self.known_face_names)} enrolled encodings"
        )
        return True

    def _gallery_paths(self, cfg: dict, default_dir: str) -> list:
        """Galleries for THIS backend only.

        The gallery carries the backend that produced it and `read_gallery`
        refuses anything else, so a file from another embedding space can never
        be compared against these — that would produce confident nonsense.
        """
        paths = []
        for filename in cfg.get("sface_gallery_paths", ["known_faces_sface.pkl"]):
            path = filename if os.path.isabs(filename) else os.path.join(default_dir, filename)
            if os.path.exists(path):
                paths.append(path)
            else:
                logging.warning(f"{path} not found")
        return paths

    def _init_face_recognition(self) -> None:
        if not self.config.get("face_recognition", {}).get("enabled"):
            return
        if not self._init_face_backend():
            logging.warning(
                "Face recognition is enabled but no backend could be built; "
                "checks will report UNAVAILABLE"
            )

    def detect_motion(self, frame: np.ndarray) -> bool:
        """Returns True if motion is detected via frame differencing."""
        if not self.config["motion"]["enabled"]:
            return False

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        motion_detected = False
        if self.previous_frame_gray is not None:
            frame_diff = cv2.absdiff(self.previous_frame_gray, gray)
            thresh = cv2.threshold(
                frame_diff, self.config["motion"]["threshold"], 255, cv2.THRESH_BINARY
            )[1]
            # Remove isolated IR/compression speckles before joining real moving areas.
            thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, None, iterations=1)
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            min_area = self.config["motion"]["min_contour_area"]
            significant = [c for c in contours if cv2.contourArea(c) > min_area]

            if significant:
                self.motion_streak += 1
                required = max(1, int(self.config["motion"].get("min_consecutive_frames", 1)))
                motion_detected = self.motion_streak >= required
                if self.config.get("debug_detection", False):
                    max_area = max(cv2.contourArea(c) for c in significant)
                    logging.debug(
                        f"Motion: {len(significant)} contours, "
                        f"max_area={int(max_area)}, threshold={self.config['motion']['threshold']}"
                    )
            else:
                self.motion_streak = 0

        self.previous_frame_gray = gray
        return motion_detected

    def detect_objects(self, frame: np.ndarray) -> list[tuple[str, float]]:
        """Run YOLO inference and return list of (label, confidence)."""
        try:
            return self._detect_objects_impl(frame)
        except Exception as e:
            self.last_backend = "local"
            logging.error(f"YOLO inference failed: {e}")
            return []

    def _detect_objects_impl(self, frame: np.ndarray) -> list[tuple[str, float]]:
        """Run YOLO inference and return list of (label, confidence)."""
        self.last_person_box = None
        yolo_cfg = self.config["yolo"]
        if yolo_cfg.get("backend", "local") == "http" and yolo_cfg.get("scorer_url"):
            now = time.monotonic()
            failure_until = getattr(self, "_remote_failure_until", 0.0)
            if now < failure_until:
                logging.debug("Remote YOLO scorer circuit open for %.1fs", failure_until - now)
                scorer_client.record_fallback()
                self.last_backend = "fallback"
                return self._detect_objects_local(frame)
            detections = self._detect_objects_http(frame)
            if detections is not None:
                self._remote_failure_until = 0.0
                self.last_backend = "http"
                return detections
            backoff = max(0.0, float(yolo_cfg.get("remote_failure_backoff_seconds", 30.0)))
            self._remote_failure_until = now + backoff
            scorer_client.record_fallback()
            logging.warning(
                "Remote YOLO scorer unavailable; circuit open for %.1fs, falling back locally", backoff
            )
            self.last_backend = "fallback"
            return self._detect_objects_local(frame)
        self.last_backend = "local"
        return self._detect_objects_local(frame)

    def _detect_objects_http(self, frame: np.ndarray) -> list[tuple[str, float]] | None:
        """Use the shared scorer service; None means caller should fall back locally."""
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            logging.warning("Failed to encode frame for remote YOLO scorer")
            return None
        result = scorer_client.score_image(
            self.config["yolo"]["scorer_url"],
            encoded.tobytes(),
            timeout=max(0.1, float(self.config["yolo"].get("remote_timeout_seconds", 2.0))),
        )
        if result is None:
            return None
        classes = result.get("classes") if isinstance(result, dict) else None
        if not isinstance(classes, dict):
            return []
        box = result.get("box")
        if (
            isinstance(box, (list, tuple))
            and len(box) == 4
            and all(isinstance(value, (int, float)) for value in box)
            and box[0] < box[2]
            and box[1] < box[3]
        ):
            self.last_person_box = tuple(float(value) for value in box)
        threshold = self.config["yolo"]["confidence_threshold"]
        person_threshold = min(
            threshold,
            float(self.config["yolo"].get("pir_notify_confidence_threshold", threshold)),
        )
        allowed_classes = self.config["yolo"].get("classes", ["person", "bird"])
        detections = []
        for label in allowed_classes:
            try:
                confidence = float(classes.get(label, 0.0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(confidence):
                continue
            label_threshold = person_threshold if label == "person" else threshold
            if label_threshold <= confidence <= 1.0:
                detections.append((label, confidence))
        return detections

    def _detect_objects_local(self, frame: np.ndarray) -> list[tuple[str, float]]:
        """Run local cv2.dnn YOLO inference and return list of (label, confidence)."""
        if not self.net:
            return []

        start_time = time.time()
        height, width = frame.shape[:2]
        class_ids, confidences, boxes = [], [], []
        threshold = self.config["yolo"]["confidence_threshold"]
        person_threshold = min(
            threshold,
            float(self.config["yolo"].get("pir_notify_confidence_threshold", threshold)),
        )
        allowed_classes = self.config["yolo"].get("classes", ["person", "bird"])

        # ONNX input: 640x640, RGB, 1/255 scaling
        blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (640, 640), (0, 0, 0), swapRB=True, crop=False)
        self.net.setInput(blob)
        outs = self.net.forward()

        if self.is_ultralytics_v8:
            # YOLOv11/v8 output: (1, 84, 8400) -> transpose to (8400, 84)
            predictions = outs[0]
            if predictions.ndim == 3:
                predictions = predictions[0]
            if predictions.shape[0] < predictions.shape[1]:
                predictions = predictions.T

            for detection in predictions:
                scores = detection[4:]
                class_id = np.argmax(scores)
                confidence = float(scores[class_id])

                if class_id < len(self.coco_classes):
                    detected_class = self.coco_classes[class_id]
                    label_threshold = person_threshold if detected_class == "person" else threshold
                    if confidence > label_threshold and detected_class in allowed_classes:
                        x_center = detection[0] * width / 640
                        y_center = detection[1] * height / 640
                        w = detection[2] * width / 640
                        h = detection[3] * height / 640
                        x = int(x_center - w / 2)
                        y = int(y_center - h / 2)
                        boxes.append([x, y, int(w), int(h)])
                        confidences.append(confidence)
                        class_ids.append(class_id)
        else:
            # YOLOv5 ONNX output: (1, 25200, 85)
            predictions = outs[0]
            conf_mask = predictions[:, 4] > min(threshold, person_threshold)
            detections = predictions[conf_mask]

            for detection in detections:
                scores = detection[5:]
                class_id = np.argmax(scores)
                confidence = scores[class_id] * detection[4]

                if class_id < len(self.coco_classes):
                    detected_class = self.coco_classes[class_id]
                    label_threshold = person_threshold if detected_class == "person" else threshold
                    if confidence > label_threshold and detected_class in allowed_classes:
                        x_center = detection[0] * width / 640
                        y_center = detection[1] * height / 640
                        w = detection[2] * width / 640
                        h = detection[3] * height / 640
                        x = int(x_center - w / 2)
                        y = int(y_center - h / 2)
                        boxes.append([x, y, int(w), int(h)])
                        confidences.append(float(confidence))
                        class_ids.append(class_id)

        # NMS
        indexes = cv2.dnn.NMSBoxes(boxes, confidences, min(threshold, person_threshold), 0.4)

        inference_time = (time.time() - start_time) * 1000
        logging.debug(f"YOLO Inference: {inference_time:.0f}ms")

        if len(indexes) == 0:
            return []

        if isinstance(indexes, tuple):
            indexes = indexes[0]

        selected = indexes.flatten()
        person_indexes = [
            i for i in selected if self.coco_classes[class_ids[i]] == "person"
        ]
        if person_indexes:
            best = max(person_indexes, key=lambda i: confidences[i])
            x, y, box_width, box_height = boxes[best]
            self.last_person_box = (x, y, x + box_width, y + box_height)
        return [(self.coco_classes[class_ids[i]], confidences[i]) for i in selected]

    def _identify_with_backend(self, backend, frame: np.ndarray) -> FaceResult:
        """Match every face in the frame against the enrolled gallery."""
        if not self.known_face_encodings:
            return FaceResult(FaceOutcome.UNAVAILABLE)

        embeddings = backend.embed(frame)
        if not embeddings:
            return FaceResult(FaceOutcome.NO_FACE)

        # Several faces can share a frame; the best match over all of them
        # decides, so a resident standing behind a stranger is still found.
        # Two scores, on purpose. The matched one decides the identity; the
        # closest one overall is reported when nothing matched, because a
        # stranger just under the threshold is the case worth looking at.
        best_name, best_name_score = None, -1.0
        closest_score = -1.0
        for embedding in embeddings:
            name, score = best_match(
                embedding,
                self.known_face_encodings,
                self.known_face_names,
                self.face_cosine_threshold,
            )
            closest_score = max(closest_score, score)
            if name is not None and score > best_name_score:
                best_name, best_name_score = name, score

        if best_name is not None:
            return FaceResult(FaceOutcome.RESIDENT, best_name, best_name_score)
        score = closest_score if closest_score > -1.0 else None
        return FaceResult(FaceOutcome.STRANGER, None, score)

    def identify_person(self, frame: np.ndarray) -> FaceResult:
        """Check one frame against the enrolled gallery.

        Returns a FaceResult so the caller can tell "nobody was recognisable"
        apart from "somebody was recognisable and is not a resident". Only the
        latter says anything about who is at the door.
        """
        backend = getattr(self, "face_backend", None)
        if backend is None:
            return FaceResult(FaceOutcome.UNAVAILABLE)

        try:
            return self._identify_with_backend(backend, frame)
        except Exception as e:
            # Runs inside the detection loop: a backend fault must read as
            # "nothing was established", never take the pipeline down with it.
            logging.error(f"Face recognition error: {e}")
            return FaceResult(FaceOutcome.ERROR)
