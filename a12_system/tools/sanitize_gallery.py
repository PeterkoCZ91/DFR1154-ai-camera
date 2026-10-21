#!/usr/bin/env python3
"""Sanitize an SFace face recognition gallery.

    python3 -m a12_system.tools.sanitize_gallery \\
        --input /data/known_faces_sface.pkl \\
        --output /data/known_faces_sface_clean.pkl \\
        --report

Performs three sanitization passes on normalized SFace embeddings:
1. Cross-pollution removal: drop samples that are too close to another identity
   (max cosine similarity to another person >= max_cross).
2. Internal outlier removal: drop samples that do not match their own cluster
   (median cosine similarity to own identity < min_within or max_cross >= within_median).
3. Burst deduplication: greedy exemplar selection per identity to eliminate
   near-duplicate video burst frames (max internal similarity between kept
   exemplars < max_internal_sim).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pickle
import sys
import tempfile
from typing import Any, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from A12_System_v2.face_backend import (  # noqa: E402
        DEFAULT_COSINE_THRESHOLD,
        SFACE_BACKEND,
    )
except ModuleNotFoundError:  # the repo calls the package a12_system
    from a12_system.face_backend import (  # noqa: E402
        DEFAULT_COSINE_THRESHOLD,
        SFACE_BACKEND,
    )


def normalize_embeddings(encodings: list | np.ndarray) -> np.ndarray:
    """L2-normalize embeddings to unit sphere. Returns shape (N, D)."""
    if len(encodings) == 0:
        return np.empty((0, 128), dtype=np.float64)
    matrix = np.asarray(encodings, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return matrix / norms


def compute_similarity_matrix(normed: np.ndarray) -> np.ndarray:
    """Compute cosine similarity matrix across normalized embeddings."""
    if len(normed) == 0:
        return np.empty((0, 0), dtype=np.float64)
    sim = normed @ normed.T
    return np.clip(sim, -1.0, 1.0)


def compute_sample_metrics(
    sim_matrix: np.ndarray, names: list[str] | np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Compute (max_cross, within_median) for each sample.

    max_cross: maximum cosine similarity to any sample of a different identity
               (-1.0 if no other identity exists).
    within_median: median cosine similarity to other samples of the same identity
                   (1.0 if no other sample of the same identity exists).
    """
    n = len(names)
    names_arr = np.asarray(names)
    max_cross = np.full(n, -1.0, dtype=np.float64)
    within_median = np.full(n, 1.0, dtype=np.float64)

    for i in range(n):
        same_mask = names_arr == names_arr[i]
        same_mask[i] = False
        other_mask = names_arr != names_arr[i]

        if np.any(other_mask):
            max_cross[i] = float(np.max(sim_matrix[i, other_mask]))
        if np.any(same_mask):
            within_median[i] = float(np.median(sim_matrix[i, same_mask]))

    return max_cross, within_median


def select_exemplars_greedy(
    sim_sub: np.ndarray,
    candidate_indices: list[int] | np.ndarray,
    max_internal_sim: float = 0.88,
    max_exemplars: Optional[int] = None,
) -> list[int]:
    """Greedy exemplar selection for deduplicating video burst frames.

    Ensures that for all kept exemplars e_i, e_j (i != j):
        sim(e_i, e_j) < max_internal_sim.

    Prioritizes samples by centrality (median similarity to peers descending),
    ensuring the primary medoid exemplar is chosen first as anchor.
    """
    if len(candidate_indices) <= 1:
        return [int(x) for x in candidate_indices]

    meds = np.zeros(len(candidate_indices), dtype=np.float64)
    for i in range(len(candidate_indices)):
        row = np.delete(sim_sub[i], i)
        meds[i] = float(np.median(row)) if len(row) > 0 else 1.0

    priority_order = np.argsort(-meds)

    kept_local: list[int] = []
    for local_idx in priority_order:
        if not kept_local:
            kept_local.append(int(local_idx))
        else:
            max_sim_to_kept = float(np.max(sim_sub[local_idx, kept_local]))
            if max_sim_to_kept < max_internal_sim:
                kept_local.append(int(local_idx))
                if max_exemplars is not None and len(kept_local) >= max_exemplars:
                    break

    # Preserve original ordering in the gallery
    return [int(candidate_indices[idx]) for idx in sorted(kept_local)]


