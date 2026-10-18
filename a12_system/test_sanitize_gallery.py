"""Unit tests for SFace gallery sanitization.

Verifies:
1. Outliers with cross >= max_cross are pruned.
2. Internal outliers (within_median < min_within or max_cross >= within_median) are pruned.
3. Redundant near-duplicate samples (burst video frames) are pruned down to diverse exemplars.
4. Clean data preserves valid separation.
5. Edge cases: single identity, metadata preservation, empty gallery.
"""

from __future__ import annotations

import os
import pickle
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.face_backend import SFACE_BACKEND, cosine_similarity  # noqa: E402
from a12_system.tools.sanitize_gallery import (  # noqa: E402
    compute_gallery_stats,
    compute_sample_metrics,
    compute_similarity_matrix,
    normalize_embeddings,
    sanitize_gallery,
    select_exemplars_greedy,
)


def _make_unit_vector(dim: int = 128, seed: int = 0) -> np.ndarray:
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim)
    return vec / np.linalg.norm(vec)


def _make_cluster(
    center: np.ndarray, target_sim: float = 0.85, n: int = 5, seed: int = 0
) -> list[np.ndarray]:
    rng = np.random.RandomState(seed)
    samples = []
    dim = len(center)
    for _ in range(n):
        v = rng.randn(dim)
        v -= np.dot(v, center) * center
        v /= np.linalg.norm(v)
        cos_theta = float(np.clip(target_sim + rng.uniform(-0.005, 0.005), -1.0, 1.0))
        sin_theta = float(np.sqrt(1.0 - cos_theta**2))
        sample = cos_theta * center + sin_theta * v
        samples.append(sample.astype(np.float32))
    return samples


def test_normalize_and_similarity():
    # Zero vector handling
    zero = np.zeros(128)
    normed = normalize_embeddings([zero])
    assert normed.shape == (1, 128)
    assert np.all(normed == 0.0)

    # Unit vector normalization
    v = np.ones(128)
    normed = normalize_embeddings([v])
    assert np.isclose(np.linalg.norm(normed[0]), 1.0)

    # Empty
    assert len(normalize_embeddings([])) == 0
    assert compute_similarity_matrix(normalize_embeddings([])).shape == (0, 0)


def test_cross_polluted_outliers_pruned():
    """Samples whose max similarity to another identity >= max_cross must be dropped."""
    alice_center = _make_unit_vector(seed=10)
    bob_center = _make_unit_vector(seed=20)
    # Orthogonalize Bob from Alice to ensure baseline cross similarity is near 0
    bob_center -= np.dot(bob_center, alice_center) * alice_center
    bob_center /= np.linalg.norm(bob_center)

    alice_samples = _make_cluster(alice_center, target_sim=0.90, n=4, seed=100)
    bob_samples = _make_cluster(bob_center, target_sim=0.90, n=4, seed=200)

    # Mislabeled sample: a photo of Bob labeled as Alice
    # Its similarity to Bob is ~0.80, which violates max_cross=0.35, while sim to Alice is ~0.0
    polluted_alice = _make_cluster(bob_center, target_sim=0.80, n=1, seed=999)[0]

    encodings = alice_samples + [polluted_alice] + bob_samples
    names = ["alice"] * 5 + ["bob"] * 4

    data = {
        "backend": SFACE_BACKEND,
        "encodings": encodings,
        "names": names,
    }

    clean = sanitize_gallery(data, max_cross=0.35, min_within=0.40, max_internal_sim=0.88)

    # Polluted sample must be purged
    assert clean["names"].count("alice") == 4
    assert clean["names"].count("bob") == 4
    assert len(clean["encodings"]) == 8

    # Verify no remaining Alice sample is close to Bob
    for enc, name in zip(clean["encodings"], clean["names"]):
        if name == "alice":
            for b_enc, b_name in zip(clean["encodings"], clean["names"]):
                if b_name == "bob":
                    assert cosine_similarity(enc, b_enc) < 0.35


