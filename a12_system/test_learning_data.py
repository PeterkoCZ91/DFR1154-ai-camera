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


def test_no_snapshot_when_there_was_no_candidate(tmp_path):
    p = _pipeline(tmp_path)
    p._log_decision_audit(
        _ctx(label="", conf=None), 0, None, None, None, "no_person_candidate",
        frame=_frame(),
    )
    assert not (tmp_path / "candidates").exists()


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
    _mk(tmp_path / "person" / "ancient.mp4", 40)
    _mk(tmp_path / "flat_debug_raw_root.jpg", 5)

    p._cleanup_old_media()

    assert not (tmp_path / "motion" / "old.mp4").exists()
    assert (tmp_path / "person" / "old.mp4").exists(), "person keeps its own retention"
    assert (tmp_path / "candidates" / "old.jpg").exists(), "candidates live as long as the audit"
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
