"""Tests for the learning-data collection additions.

Three gaps found on 2026-08-17 while reconstructing why a real person went
unnotified all morning:

* audit rows for rejected/unconfirmed candidates had no image, so nobody can
  later verify what YOLO actually saw at 0.55 — or at an unconfirmed 0.92;
* person media shared the default 2-day retention while the decision audit
  lives 30 days, so week-old audit rows pointed at deleted clips;
* stream stalls lived only in the rotating (and once corrupted) docker log.
"""

import os
import sys
import time
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.camera import Camera, stall_event_detail
from a12_system.database import EventDB
from a12_system.pipeline import DetectionPipeline, retention_days
from a12_system.test_freeze_logging import _StallingResponse, _jpeg_bytes


# ── audit rows get an id, candidates get an image ────────────────────────────

def _audit_args(**over):
    args = {
        "trigger_source": "esp32_motion",
        "backend": "local",
        "candidate_label": "person",
        "candidate_confidence": 0.91,
        "yolo_confidence_threshold": 0.3,
        "notify_confidence_threshold": 0.7,
        "confirmations_required": 1,
        "confirmation_streak": 1,
        "sensor_confirmed": True,
        "active_sensors": ["esp32_motion"],
        "event_score": 95,
        "notify_threshold": 70,
        "local_record_threshold": 45,
        "decision_outcome": "recorded_and_notified",
    }
    args.update(over)
    return args


