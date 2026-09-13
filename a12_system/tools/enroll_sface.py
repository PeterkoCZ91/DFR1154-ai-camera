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
import time
from datetime import datetime

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from a12_system.face_backend import (  # noqa: E402
    DEFAULT_COSINE_THRESHOLD,
    SFACE_BACKEND,
    SFaceBackend,
    cosine_similarity,
    enrolment_quality_problem,
    is_distinct_enough,
    flag_unusual_samples,
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


def enrol(backend: SFaceBackend, faces_dir: str) -> tuple[list, list, list, list]:
    encodings, names, sources, skipped = [], [], [], []
    for person in sorted(os.listdir(faces_dir)):
        person_dir = os.path.join(faces_dir, person)
        if not os.path.isdir(person_dir):
            continue
        for photo in photos_for(person_dir):
            image = cv2.imread(photo)
            if image is None:
                skipped.append((photo, "unreadable"))
                continue
            found = backend.detect_and_embed(image)
            if not found:
                skipped.append((photo, "no face found"))
                continue
            if len(found) > 1:
                # Two faces in a reference photo means we cannot tell which one
                # is the person being enrolled. Guessing would poison the
                # gallery permanently.
                skipped.append((photo, f"{len(found)} faces — ambiguous"))
                continue
            face, embedding = found[0]
            # Same bar as live capture: the source of a sample does not change
            # how permanent it is.
            problem = enrolment_quality_problem(int(face[2]), float(face[14]))
            if problem:
                skipped.append((photo, problem))
                continue
            encodings.append(embedding)
            names.append(person)
            sources.append(photo)
    return encodings, names, sources, skipped


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


def camera_url_from_config(data_dir: str) -> str:
    """The camera A12 is actually watching, so enrolment uses the same view.

    A gallery built from phone photos is a gallery of a different lens: this
    camera is a 160-degree fisheye behind plastic, and it distorts hard away
    from centre.
    """
    path = os.path.join(data_dir, "config.env")
    if not os.path.exists(path):
        return ""
    for line in open(path):
        key, _, value = line.strip().partition("=")
        if key == "ESP32_IP" and value:
            return value if "://" in value else f"http://{value}"
    return ""


def capture_from_camera(
    backend: SFaceBackend,
    camera_url: str,
    person: str,
    faces_dir: str,
    seconds: float,
    auth,
    max_similarity: float = 0.92,
) -> int:
    """Collect varied face samples straight off the door camera.

    Photos are written, not just embeddings. The dlib gallery became worthless
    the moment the backend changed because only encodings had been kept; with
    the source frames on disk the gallery can always be rebuilt.

    Nobody can read this terminal while standing at the door, so there are no
    prompts to follow — just keep moving your head slowly. Near-duplicate poses
    are dropped, so holding still simply collects nothing.
    """
    import requests

    person_dir = os.path.join(faces_dir, person)
    os.makedirs(person_dir, exist_ok=True)
    frame_url = camera_url.rstrip("/") + "/frame"

    kept, saved = [], 0
    started = time.time()
    frames = 0
    print(f"capturing for {seconds:.0f}s from {frame_url}")
    print("move your head slowly: straight, left, right, up, down\n")

    while time.time() - started < seconds:
        try:
            response = requests.get(frame_url, auth=auth, timeout=5)
            response.raise_for_status()
            image = cv2.imdecode(
                np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR
            )
        except Exception as e:
            print(f"  camera unreachable: {e}")
            time.sleep(1.0)
            continue
        if image is None:
            continue

        frames += 1
        found = backend.detect_and_embed(image)
        elapsed = time.time() - started
        if len(found) != 1:
            # Zero faces is the common case while moving; more than one means
            # we cannot tell which is the person being enrolled.
            print(f"  [{elapsed:4.0f}s] {len(found)} faces — skipped")
            continue

        face, embedding = found[0]
        problem = enrolment_quality_problem(int(face[2]), float(face[14]))
        if problem:
            print(f"  [{elapsed:4.0f}s] {problem}")
            continue
        if not is_distinct_enough(embedding, kept, max_similarity):
            print(f"  [{elapsed:4.0f}s] same pose as before — move more")
            continue

        kept.append(embedding)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        cv2.imwrite(os.path.join(person_dir, f"{stamp}.jpg"), image)
        saved += 1
        print(f"  [{elapsed:4.0f}s] kept sample {saved}")

    print(f"\n{frames} frames seen, {saved} distinct poses saved to {person_dir}")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.environ.get("A12_DATA_DIR", "/data"))
    parser.add_argument("--out", default=None, help="default: <data-dir>/known_faces_sface.pkl")
    parser.add_argument("--detector-score", type=float, default=0.6)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--capture", metavar="NAME",
                        help="first collect samples of NAME from the live camera")
    parser.add_argument("--camera", default=None,
                        help="camera base URL; defaults to ESP32_IP from the "
                             "running config, so samples come from the same "
                             "lens, angle and light that will do the matching")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--auth", default="admin:admin", help="user:pass for the camera")
    args = parser.parse_args()

    faces_dir = os.path.join(args.data_dir, "known_faces")
    if not os.path.isdir(faces_dir):
        raise SystemExit(f"no reference photos at {faces_dir}")

    backend = build_backend(args.data_dir, args.detector_score)

    if args.capture:
        camera = args.camera or camera_url_from_config(args.data_dir)
        if not camera:
            raise SystemExit(
                "--capture needs --camera; no ESP32_IP found in "
                f"{os.path.join(args.data_dir, 'config.env')}"
            )
        user, _, password = args.auth.partition(":")
        if not capture_from_camera(
            backend, camera, args.capture, faces_dir, args.seconds,
            (user, password) if user else None,
        ):
            raise SystemExit("captured nothing usable — nothing written")
        print()

    encodings, names, sources, skipped = enrol(backend, faces_dir)

    for person in sorted(set(names)):
        print(f"  {person}: {names.count(person)} encodings")
    for photo, why in skipped:
        print(f"  skipped {os.path.basename(photo)}: {why}")
    if not encodings:
        raise SystemExit("no usable reference photos — nothing written")

    # Reported, never deleted. On the live gallery the sample this flags is a
    # full profile shot: a genuine outlier by similarity and the most valuable
    # pose in the set. Similarity cannot tell an extreme angle from a different
    # person, so a human looks at the photo and decides.
    flagged = flag_unusual_samples(encodings, names)
    if flagged:
        print("\nunusual samples — check these are the right person, then either")
        print("keep them (an extreme angle is valuable) or delete the photo:")
        for index, median in flagged:
            print(f"  {sources[index]}  (similarity {median:.3f} to the rest)")

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
