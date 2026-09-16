"""Inference latency belongs in the audit row, not only in a process counter.

`ScorerStats` keeps p50/p95/max in memory and every A12 restart resets them, so
"were the misses concentrated when inference was slow?" could not be asked about
last week. Each decision already writes an audit row; the time the inference
took is a property of that decision, so it goes on that row.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.database import EventDB

_AUDIT = dict(
    trigger_source="motion",
    backend="local",
    candidate_label="person",
    candidate_confidence=0.71,
    yolo_confidence_threshold=0.5,
    notify_confidence_threshold=0.65,
    confirmations_required=1,
    confirmation_streak=1,
    sensor_confirmed=True,
    active_sensors=["pir"],
    event_score=5,
    notify_threshold=4,
    local_record_threshold=2,
    decision_outcome="recorded_and_notified",
)


def test_audit_row_records_how_long_the_inference_took(tmp_path):
    db = EventDB(str(tmp_path / "e.db"))
    try:
        audit_id = db.log_decision_audit(**_AUDIT, inference_seconds=0.284)
        row = db.conn.execute(
            "SELECT inference_seconds FROM decision_audit WHERE id=?", (audit_id,)
        ).fetchone()
    finally:
        db.conn.close()
    assert row[0] == 0.284


def test_inference_seconds_is_optional(tmp_path):
    """Callers that never measured it must still be able to write a row.

    Several audit paths (the frozen/flat ladders, sensor-only decisions) reach
    the audit without running YOLO at all; a NOT NULL here would drop them.
    """
    db = EventDB(str(tmp_path / "e.db"))
    try:
        audit_id = db.log_decision_audit(**_AUDIT)
        row = db.conn.execute(
            "SELECT inference_seconds FROM decision_audit WHERE id=?", (audit_id,)
        ).fetchone()
    finally:
        db.conn.close()
    assert audit_id is not None
    assert row[0] is None


def test_existing_database_is_migrated_not_replaced(tmp_path):
    """An installed A12 has months of audit rows; they must survive the upgrade."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE decision_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        datetime TEXT NOT NULL,
        trigger_source TEXT NOT NULL,
        backend TEXT NOT NULL,
        candidate_label TEXT NOT NULL,
        candidate_confidence REAL,
        yolo_confidence_threshold REAL NOT NULL,
        notify_confidence_threshold REAL NOT NULL,
        confirmations_required INTEGER NOT NULL,
        confirmation_streak INTEGER NOT NULL,
        sensor_confirmed INTEGER,
        active_sensors TEXT NOT NULL,
        event_score INTEGER,
        notify_threshold INTEGER NOT NULL,
        local_record_threshold INTEGER NOT NULL,
        decision_outcome TEXT NOT NULL,
        media_path TEXT
    )""")
    conn.execute(
        "INSERT INTO decision_audit (timestamp, datetime, trigger_source, backend,"
        " candidate_label, yolo_confidence_threshold, notify_confidence_threshold,"
        " confirmations_required, confirmation_streak, active_sensors, notify_threshold,"
        " local_record_threshold, decision_outcome) VALUES"
        " (1.0, 'then', 'motion', 'local', 'person', 0.5, 0.65, 1, 1, '[]', 4, 2, 'old_row')"
    )
    conn.commit()
    conn.close()

    db = EventDB(path)
    try:
        columns = {r[1] for r in db.conn.execute("PRAGMA table_info(decision_audit)")}
        kept = db.conn.execute(
            "SELECT decision_outcome, inference_seconds FROM decision_audit"
        ).fetchall()
    finally:
        db.conn.close()

    assert "inference_seconds" in columns
    assert kept == [("old_row", None)]


# --- the pipeline side: measuring it, and getting it onto the row ---

import time  # noqa: E402

from a12_system.pipeline import DetectionPipeline  # noqa: E402


class _Detector:
    def __init__(self, delay=0.0, boom=None):
        self.delay = delay
        self.boom = boom
        self.last_backend = "local"

    def detect_objects(self, frame):
        time.sleep(self.delay)
        if self.boom:
            raise self.boom
        return [("person", 0.8)]


def test_inference_is_timed():
    p = object.__new__(DetectionPipeline)
    p.detector = _Detector(delay=0.05)

    assert p._run_inference(object()) == [("person", 0.8)]
    assert p._last_inference_seconds >= 0.05
    assert p._last_inference_seconds < 5.0


def test_a_failed_inference_still_records_its_cost():
    """A slow failure is the interesting case — it must not vanish."""
    p = object.__new__(DetectionPipeline)
    p.detector = _Detector(delay=0.02, boom=RuntimeError("scorer down"))

    try:
        p._run_inference(object())
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the detector error to propagate")

    assert p._last_inference_seconds >= 0.02


def test_the_audit_context_carries_the_measurement():
    """The decision and the inference that produced it share one row."""
    p = object.__new__(DetectionPipeline)
    p.detector = _Detector(delay=0.01)
    # Both thresholds are properties over the live runtime config, so the
    # context must be built against that and not against instance attributes.
    p.runtime_config = {
        "event_scoring.notify_threshold": 4,
        "event_scoring.local_record_threshold": 2,
    }

    p._run_inference(object())
    ctx = p._build_audit_context(
        trigger_source="motion",
        candidate_label="person",
        candidate_confidence=0.8,
        yolo_confidence_threshold=0.5,
        notify_confidence=0.65,
        confirmations_required=1,
    )

    assert ctx["inference_seconds"] == p._last_inference_seconds
    assert ctx["inference_seconds"] >= 0.01
    # And the rest of the context still comes from the same place.
    assert ctx["backend"] == "local"
    assert ctx["notify_threshold"] == 4
    assert ctx["local_record_threshold"] == 2


# --- reading it back: a column nothing reports is a column nobody checks ---

from a12_system.tools.review_decisions import latency_report  # noqa: E402


def test_latency_report_splits_by_outcome(tmp_path):
    """The question the column exists for: were the misses the slow ones?"""
    db = EventDB(str(tmp_path / "e.db"))
    try:
        for seconds in (0.10, 0.12, 0.14):
            db.log_decision_audit(
                **{**_AUDIT, "decision_outcome": "recorded_and_notified"},
                inference_seconds=seconds,
            )
        for seconds in (1.80, 2.20):
            db.log_decision_audit(
                **{**_AUDIT, "decision_outcome": "no_person_candidate"},
                inference_seconds=seconds,
            )
        db.log_decision_audit(**{**_AUDIT, "decision_outcome": "no_person_candidate"})
        report = latency_report(db)
    finally:
        db.conn.close()

    assert "no_person_candidate" in report
    assert "recorded_and_notified" in report
    # p50 of the three fast rows, and of the two slow ones.
    assert "0.12" in report
    assert "2.20" in report or "1.80" in report
    # The unmeasured row must be visible as unmeasured, not averaged in as zero.
    assert "1 unmeasured" in report


def test_latency_report_says_so_when_nothing_was_measured(tmp_path):
    db = EventDB(str(tmp_path / "e.db"))
    try:
        db.log_decision_audit(**_AUDIT)
        report = latency_report(db)
    finally:
        db.conn.close()
    assert "no inference timings" in report.lower()