def test_log_decision_audit_returns_the_row_id(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    first = db.log_decision_audit(**_audit_args())
    second = db.log_decision_audit(**_audit_args())
    db.close()
    assert (first, second) == (1, 2)


class _DbStub:
    def __init__(self):
        self.rows = []

    def log_decision_audit(self, **audit):
        self.rows.append(audit)
        return len(self.rows) + 6


def _pipeline(tmp_path, **over):
    p = object.__new__(DetectionPipeline)
    p.screenshot_folder = str(tmp_path)
    p.db = _DbStub()
    p.candidate_snapshot_enabled = True
    p.candidate_snapshot_min_interval = 0.0
    p._last_candidate_snapshot = 0.0
    p.miss_snapshot_enabled = True
    p.miss_snapshot_min_interval = 0.0
    p._last_miss_snapshot = 0.0
    for key, value in over.items():
        setattr(p, key, value)
    return p


def _frame():
    return np.random.randint(0, 255, (48, 64, 3), dtype=np.uint8)


def _ctx(label="person", conf=0.91):
    return {
        "trigger_source": "binary_sensor.venkovni_senzor",
        "backend": "local",
        "candidate_label": label,
        "candidate_confidence": conf,
    }


def test_person_candidate_snapshot_saved_with_audit_id_and_outcome(tmp_path):
    p = _pipeline(tmp_path)
    p._log_decision_audit(
        _ctx(), 1, None, None, None, "awaiting_confirmation", frame=_frame()
    )
    files = os.listdir(tmp_path / "candidates")
    assert len(files) == 1
    assert "a7" in files[0], "filename must carry the audit row id"
    assert "awaiting_confirmation" in files[0]


def test_a_sensor_triggered_miss_is_photographed_but_not_as_a_candidate(tmp_path):
    """A sensor fired and YOLO found nothing. Either it missed a person — the
    failure that matters most — or it was a cat, and the image is the only way
    to tell. These rows carry no candidate_label, so the candidate gate never
    covered them: the largest population in the audit was the only blind one."""
    p = _pipeline(tmp_path)
    p._log_decision_audit(
        _ctx(label="", conf=None), 0, None, None, None, "no_person_candidate",
        frame=_frame(),
    )
    assert not (tmp_path / "candidates").exists(), "not a candidate, has no label"
    files = os.listdir(tmp_path / "misses")
    assert len(files) == 1
    assert "a7" in files[0], "filename must carry the audit row id"


def test_a_periodic_check_finding_nobody_is_not_evidence(tmp_path):
    """Without a sensor behind it, "nobody here" claims nothing was there, so
    there is nothing to verify and no reason to keep 288 frames a day."""
    p = _pipeline(tmp_path)
    p._log_decision_audit(
        {"trigger_source": "periodic", "backend": "local",
         "candidate_label": "", "candidate_confidence": None},
        0, None, None, None, "no_person_candidate", frame=_frame(),
    )
    assert not (tmp_path / "misses").exists()


def test_miss_snapshots_have_their_own_rate_limit(tmp_path):
    """Sharing the candidate limiter would let a busy candidate stream starve
    the population that has no other record."""
    p = _pipeline(tmp_path, miss_snapshot_min_interval=60.0)
    for _ in range(2):
        p._log_decision_audit(
            _ctx(label="", conf=None), 0, None, None, None, "no_person_candidate",
            frame=_frame(),
        )
    assert len(os.listdir(tmp_path / "misses")) == 1

    # A candidate snapshot in between must not consume the miss budget.
    p._log_decision_audit(
        _ctx(), 1, None, None, None, "awaiting_confirmation", frame=_frame()
    )
    assert len(os.listdir(tmp_path / "candidates")) == 1
    assert len(os.listdir(tmp_path / "misses")) == 1


def test_miss_snapshots_can_be_disabled(tmp_path):
    p = _pipeline(tmp_path, miss_snapshot_enabled=False)
    p._log_decision_audit(
        _ctx(label="", conf=None), 0, None, None, None, "no_person_candidate",
        frame=_frame(),
    )
    assert not (tmp_path / "misses").exists()


def test_no_snapshot_without_a_frame(tmp_path):
    p = _pipeline(tmp_path)
    p._log_decision_audit(_ctx(), 1, None, None, None, "awaiting_confirmation")
    assert not (tmp_path / "candidates").exists()


def test_snapshots_are_rate_limited(tmp_path):
    p = _pipeline(tmp_path, candidate_snapshot_min_interval=60.0)
    for _ in range(2):
        p._log_decision_audit(
            _ctx(), 1, None, None, None, "awaiting_confirmation", frame=_frame()
        )
    assert len(os.listdir(tmp_path / "candidates")) == 1


def test_no_snapshot_for_outcomes_that_already_saved_their_own_media(tmp_path):
    """A recorded event writes a clip; a candidate copy of the same frame would
    duplicate it and burn the shared rate-limit slot an unconfirmed candidate
    needs a second later."""
    p = _pipeline(tmp_path, candidate_snapshot_min_interval=60.0)

    p._log_decision_audit(
        _ctx(), 1, None, None, None, "recorded_and_notified", frame=_frame()
    )
    assert not (tmp_path / "candidates").exists()

    # The slot was never consumed, so the next unconfirmed candidate still lands.
    p._log_decision_audit(
        _ctx(), 1, None, None, None, "awaiting_confirmation", frame=_frame()
    )
    assert len(os.listdir(tmp_path / "candidates")) == 1


def test_snapshots_can_be_disabled(tmp_path):
    p = _pipeline(tmp_path, candidate_snapshot_enabled=False)
    p._log_decision_audit(
        _ctx(), 1, None, None, None, "awaiting_confirmation", frame=_frame()
    )
    assert not (tmp_path / "candidates").exists()


# ── per-folder media retention ───────────────────────────────────────────────

def _mk(path, age_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    old = time.time() - age_days * 86400
    os.utime(path, (old, old))


def test_cleanup_keeps_person_and_candidates_longer(tmp_path):
    p = object.__new__(DetectionPipeline)
    p.screenshot_folder = str(tmp_path)
    p.cleanup_max_age_days = 2
    p.person_media_retention_days = 30
    p.decision_audit_retention_days = 30

    _mk(tmp_path / "motion" / "old.mp4", 5)
    _mk(tmp_path / "person" / "old.mp4", 5)
    _mk(tmp_path / "candidates" / "old.jpg", 5)
    _mk(tmp_path / "misses" / "old.jpg", 5)
    _mk(tmp_path / "person" / "ancient.mp4", 40)
    _mk(tmp_path / "flat_debug_raw_root.jpg", 5)

    p._cleanup_old_media()

    assert not (tmp_path / "motion" / "old.mp4").exists()
    assert (tmp_path / "person" / "old.mp4").exists(), "person keeps its own retention"
    assert (tmp_path / "candidates" / "old.jpg").exists(), "candidates live as long as the audit"
    assert (tmp_path / "misses" / "old.jpg").exists(), "misses live as long as the audit"
    assert not (tmp_path / "person" / "ancient.mp4").exists()
    assert not (tmp_path / "flat_debug_raw_root.jpg").exists(), "root files keep the default"


def test_zero_retention_keeps_files_instead_of_deleting_them_all(tmp_path):
    """0 is documented as "keep forever" for every retention knob. Read as a
    literal day count it means the opposite: delete everything, now."""
    p = object.__new__(DetectionPipeline)
    p.screenshot_folder = str(tmp_path)
    p.cleanup_max_age_days = retention_days(0, 0.25)
    p.person_media_retention_days = retention_days(0, p.cleanup_max_age_days)
    p.decision_audit_retention_days = 0.0

    _mk(tmp_path / "motion" / "ancient.mp4", 400)
    _mk(tmp_path / "person" / "ancient.mp4", 400)
    _mk(tmp_path / "candidates" / "ancient.jpg", 400)

    p._cleanup_old_media()

    assert (tmp_path / "motion" / "ancient.mp4").exists()
    assert (tmp_path / "person" / "ancient.mp4").exists()
    assert (tmp_path / "candidates" / "ancient.jpg").exists()


def test_retention_days_maps_zero_to_infinity_and_respects_the_floor():
    assert retention_days(0) == float("inf")
    assert retention_days(-1) == float("inf")
    assert retention_days(0.1, 0.25) == 0.25
    assert retention_days(30, 0.25) == 30.0
    assert retention_days(14, float("inf")) == float("inf")


# ── stream stalls persisted for long-term statistics ─────────────────────────

def test_freeze_records_a_summary_for_persistence():
    cam = object.__new__(Camera)
    cam.log_prefix = "[test:cam]"
    resp = _StallingResponse(_jpeg_bytes())
    reason = cam.process_stream(resp, lambda frame: None, freeze_timeout=1.0)
    assert reason == "frozen"
    assert cam.last_freeze_summary is not None
    assert "likely=no_bytes_from_camera" in cam.last_freeze_summary


def test_stall_event_detail_merges_summary_and_camera_counters():
    detail = stall_event_detail(
        "likely=no_bytes_from_camera bytes=0 raw=0",
        {
            "wifi_rssi": -45,
            "stream_health": {
                "send_fail_count": 2114,
                "last_drop_reason": "send-fail",
                "last_errno": 104,
            },
        },
    )
    assert "likely=no_bytes_from_camera" in detail
    assert "send_fail_count=2114" in detail
    assert "last_drop_reason=send-fail" in detail
    assert "wifi_rssi=-45" in detail


def test_stall_event_detail_survives_missing_everything():
    assert stall_event_detail(None, None) == "no diagnostics"


# ── config knobs ─────────────────────────────────────────────────────────────

def test_daily_summary_reports_stalls_and_dates_the_scorer_counters():
    """Stalls are persisted because the docker log rotates them away; a counter
    nobody reports is still invisible. The scorer counters are cumulative since
    process start, so the summary must not present them as a 24h figure."""
    from a12_system.status_monitor import StatusMonitor

    sent = []
    monitor = object.__new__(StatusMonitor)
    monitor.stats = SimpleNamespace(
        get_summary=lambda: {
            "session": {"uptime_formatted": "1d 2h"},
            "scorer": {
                "requests": 900,
                "successes": 880,
                "transport_failures": 12,
                "http_errors": 8,
                "fallbacks": 20,
                "request_seconds_p95": 0.5,
            },
        }
    )
    monitor.db = SimpleNamespace(
        get_event_counts_since=lambda _since: {
            ("stream_stall", "frozen"): 17,
            ("stream_stall", "stream_ended"): 3,
            ("detection", "person"): 4,
        }
    )
    monitor.runtime_config = SimpleNamespace(get=lambda *_args: False)
    monitor.notifier = SimpleNamespace(
        send_telegram=lambda msg, **_kwargs: sent.append(msg)
    )

    monitor._send_daily_summary()

    assert len(sent) == 1
    # Both stall labels roll into one line the operator actually sees.
    assert "Přerušení streamu: 20x" in sent[0]
    # Transport failures and non-2xx answers are both scorer failures here.
    assert "20 chyb" in sent[0]
    assert "od startu" in sent[0], "must not read as a 24h number"


def test_new_knobs_have_env_overrides(monkeypatch, tmp_path):
    from a12_system.config import load_config

    monkeypatch.setenv("PERSON_MEDIA_RETENTION_DAYS", "14")
    monkeypatch.setenv("CANDIDATE_SNAPSHOT_ENABLED", "false")
    cfg = load_config(str(tmp_path))
    assert cfg["person_media_retention_days"] == 14
    assert cfg["candidate_snapshot_enabled"] is False


# ── ground truth: the one thing here that cannot be recomputed ───────────────

def test_media_path_links_a_decision_to_the_clip_it_produced(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    audit_id = db.log_decision_audit(**_audit_args())
    db.set_decision_audit_media(audit_id, "/data/screenshots/person/clip.mp4")
    stored = db.conn.execute(
        "select media_path from decision_audit where id = ?", (audit_id,)
    ).fetchone()[0]
    db.close()
    assert stored == "/data/screenshots/person/clip.mp4"


def test_media_path_ignores_a_missing_audit_id(tmp_path):
    """log_decision_audit returns None when the insert failed; that must not
    raise on the recording path."""
    db = EventDB(str(tmp_path / "events.db"))
    db.set_decision_audit_media(None, "/data/x.jpg")
    db.set_decision_audit_media(1, "")
    db.close()


def test_labels_survive_the_pruning_of_the_row_they_describe(tmp_path):
    """Audit rows expire after DECISION_AUDIT_RETENTION_DAYS. A human verdict is
    the only thing in this database that cannot be recomputed, so the label
    table denormalises what it needs and is never pruned."""
    db = EventDB(str(tmp_path / "events.db"))
    audit_id = db.log_decision_audit(**_audit_args())
    db.conn.execute(
        "update decision_audit set timestamp = timestamp - ? where id = ?",
        (99 * 86400, audit_id),
    )
    db.conn.commit()

    assert db.save_decision_label(
        audit_id, "person", candidate_confidence=0.62,
        decision_outcome="below_notify_confidence",
        trigger_source="binary_sensor.venkovni_senzor",
        image_path="candidates/cand_x.jpg",
    )
    assert db.prune_decision_audit(30) == 1

    row = db.conn.execute(
        """select truth, candidate_confidence, decision_outcome, image_path
           from decision_labels where audit_id = ?""", (audit_id,)
    ).fetchone()
    db.close()
    assert row == ("person", 0.62, "below_notify_confidence", "candidates/cand_x.jpg")


def test_relabelling_replaces_the_verdict_instead_of_duplicating_it(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    audit_id = db.log_decision_audit(**_audit_args())
    db.save_decision_label(audit_id, "not_person")
    db.save_decision_label(audit_id, "person", note="was a person after all")
    rows = db.conn.execute(
        "select truth, note from decision_labels where audit_id = ?", (audit_id,)
    ).fetchall()
    assert db.labeled_audit_ids() == {audit_id}
    db.close()
    assert rows == [("person", "was a person after all")]


def test_audit_rows_can_be_fetched_for_review(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    first = db.log_decision_audit(**_audit_args(candidate_confidence=0.62))
    second = db.log_decision_audit(**_audit_args(candidate_label="", candidate_confidence=None))
    rows = db.decision_audit_rows([first, second, 9999])
    db.close()
    assert set(rows) == {first, second}
    assert rows[first]["candidate_confidence"] == 0.62
    assert rows[second]["candidate_label"] == ""


def test_new_snapshot_knobs_have_env_overrides(monkeypatch, tmp_path):
    from a12_system.config import load_config

    monkeypatch.setenv("MISS_SNAPSHOT_ENABLED", "false")
    monkeypatch.setenv("MISS_SNAPSHOT_MIN_INTERVAL_SECONDS", "12")
    cfg = load_config(str(tmp_path))
    assert cfg["miss_snapshot_enabled"] is False
    assert cfg["miss_snapshot_min_interval_seconds"] == 12


# ── the review tool ─────────────────────────────────────────────────────────

def test_audit_id_is_recoverable_from_both_snapshot_names():
    from a12_system.tools.review_decisions import parse_audit_id

    assert parse_audit_id("cand_20260826_100108_a8683_0.90_recorded_and_notified.jpg") == 8683
    assert parse_audit_id("miss_20260826_100108_a8683.jpg") == 8683
    # Snapshots taken when the insert failed cannot be tied to a decision.
    assert parse_audit_id("cand_20260826_100108_ax_0.90_awaiting_confirmation.jpg") is None
    assert parse_audit_id("unrelated.jpg") is None


def test_review_skips_what_is_already_labeled(tmp_path):
    from a12_system.tools.review_decisions import discover

    shots = tmp_path / "screenshots"
    for kind, name in (
        ("candidates", "cand_20260826_100108_a11_0.62_below_notify_confidence.jpg"),
        ("misses", "miss_20260826_100109_a12.jpg"),
        ("misses", "miss_20260826_100110_ax.jpg"),
    ):
        (shots / kind).mkdir(parents=True, exist_ok=True)
        (shots / kind / name).write_bytes(b"x")

    every = discover(str(tmp_path))
    assert sorted(i["audit_id"] for i in every) == [11, 12], "the `ax` file is not reviewable"

    remaining = discover(str(tmp_path), skip_ids={11})
    assert [i["audit_id"] for i in remaining] == [12]


def test_stats_reports_precision_per_confidence_band(tmp_path):
    """The point of the labels: find the band where precision collapses."""
    from a12_system.tools.review_decisions import stats_report

    db = EventDB(str(tmp_path / "events.db"))
    for confidence, truth in (
        (0.92, "person"), (0.88, "person"),
        (0.60, "person"), (0.58, "not_person"), (0.56, "not_person"),
    ):
        audit_id = db.log_decision_audit(**_audit_args(candidate_confidence=confidence))
        db.save_decision_label(audit_id, truth, candidate_confidence=confidence)
    report = stats_report(db, str(tmp_path))
    db.close()

    assert "Labeled: 5" in report
    assert "0.85-1.00" in report and "0.55-0.64" in report
    # 2/2 in the top band, 1/3 in the contested one.
    assert "100%" in report and "33%" in report


def test_stats_says_so_when_there_is_no_ground_truth_yet(tmp_path):
    from a12_system.tools.review_decisions import stats_report

    db = EventDB(str(tmp_path / "events.db"))
    report = stats_report(db, str(tmp_path))
    db.close()
    assert "No verdicts yet" in report


def test_label_carries_the_audit_context_so_it_survives_pruning(tmp_path):
    from a12_system.tools.review_decisions import label

    db = EventDB(str(tmp_path / "events.db"))
    audit_id = db.log_decision_audit(
        **_audit_args(candidate_confidence=0.62,
                      decision_outcome="below_notify_confidence")
    )
    assert label(db, audit_id, "person", "candidates/x.jpg")
    row = db.conn.execute(
        """select candidate_confidence, decision_outcome, trigger_source, image_path
           from decision_labels where audit_id = ?""", (audit_id,)
    ).fetchone()
    db.close()
    assert row == (0.62, "below_notify_confidence", "esp32_motion", "candidates/x.jpg")
