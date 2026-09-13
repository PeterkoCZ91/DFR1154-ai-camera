"""Face embedding via OpenCV's own YuNet detector and SFace recogniser.

Both APIs ship inside opencv-python-headless 4.11 and run on `cv2.dnn`, the
same engine the YOLO path already uses and under the same single-thread
pinning. Enabling this costs two model files in the data dir — the convention
`yolo11n.onnx` already follows — and no new dependency. That matters: dlib,
face_recognition, onnxruntime and insightface are all absent from the image, so
the historical dlib path could never have run there.

Model files, fetched host-side into ${A12_DATA_DIR} (see tools/setup.sh for the
same pattern):
  face_detection_yunet_2023mar.onnx    (~230 KB)
  face_recognition_sface_2021dec.onnx  (~38 MB)
"""

import logging
from typing import Optional

import numpy as np

SFACE_BACKEND = "sface"

# OpenCV's published operating point for SFace cosine similarity. Like dlib's
# 0.6 it is a starting value, not one fitted to this camera — hence the config
# key. Higher is more similar, the opposite sense to a dlib distance.
DEFAULT_COSINE_THRESHOLD = 0.363

# Below this a crop cannot contain a face worth embedding, and running the
# detector on it only costs time.
_MIN_FRAME_PIXELS = 32


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Direction-only similarity. SFace features are not unit-normalised.

    A zero vector (a failed alignment can produce one) is similar to nothing
    rather than raising or matching everything.
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0.0:
        return 0.0
    return float(np.dot(a, b) / norm)


def best_match(
    embedding: np.ndarray,
    gallery: list,
    names: list,
    threshold: float = DEFAULT_COSINE_THRESHOLD,
) -> tuple[Optional[str], float]:
    """The closest enrolled identity, or (None, score) if none is close enough."""
    best_name, best_score = None, -1.0
    for candidate, name in zip(gallery, names):
        score = cosine_similarity(embedding, candidate)
        if score > best_score:
            best_name, best_score = name, score
    if best_name is None or best_score < threshold:
        return None, best_score if best_score > -1.0 else 0.0
    return best_name, best_score


def read_gallery(data, expected_backend: str) -> tuple[list, list]:
    """Validate an unpickled gallery, refusing anything not from this backend.

    dlib and SFace embeddings are both 128-d but live in different spaces, so
    no shape check can tell them apart — only the recorded backend can. A
    silent mix-up would produce confident nonsense, and since recognition is
    used to SUPPRESS alerts that means muting real strangers. Refuse instead.
    """
    if not isinstance(data, dict):
        logging.warning("Face gallery is not a tagged dict; refusing to use it")
        return [], []
    backend = data.get("backend")
    if backend != expected_backend:
        logging.warning(
            f"Face gallery was built by {backend!r}, not {expected_backend!r}; "
            "refusing to compare embeddings from different spaces"
        )
        return [], []
    encodings = list(data.get("encodings") or [])
    names = list(data.get("names") or [])
    if len(encodings) != len(names):
        logging.warning(
            f"Face gallery has {len(encodings)} encodings for {len(names)} names; "
            "refusing a gallery that cannot be indexed"
        )
        return [], []
    return encodings, names


class SFaceBackend:
    """Detect faces with YuNet and embed them with SFace.

    The cv2 objects are injected so the logic can be exercised without the
    model files; use `from_paths` in production.
    """

    def __init__(self, detector, recognizer, threshold: float = DEFAULT_COSINE_THRESHOLD):
        self.detector = detector
        self.recognizer = recognizer
        self.threshold = float(threshold)

    @classmethod
    def from_paths(
        cls,
        detector_path: str,
        recognizer_path: str,
        threshold: float = DEFAULT_COSINE_THRESHOLD,
        score_threshold: float = 0.6,
    ) -> "SFaceBackend":
        import cv2

        detector = cv2.FaceDetectorYN_create(detector_path, "", (320, 320), score_threshold)
        recognizer = cv2.FaceRecognizerSF_create(recognizer_path, "")
        return cls(detector, recognizer, threshold)

    def embed(self, frame: np.ndarray) -> list:
        """Every face found in this frame, as a 128-d SFace feature."""
        return [embedding for _, embedding in self.detect_and_embed(frame)]

    def detect_and_embed(self, frame: np.ndarray) -> list:
        """(face, embedding) pairs. The face row carries the box and score,
        which enrolment needs to refuse a sample it would be stuck with.

        Returns [] rather than raising: a backend failure must read as "nothing
        was established", never as a statement about who is present.
        """
        if frame is None or frame.size == 0:
            return []
        height, width = frame.shape[:2]
        if height < _MIN_FRAME_PIXELS or width < _MIN_FRAME_PIXELS:
            return []

        try:
            # YuNet silently returns nothing when the configured input size does
            # not match the image, and the crop size varies with the person box.
            self.detector.setInputSize((width, height))
            _, faces = self.detector.detect(frame)
            if faces is None or len(faces) == 0:
                return []

            embeddings = []
            for face in faces:
                aligned = self.recognizer.alignCrop(frame, face)
                feature = self.recognizer.feature(aligned)
                if feature is None:
                    continue
                embeddings.append((face, np.asarray(feature, dtype=np.float32).ravel()))
            return embeddings
        except Exception as e:
            logging.error(f"SFace backend failed: {e}")
            return []


