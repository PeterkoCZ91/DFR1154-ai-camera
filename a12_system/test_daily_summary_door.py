"""The daily summary carries one line on the door.

Openings, misses and openings from outside A12 are counted from the events
table; the lock's answer time comes from the `unlock` rows' value. Rows logged
before the time was stored hold a flat 1.0 and must not pull the median down.
"""

import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.database import EventDB
from a12_system.messages import CZECH
from a12_system.status_monitor import StatusMonitor


def _monitor(db, language="en"):
    sent = []
    m = StatusMonitor.__new__(StatusMonitor)
    m.stats = SimpleNamespace(get_summary=lambda: {"session": {"uptime_formatted": "1h"}})
    m.db = db
    m.runtime_config = SimpleNamespace(get=lambda *_a: False)
    m.notifier = SimpleNamespace(
        send_telegram=lambda msg, **_kw: sent.append(msg) or True,
        language=lambda: language,
    )
    m.log_prefix = "[test:cam]"
    m.telegram_label = ""
    return m, sent


def _db(tmp_path):
    return EventDB(str(tmp_path / "events.db"))


def test_door_line_counts_and_answer_time(tmp_path):
    db = _db(tmp_path)
    for seconds in (2.3, 1.4, 7.3):
        db.log_event("unlock", "alice", seconds)
    db.log_event("unlock", "bob", 1.0)          # legacy row, no time stored
    db.log_event("unlock_failed", "alice", 0.0)
    db.log_event("unlock_external", "Home Assistant", 1.0)
    m, sent = _monitor(db)
    assert m._send_daily_summary() is True
    line = next(row for row in sent[0].splitlines() if row.startswith("Door"))
    assert line == (
        "Door (24h): 4 opened / 1 not confirmed / 1 from outside A12 "
        "(lock answered in 2.3 s median, 7.3 s slowest)"
    )


def test_old_rows_fall_outside_the_day(tmp_path):
    db = _db(tmp_path)
    db.log_event("unlock", "alice", 3.0)
    db.conn.execute("UPDATE events SET timestamp = ?", (time.time() - 2 * 86400,))
    db.conn.commit()
    m, sent = _monitor(db)
    m._send_daily_summary()
    assert "Door" not in sent[0]


def test_no_door_line_without_door_events(tmp_path):
    m, sent = _monitor(_db(tmp_path))
    m._send_daily_summary()
    assert not any(row.startswith(("Door", CZECH["Door (24h): {} opened / {} not confirmed / {} from outside A12"][:5])) for row in sent[0].splitlines())


def test_only_legacy_rows_give_counts_without_a_time(tmp_path):
    db = _db(tmp_path)
    db.log_event("unlock", "alice", 1.0)
    m, sent = _monitor(db)
    m._send_daily_summary()
    assert "Door (24h): 1 opened / 0 not confirmed / 0 from outside A12" in sent[0]
    assert "median" not in sent[0]


def test_door_line_in_czech(tmp_path):
    db = _db(tmp_path)
    db.log_event("unlock", "alice", 2.0)
    m, sent = _monitor(db, language="cz")
    m._send_daily_summary()
    expected = (
        CZECH["Door (24h): {} opened / {} not confirmed / {} from outside A12"].format(1, 0, 0)
        + " "
        + CZECH["(lock answered in {} s median, {} s slowest)"].format("2.0", "2.0")
    )
    assert expected in sent[0]
    assert "Door" not in sent[0]


def test_a_db_without_the_values_query_still_reports_counts():
    db = SimpleNamespace(get_event_counts_since=lambda _s: {("unlock", "alice"): 2})
    m, sent = _monitor(db)
    m._send_daily_summary()
    assert "Door (24h): 2 opened / 0 not confirmed / 0 from outside A12" in sent[0]