def compute_gallery_stats(
    normed: np.ndarray, names: list[str] | np.ndarray
) -> dict[str, Any]:
    """Calculate summary statistics for a gallery (counts, separation, internal medians)."""
    n = len(names)
    if n == 0:
        return {
            "total_samples": 0,
            "identities": {},
            "max_cross_similarity": -1.0,
        }

    names_arr = np.asarray(names)
    sim = compute_similarity_matrix(normed)
    unique_names = sorted(set(names_arr))

    identities: dict[str, Any] = {}
    overall_max_cross = -1.0

    for name in unique_names:
        idx = np.where(names_arr == name)[0]
        other_idx = np.where(names_arr != name)[0]

        psim = sim[np.ix_(idx, idx)]
        meds = []
        for i in range(len(idx)):
            peer_row = np.delete(psim[i], i)
            if len(peer_row) > 0:
                meds.append(float(np.median(peer_row)))
            else:
                meds.append(1.0)

        cross_max = -1.0
        if len(other_idx) > 0:
            cross_max = float(np.max(sim[np.ix_(idx, other_idx)]))
            if cross_max > overall_max_cross:
                overall_max_cross = cross_max

        identities[name] = {
            "count": int(len(idx)),
            "within_median_min": float(np.min(meds)),
            "within_median_median": float(np.median(meds)),
            "within_median_max": float(np.max(meds)),
            "max_cross": cross_max,
        }

    return {
        "total_samples": n,
        "identities": identities,
        "max_cross_similarity": overall_max_cross,
    }


