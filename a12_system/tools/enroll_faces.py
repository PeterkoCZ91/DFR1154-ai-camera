#!/usr/bin/env python3
"""
Face enrollment tool for A12 System.

Usage:
  # Enroll from photos directory
  python tools/enroll_faces.py --name "John" --photos ./photos/john/

  # Capture directly from ESP32 camera (stand in front for ~30s)
  python tools/enroll_faces.py --name "John" --capture --camera http://192.168.1.100

  # Capture with custom count and auth
  python tools/enroll_faces.py --name "John" --capture --camera http://192.168.1.100 --count 30 --auth admin:admin

  # List enrolled people
  python tools/enroll_faces.py --list

  # Remove a person
  python tools/enroll_faces.py --remove "John"
"""

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path

try:
    import face_recognition
    import numpy as np
except ImportError:
    print("ERROR: face_recognition not installed. Run: pip install face-recognition numpy")
    sys.exit(1)

DEFAULT_DATA_DIR = Path(os.environ.get("A12_DATA_DIR", "/opt/a12-data"))
PKL_FILE = "known_faces.pkl"
CONFIG_FILE = "config.json"


def load_db(data_dir: Path) -> tuple[list, list]:
    pkl_path = data_dir / PKL_FILE
    if pkl_path.exists():
        with open(pkl_path, "rb") as f:
            data = pickle.load(f)
        return data.get("encodings", []), data.get("names", [])
    return [], []


def save_db(data_dir: Path, encodings: list, names: list) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    pkl_path = data_dir / PKL_FILE
    with open(pkl_path, "wb") as f:
        pickle.dump({"encodings": encodings, "names": names}, f)


def update_whitelist(data_dir: Path, names: list) -> None:
    config_path = data_dir / CONFIG_FILE
    config = {}
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    unique = sorted(set(names))
    config.setdefault("face_recognition", {})["whitelisted_names"] = unique
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"  Whitelist updated: {unique}")


def _encode_image_bytes(img_bytes: bytes) -> list:
    import numpy as np
    import io
    try:
        from PIL import Image
        img = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    except Exception:
        img = face_recognition.load_image_file(io.BytesIO(img_bytes))
    return face_recognition.face_encodings(img)


def _is_duplicate(new_enc, existing_encs: list, threshold: float = 0.35) -> bool:
    """Skip frames that are too similar to already captured encodings."""
    if not existing_encs:
        return False
    distances = face_recognition.face_distance(existing_encs, new_enc)
    return bool(np.min(distances) < threshold)


def _fetch_frame_cv2(url: str, auth=None):
    """Fetch JPEG from camera, return (cv2_bgr_image, raw_bytes) or (None, None)."""
    try:
        import requests
        import cv2
        resp = requests.get(url, auth=auth, timeout=5)
        resp.raise_for_status()
        data = resp.content
        arr = np.frombuffer(data, dtype=np.uint8)
        img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img_bgr, data
    except Exception:
        return None, None


# Guided capture phases: (label, instruction, frames, dedup_threshold)
CAPTURE_PHASES = [
    ("Straight",    "Look straight at the camera",          5, 0.35),
    ("Left",        "Turn head slightly to the LEFT",        4, 0.40),
    ("Right",       "Turn head slightly to the RIGHT",       4, 0.40),
    ("Up",          "Tilt head slightly UP",                 3, 0.40),
    ("Down",        "Tilt head slightly DOWN",               3, 0.40),
    ("Free",        "Move freely — any angle",               4, 0.38),
]
PHASE_TOTAL = sum(p[2] for p in CAPTURE_PHASES)  # 23 default


def _test_camera(frame_url: str, auth) -> bool:
    try:
        import requests
        r = requests.get(frame_url, auth=auth, timeout=6)
        r.raise_for_status()
        print(f"  Camera OK — {len(r.content) // 1024} KB frame received")
        return True
    except Exception as e:
        print(f"  Camera UNREACHABLE: {e}")
        return False


