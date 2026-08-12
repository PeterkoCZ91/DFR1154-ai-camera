import json
import os
import subprocess
import time

from a12_system.database import EventDB


def _audit_args():
    return {
        "trigger_source": "esp32_motion",
        "backend": "fallback",
        "candidate_label": "person",
        "candidate_confidence": 0.72,
        "yolo_confidence_threshold": 0.55,
        "notify_confidence_threshold": 0.45,
        "confirmations_required": 1,
        "confirmation_streak": 1,
        "sensor_confirmed": True,
        "active_sensors": ["esp32_motion"],
        "event_score": 95,
        "notify_threshold": 70,
        "local_record_threshold": 45,
        "decision_outcome": "recorded_and_notified",
    }


def test_decision_audit_keeps_existing_events_and_full_decision_context(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    db.log_event("detection", "person", 0.72)
    db.log_decision_audit(**_audit_args())

    row = db.conn.execute("SELECT * FROM decision_audit").fetchone()
    columns = [item[1] for item in db.conn.execute("PRAGMA table_info(decision_audit)")]
    assert {"trigger_source", "backend", "candidate_confidence", "event_score", "decision_outcome"} <= set(columns)
    assert row[4:7] == ("fallback", "person", 0.72)
    assert json.loads(row[12]) == ["esp32_motion"]
    assert db.get_recent_events()[0]["type"] == "detection"
    db.close()


def test_decision_audit_prunes_expired_rows_and_keeps_recent_rows(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    db.log_decision_audit(**_audit_args())
    db.log_decision_audit(**_audit_args())
    db.conn.execute(
        "UPDATE decision_audit SET timestamp = ? WHERE id = 1",
        (time.time() - 31 * 86400,),
    )
    db.conn.commit()

    assert db.prune_decision_audit(30) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0] == 1
    db.close()



def test_calibrate_aggregates_local_audit(tmp_path):
    db = EventDB(str(tmp_path / "events.db"))
    db.log_decision_audit(**_audit_args())
    db.close()

    env = os.environ | {"A12_DATA_DIR": str(tmp_path)}
    result = subprocess.run(
        ["bash", "a12_system/tools/a12", "calibrate", "1"],
        capture_output=True,
        check=False,
        cwd=os.getcwd(),
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "local audit only" in result.stdout
    assert "recorded_and_notified" in result.stdout
