#!/usr/bin/env python3
"""Build an SFace gallery from reference photos.

    python3 -m a12_system.tools.enroll_sface --data-dir /data

Reads `<data-dir>/known_faces/<name>/*.jpg|png` — the same directory layout the
Groq path already uses — and writes `<data-dir>/known_faces_sface.pkl`.

The output is tagged with the backend that produced it. dlib and SFace
embeddings are both 128-d but live in different spaces, so nothing except that
tag can stop them being compared; a silent mix-up would yield confident
nonsense, and since recognition only ever SUPPRESSES alerts, that means muting
real strangers. The existing `known_faces.pkl` is dlib-era and is deliberately
left alone — it is not convertible, the photos have to be re-embedded.

Run it wherever the models are, e.g. inside the container:
    docker compose -p a12_system exec a12 \\
        python3 -m a12_system.tools.enroll_sface --data-dir /data
"""

import argparse
import glob
import os
import pickle
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from a12_system.face_backend import (  # noqa: E402
    DEFAULT_COSINE_THRESHOLD,
    SFACE_BACKEND,
    SFaceBackend,
    cosine_similarity,
)

IMAGE_PATTERNS = ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.PNG")


def build_backend(data_dir: str, score_threshold: float) -> SFaceBackend:
    detector = os.path.join(data_dir, "face_detection_yunet_2023mar.onnx")
    recognizer = os.path.join(data_dir, "face_recognition_sface_2021dec.onnx")
    for path in (detector, recognizer):
        if not os.path.exists(path):
            raise SystemExit(
                f"missing model: {path}\n"
                "fetch both from https://github.com/opencv/opencv_zoo "
                "(models/face_detection_yunet, models/face_recognition_sface)"
            )
    return SFaceBackend.from_paths(detector, recognizer, score_threshold=score_threshold)


def photos_for(person_dir: str) -> list:
    found = []
    for pattern in IMAGE_PATTERNS:
        found.extend(glob.glob(os.path.join(person_dir, pattern)))
    return sorted(set(found))


def enrol(backend: SFaceBackend, faces_dir: str) -> tuple[list, list, list]:
    encodings, names, skipped = [], [], []
    for person in sorted(os.listdir(faces_dir)):
        person_dir = os.path.join(faces_dir, person)
        if not os.path.isdir(person_dir):
            continue
        for photo in photos_for(person_dir):
            image = cv2.imread(photo)
            if image is None:
                skipped.append((photo, "unreadable"))
                continue
            embeddings = backend.embed(image)
            if not embeddings:
                skipped.append((photo, "no face found"))
                continue
            if len(embeddings) > 1:
                # Two faces in a reference photo means we cannot tell which one
                # is the person being enrolled. Guessing would poison the
                # gallery permanently.
                skipped.append((photo, f"{len(embeddings)} faces — ambiguous"))
                continue
            encodings.append(embeddings[0])
            names.append(person)
    return encodings, names, skipped


def report_separation(encodings: list, names: list) -> None:
    """How far apart the enrolled people are, against the match threshold.

    A gallery whose own members sit closer to each other than the threshold
    cannot tell them apart, and no amount of tuning downstream will fix that.
    """
    people = sorted(set(names))
    if len(people) < 2:
        print(f"\nseparation: only {len(people)} person enrolled, nothing to compare")
        return
    print("\nworst-case similarity between different people "
          f"(threshold {DEFAULT_COSINE_THRESHOLD}):")
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            worst = max(
                cosine_similarity(ea, eb)
                for ea, na in zip(encodings, names) if na == a
                for eb, nb in zip(encodings, names) if nb == b
            )
            flag = "  <-- TOO CLOSE" if worst >= DEFAULT_COSINE_THRESHOLD else ""
            print(f"  {a} vs {b}: {worst:.3f}{flag}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("A12_DATA_DIR", "/data"))
    parser.add_argument("--out", default=None, help="default: <data-dir>/known_faces_sface.pkl")
    parser.add_argument("--detector-score", type=float, default=0.6)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    faces_dir = os.path.join(args.data_dir, "known_faces")
    if not os.path.isdir(faces_dir):
        raise SystemExit(f"no reference photos at {faces_dir}")

    backend = build_backend(args.data_dir, args.detector_score)
    encodings, names, skipped = enrol(backend, faces_dir)

    for person in sorted(set(names)):
        print(f"  {person}: {names.count(person)} encodings")
    for photo, why in skipped:
        print(f"  skipped {os.path.basename(photo)}: {why}")
    if not encodings:
        raise SystemExit("no usable reference photos — nothing written")

    report_separation(encodings, names)

    out = args.out or os.path.join(args.data_dir, "known_faces_sface.pkl")
    if args.dry_run:
        print(f"\ndry run — would write {len(encodings)} encodings to {out}")
        return 0
    with open(out, "wb") as handle:
        pickle.dump(
            {"backend": SFACE_BACKEND, "encodings": encodings, "names": names}, handle
        )
    print(f"\nwrote {len(encodings)} encodings for "
          f"{len(set(names))} people to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
