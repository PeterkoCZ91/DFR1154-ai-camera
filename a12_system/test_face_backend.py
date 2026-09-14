"""The YuNet + SFace backend, and the gallery it is allowed to compare against.

Why this backend: verified inside the running container, OpenCV 4.11 already
exposes `cv2.FaceDetectorYN_create` and `cv2.FaceRecognizerSF_create`, and they
run on `cv2.dnn` — the same engine the YOLO path already uses, under the same
single-thread pinning. Enabling it costs two model files in the data dir and no
new dependency. dlib, face_recognition, onnxruntime and insightface are all
absent from the image, so the old path could never have run there at all.

The trap this file mostly guards: dlib and SFace embeddings are BOTH 128-d, but
they live in different spaces and mean nothing to each other. A dimension check
cannot catch a mix-up, so the gallery carries the backend that produced it and
a mismatch is refused rather than silently compared — silently comparing would
produce confident garbage, which for a suppressor means muting real strangers.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.face_backend import (
    SFACE_BACKEND,
    SFaceBackend,
    best_match,
    cosine_similarity,
    read_gallery,
)


def _emb(*values):
    vec = np.zeros(128, dtype=np.float32)
    for i, v in enumerate(values):
        vec[i] = v
    return vec


# --- similarity ------------------------------------------------------------


def test_identical_embeddings_are_maximally_similar():
    assert cosine_similarity(_emb(1, 0), _emb(1, 0)) == 1.0


def test_orthogonal_embeddings_are_not_similar():
    assert abs(cosine_similarity(_emb(1, 0), _emb(0, 1))) < 1e-6


def test_magnitude_does_not_change_similarity():
    """SFace features are not unit-normalised; only direction carries identity."""
    assert cosine_similarity(_emb(1, 0), _emb(7, 0)) == 1.0


def test_a_zero_embedding_is_similar_to_nothing():
    """A failed alignment can yield an all-zero feature; it must not match."""
    assert cosine_similarity(_emb(0, 0), _emb(1, 0)) == 0.0


# --- matching --------------------------------------------------------------


def test_no_match_below_the_threshold():
    gallery = [_emb(0, 1)]
    name, score = best_match(_emb(1, 0), gallery, ["Resident"], threshold=0.363)
    assert name is None


def test_the_closest_gallery_entry_wins():
    gallery = [_emb(1, 0.9), _emb(1, 0.05)]
    name, score = best_match(
        _emb(1, 0), gallery, ["Other", "Resident"], threshold=0.363
    )
    assert name == "Resident"
    assert score > 0.9


def test_an_empty_gallery_matches_nobody():
    name, score = best_match(_emb(1, 0), [], [], threshold=0.363)
    assert name is None


def test_the_default_threshold_is_the_documented_sface_value():
    """0.363 cosine is OpenCV's own published operating point for SFace.

    It is a starting value, not a fitted one — the same mistake as dlib's 0.6,
    and it is configurable for that reason.
    """
    from a12_system.face_backend import DEFAULT_COSINE_THRESHOLD

    assert DEFAULT_COSINE_THRESHOLD == 0.363


# --- the gallery -----------------------------------------------------------


def test_a_gallery_from_this_backend_is_accepted():
    data = {
        "backend": SFACE_BACKEND,
        "encodings": [_emb(1, 0), _emb(0, 1)],
        "names": ["Resident", "Other"],
    }
    encodings, names = read_gallery(data, SFACE_BACKEND)
    assert len(encodings) == 2
    assert names == ["Resident", "Other"]


def test_a_dlib_era_gallery_is_refused_not_compared():
    """Both are 128-d, so nothing but the tag can tell them apart."""
    data = {"encodings": [_emb(1, 0)], "names": ["Resident"]}
    encodings, names = read_gallery(data, SFACE_BACKEND)
    assert encodings == [] and names == []


def test_a_gallery_from_another_backend_is_refused():
    data = {"backend": "dlib", "encodings": [_emb(1, 0)], "names": ["Resident"]}
    assert read_gallery(data, SFACE_BACKEND) == ([], [])


def test_a_gallery_with_mismatched_lengths_is_refused():
    """known_face_names[best_idx] would otherwise raise or name the wrong person."""
    data = {
        "backend": SFACE_BACKEND,
        "encodings": [_emb(1, 0), _emb(0, 1)],
        "names": ["Resident"],
    }
    assert read_gallery(data, SFACE_BACKEND) == ([], [])


def test_a_bare_list_gallery_is_refused():
    """The old loader accepted a bare list and named everyone after the file."""
    assert read_gallery([_emb(1, 0)], SFACE_BACKEND) == ([], [])


# --- the backend wrapper ---------------------------------------------------


class _FakeDetector:
    def __init__(self, faces):
        self.faces = faces
        self.sizes = []

    def setInputSize(self, size):
        self.sizes.append(size)

    def detect(self, image):
        return 1, self.faces


class _FakeRecognizer:
    def __init__(self, feature=None):
        self._feature = feature if feature is not None else _emb(1, 0)
        self.aligned = 0

    def alignCrop(self, image, face):
        self.aligned += 1
        return image

    def feature(self, aligned):
        return self._feature.reshape(1, -1)


def _frame(h=240, w=320):
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_no_detected_face_yields_no_embeddings():
    backend = SFaceBackend(_FakeDetector(None), _FakeRecognizer())
    assert backend.embed(_frame()) == []


def test_a_detected_face_yields_one_embedding():
    face = np.array([10, 10, 90, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.9], dtype=np.float32)
    backend = SFaceBackend(_FakeDetector(np.array([face])), _FakeRecognizer())
    embeddings = backend.embed(_frame())
    assert len(embeddings) == 1
    assert embeddings[0].shape == (128,)


def test_the_detector_is_told_the_real_frame_size():
    """YuNet silently finds nothing when the input size does not match.

    The crop handed in varies with the person box, so a fixed size configured
    once at construction would quietly return zero faces forever.
    """
    face = np.array([10, 10, 90, 90] + [0] * 10 + [0.9], dtype=np.float32)
    detector = _FakeDetector(np.array([face]))
    backend = SFaceBackend(detector, _FakeRecognizer())
    backend.embed(_frame(240, 320))
    backend.embed(_frame(100, 150))
    assert detector.sizes == [(320, 240), (150, 100)]


def test_a_frame_too_small_to_hold_a_face_is_skipped():
    detector = _FakeDetector(None)
    backend = SFaceBackend(detector, _FakeRecognizer())
    assert backend.embed(_frame(8, 8)) == []
    assert detector.sizes == [], "YuNet was run on something that cannot hold a face"


def test_a_raising_backend_yields_no_embeddings_rather_than_exploding():
    class _Boom:
        def setInputSize(self, size):
            raise RuntimeError("dnn exploded")

    assert SFaceBackend(_Boom(), _FakeRecognizer()).embed(_frame()) == []


# --- the detector speaking through this backend ----------------------------
# Same FaceOutcome contract as the dlib path, so everything downstream — the
# episode vote, the caption, the daily summary — is unchanged by the swap.

from a12_system.detection import Detector  # noqa: E402
from a12_system.face_result import FaceOutcome  # noqa: E402


class _StubBackend:
    def __init__(self, embeddings):
        self._embeddings = embeddings

    def embed(self, frame):
        return list(self._embeddings)


def _detector(embeddings, gallery, names, threshold=0.363):
    det = Detector.__new__(Detector)
    det.config = {}
    det.face_backend = _StubBackend(embeddings)
    det.face_cosine_threshold = threshold
    det.known_face_encodings = list(gallery)
    det.known_face_names = list(names)
    return det


def test_a_matching_face_is_a_resident():
    det = _detector([_emb(1, 0)], [_emb(1, 0)], ["Resident"])
    result = det.identify_person(_frame())
    assert result.outcome is FaceOutcome.RESIDENT
    assert result.name == "Resident"


def test_a_face_matching_nobody_is_a_stranger():
    det = _detector([_emb(1, 0)], [_emb(0, 1)], ["Resident"])
    assert det.identify_person(_frame()).outcome is FaceOutcome.STRANGER


def test_a_frame_with_no_face_is_no_face():
    det = _detector([], [_emb(1, 0)], ["Resident"])
    assert det.identify_person(_frame()).outcome is FaceOutcome.NO_FACE


def test_an_empty_gallery_is_unavailable_even_with_a_working_backend():
    """A face was seen but nothing could be compared — claim nothing."""
    det = _detector([_emb(1, 0)], [], [])
    assert det.identify_person(_frame()).outcome is FaceOutcome.UNAVAILABLE


def test_the_best_of_several_faces_decides():
    """Two people in frame: the resident must be found, not just the first face."""
    det = _detector([_emb(0, 1), _emb(1, 0)], [_emb(1, 0)], ["Resident"])
    assert det.identify_person(_frame()).outcome is FaceOutcome.RESIDENT


# --- what the check saw, kept for replay -----------------------------------
# Saving the actual crop plus the score is the only way to tell "the face was
# too small" from "the crop was off the person" from "the threshold is wrong".
# The verdict alone cannot distinguish those three.

from a12_system.face_result import debug_crop_name  # noqa: E402
from a12_system.face_result import FaceResult as FR  # noqa: E402


def test_the_score_is_carried_out_of_the_match():
    """Item 6 needs the distribution, not just the yes/no."""
    det = _detector([_emb(1, 0)], [_emb(1, 0)], ["Resident"])
    assert det.identify_person(_frame()).score > 0.99


def test_a_near_miss_reports_how_near():
    """A stranger just under the threshold is the interesting case."""
    det = _detector([_emb(1, 0.4)], [_emb(1, 0)], ["Resident"], threshold=0.99)
    result = det.identify_person(_frame())
    assert result.outcome is FaceOutcome.STRANGER
    assert 0.9 < result.score < 0.99


def test_a_frame_with_no_face_has_no_score():
    det = _detector([], [_emb(1, 0)], ["Resident"])
    assert det.identify_person(_frame()).score is None


def test_the_filename_records_the_outcome():
    name = debug_crop_name(1757700000.0, FR(FaceOutcome.NO_FACE))
    assert "no_face" in name and name.endswith(".jpg")


def test_the_filename_records_the_score_so_files_sort_by_how_close():
    name = debug_crop_name(1757700000.0, FR(FaceOutcome.STRANGER, None, 0.2718))
    assert "0.272" in name


def test_the_filename_records_who_was_matched():
    name = debug_crop_name(1757700000.0, FR(FaceOutcome.RESIDENT, "Resident", 0.9))
    assert "Resident" in name


def test_a_name_can_never_escape_the_debug_directory():
    """Gallery names come from directory names, which a user controls."""
    name = debug_crop_name(1757700000.0, FR(FaceOutcome.RESIDENT, "../../etc/passwd", 0.9))
    assert "/" not in name and ".." not in name


def test_the_strongest_match_wins_regardless_of_face_order():
    """Two people in frame, the resident seen better but detected first.

    Tracking "the last face that matched somebody" instead of "the best match"
    makes the answer depend on detection order, which is arbitrary.
    """
    gallery = [_emb(1, 0), _emb(0, 1)]
    names = ["Resident", "Other"]
    det = _detector([_emb(1, 0), _emb(0.8, 1)], gallery, names)
    result = det.identify_person(_frame())
    assert result.name == "Resident"
    assert result.score > 0.99


def test_the_reported_score_is_the_closest_face_not_the_last_one():
    """With nobody matched, the near miss is the number worth keeping.

    Overwriting instead of maximising makes the reported score depend on
    detection order, which would quietly poison any threshold fitted from it.
    """
    det = _detector([_emb(1, 0.2), _emb(0, 1)], [_emb(1, 0)], ["Resident"], threshold=0.99)
    result = det.identify_person(_frame())
    assert result.outcome is FaceOutcome.STRANGER
    assert result.score > 0.95


# --- collecting a gallery that spans poses ---------------------------------


def test_the_first_capture_is_always_kept():
    from a12_system.face_backend import is_distinct_enough

    assert is_distinct_enough(_emb(1, 0), [], 0.92) is True


def test_a_near_duplicate_pose_is_rejected():
    """Twenty frames of one pose is one pose, not twenty samples.

    A gallery has to span head angles; the measured failure mode is a resident
    at 35 degrees of pitch scoring like a stranger.
    """
    from a12_system.face_backend import is_distinct_enough

    assert is_distinct_enough(_emb(1, 0.01), [_emb(1, 0)], 0.92) is False


def test_a_genuinely_different_pose_is_kept():
    from a12_system.face_backend import is_distinct_enough

    assert is_distinct_enough(_emb(1, 1), [_emb(1, 0)], 0.92) is True


def test_distinctness_is_judged_against_every_sample_kept_so_far():
    """Not just the previous one, or the person can drift back to an old pose."""
    from a12_system.face_backend import is_distinct_enough

    kept = [_emb(1, 0), _emb(0, 1)]
    assert is_distinct_enough(_emb(0, 1.01), kept, 0.92) is False


# --- what may go INTO a gallery -------------------------------------------
# An enrolment sample is permanent: a bad one degrades every later match and
# nothing downstream can undo it. Enrolment can therefore be far stricter than
# runtime, because we control the conditions — the person is standing still,
# on purpose, and a rejected frame costs a second.
#
# Thresholds measured 2026-09-12 on samples known to work. The ten reference
# photos in the live gallery: face width 146-310px, detector score 0.851+.
# Live crops that matched at 0.59-0.75: width 61-136px, score 0.655+.
#
# Blur was measured too and deliberately NOT used: Laplacian variance runs
# 10-39 on the reference photos and 9-18 on good live crops, so it does not
# separate good from bad on this camera and any threshold would either pass
# everything or reject everything.


def test_a_large_sharp_face_is_accepted():
    from a12_system.face_backend import enrolment_quality_problem

    assert enrolment_quality_problem(210, 0.905) is None


def test_a_face_too_small_to_enrol_is_rejected():
    """The sample is permanent, so the bar sits above the bottom of the range."""
    from a12_system.face_backend import enrolment_quality_problem

    problem = enrolment_quality_problem(61, 0.905)
    assert problem is not None and "small" in problem


def test_a_face_of_a_size_the_door_can_actually_produce_is_accepted():
    """The first thresholds rejected every frame of a real capture.

    A second person at the door measured 79-99px at detector scores of
    0.70-0.78, so a 100px/0.85 floor collected nothing at all. Sizes in this
    range demonstrably produce matching embeddings on this camera.
    """
    from a12_system.face_backend import enrolment_quality_problem

    assert enrolment_quality_problem(89, 0.777) is None
    assert enrolment_quality_problem(99, 0.80) is None


def test_an_uncertain_detection_is_rejected():
    """0.65 is fine at runtime but too loose to write into a gallery."""
    from a12_system.face_backend import enrolment_quality_problem

    problem = enrolment_quality_problem(210, 0.64)
    assert problem is not None and "uncertain" in problem


def test_the_reported_problem_names_the_measurement():
    """The person is at the door and cannot read this; the log must explain."""
    from a12_system.face_backend import enrolment_quality_problem

    assert "61" in enrolment_quality_problem(61, 0.9)


def test_every_reference_photo_that_works_today_would_still_be_accepted():
    """A stricter gate that rejects the working gallery is a broken gate."""
    from a12_system.face_backend import enrolment_quality_problem

    for width, score in ((146, 0.851), (210, 0.905), (310, 0.95)):
        assert enrolment_quality_problem(width, score) is None


# --- flagging an odd sample, without deleting it --------------------------
# Measured: the one sample this flags in the live gallery is a full profile
# shot — a real outlier by similarity and the most valuable pose in the set.
# Similarity cannot separate "somebody else" from "an extreme angle", so the
# tool reports and a human decides.


def test_a_consistent_set_flags_nothing():
    from a12_system.face_backend import flag_unusual_samples

    samples = [_emb(1, 0), _emb(1, 0.1), _emb(1, 0.2)]
    assert flag_unusual_samples(samples, ["a"] * 3, floor=0.45) == []


def test_a_sample_far_from_its_own_set_is_flagged():
    from a12_system.face_backend import flag_unusual_samples

    samples = [_emb(1, 0), _emb(1, 0.1), _emb(1, 0.2), _emb(0, 1)]
    flagged = flag_unusual_samples(samples, ["a"] * 4, floor=0.45)
    assert [i for i, _ in flagged] == [3]


def test_flagging_never_removes_anything():
    """The caller keeps every sample; this only produces a warning."""
    from a12_system.face_backend import flag_unusual_samples

    samples = [_emb(1, 0), _emb(1, 0.1), _emb(1, 0.2), _emb(0, 1)]
    names = ["a"] * 4
    flag_unusual_samples(samples, names, floor=0.45)
    assert len(samples) == 4 and len(names) == 4


def test_oddness_is_judged_per_person_not_across_the_gallery():
    """Two enrolled people are SUPPOSED to be far apart."""
    from a12_system.face_backend import flag_unusual_samples

    samples = [_emb(1, 0), _emb(1, 0.1), _emb(0, 1), _emb(0.1, 1)]
    assert flag_unusual_samples(samples, ["a", "a", "b", "b"], floor=0.45) == []


def test_a_person_with_too_few_samples_is_left_alone():
    """With two samples there is no majority to be unusual against."""
    from a12_system.face_backend import flag_unusual_samples

    samples = [_emb(1, 0), _emb(0, 1)]
    assert flag_unusual_samples(samples, ["a", "a"], floor=0.45) == []


def test_two_crops_in_the_same_millisecond_get_different_names():
    """Timestamps do not make a filename unique, and CI proved it.

    The name carried seconds plus milliseconds, so two writes inside one
    millisecond collided and the second silently overwrote the first — three
    saves, two files. It passed locally only because the loop happened to be
    slower there.
    """
    when = 1757700000.123456
    first = debug_crop_name(when, FR(FaceOutcome.NO_FACE), seq=1)
    second = debug_crop_name(when, FR(FaceOutcome.NO_FACE), seq=2)
    assert first != second


def test_the_sequence_number_is_optional():
    """Callers that do not care keep the old shape."""
    name = debug_crop_name(1757700000.0, FR(FaceOutcome.NO_FACE))
    assert name.endswith(".jpg") and "no_face" in name