def test_internal_outliers_pruned():
    """Extreme internal outliers with within_median < min_within must be dropped."""
    carol_center = _make_unit_vector(seed=30)
    carol_samples = _make_cluster(carol_center, target_sim=0.90, n=5, seed=300)

    # An orthogonal random noise sample labeled Carol (sim ~0.0 to Carol cluster)
    noise_sample = _make_unit_vector(seed=999)
    # Ensure orthogonal
    noise_sample -= np.dot(noise_sample, carol_center) * carol_center
    noise_sample /= np.linalg.norm(noise_sample)

    encodings = carol_samples + [noise_sample]
    names = ["carol"] * 6

    data = {
        "backend": SFACE_BACKEND,
        "encodings": encodings,
        "names": names,
    }

    clean = sanitize_gallery(data, max_cross=0.35, min_within=0.40, max_internal_sim=0.88)

    assert len(clean["encodings"]) == 5
    assert clean["names"] == ["carol"] * 5
    for enc in clean["encodings"]:
        assert cosine_similarity(enc, carol_center) > 0.80


def test_internal_outlier_closer_to_other_identity_pruned():
    """A sample with max_cross >= within_median must be dropped."""
    base_a = _make_unit_vector(seed=40)
    base_b = _make_unit_vector(seed=50)
    base_b -= np.dot(base_b, base_a) * base_a
    base_b /= np.linalg.norm(base_b)

    samples_a = _make_cluster(base_a, target_sim=0.90, n=4, seed=400)
    samples_b = _make_cluster(base_b, target_sim=0.90, n=4, seed=500)

    # Ambiguous sample labeled A: within_median to A ~0.65, but similarity to B ~0.76 (max_cross >= within_median)
    ambiguous = 0.55 * base_a + 0.65 * base_b
    ambiguous /= np.linalg.norm(ambiguous)

    encodings = samples_a + [ambiguous] + samples_b
    names = ["a"] * 5 + ["b"] * 4

    data = {
        "backend": SFACE_BACKEND,
        "encodings": encodings,
        "names": names,
    }

    clean = sanitize_gallery(data, max_cross=0.35, min_within=0.40, max_internal_sim=0.88)
    assert clean["names"].count("a") == 4
    assert clean["names"].count("b") == 4


def test_redundant_near_duplicates_pruned_down_to_diverse_exemplars():
    """Video burst frames (sim >= max_internal_sim) must be deduplicated to diverse exemplars."""
    center = _make_unit_vector(seed=60)
    # Burst of 5 near-duplicate frames from stationary video (sim >= 0.95 to each other)
    burst_samples = _make_cluster(center, target_sim=0.98, n=5, seed=600)
    # Two diverse head poses (sim ~0.82 to center, so sim to burst is < 0.88, and sim to each other < 0.88)
    diverse_samples = _make_cluster(center, target_sim=0.82, n=2, seed=700)

    encodings = burst_samples + diverse_samples
    names = ["dave"] * 7

    data = {
        "backend": SFACE_BACKEND,
        "encodings": encodings,
        "names": names,
    }

    clean = sanitize_gallery(data, max_cross=0.35, min_within=0.40, max_internal_sim=0.88)

    # The 5 burst frames collapse into 1 exemplar, while the 2 diverse poses are kept
    assert len(clean["encodings"]) == 3
    assert clean["names"] == ["dave"] * 3

    # Invariant: max pairwise similarity between kept exemplars must be < 0.88
    kept = clean["encodings"]
    for i in range(len(kept)):
        for j in range(i + 1, len(kept)):
            sim = cosine_similarity(kept[i], kept[j])
            assert sim < 0.88, f"Pairwise similarity {sim} >= 0.88 between exemplars {i} and {j}"


