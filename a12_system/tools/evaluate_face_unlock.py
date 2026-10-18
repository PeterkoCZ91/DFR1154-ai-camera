"""Offline quality/window replay. Never connects to HA, MQTT, or Telegram.

Requires an explicit output directory. Old crops have neither ground-truth
labels nor physical-person track IDs: window results use episode proxies and
must not be interpreted as validated access decisions.
"""

import argparse
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime
import html
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from a12_system.door_unlock import unlock_decision
from a12_system.face_backend import SFACE_BACKEND, SFaceBackend, read_gallery
from a12_system.face_quality import QualityPolicy, measure_face_quality, quality_reasons
from a12_system.face_result import FaceEpisode, FaceOutcome, FaceResult
from a12_system.face_window import FaceUnlockWindow


def timestamp(name):
    match = re.match(r"^(\d{8}_\d{6})-(\d{3})-", name)
    if not match:
        raise ValueError(f"unrecognised capture timestamp: {name}")
    return datetime.strptime(match[1], "%Y%m%d_%H%M%S").replace(
        tzinfo=ZoneInfo("Europe/Prague")
    ).timestamp() + int(match[2]) / 1000


def identify(backend, frame, gallery, names):
    pairs = backend.detect_and_embed(frame)
    if not pairs:
        return FaceResult(FaceOutcome.NO_FACE), None
    matches = []
    for _, embedding in pairs:
        vector = np.asarray(embedding, dtype=np.float64)
        norm = np.linalg.norm(vector)
        if not np.isfinite(norm) or norm == 0:
            matches.append((None, -1.0))
            continue
        scores = gallery @ (vector / norm)
        index = int(np.argmax(scores))
        score = float(scores[index])
        matches.append((names[index] if score >= .363 else None, score))
    lead = max(range(len(pairs)), key=lambda i: float(pairs[i][0][2]) * float(pairs[i][0][3]))
    known = [(name, score) for name, score in matches if name is not None]
    quality = measure_face_quality(frame, pairs[lead][0])
    # The unlock window fails closed without landmarks and quality, so the
    # replay has to carry both or it can never confirm anybody.
    if known:
        name, score = max(known, key=lambda pair: pair[1])
        result = FaceResult(
            FaceOutcome.RESIDENT, name, score, len(pairs), *matches[lead],
            quality=quality, face_row=pairs[lead][0],
        )
    else:
        result = FaceResult(
            FaceOutcome.STRANGER, score=max(score for _, score in matches), faces=len(pairs),
            quality=quality, face_row=pairs[lead][0],
        )
    return result, quality


def baseline_allowed(face, allowed_names):
    return unlock_decision(
        face, enabled=True, allowed_names=allowed_names, min_score=.45,
        now=1000, last_unlock_at=0, cooldown=60,
    )[0]


def replay(episode, policy, seconds, allowed_names):
    window = FaceUnlockWindow(allowed_names=allowed_names, seconds=seconds)
    for row in episode:
        allowed, _ = window.record(
            row["result"], at=row["at"], track_id="episode-proxy",
            quality_ok=not quality_reasons(row["quality"], policy),
            face_row=getattr(row["result"], "face_row", None),
        )
        if allowed:
            return row["file"]
    return None


