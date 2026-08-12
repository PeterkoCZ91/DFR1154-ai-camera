"""SQLite event logging with persistent connection and indexes."""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime


class EventDB:
    def __init__(self, db_path: str = "events.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_tables()
        logging.info(f"Database initialized: {db_path} (WAL Mode, persistent connection)")

    def _init_tables(self) -> None:
        """Create schema and indexes."""
        c = self.conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            datetime TEXT,
            type TEXT,
            label TEXT,
            value REAL,
            media_path TEXT
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS audio_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            avg_level REAL,
            peak_level REAL
        )""")

        # This table deliberately does not reference events: a decision is useful
        # for calibration even when it was rejected before an event or alert existed.
        c.execute("""CREATE TABLE IF NOT EXISTS decision_audit (
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
            decision_outcome TEXT NOT NULL
        )""")

        # Indexes for common queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decision_audit_timestamp ON decision_audit(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decision_audit_outcome ON decision_audit(decision_outcome)")

        self.conn.commit()

    def log_event(self, event_type: str, label: str, value: float = 0.0, media_path: str = None) -> None:
        """Log an event (thread-safe)."""
        with self.lock:
            try:
                timestamp = time.time()
                dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                self.conn.execute(
                    "INSERT INTO events (timestamp, datetime, type, label, value, media_path) VALUES (?, ?, ?, ?, ?, ?)",
                    (timestamp, dt, event_type, label, value, media_path),
                )
                self.conn.commit()
                logging.debug(f"Event logged: {event_type} - {label}")
            except Exception as e:
                logging.error(f"Failed to log event: {e}")

    def log_audio_stat(self, avg_level: float, peak_level: float) -> None:
        """Log audio statistics (thread-safe)."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO audio_stats (timestamp, avg_level, peak_level) VALUES (?, ?, ?)",
                    (time.time(), avg_level, peak_level),
                )
                self.conn.commit()
            except Exception as e:
                logging.error(f"Failed to log audio stats: {e}")

    def log_decision_audit(self, **audit: object) -> None:
        """Persist one YOLO/policy decision without changing event logging."""
        with self.lock:
            try:
                timestamp = time.time()
                dt = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                self.conn.execute(
                    """
                    INSERT INTO decision_audit (
                        timestamp, datetime, trigger_source, backend, candidate_label,
                        candidate_confidence, yolo_confidence_threshold,
                        notify_confidence_threshold, confirmations_required,
                        confirmation_streak, sensor_confirmed, active_sensors,
                        event_score, notify_threshold, local_record_threshold,
                        decision_outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp, dt, audit["trigger_source"], audit["backend"],
                        audit["candidate_label"], audit["candidate_confidence"],
                        audit["yolo_confidence_threshold"], audit["notify_confidence_threshold"],
                        audit["confirmations_required"], audit["confirmation_streak"],
                        None if audit["sensor_confirmed"] is None else int(bool(audit["sensor_confirmed"])),
                        json.dumps(audit["active_sensors"] or []), audit["event_score"],
                        audit["notify_threshold"], audit["local_record_threshold"],
                        audit["decision_outcome"],
                    ),
                )
                self.conn.commit()
            except Exception as e:
                logging.error(f"Failed to log decision audit: {e}")

    def prune_decision_audit(self, max_age_days: float, batch_size: int = 1000) -> int:
        """Delete expired audit rows and checkpoint WAL without blocking inference."""
        if max_age_days <= 0:
            return 0
        cutoff = time.time() - max_age_days * 86400
        with self.lock:
            try:
                cursor = self.conn.execute(
                    """
                    DELETE FROM decision_audit
                    WHERE id IN (
                        SELECT id FROM decision_audit WHERE timestamp < ?
                        ORDER BY timestamp LIMIT ?
                    )
                    """,
                    (cutoff, max(1, batch_size)),
                )
                removed = cursor.rowcount
                self.conn.commit()
                if removed:
                    self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    logging.info(
                        "Decision audit cleanup: removed %d rows older than %.1f days",
                        removed,
                        max_age_days,
                    )
                return removed
            except Exception as e:
                logging.error(f"Decision audit cleanup failed: {e}")
                return 0

    def get_recent_events(self, limit: int = 10) -> list[dict]:
        """Get recent events (thread-safe)."""
        with self.lock:
            try:
                self.conn.row_factory = sqlite3.Row
                c = self.conn.cursor()
                c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
                rows = [dict(row) for row in c.fetchall()]
                self.conn.row_factory = None
                return rows
            except Exception as e:
                logging.error(f"Failed to get events: {e}")
                return []

    def get_event_counts_since(self, since_timestamp: float) -> dict[tuple[str, str], int]:
        """Return event counts grouped by type and label since a Unix timestamp."""
        with self.lock:
            try:
                c = self.conn.cursor()
                c.execute(
                    """
                    SELECT type, label, COUNT(*)
                    FROM events
                    WHERE timestamp >= ?
                    GROUP BY type, label
                    """,
                    (since_timestamp,),
                )
                return {
                    (str(event_type), str(label)): int(count)
                    for event_type, label, count in c.fetchall()
                }
            except Exception as e:
                logging.error(f"Failed to get event counts: {e}")
                return {}

    def close(self) -> None:
        """Close the database connection."""
        with self.lock:
            if self.conn:
                self.conn.close()
                logging.info("Database connection closed")
