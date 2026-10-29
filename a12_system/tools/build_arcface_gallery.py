"""Build the ArcFace shadow gallery from enrolment photos.

    python -m a12_system.tools.build_arcface_gallery \\
        --photos <dir with one sub-directory per person> \\
        --yunet face_detection_yunet_2023mar.onnx \\
        --model w600k_mbf.onnx --out known_faces_arcface.pkl

Every photo goes through the same path as a live check: the largest YuNet
face (with the contrast-enhanced retry), aligned on its five landmarks. A photo
without a face is skipped and counted. The per-person separation it prints is
the first sanity check that the model and the alignment work together.
"""

import argparse
import glob
import os
import pickle
import sys

import cv2
import numpy as np

from a12_system.face_shadow import ARCFACE_BACKEND, ArcFaceShadow


def largest_face(detector, image):
    """(YuNet row, image the face was found on) or (None, None)."""
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    used = image
    if faces is None or len(faces) == 0:
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        lum, a_chan, b_chan = cv2.split(lab)
        lum = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lum)
        used = cv2.cvtColor(cv2.merge((lum, a_chan, b_chan)), cv2.COLOR_LAB2BGR)
        _, faces = detector.detect(used)
    if faces is None or len(faces) == 0:
        return None, None
    return max(faces, key=lambda f: f[2] * f[3]), used


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photos", required=True)
    parser.add_argument("--yunet", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    detector = cv2.FaceDetectorYN_create(args.yunet, "", (320, 320), 0.6)
    shadow = ArcFaceShadow(cv2.dnn.readNetFromONNX(args.model), {})
    embeddings = {}
    for person_dir in sorted(glob.glob(os.path.join(args.photos, "*"))):
        if not os.path.isdir(person_dir):
            continue
        name = os.path.basename(person_dir)
        rows, skipped = [], 0
        for path in sorted(glob.glob(os.path.join(person_dir, "*"))):
            image = cv2.imread(path)
            face, used = (None, None) if image is None else largest_face(detector, image)
            embedding = None if face is None else shadow.embed(used, face)
            if embedding is None:
                skipped += 1
                continue
            rows.append(embedding)
        if rows:
            embeddings[name] = np.stack(rows)
        print(f"{name}: {len(rows)} embeddings, {skipped} skipped")
    if not embeddings:
        print("no embeddings", file=sys.stderr)
        return 1

    names = sorted(embeddings)
    for a in names:
        for b in names:
            if a < b:
                cross = float(np.max(embeddings[a] @ embeddings[b].T))
                print(f"highest {a} vs {b}: {cross:.3f}")
    with open(args.out, "wb") as handle:
        pickle.dump({"backend": ARCFACE_BACKEND, "embeddings": embeddings}, handle)
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