def is_distinct_enough(embedding, kept: list, max_similarity: float) -> bool:
    """Is this sample a new pose, or the same one again?

    Twenty frames of somebody holding still is one sample, not twenty. A
    gallery has to span head angles: the measured failure mode is the same
    person at 35 degrees of pitch scoring like a stranger, and only enrolled
    variety fixes that. Compared against every sample kept so far, not just the
    previous one, because a person drifts back to a pose they already gave.
    """
    return all(cosine_similarity(embedding, other) < max_similarity for other in kept)


# Measured 2026-09-12, and corrected the same evening after a live capture
# produced ZERO usable samples.
#
# The first values (100px, 0.85) came from the ten reference photos, which run
# 146-310px at scores of 0.851+ — but those were taken deliberately close. At
# the door, where enrolment actually happens, a second person measured 79-99px
# at scores of 0.70-0.78, so nothing passed. A floor no achievable sample can
# clear is not strict, it is broken.
#
# These values come instead from what demonstrably produces usable embeddings
# ON THIS CAMERA: live crops of 61-136px at scores of 0.655+ matched the
# gallery at 0.59-0.75 the same evening. Enrolment still sits above the bottom
# of that range, because a bad sample is permanent while a rejected frame only
# costs a second of standing still.
MIN_ENROLMENT_FACE_PIXELS = 85
MIN_ENROLMENT_DETECTOR_SCORE = 0.75

# Blur was measured as a third signal and rejected: Laplacian variance runs
# 10-39 on the working reference photos and 9-18 on good live crops, so on this
# camera — soft optics behind a plastic enclosure — it does not separate good
# samples from bad, and any threshold would pass or reject everything.


def enrolment_quality_problem(
    face_width: int,
    detector_score: float,
    min_width: int = MIN_ENROLMENT_FACE_PIXELS,
    min_score: float = MIN_ENROLMENT_DETECTOR_SCORE,
) -> Optional[str]:
    """Why this sample must not be enrolled, or None if it may be.

    Returns the measurement, not just a verdict: the person being enrolled is
    standing at the door and cannot read the terminal, so whoever reads it
    afterwards needs to know whether to move closer or improve the light.
    """
    if face_width < min_width:
        return f"face too small ({face_width}px, need {min_width}px)"
    if detector_score < min_score:
        return f"detection too uncertain ({detector_score:.3f}, need {min_score})"
    return None


def flag_unusual_samples(
    encodings: list, names: list, floor: float = 0.45
) -> list:
    """Samples that sit far from the rest of their own person's set.

    These are REPORTED, never dropped. Measured 2026-09-12 on the live gallery:
    the one sample this flags is a full profile shot, at a median similarity of
    0.449 against 0.744-0.848 for the rest — a real outlier by the numbers, and
    the single most valuable pose in the set. Similarity alone cannot tell
    "somebody else walked into frame" from "an extreme angle", and an extreme
    angle is exactly what a gallery is collected for, so deleting on this signal
    would remove the coverage it exists to provide.

    Judged per person — two enrolled people are supposed to be far apart — and
    only where enough samples exist for a majority to mean anything.

    Returns [(index, median_similarity)], worst first.
    """
    flagged = []
    for person in set(names):
        members = [i for i, n in enumerate(names) if n == person]
        if len(members) < 3:
            continue
        for i in members:
            others = [encodings[j] for j in members if j != i]
            similarities = sorted(cosine_similarity(encodings[i], o) for o in others)
            median = similarities[len(similarities) // 2]
            if median < floor:
                flagged.append((i, median))
    return sorted(flagged, key=lambda pair: pair[1])
