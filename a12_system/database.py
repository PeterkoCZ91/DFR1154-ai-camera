"""SQLite event logging with persistent connection and indexes."""

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

        # Indexes for common queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, timestamp)")

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

    def close(self) -> None:
        """Close the database connection."""
        with self.lock:
            if self.conn:
                self.conn.close()
                logging.info("Database connection closed")