def render_review(data, output, episodes, review, selected):
    """Write Czech operator-facing reports without repeating model inference."""
    labels = json.loads(Path(__file__).with_name("face_review_cs.json").read_text())
    esc = html.escape
    body = ['<!doctype html><html lang="cs"><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f'<title>{esc(labels["title"])}</title>',
            '<style>body{font:16px sans-serif;max-width:1250px;margin:25px auto;padding:15px}section{border-top:2px solid #aaa;padding:15px 0}figure{display:inline-block;width:280px;vertical-align:top;margin:8px}img{width:270px}figcaption{line-height:1.6;overflow-wrap:anywhere}details{font-size:12px;color:#555}.intro{background:#eef5ff;padding:16px}</style>',
            f'<h1>{esc(labels["heading"])}</h1><p class="intro">{esc(labels["intro"])}</p>']
    new_body = body.copy()
    for item in review:
        section_start = len(body)
        body.append(f'<section><h2>{esc(labels["visit"])} {item["episode"]}</h2>')
        for key in ("baseline", "window5", "window10"):
            answer = labels["yes"] if item[key] else labels["no"]
            body.append(f'<div>{esc(labels[key])}: <strong>{answer}</strong></div>')
        for row in episodes[int(item["episode"]) - 1]:
            face, quality = row["result"], row["quality"]
            reasons = quality_reasons(quality, selected)
            score = f"{face.lead_score:.3f}".replace(".", ",") if face.lead_score is not None else labels["unknown"]
            sharpness = f"{quality.sharpness:.1f}".replace(".", ",") if quality is not None else labels["unknown"]
            reason = "; ".join(labels["reasons"][r] for r in reasons) if reasons else labels["pass"]
            name = face.lead_name or labels["unknown"]
            stamp = datetime.fromtimestamp(row["at"], ZoneInfo("Europe/Prague")).strftime("%d. %m. %Y %H:%M:%S")
            relative = os.path.relpath(data / "face_debug" / row["file"], output)
            body.append(f'<figure><img loading="lazy" alt="{esc(stamp)}" src="{esc(relative, quote=True)}"><figcaption><strong>{stamp}</strong><br>{esc(labels["model"])}: {esc(name)}<br>{esc(labels["score"])}: {score}<br>{esc(labels["sharpness"])}: {sharpness}<br>{esc(labels["quality"])}: {esc(reason)}<details><summary>{esc(labels["details"])}</summary>{esc(row["file"])}</details></figcaption></figure>')
        body.append('</section>')
        if not item['baseline'] and (item['window5'] or item['window10']):
            new_body.extend(body[section_start:])
    (output / 'review.html').write_text('\n'.join(body) + '</html>')
    (output / 'review_new.html').write_text('\n'.join(new_body) + '</html>')
    with (output / 'oznaceni.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle, delimiter=';')
        writer.writerow(labels['columns'].values())
        for item in review:
            writer.writerow(
                (labels['yes'] if item[key] else labels['no'])
                if key in ('baseline', 'window5', 'window10') else item[key]
                for key in labels['columns']
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allowed-name", action="append", required=True)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    output, data = args.output_dir, args.data_dir
    output.mkdir(parents=True, exist_ok=True)
    backend = SFaceBackend.from_paths(
        str(data / "face_detection_yunet_2023mar.onnx"),
        str(data / "face_recognition_sface_2021dec.onnx"), score_threshold=.6,
    )
    with (data / "known_faces_sface.pkl").open("rb") as handle:
        vectors, names = read_gallery(pickle.load(handle), SFACE_BACKEND)
    if not vectors:
        raise SystemExit("no valid gallery")
    gallery = np.asarray(vectors, dtype=np.float64)
    gallery /= np.linalg.norm(gallery, axis=1, keepdims=True)
    rows, episodes = [], []
    files = sorted((data / "face_debug").glob("*.jpg"))
    if not files:
        raise SystemExit("no saved JPG crops to evaluate")
    for index, path in enumerate(files):
        frame = cv2.imread(str(path))
        if frame is None:
            raise SystemExit(f"unreadable crop: {path.name}")
        result, quality = identify(backend, frame, gallery, names)
        at = timestamp(path.name)
        if not rows or at - rows[-1]["at"] >= 30:
            episodes.append([])
        row = {"file": path.name, "at": at, "episode": len(episodes), "result": result, "quality": quality}
        rows.append(row)
        episodes[-1].append(row)
        if index % 250 == 0:
            print(f"Measured {index + 1} crops", flush=True)
    with (output / "measurements.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps({
                **{k: v for k, v in row.items() if k not in ("result", "quality")},
                "result": {**row["result"]._asdict(), "outcome": row["result"].outcome.value},
                "quality": asdict(row["quality"]) if row["quality"] else None,
            }) + "\n")

    baseline = []
    for episode in episodes:
        aggregate = FaceEpisode(2)
        opened = None
        for row in episode:
            aggregate.record(row["result"])
            if opened is None and baseline_allowed(aggregate.verdict(), args.allowed_name):
                opened = row["file"]
        baseline.append(opened)
    # Grid values are exploratory, not proposed production thresholds.
    sensitivity = []
    selected = QualityPolicy(min_size=60, min_sharpness=50, max_dark_fraction=.5, max_bright_fraction=.5)
    for size in (40, 60, 80):
        for sharpness in (0, 25, 50, 100):
            policy = QualityPolicy(min_size=size, min_sharpness=sharpness, max_dark_fraction=.5, max_bright_fraction=.5)
            for seconds in (5, 10):
                hits = [replay(episode, policy, seconds, args.allowed_name) for episode in episodes]
                sensitivity.append({
                    "size": size, "sharpness": sharpness, "seconds": seconds,
                    "accepted_faces": sum(row["quality"] is not None and not quality_reasons(row["quality"], policy) for row in rows),
                    "candidate_episodes": sum(bool(hit) for hit in hits),
                    "new_vs_baseline": sum(bool(hit) and not old for hit, old in zip(hits, baseline)),
                    "lost_vs_baseline": sum(bool(old) and not hit for hit, old in zip(hits, baseline)),
                })
    with (output / "sensitivity.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(sensitivity[0]))
        writer.writeheader()
        writer.writerows(sensitivity)
    review = []
    for number, episode in enumerate(episodes, 1):
        short, longer = (replay(episode, selected, seconds, args.allowed_name) for seconds in (5, 10))
        if baseline[number - 1] or short or longer:
            review.append({
                "episode": number, "first_file": episode[0]["file"], "last_file": episode[-1]["file"],
                "baseline": baseline[number - 1] or "", "window5": short or "", "window10": longer or "",
                "actual_names": "", "expected_open": "", "same_person_throughout": "", "notes": "",
            })
    with (output / "review.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(review[0]) if review else ["episode", "actual_names", "expected_open"])
        writer.writeheader()
        writer.writerows(review)
    render_review(data, output, episodes, review, selected)
    metrics = [row['quality'] for row in rows if row['quality'] is not None]
    gaps = [b['at'] - a['at'] for episode in episodes for a, b in zip(episode, episode[1:])]
    summary = {
        "opencv": cv2.__version__, "gallery_samples": len(names), "crops": len(rows), "episodes_inferred": len(episodes),
        "allowed_names": args.allowed_name,
        "outcomes": dict(Counter(row['result'].outcome.value for row in rows)),
        "baseline_candidate_episodes": sum(bool(hit) for hit in baseline),
        "selected_exploratory_policy": asdict(selected),
        "quality_rejections": dict(Counter(reason for row in rows for reason in quality_reasons(row['quality'], selected))),
        "face_quality_percentiles": {key: dict(zip(['p10', 'p50', 'p90'], map(float, np.percentile([getattr(q, key) for q in metrics], [10, 50, 90])))) for key in ('size', 'sharpness', 'dark_fraction', 'bright_fraction')} if metrics else {},
        "adjacent_gaps": {"count": len(gaps), "within5s": sum(gap <= 5 for gap in gaps), "median": float(np.median(gaps)) if gaps else None},
        "selected_policy_results": [row for row in sensitivity if row['size'] == 60 and row['sharpness'] == 50],
        "ground_truth_available": False, "physical_tracks_available": False,
        "source": "Recomputed saved JPEG crops with the supplied gallery; not original live decisions",
        "gallery_sha256": hashlib.sha256((data / 'known_faces_sface.pkl').read_bytes()).hexdigest(),
        "not_simulated": ["PIR/YOLO/recording gates", "attempt budgets and early stopping", "lock cooldown across visits", "actual HA/Nuki commands"],
    }
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
