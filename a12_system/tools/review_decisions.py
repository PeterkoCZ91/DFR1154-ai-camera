#!/usr/bin/env python3
"""Attach ground truth to A12's decision audit.

The audit records every knob that applied when a decision was made, which is
enough to replay "what would a different threshold have done" — but only if
something says whether the frame actually held a person. Nothing did. This tool
is that something: it walks the images A12 keeps for its unverifiable decisions
(screenshots/candidates/ and screenshots/misses/) and writes a human verdict
into the decision_labels table, keyed by the audit row id carried in each
filename.

The verdicts are the only data in events.db that cannot be recomputed, so they
are never pruned — see EventDB._init_tables.

Works without a display: --stats and --list need no GUI, and --set labels a row
straight from the shell. Interactive review needs a cv2 window.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from a12_system.database import EventDB  # noqa: E402

TRUTHS = {"person": "person", "not_person": "not_person", "unsure": "unsure"}
KEY_TO_TRUTH = {"p": "person", "n": "not_person", "u": "unsure"}
# cand_20260826_100108_a8683_0.90_recorded_and_notified.jpg
# miss_20260826_100108_a8683.jpg
AUDIT_ID_RE = re.compile(r"_a(\d+)[_.]")
CONFIDENCE_BANDS = ((0.0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.85), (0.85, 1.01))


def parse_audit_id(filename):
    """Pull the audit row id out of a snapshot filename, or None if absent.

    Snapshots taken before the row id existed, or when the insert failed, are
    named with `ax` and cannot be tied to a decision — they are not reviewable.
    """
    match = AUDIT_ID_RE.search(filename)
    return int(match.group(1)) if match else None


def discover(data_dir, skip_ids=frozenset()):
    """Reviewable snapshots, newest first, excluding anything already labeled."""
    found = []
    for kind in ("candidates", "misses"):
        folder = os.path.join(data_dir, "screenshots", kind)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.lower().endswith(".jpg"):
                continue
            audit_id = parse_audit_id(name)
            if audit_id is None or audit_id in skip_ids:
                continue
            found.append({
                "kind": kind,
                "audit_id": audit_id,
                "path": os.path.join(folder, name),
                "name": name,
            })
    found.sort(key=lambda item: item["name"], reverse=True)
    return found


def band_of(confidence):
    if confidence is None:
        return "no candidate"
    for low, high in CONFIDENCE_BANDS:
        if low <= confidence < high:
            return f"{low:.2f}-{high - 0.01:.2f}"
    return "out of range"


def stats_report(db, data_dir):
    """Coverage plus the table threshold tuning actually needs."""
    labeled = db.labeled_audit_ids()
    pending = discover(data_dir, labeled)
    lines = ["A12 decision ground truth", "-" * 57]

    rows = db.conn.execute(
        """SELECT truth, candidate_confidence, decision_outcome
           FROM decision_labels"""
    ).fetchall()
    lines.append(f"Labeled: {len(rows)}    unlabeled snapshots on disk: {len(pending)}")
    by_kind = {}
    for item in pending:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    for kind, count in sorted(by_kind.items()):
        lines.append(f"  pending {kind}: {count}")
    if not rows:
        lines += ["", "No verdicts yet. Without them the confidence bands below stay",
                  "empty and every threshold is still a guess."]
        return "\n".join(lines)

    bands = {}
    for truth, confidence, _outcome in rows:
        entry = bands.setdefault(band_of(confidence), {"person": 0, "not_person": 0, "unsure": 0})
        entry[truth] = entry.get(truth, 0) + 1

    lines += ["", f"{'confidence':<14}{'person':>8}{'not person':>12}{'unsure':>8}{'precision':>11}"]
    for band in sorted(bands):
        entry = bands[band]
        decided = entry["person"] + entry["not_person"]
        precision = f"{100 * entry['person'] / decided:.0f}%" if decided else "—"
        lines.append(
            f"{band:<14}{entry['person']:>8}{entry['not_person']:>12}"
            f"{entry['unsure']:>8}{precision:>11}"
        )
    lines += ["", "Precision is the share of real persons in that band. A band where it",
              "collapses is a band the notify threshold should sit above."]
    return "\n".join(lines)


def label(db, audit_id, truth, image_path=None):
    """Write one verdict, carrying the audit context along for survival."""
    context = db.decision_audit_rows([audit_id]).get(audit_id, {})
    return db.save_decision_label(
        audit_id,
        truth,
        candidate_confidence=context.get("candidate_confidence"),
        decision_outcome=context.get("decision_outcome"),
        trigger_source=context.get("trigger_source"),
        image_path=image_path,
    )


def review(db, data_dir, limit):
    """Interactive pass over the pending snapshots."""
    try:
        import cv2
    except ImportError:
        print("ERROR: interactive review needs opencv. Use --list and --set instead.")
        return 1

    pending = discover(data_dir, db.labeled_audit_ids())[:limit]
    if not pending:
        print("Nothing to review.")
        return 0

    context = db.decision_audit_rows([item["audit_id"] for item in pending])
    print(f"{len(pending)} to review.  [p] person  [n] not a person  [u] unsure  "
          "[s] skip  [q] quit")
    done = 0
    for index, item in enumerate(pending, 1):
        image = cv2.imread(item["path"])
        if image is None:
            print(f"  unreadable, skipped: {item['name']}")
            continue
        info = context.get(item["audit_id"], {})
        confidence = info.get("candidate_confidence")
        caption = (
            f"[{index}/{len(pending)}] {item['kind']} a{item['audit_id']} "
            f"conf={'—' if confidence is None else f'{confidence:.2f}'} "
            f"{info.get('decision_outcome', '?')}"
        )
        print(f"  {caption}")
        cv2.imshow("A12 review", image)
        try:
            while True:
                key = chr(cv2.waitKey(0) & 0xFF).lower()
                if key in ("q", "\x1b"):
                    cv2.destroyAllWindows()
                    print(f"Stopped. {done} labeled.")
                    return 0
                if key == "s":
                    break
                if key in KEY_TO_TRUTH:
                    if label(db, item["audit_id"], KEY_TO_TRUTH[key], item["path"]):
                        done += 1
                    break
        except ValueError:
            continue
    cv2.destroyAllWindows()
    print(f"Done. {done} labeled.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", default=os.environ.get("A12_DATA_DIR", "/opt/a12-data"))
    parser.add_argument("--stats", action="store_true", help="coverage and calibration table")
    parser.add_argument("--list", action="store_true", help="print unlabeled snapshot paths")
    parser.add_argument("--set", nargs=2, metavar=("AUDIT_ID", "TRUTH"),
                        help=f"label one row headless; TRUTH is one of {sorted(TRUTHS)}")
    parser.add_argument("--limit", type=int, default=100, help="max snapshots per review pass")
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "events.db")
    if not os.path.isfile(db_path):
        print(f"ERROR: no events.db in {args.data_dir}")
        return 1
    db = EventDB(db_path)
    try:
        if args.set:
            audit_id, truth = args.set
            if truth not in TRUTHS:
                print(f"ERROR: TRUTH must be one of {sorted(TRUTHS)}")
                return 1
            if not audit_id.isdigit():
                print("ERROR: AUDIT_ID must be a number")
                return 1
            ok = label(db, int(audit_id), truth)
            print(f"a{audit_id} = {truth}" if ok else "ERROR: could not save label")
            return 0 if ok else 1
        if args.stats:
            print(stats_report(db, args.data_dir))
            return 0
        if args.list:
            for item in discover(args.data_dir, db.labeled_audit_ids()):
                print(f"a{item['audit_id']}\t{item['kind']}\t{item['path']}")
            return 0
        return review(db, args.data_dir, args.limit)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