def sanitize_gallery(
    data: dict[str, Any] | str | Path,
    max_cross: float = 0.35,
    min_within: float = 0.40,
    max_internal_sim: float = 0.88,
    max_exemplars: Optional[int] = None,
    return_stats: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    """Sanitize SFace gallery by pruning cross-polluted samples, outliers, and duplicates.

    Parameters:
        data: Gallery dict or path to pickle file containing 'encodings' and 'names'.
        max_cross: Maximum cosine similarity to another identity. Samples with
                   max_cross >= this threshold are dropped.
        min_within: Minimum median cosine similarity to own identity. Samples with
                    within_median < this threshold are dropped.
        max_internal_sim: Maximum similarity between kept exemplars of the same identity.
                          Greedy exemplar selection drops redundant video burst frames.
        max_exemplars: Optional maximum number of exemplars to retain per person.
        return_stats: If True, returns (sanitized_dict, report_stats).

    Returns:
        sanitized_dict: dict with 'backend', 'encodings', 'names' (and 'sources' if present).
        (Optional) report_stats: dict with before/after detailed metrics.
    """
    if isinstance(data, (str, Path)):
        with open(data, "rb") as handle:
            raw_data = pickle.load(handle)
    elif not isinstance(data, dict):
        raise ValueError("Gallery data must be a dictionary")
    else:
        raw_data = dict(data)

    if not isinstance(raw_data, dict):
        raise ValueError("Gallery data must be a dictionary")

    encodings = list(raw_data.get("encodings") or [])
    names = list(raw_data.get("names") or [])
    backend = raw_data.get("backend", SFACE_BACKEND)

    if len(encodings) != len(names):
        raise ValueError(
            f"Mismatched gallery lengths: {len(encodings)} encodings vs {len(names)} names"
        )

    n_total = len(names)
    if n_total == 0:
        empty_clean = {"backend": backend, "encodings": [], "names": []}
        if return_stats:
            return empty_clean, {"before": {}, "after": {}, "pruned": {}}
        return empty_clean

    X_norm = normalize_embeddings(encodings)
    sim_matrix = compute_similarity_matrix(X_norm)
    names_arr = np.asarray(names)

    stats_before = compute_gallery_stats(X_norm, names_arr)

    # Step 1: Identify and remove extreme internal outliers
    # (median similarity to own identity < min_within or max_cross >= within_median)
    max_cross_init, within_median_init = compute_sample_metrics(sim_matrix, names_arr)
    is_internal_outlier = (within_median_init < min_within) | (max_cross_init >= within_median_init)
    surv_after_outliers = np.where(~is_internal_outlier)[0]

    # Step 2: Identify and remove cross-polluted samples on survivors
    # (max similarity to another identity >= max_cross)
    sim_clean = sim_matrix[np.ix_(surv_after_outliers, surv_after_outliers)]
    names_clean = names_arr[surv_after_outliers]
    max_cross_clean, _ = compute_sample_metrics(sim_clean, names_clean)
    is_cross_polluted = max_cross_clean >= max_cross
    surviving_indices = surv_after_outliers[~is_cross_polluted]

    # Step 3: Greedy exemplar selection per identity to eliminate video burst frames
    kept_indices: list[int] = []
    unique_names = sorted(set(names_arr))

    for name in unique_names:
        person_candidates = [i for i in surviving_indices if names_arr[i] == name]
        if not person_candidates:
            continue
        sub_sim = sim_matrix[np.ix_(person_candidates, person_candidates)]
        person_kept = select_exemplars_greedy(
            sub_sim,
            person_candidates,
            max_internal_sim=max_internal_sim,
            max_exemplars=max_exemplars,
        )
        kept_indices.extend(person_kept)

    kept_indices = sorted(kept_indices)

    kept_encodings = [encodings[i] for i in kept_indices]
    kept_names = [names[i] for i in kept_indices]

    sanitized: dict[str, Any] = {
        "backend": backend,
        "encodings": kept_encodings,
        "names": kept_names,
    }

    if "sources" in raw_data and len(raw_data["sources"]) == n_total:
        sanitized["sources"] = [raw_data["sources"][i] for i in kept_indices]

    # Preserve any other metadata
    for k, v in raw_data.items():
        if k not in ("encodings", "names", "sources", "backend"):
            sanitized[k] = v

    stats_after = compute_gallery_stats(
        X_norm[kept_indices] if kept_indices else np.empty((0, 128)),
        names_arr[kept_indices] if kept_indices else [],
    )

    initially_cross_polluted = max_cross_init >= max_cross
    pruning_report = {
        "initial_total": n_total,
        "internal_outlier_count": int(np.sum(is_internal_outlier)),
        "cross_polluted_count": int(np.sum(is_cross_polluted)),
        "initial_cross_polluted_count": int(np.sum(initially_cross_polluted)),
        "both_outlier_reasons_count": int(np.sum(initially_cross_polluted & is_internal_outlier)),
        "total_outliers_dropped": int(n_total - len(surviving_indices)),
        "surviving_outlier_filter": int(len(surviving_indices)),
        "burst_duplicates_dropped": int(len(surviving_indices) - len(kept_indices)),
        "final_total": len(kept_indices),
    }

    full_stats = {
        "parameters": {
            "max_cross": max_cross,
            "min_within": min_within,
            "max_internal_sim": max_internal_sim,
            "max_exemplars": max_exemplars,
        },
        "pruning": pruning_report,
        "before": stats_before,
        "after": stats_after,
    }

    if return_stats:
        return sanitized, full_stats
    return sanitized


def print_stats_report(report: dict[str, Any]) -> None:
    """Print a clean human-readable sanitization report."""
    params = report["parameters"]
    pruning = report["pruning"]
    before = report["before"]
    after = report["after"]

    print("\n" + "=" * 60)
    print("SFace Gallery Sanitization Report")
    print("=" * 60)
    print(
        f"Parameters: max_cross={params['max_cross']}, min_within={params['min_within']}, "
        f"max_internal_sim={params['max_internal_sim']}"
    )

    print("\n=== BEFORE SANITIZATION ===")
    print(f"Total samples: {before['total_samples']}")
    for name, s in before["identities"].items():
        mc_str = f"{s['max_cross']:.4f}" if s["max_cross"] >= 0 else "N/A"
        print(
            f"  {name:10s}: {s['count']:3d} samples | "
            f"within-median [{s['within_median_min']:.4f}..{s['within_median_median']:.4f}..{s['within_median_max']:.4f}] | "
            f"max-cross: {mc_str}"
        )
    mc_all = f"{before['max_cross_similarity']:.4f}" if before["max_cross_similarity"] >= 0 else "N/A"
    print(f"Overall max cross-similarity: {mc_all}")

    print("\n=== PRUNING BREAKDOWN ===")
    print(f"  Internal outliers dropped:                       {pruning['internal_outlier_count']:3d}")
    print(f"  Cross-polluted dropped (cross >= {params['max_cross']}):        {pruning['cross_polluted_count']:3d}")
    print(f"  Total outlier samples dropped:                   {pruning['total_outliers_dropped']:3d}")
    print(f"  Burst duplicates pruned (< {params['max_internal_sim']}):              {pruning['burst_duplicates_dropped']:3d}")
    print(f"  Total pruned samples:                            {pruning['initial_total'] - pruning['final_total']:3d}")

    print("\n=== AFTER SANITIZATION ===")
    print(f"Total samples: {after['total_samples']}")
    for name, s in after["identities"].items():
        mc_str = f"{s['max_cross']:.4f}" if s["max_cross"] >= 0 else "N/A"
        print(
            f"  {name:10s}: {s['count']:3d} samples | "
            f"within-median [{s['within_median_min']:.4f}..{s['within_median_median']:.4f}..{s['within_median_max']:.4f}] | "
            f"max-cross: {mc_str}"
        )
    mc_after = after["max_cross_similarity"]
    mc_after_str = f"{mc_after:.4f}" if mc_after >= 0 else "N/A"
    sep_status = "OK (distinct identities)" if mc_after < DEFAULT_COSINE_THRESHOLD else "WARNING (too close)"
    print(f"Overall max cross-similarity: {mc_after_str} (match threshold {DEFAULT_COSINE_THRESHOLD}) -> {sep_status}")
    print("=" * 60 + "\n")


def to_plain(value: Any) -> Any:
    """Turn numpy arrays and scalars into plain Python lists, floats and ints.

    A pickle of a numpy array names numpy's internal modules, and those moved
    in numpy 2 (`numpy._core`). A gallery written on a numpy 2 machine then
    fails to load in the numpy 1.26 container with "No module named
    'numpy._core.numeric'" and A12 runs with no enrolled faces at all. Plain
    lists of floats load anywhere; the matcher calls np.asarray on each
    encoding, so it accepts them unchanged. float32 values widen to Python
    floats exactly, so no precision is lost.
    """
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {to_plain(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(to_plain(v) for v in value)
    return value


def write_gallery(path: str | Path, gallery: dict[str, Any]) -> None:
    """Write a gallery pickle free of numpy objects, atomically.

    The container reads this file on start; a half-written pickle would leave
    it with no faces, so the bytes go to a temporary file in the same directory
    and replace the target only once complete.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    plain = to_plain(gallery)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            # Protocol 4 is readable by every Python the container could run.
            pickle.dump(plain, handle, protocol=4)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; the container may read as another user, so
        # keep the old file's mode, or the usual 0644 for a new one.
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", "-i", required=True, help="Input gallery pkl path")
    parser.add_argument("--output", "-o", required=True, help="Output cleaned gallery pkl path")
    parser.add_argument(
        "--max-cross",
        type=float,
        default=0.35,
        help="Maximum cosine similarity to another identity (default: 0.35)",
    )
    parser.add_argument(
        "--min-within",
        type=float,
        default=0.40,
        help="Minimum median cosine similarity to own identity (default: 0.40)",
    )
    parser.add_argument(
        "--max-internal-sim",
        type=float,
        default=0.88,
        help="Maximum similarity between kept exemplars of the same identity (default: 0.88)",
    )
    parser.add_argument(
        "--max-exemplars",
        type=int,
        default=None,
        help="Optional upper bound on kept exemplars per identity",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const="stdout",
        default=None,
        help="Report detailed statistics (optionally specify path to save JSON report)",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    cleaned, stats = sanitize_gallery(
        input_path,
        max_cross=args.max_cross,
        min_within=args.min_within,
        max_internal_sim=args.max_internal_sim,
        max_exemplars=args.max_exemplars,
        return_stats=True,
    )

    output_path = Path(args.output)
    write_gallery(output_path, cleaned)

    print(f"Wrote cleaned gallery with {len(cleaned['encodings'])} encodings to {output_path}")

    if args.report:
        print_stats_report(stats)
        if args.report != "stdout":
            report_file = Path(args.report)
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text(json.dumps(stats, indent=2))
            print(f"Saved JSON report to {report_file}")
    else:
        # Minimal stats summary
        pruning = stats["pruning"]
        print(
            f"Pruned {pruning['initial_total'] - pruning['final_total']} samples: "
            f"{pruning['total_outliers_dropped']} outliers, {pruning['burst_duplicates_dropped']} burst duplicates. "
            f"Max cross-similarity: {stats['after']['max_cross_similarity']:.4f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