def cmd_capture(args) -> None:
    try:
        import cv2
    except ImportError:
        print("ERROR: install missing packages: pip install requests opencv-python")
        sys.exit(1)

    data_dir = Path(args.data_dir)
    camera_url = args.camera.rstrip("/")
    frame_url = f"{camera_url}/frame"
    interval = args.interval

    auth = None
    if args.auth:
        user, _, pwd = args.auth.partition(":")
        auth = (user, pwd)

    # Connection test
    print(f"Testing camera connection: {frame_url}")
    if not _test_camera(frame_url, auth):
        print("Check --camera URL and --auth credentials.")
        sys.exit(1)

    # Build phase list — scale to --count if custom value given
    phases = list(CAPTURE_PHASES)
    if args.count != PHASE_TOTAL:
        ratio = args.count / PHASE_TOTAL
        phases = [(n, inst, max(1, round(c * ratio)), t) for n, inst, c, t in phases]

    total_target = sum(p[2] for p in phases)
    print(f"\nEnrolling '{args.name}' — {total_target} frames across {len(phases)} guided poses.")
    print("[Green = capturing]  [Orange = too similar, move more]  [Red = no/multiple faces]")
    print("[Q / Esc = stop early]\n")

    encodings, names = load_db(data_dir)
    all_session_encs: list = []
    total_added = 0

    window = f"A12 Enrollment: {args.name} — Q to finish"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 700, 540)

    try:
        for phase_idx, (phase_name, instruction, phase_target, dedup_thresh) in enumerate(phases):
            phase_enc: list = []
            phase_added = 0
            last_capture = 0.0
            print(f"Phase {phase_idx + 1}/{len(phases)}: {instruction} ({phase_target} frames)")

            while phase_added < phase_target:
                img_bgr, _ = _fetch_frame_cv2(frame_url, auth)
                if img_bgr is None:
                    blank = np.zeros((120, 500, 3), dtype=np.uint8)
                    cv2.putText(blank, "Camera unreachable — retrying...", (10, 65),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                    cv2.imshow(window, blank)
                    if cv2.waitKey(1500) & 0xFF in (ord('q'), 27):
                        raise KeyboardInterrupt
                    continue

                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(img_rgb)
                display = img_bgr.copy()
                h, w = display.shape[:2]
                now = time.time()
                ready = (now - last_capture) >= interval

                # Header bar
                cv2.rectangle(display, (0, 0), (w, 52), (30, 30, 30), -1)
                cv2.putText(display, f"Phase {phase_idx+1}/{len(phases)}: {phase_name}",
                            (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
                cv2.putText(display, instruction,
                            (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 100), 1)

                # Progress bar
                progress = total_added / total_target
                cv2.rectangle(display, (0, h - 8), (w, h), (50, 50, 50), -1)
                cv2.rectangle(display, (0, h - 8), (int(w * progress), h), (0, 200, 0), -1)

                status_text = ""
                status_color = (200, 200, 200)

                if len(locations) == 1:
                    top, right, bottom, left = locations[0]
                    if ready:
                        encs = face_recognition.face_encodings(img_rgb, locations)
                        enc = encs[0]
                        if _is_duplicate(enc, phase_enc, dedup_thresh):
                            color = (0, 165, 255)
                            status_text = "Move more for variety"
                            status_color = (0, 165, 255)
                        else:
                            color = (0, 255, 0)
                            phase_enc.append(enc)
                            all_session_encs.append(enc)
                            encodings.append(enc)
                            names.append(args.name)
                            phase_added += 1
                            total_added += 1
                            last_capture = now
                            status_text = f"Captured! ({phase_added}/{phase_target})"
                            status_color = (0, 255, 0)
                    else:
                        color = (0, 200, 0)
                        remaining = interval - (now - last_capture)
                        status_text = f"Hold still... {remaining:.1f}s  ({phase_added}/{phase_target})"
                    cv2.rectangle(display, (left, top + 52), (right, bottom + 52), color, 2)
                    label = f"{args.name}  {phase_name}"
                    cv2.putText(display, label, (left, top + 46),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

                elif len(locations) == 0:
                    status_text = "No face detected — move closer"
                    status_color = (0, 0, 255)
                else:
                    for top, right, bottom, left in locations:
                        cv2.rectangle(display, (left, top + 52), (right, bottom + 52), (0, 0, 255), 2)
                    status_text = f"{len(locations)} faces — only you in frame please"
                    status_color = (0, 0, 255)

                # Status bar
                cv2.rectangle(display, (0, h - 36), (w, h - 8), (30, 30, 30), -1)
                total_str = f"Total: {total_added}/{total_target}"
                cv2.putText(display, status_text, (10, h - 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
                cv2.putText(display, total_str, (w - 160, h - 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)

                cv2.imshow(window, display)
                key = cv2.waitKey(50) & 0xFF
                if key in (ord('q'), 27):
                    raise KeyboardInterrupt

            print(f"  Phase {phase_idx + 1} done — {phase_added} frames")

    except KeyboardInterrupt:
        print("\nStopped early.")
    finally:
        cv2.destroyAllWindows()

    if total_added == 0:
        print("ERROR: No faces captured.")
        sys.exit(1)

    save_db(data_dir, encodings, names)
    update_whitelist(data_dir, names)

    total_for_name = names.count(args.name)
    print(f"\nDone: {total_added} frames captured across {len(phases)} poses.")
    print(f"'{args.name}' now has {total_for_name} encodings total.")


def cmd_enroll(args) -> None:
    data_dir = Path(args.data_dir)
    photos_dir = Path(args.photos)

    if not photos_dir.exists():
        print(f"ERROR: Photos directory not found: {photos_dir}")
        sys.exit(1)

    image_paths = sorted(
        list(photos_dir.glob("*.jpg")) + list(photos_dir.glob("*.jpeg")) +
        list(photos_dir.glob("*.png")) + list(photos_dir.glob("*.JPG")) +
        list(photos_dir.glob("*.JPEG")) + list(photos_dir.glob("*.PNG"))
    )

    if not image_paths:
        print(f"ERROR: No images found in {photos_dir}")
        sys.exit(1)

    print(f"Enrolling '{args.name}' from {len(image_paths)} images...")

    encodings, names = load_db(data_dir)
    added = 0
    skipped = 0

    for img_path in image_paths:
        img = face_recognition.load_image_file(str(img_path))
        found = face_recognition.face_encodings(img)
        if not found:
            print(f"  SKIP {img_path.name} — no face detected")
            skipped += 1
            continue
        if len(found) > 1:
            print(f"  SKIP {img_path.name} — {len(found)} faces (use single-face photos)")
            skipped += 1
            continue
        encodings.append(found[0])
        names.append(args.name)
        added += 1
        print(f"  OK   {img_path.name}")

    if added == 0:
        print("ERROR: No faces enrolled.")
        sys.exit(1)

    save_db(data_dir, encodings, names)
    update_whitelist(data_dir, names)

    total_for_name = names.count(args.name)
    print(f"\nDone: {added} added, {skipped} skipped. '{args.name}' now has {total_for_name} encodings total.")


def cmd_list(args) -> None:
    data_dir = Path(args.data_dir)
    encodings, names = load_db(data_dir)
    if not names:
        print("No faces enrolled.")
        return
    from collections import Counter
    for name, count in sorted(Counter(names).items()):
        print(f"  {name}: {count} encodings")
    print(f"\nTotal: {len(names)} encodings, {len(set(names))} people")


def cmd_remove(args) -> None:
    data_dir = Path(args.data_dir)
    encodings, names = load_db(data_dir)
    target = args.remove
    before = names.count(target)
    if before == 0:
        print(f"'{target}' not found.")
        return
    pairs = [(e, n) for e, n in zip(encodings, names) if n != target]
    new_enc = [p[0] for p in pairs]
    new_names = [p[1] for p in pairs]
    save_db(data_dir, new_enc, new_names)
    update_whitelist(data_dir, new_names)
    print(f"Removed '{target}' ({before} encodings).")


def main():
    parser = argparse.ArgumentParser(
        description="A12 face enrollment tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help=f"A12 data directory (default: {DEFAULT_DATA_DIR})")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--photos", metavar="DIR", help="Enroll from photo directory")
    group.add_argument("--capture", action="store_true", help="Capture from ESP32 camera live")
    group.add_argument("--list", action="store_true", help="List enrolled people")
    group.add_argument("--remove", metavar="NAME", help="Remove a person")

    parser.add_argument("--name", help="Person name (required for --photos and --capture)")
    parser.add_argument("--camera", default="http://192.168.1.100",
                        help="Camera URL for --capture (default: http://192.168.1.100)")
    parser.add_argument("--auth", metavar="USER:PASS", default="admin:admin",
                        help="Camera HTTP auth (default: admin:admin)")
    parser.add_argument("--count", type=int, default=20,
                        help="Number of frames to capture (default: 20)")
    parser.add_argument("--interval", type=float, default=1.5,
                        help="Seconds between captures (default: 1.5)")

    args = parser.parse_args()

    if (args.photos or args.capture) and not args.name:
        parser.error("--name is required with --photos and --capture")

    if args.capture and not args.camera:
        parser.error("--camera is required with --capture")

    if args.photos:
        cmd_enroll(args)
    elif args.capture:
        cmd_capture(args)
    elif args.list:
        cmd_list(args)
    elif args.remove:
        cmd_remove(args)


if __name__ == "__main__":
    main()
