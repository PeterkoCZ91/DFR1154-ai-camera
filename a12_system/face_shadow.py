"""A second face recogniser that only writes its opinion into the log.

ArcFace MobileFaceNet (InsightFace `buffalo_s/w600k_mbf.onnx`) separated the two
residents far better than SFace in an offline test on 2026-10-28 (highest score
of one against the other 0.34 against 0.54), at about the same cost. Before it
may decide anything it runs in the shadow: it scores the same face YuNet found
for the real check, and its best match goes into the "Face check:" line as
`arc=name:score`. It never opens, alerts or changes a verdict.

The model is free for non-commercial use only and is not shipped; point
`FACE_SHADOW_ARCFACE_MODEL` at a local copy. The gallery is built from the
enrolment photos by `tools/build_arcface_gallery.py`. ArcFace and SFace
embeddings live in different spaces, so the gallery says which model made it
and is refused otherwise.
"""

import logging
import pickle
from typing import Dict, Optional, Tuple

import numpy as np

ARCFACE_BACKEND = "arcface_mbf"
# InsightFace's 112x112 template. YuNet gives the subject's right eye first,
# which is the image-left one: the same order.
_ARC_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


def _unit(vector) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else v


class ArcFaceShadow:
    def __init__(self, net, gallery: Dict[str, np.ndarray]):
        self.net = net
        # name -> (n, 512) unit rows
        self.gallery = gallery

    @classmethod
    def from_paths(cls, model_path: str, gallery_path: str) -> "ArcFaceShadow":
        import cv2

        net = cv2.dnn.readNetFromONNX(model_path)
        with open(gallery_path, "rb") as handle:
            data = pickle.load(handle)
        if not isinstance(data, dict) or data.get("backend") != ARCFACE_BACKEND:
            raise ValueError(f"{gallery_path} is not an {ARCFACE_BACKEND} gallery")
        gallery = {}
        for name, rows in data.get("embeddings", {}).items():
            rows = np.asarray(rows, dtype=np.float32)
            if rows.ndim == 2 and len(rows):
                gallery[str(name)] = rows / np.linalg.norm(rows, axis=1, keepdims=True)
        if not gallery:
            raise ValueError(f"{gallery_path} has no embeddings")
        return cls(net, gallery)

    def embed(self, image: np.ndarray, face_row) -> Optional[np.ndarray]:
        """The unit ArcFace embedding of the face YuNet found at `face_row`
        (in `image` pixels), or None when it cannot be aligned."""
        import cv2

        try:
            row = np.asarray(face_row, dtype=np.float32).ravel()
            if row.size < 14 or image is None or image.size == 0:
                return None
            landmarks = row[4:14].reshape(5, 2)
            matrix, _ = cv2.estimateAffinePartial2D(landmarks, _ARC_TEMPLATE, method=cv2.LMEDS)
            if matrix is None:
                return None
            aligned = cv2.warpAffine(image, matrix, (112, 112), borderValue=0.0)
            blob = cv2.dnn.blobFromImage(
                aligned, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True
            )
            self.net.setInput(blob)
            return _unit(self.net.forward())
        except Exception as e:
            logging.debug(f"ArcFace shadow could not embed: {e}")
            return None

    def best(self, image: np.ndarray, face_row) -> Optional[Tuple[str, float]]:
        """(name, highest cosine) over the gallery, or None."""
        embedding = self.embed(image, face_row)
        if embedding is None:
            return None
        scores = {name: float(np.max(rows @ embedding)) for name, rows in self.gallery.items()}
        name = max(scores, key=scores.get)
        return name, scores[name]