def test_clean_data_preserves_valid_separation():
    """Already clean, distinct data must remain intact and well-separated."""
    alice_center = _make_unit_vector(seed=10)
    bob_center = _make_unit_vector(seed=20)
    bob_center -= np.dot(bob_center, alice_center) * alice_center
    bob_center /= np.linalg.norm(bob_center)

    alice_samples = _make_cluster(alice_center, target_sim=0.82, n=3, seed=110)
    bob_samples = _make_cluster(bob_center, target_sim=0.82, n=3, seed=210)

    data = {
        "backend": SFACE_BACKEND,
        "encodings": alice_samples + bob_samples,
        "names": ["alice"] * 3 + ["bob"] * 3,
    }

    clean, stats = sanitize_gallery(
        data, max_cross=0.35, min_within=0.40, max_internal_sim=0.88, return_stats=True
    )

    assert len(clean["encodings"]) == 6
    assert clean["names"] == ["alice"] * 3 + ["bob"] * 3
    assert stats["after"]["max_cross_similarity"] < 0.35
    assert stats["pruning"]["total_outliers_dropped"] == 0
    assert stats["pruning"]["burst_duplicates_dropped"] == 0


def test_single_identity_gallery():
    """Galleries with only one identity have no cross identities to compare against."""
    base = _make_unit_vector(seed=70)
    samples = _make_cluster(base, target_sim=0.82, n=4, seed=710)

    data = {
        "backend": SFACE_BACKEND,
        "encodings": samples,
        "names": ["solo"] * 4,
    }

    clean, stats = sanitize_gallery(data, return_stats=True)
    assert len(clean["encodings"]) == 4
    assert stats["after"]["max_cross_similarity"] == -1.0


def test_empty_and_mismatched_inputs():
    """Empty galleries or malformed inputs must raise or return empty cleanly."""
    empty = sanitize_gallery({"backend": SFACE_BACKEND, "encodings": [], "names": []})
    assert empty["encodings"] == []
    assert empty["names"] == []

    with pytest.raises(ValueError, match="Mismatched gallery lengths"):
        sanitize_gallery({"backend": SFACE_BACKEND, "encodings": [_make_unit_vector()], "names": []})

    with pytest.raises(ValueError, match="must be a dictionary"):
        sanitize_gallery(12345)


def test_metadata_and_sources_preserved(tmp_path):
    """File path inputs, 'sources' alignment and custom metadata must be preserved."""
    base = _make_unit_vector(seed=80)
    samples = _make_cluster(base, target_sim=0.82, n=3, seed=810)
    sources = ["/img/1.jpg", "/img/2.jpg", "/img/3.jpg"]

    data = {
        "backend": SFACE_BACKEND,
        "encodings": samples,
        "names": ["eva"] * 3,
        "sources": sources,
        "custom_tag": "test_tag_value",
    }

    pkl_file = tmp_path / "test_gallery.pkl"
    with open(pkl_file, "wb") as f:
        pickle.dump(data, f)

    clean = sanitize_gallery(pkl_file, max_internal_sim=0.99)
    assert clean["custom_tag"] == "test_tag_value"
    assert clean["sources"] == sources
    assert len(clean["encodings"]) == 3


def test_compute_sample_metrics_and_greedy_helpers():
    """Direct verification of helper math functions."""
    normed = np.array([
        [1.0, 0.0],
        [0.0, 1.0],
        [1.0, 0.0],
    ])
    sim = compute_similarity_matrix(normed)
    names = ["p1", "p2", "p1"]

    max_cross, within_med = compute_sample_metrics(sim, names)
    assert max_cross[0] == 0.0
    assert max_cross[1] == 0.0
    assert within_med[0] == 1.0
    assert within_med[1] == 1.0  # Solo sample defaults to 1.0

    stats = compute_gallery_stats(normed, names)
    assert stats["total_samples"] == 3
    assert "p1" in stats["identities"]
    assert "p2" in stats["identities"]

    # Greedy selection with max_internal_sim = 0.5 collapses duplicate
    kept = select_exemplars_greedy(sim[np.ix_([0, 2], [0, 2])], [0, 2], max_internal_sim=0.5)
    assert len(kept) == 1
