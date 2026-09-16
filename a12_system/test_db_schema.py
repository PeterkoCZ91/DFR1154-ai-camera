"""The schema may not carry tables nothing writes.

`audio_stats` was created on every startup for months and never received a
single row — `log_audio_stat()` had no callers anywhere in the tree, and the
audio monitor it was written for never touched the database. An empty table is
not harmful by itself; what it costs is a reader's time, because a schema is
read as a statement of what the system records.
"""

import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.database import EventDB

_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.py")


def _created_tables(tmp_path) -> set[str]:
    db = EventDB(str(tmp_path / "schema.db"))
    try:
        rows = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        db.conn.close()
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


def _writer_methods() -> dict[str, str]:
    """table -> name of the EventDB method whose body inserts into it."""
    source = open(_SOURCE, encoding="utf-8").read()
    current = ""
    found: dict[str, str] = {}
    for line in source.splitlines():
        m = re.match(r"\s*def\s+([a-z_0-9]+)\s*\(", line)
        if m:
            current = m.group(1)
        t = re.search(r"INSERT\s+INTO\s+([a-z_]+)", line, re.IGNORECASE)
        if t:
            found[t.group(1)] = current
    return found


def _has_caller_outside_database(method: str) -> bool:
    """Is this writer reachable from anywhere but its own definition?

    An INSERT statement is not evidence that a table is used — `audio_stats`
    had one for months with no caller anywhere. Tests do not count either: a
    table kept alive only by its own unit test is still dead in production.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    pattern = re.compile(rf"\b{re.escape(method)}\s*\(")
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            if not name.endswith(".py") or name.startswith("test_") or name == "database.py":
                continue
            path = os.path.join(dirpath, name)
            if pattern.search(open(path, encoding="utf-8").read()):
                return True
    return False


def test_every_created_table_has_a_writer_someone_calls(tmp_path):
    created = _created_tables(tmp_path)
    writers = _writer_methods()

    dead = sorted(
        t for t in created
        if t not in writers or not _has_caller_outside_database(writers[t])
    )
    assert dead == [], f"schema creates tables nothing in production writes: {dead}"


def test_every_insert_targets_a_created_table(tmp_path):
    created = _created_tables(tmp_path)
    written = set(_writer_methods())
    assert written - created == set(), (
        f"code inserts into tables the schema never creates: {sorted(written - created)}"
    )


def test_schema_still_creates_the_tables_the_system_relies_on(tmp_path):
    # Guards the test above from passing by deleting everything.
    assert {"events", "decision_audit", "decision_labels"} <= _created_tables(tmp_path)


def test_sqlite_accepts_the_schema(tmp_path):
    db = EventDB(str(tmp_path / "ok.db"))
    try:
        db.log_event("motion", "test", 1.0)
        assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    finally:
        db.conn.close()
    assert sqlite3.connect(str(tmp_path / "ok.db")) is not None
