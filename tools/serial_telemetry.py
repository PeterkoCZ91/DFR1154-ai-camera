#!/usr/bin/env python3
"""
ESP32-S3 Camera Serial Telemetry Logger v1.0

Captures structured telemetry from firmware serial output into SQLite.
Pipeline: SerialReader → LineParser → EventRouter → SQLiteWriter

Usage:
  python3 tools/serial_telemetry.py                          # basic logging
  python3 tools/serial_telemetry.py --port /dev/ttyUSB0      # different port
  python3 tools/serial_telemetry.py --db tools/camera.db     # custom DB path
  python3 tools/serial_telemetry.py --no-raw-log             # skip raw_log table
  python3 tools/serial_telemetry.py --stats                  # show stats from DB
  python3 tools/serial_telemetry.py --export-csv telemetry   # export tables to CSV

Dependencies: pyserial>=3.5 (sqlite3 is stdlib)
"""

import argparse
import csv
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

# --- Config ---
SERIAL_PORT = "/dev/ttyACM0"
BAUD_RATE = 115200
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera.db")
BATCH_SIZE = 50  # flush every N lines

# --- Terminal colors ---
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ============================================================================
# SQLite Database
# ============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    port TEXT,
    lines_total INTEGER DEFAULT 0,
    lines_parsed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    heap_free INTEGER,
    psram_free INTEGER,
    rssi INTEGER,
    uptime_s INTEGER,
    lux REAL,
    lux_raw INTEGER,
    profile TEXT,
    stack_free INTEGER,
    frame_count INTEGER,
    avg_brightness INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS motion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    blocks_changed INTEGER,
    blocks_total INTEGER,
    motion_pct REAL,
    threshold_pct REAL,
    block_thresh INTEGER,
    gain INTEGER,
    brightness INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS person_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    confidence REAL,
    detections INTEGER,
    inference_ms INTEGER,
    miss_count INTEGER,
    miss_max INTEGER,
    cooldown_remaining INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS profile_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    profile TEXT NOT NULL,
    lux REAL,
    denoise INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS stream_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    stream_name TEXT,
    clients_current INTEGER,
    clients_max INTEGER,
    frames INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS telegram_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    bytes_size INTEGER,
    filename TEXT,
    command TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS recording_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    filename TEXT,
    width INTEGER,
    height INTEGER,
    fps INTEGER,
    frames INTEGER,
    audio_chunks INTEGER,
    sd_usage_pct INTEGER,
    sd_threshold_pct INTEGER,
    sd_size_mb INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    event_type TEXT NOT NULL,
    detail TEXT,
    value INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS raw_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id INTEGER,
    line TEXT NOT NULL,
    parsed INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);
CREATE INDEX IF NOT EXISTS idx_motion_ts ON motion_events(ts);
CREATE INDEX IF NOT EXISTS idx_person_ts ON person_events(ts);
CREATE INDEX IF NOT EXISTS idx_stream_ts ON stream_events(ts);
CREATE INDEX IF NOT EXISTS idx_telegram_ts ON telegram_events(ts);
CREATE INDEX IF NOT EXISTS idx_recording_ts ON recording_events(ts);
CREATE INDEX IF NOT EXISTS idx_system_ts ON system_events(ts);
CREATE INDEX IF NOT EXISTS idx_raw_log_ts ON raw_log(ts);
CREATE INDEX IF NOT EXISTS idx_raw_log_parsed ON raw_log(parsed);
"""


class TelemetryDB:
    """SQLite backend with WAL mode and batch inserts."""

    def __init__(self, db_path, store_raw=True):
        self.db_path = db_path
        self.store_raw = store_raw
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

        self.session_id = None
        self._batch = []  # list of (sql, params) tuples
        self._raw_batch = []
        self._lines_total = 0
        self._lines_parsed = 0

    def start_session(self, port):
        """Create a new logging session."""
        now = datetime.now().isoformat()
        cur = self.conn.execute(
            "INSERT INTO sessions (started_at, port) VALUES (?, ?)",
            (now, port)
        )
        self.session_id = cur.lastrowid
        self.conn.commit()
        return self.session_id

    def stop_session(self):
        """Finalize current session."""
        if self.session_id is None:
            return
        self.flush()
        now = datetime.now().isoformat()
        self.conn.execute(
            "UPDATE sessions SET stopped_at=?, lines_total=?, lines_parsed=? WHERE id=?",
            (now, self._lines_total, self._lines_parsed, self.session_id)
        )
        self.conn.commit()

    def queue(self, sql, params):
        """Queue an INSERT for batch execution."""
        self._batch.append((sql, params))
        self._lines_parsed += 1
        if len(self._batch) + len(self._raw_batch) >= BATCH_SIZE:
            self.flush()

    def queue_raw(self, ts, line, parsed):
        """Queue a raw log line."""
        if not self.store_raw:
            return
        self._raw_batch.append((ts, self.session_id, line, 1 if parsed else 0))
        self._lines_total += 1
        if len(self._batch) + len(self._raw_batch) >= BATCH_SIZE:
            self.flush()

    def count_raw(self):
        """Count raw line without storing it."""
        self._lines_total += 1

    def flush(self):
        """Execute all queued inserts."""
        if not self._batch and not self._raw_batch:
            return
        try:
            cur = self.conn.cursor()
            for sql, params in self._batch:
                cur.execute(sql, params)
            if self._raw_batch:
                cur.executemany(
                    "INSERT INTO raw_log (ts, session_id, line, parsed) VALUES (?, ?, ?, ?)",
                    self._raw_batch
                )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"{RED}[DB] Error: {e}{RESET}", file=sys.stderr)
        self._batch.clear()
        self._raw_batch.clear()

    def close(self):
        """Close the database connection."""
        self.stop_session()
        self.conn.close()


# ============================================================================
# Line Parser — compiled regex patterns
# ============================================================================

# Strip emoji prefix: matches common firmware emoji sequences at start of line
RE_EMOJI_PREFIX = re.compile(
    r'^[\U0001F300-\U0001F9FF\u2600-\u26FF\u2700-\u27BF\u2139\uFE0F\u200D\u00A9\u00AE'
    r'\u203C\u2049\u2122\u2328\u23CF\u23E9-\u23F3\u23F8-\u23FA'
    r'\u24C2\u25AA-\u25FE\u2602-\u2B55\u3030\u303D\u3297\u3299'
    r'\U0001FA70-\U0001FAFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF'
    r'\U0001F1E0-\U0001F1FF]+\s*'
)

# --- Periodic metrics ---
RE_LTR308 = re.compile(r'LTR-308: raw=(\d+) lux=([\d.]+)')
RE_HEAP = re.compile(
    r'Heap: (\d+) bytes free, PSRAM: (\d+) bytes free'
    r'(?:, RSSI: (-?\d+) dBm, uptime: (\d+)s)?'
)
RE_STACK = re.compile(r'CaptureTask stack free: (\d+) bytes \(frame: (\d+)\)')

# --- Motion ---
RE_MOTION_DETECTED = re.compile(
    r'Motion DETECTED: blocks=(\d+)/(\d+) \(([\d.]+)%\), '
    r'thresh=([\d.]+)%, blockThresh=(\d+), gain=(\d+), bright=(\d+)'
)
RE_MOTION_CLEARED = re.compile(r'Motion cleared: blocks=(\d+) \(([\d.]+)%\)')
RE_MOTION_SUPPRESSED = re.compile(r'Motion suppressed \(night mode: brightness=(\d+)\)')

# --- Person detection ---
RE_PD_DETECTED = re.compile(
    r'PD: Person detected! confidence=([\d.]+)%, detections=(\d+), inference=(\d+)ms'
)
RE_PD_NO_PERSON_MISS = re.compile(r'PD: No person \(miss (\d+)/(\d+)\)')
RE_PD_NO_PERSON_INF = re.compile(r'PD: No person detected \(inference=(\d+)ms\)')
RE_PD_CLEARED = re.compile(r'PD: Presence cleared')
RE_PD_COOLDOWN = re.compile(r'PD: person confirmed, cooldown active \((\d+)s remaining\)')
RE_PD_RECHECK = re.compile(r'PD: presence re-check \(miss (\d+)/(\d+)\)')
RE_PD_STILL_PRESENT = re.compile(r'PD: still present \(confidence=([\d.]+)%, recheck\)')
RE_PD_TELE_PHOTO = re.compile(r'PD: Telegram photo sent')
RE_PD_MOTION_TRIGGER = re.compile(r'PD: woke from motion trigger')
RE_PD_MOTION_NOTIFY = re.compile(r'PD: motion.*notifying AI task')

# --- Profiles ---
RE_PROFILE_DAY = re.compile(r'Profile: DAY \(([\d.]+) lux\)')
RE_PROFILE_DUSK = re.compile(r'Profile: DUSK \(([\d.]+) lux\).*?denoise=(\d+)')
RE_PROFILE_NIGHT = re.compile(r'Profile: NIGHT \(([\d.]+) lux\)')

# --- Streams ---
RE_STREAM_CONNECT = re.compile(r'(\S[\w ]*?) connected \(clients: (\d+)/(\d+)\)')
RE_STREAM_DISCONNECT = re.compile(
    r'(\S[\w ]*?) disconnected \(clients: (\d+)/(\d+), frames: (\d+)\)'
)
RE_STREAM_REJECTED = re.compile(r'(\S[\w ]*?): Max clients reached')

# --- Telegram ---
RE_TELE_PHOTO = re.compile(r'Telegram photo sent \((\d+) bytes\)')
RE_TELE_DOC = re.compile(r'Telegram document sent: (.+?) \((\d+)KB\)')
RE_TELE_CMD = re.compile(r'Telegram command: (.+)')
RE_TELE_PHOTO_FAIL = re.compile(r'Telegram photo API error: (.+)')
RE_TELE_DOC_FAIL = re.compile(r'Telegram document API error: (.+)')

# --- Recording ---
RE_AVI_START = re.compile(r'AVI recording started: (\S+) \((\d+)x(\d+) @(\d+)fps\)')
RE_AVI_STOP = re.compile(r'AVI recording stopped: (\d+) frames, (\d+) audio')
RE_AVI_AUTOSTOP = re.compile(r'AVI auto-stop: (\d+)KB, (\d+)s')
RE_SD_USAGE = re.compile(r'SD usage (\d+)% >= (\d+)%')
RE_SD_MOUNTED = re.compile(r'SD Card Mounted: (\d+) MB')

# --- System ---
RE_EMA_TRAINED = re.compile(r'EMA background model trained \((\d+) frames, avg brightness=(\d+)\)')
RE_WDT_COUNT = re.compile(r'WDT reset count: (\d+)')
RE_RESTARTS = re.compile(r'Total restarts: (\d+)')
RE_LOW_HEAP = re.compile(r'LOW HEAP WARNING: (\d+)')
RE_CRIT_HEAP = re.compile(r'CRITICAL LOW HEAP: (\d+)')
RE_CAM_FAIL = re.compile(r'Camera capture failed')
RE_HEARTBEAT_LOST = re.compile(r'Camera heartbeat lost')
RE_IR_LED = re.compile(r'IR LED: (ON|OFF)')
RE_WIFI_DISCONNECT = re.compile(r'WiFi disconnected, attempt (\d+)/(\d+)')
RE_WIFI_CONNECTED = re.compile(r'WiFi connected.*?IP: (\S+)')
RE_CAMERA_READY = re.compile(r'Camera Ready|Camera initialized')
RE_BOOT_VERSION = re.compile(r'Firmware v([\d.]+)')
RE_ESP_ERROR = re.compile(r'E \((\d+)\) (\w+): (.+)')


class LineParser:
    """Parses serial lines against compiled regex patterns.

    Returns (table, event_type, data_dict) or None if no match.
    """

    def parse(self, line):
        """Parse a single line (emoji prefix already stripped by caller)."""

        # --- Periodic metrics (return special "metric" table) ---
        m = RE_LTR308.search(line)
        if m:
            return ('metric_lux', 'lux', {
                'lux_raw': int(m.group(1)),
                'lux': float(m.group(2)),
            })

        m = RE_HEAP.search(line)
        if m:
            data = {
                'heap_free': int(m.group(1)),
                'psram_free': int(m.group(2)),
            }
            if m.group(3):
                data['rssi'] = int(m.group(3))
            if m.group(4):
                data['uptime_s'] = int(m.group(4))
            return ('metric_heap', 'heap', data)

        m = RE_STACK.search(line)
        if m:
            return ('metric_stack', 'stack', {
                'stack_free': int(m.group(1)),
                'frame_count': int(m.group(2)),
            })

        # --- Motion ---
        m = RE_MOTION_DETECTED.search(line)
        if m:
            return ('motion_events', 'detected', {
                'blocks_changed': int(m.group(1)),
                'blocks_total': int(m.group(2)),
                'motion_pct': float(m.group(3)),
                'threshold_pct': float(m.group(4)),
                'block_thresh': int(m.group(5)),
                'gain': int(m.group(6)),
                'brightness': int(m.group(7)),
            })

        m = RE_MOTION_CLEARED.search(line)
        if m:
            return ('motion_events', 'cleared', {
                'blocks_changed': int(m.group(1)),
                'motion_pct': float(m.group(2)),
            })

        m = RE_MOTION_SUPPRESSED.search(line)
        if m:
            return ('motion_events', 'suppressed', {
                'brightness': int(m.group(1)),
            })

        # --- Person detection ---
        m = RE_PD_DETECTED.search(line)
        if m:
            return ('person_events', 'detected', {
                'confidence': float(m.group(1)),
                'detections': int(m.group(2)),
                'inference_ms': int(m.group(3)),
            })

        m = RE_PD_NO_PERSON_MISS.search(line)
        if m:
            return ('person_events', 'no_person_miss', {
                'miss_count': int(m.group(1)),
                'miss_max': int(m.group(2)),
            })

        m = RE_PD_NO_PERSON_INF.search(line)
        if m:
            return ('person_events', 'no_person', {
                'inference_ms': int(m.group(1)),
            })

        if RE_PD_CLEARED.search(line):
            return ('person_events', 'cleared', {})

        m = RE_PD_COOLDOWN.search(line)
        if m:
            return ('person_events', 'cooldown', {
                'cooldown_remaining': int(m.group(1)),
            })

        m = RE_PD_RECHECK.search(line)
        if m:
            return ('person_events', 'recheck', {
                'miss_count': int(m.group(1)),
                'miss_max': int(m.group(2)),
            })

        m = RE_PD_STILL_PRESENT.search(line)
        if m:
            return ('person_events', 'still_present', {
                'confidence': float(m.group(1)),
            })

        if RE_PD_TELE_PHOTO.search(line):
            return ('telegram_events', 'pd_photo_sent', {})

        if RE_PD_MOTION_TRIGGER.search(line):
            return ('person_events', 'motion_trigger', {})

        if RE_PD_MOTION_NOTIFY.search(line):
            return ('person_events', 'motion_notify', {})

        # --- Profiles ---
        m = RE_PROFILE_DAY.search(line)
        if m:
            return ('profile_changes', 'DAY', {
                'lux': float(m.group(1)),
            })

        m = RE_PROFILE_DUSK.search(line)
        if m:
            return ('profile_changes', 'DUSK', {
                'lux': float(m.group(1)),
                'denoise': int(m.group(2)),
            })

        m = RE_PROFILE_NIGHT.search(line)
        if m:
            return ('profile_changes', 'NIGHT', {
                'lux': float(m.group(1)),
            })

        # --- Streams (check disconnect before connect — disconnect regex is more specific) ---
        m = RE_STREAM_DISCONNECT.search(line)
        if m:
            return ('stream_events', 'disconnected', {
                'stream_name': m.group(1),
                'clients_current': int(m.group(2)),
                'clients_max': int(m.group(3)),
                'frames': int(m.group(4)),
            })

        m = RE_STREAM_CONNECT.search(line)
        if m:
            return ('stream_events', 'connected', {
                'stream_name': m.group(1),
                'clients_current': int(m.group(2)),
                'clients_max': int(m.group(3)),
            })

        m = RE_STREAM_REJECTED.search(line)
        if m:
            return ('stream_events', 'rejected', {
                'stream_name': m.group(1),
            })

        # --- Telegram ---
        m = RE_TELE_PHOTO.search(line)
        if m:
            return ('telegram_events', 'photo_sent', {
                'bytes_size': int(m.group(1)),
            })

        m = RE_TELE_DOC.search(line)
        if m:
            return ('telegram_events', 'doc_sent', {
                'filename': m.group(1),
                'bytes_size': int(m.group(2)) * 1024,
            })

        m = RE_TELE_CMD.search(line)
        if m:
            return ('telegram_events', 'command', {
                'command': m.group(1).strip(),
            })

        m = RE_TELE_PHOTO_FAIL.search(line)
        if m:
            return ('telegram_events', 'photo_failed', {
                'command': m.group(1).strip(),
            })

        m = RE_TELE_DOC_FAIL.search(line)
        if m:
            return ('telegram_events', 'doc_failed', {
                'command': m.group(1).strip(),
            })

        # --- Recording ---
        m = RE_AVI_START.search(line)
        if m:
            return ('recording_events', 'avi_started', {
                'filename': m.group(1),
                'width': int(m.group(2)),
                'height': int(m.group(3)),
                'fps': int(m.group(4)),
            })

        m = RE_AVI_STOP.search(line)
        if m:
            return ('recording_events', 'avi_stopped', {
                'frames': int(m.group(1)),
                'audio_chunks': int(m.group(2)),
            })

        m = RE_AVI_AUTOSTOP.search(line)
        if m:
            return ('recording_events', 'avi_autostop', {
                'bytes_size': int(m.group(1)) * 1024,
                'frames': int(m.group(2)),  # actually duration_s, store in frames col
            })

        m = RE_SD_USAGE.search(line)
        if m:
            return ('recording_events', 'sd_rotation', {
                'sd_usage_pct': int(m.group(1)),
                'sd_threshold_pct': int(m.group(2)),
            })

        m = RE_SD_MOUNTED.search(line)
        if m:
            return ('recording_events', 'sd_mounted', {
                'sd_size_mb': int(m.group(1)),
            })

        # --- System events ---
        m = RE_EMA_TRAINED.search(line)
        if m:
            return ('system_events', 'ema_trained', {
                'detail': f"frames={m.group(1)}, brightness={m.group(2)}",
                'value': int(m.group(2)),
            })

        m = RE_WDT_COUNT.search(line)
        if m:
            return ('system_events', 'wdt_count', {
                'value': int(m.group(1)),
            })

        m = RE_RESTARTS.search(line)
        if m:
            return ('system_events', 'total_restarts', {
                'value': int(m.group(1)),
            })

        m = RE_LOW_HEAP.search(line)
        if m:
            return ('system_events', 'low_heap', {
                'value': int(m.group(1)),
            })

        m = RE_CRIT_HEAP.search(line)
        if m:
            return ('system_events', 'critical_heap', {
                'value': int(m.group(1)),
            })

        if RE_CAM_FAIL.search(line):
            return ('system_events', 'camera_fail', {})

        if RE_HEARTBEAT_LOST.search(line):
            return ('system_events', 'heartbeat_lost', {})

        m = RE_IR_LED.search(line)
        if m:
            return ('system_events', 'ir_led', {
                'detail': m.group(1),
            })

        m = RE_WIFI_DISCONNECT.search(line)
        if m:
            return ('system_events', 'wifi_disconnect', {
                'detail': f"attempt {m.group(1)}/{m.group(2)}",
                'value': int(m.group(1)),
            })

        m = RE_WIFI_CONNECTED.search(line)
        if m:
            return ('system_events', 'wifi_connected', {
                'detail': m.group(1),
            })

        if RE_CAMERA_READY.search(line):
            return ('system_events', 'boot', {})

        m = RE_BOOT_VERSION.search(line)
        if m:
            return ('system_events', 'firmware_version', {
                'detail': m.group(1),
            })

        # ESP-IDF error logs: E (12345) component: message
        m = RE_ESP_ERROR.search(line)
        if m:
            return ('system_events', 'esp_error', {
                'detail': f"{m.group(2)}: {m.group(3)}",
                'value': int(m.group(1)),
            })

        return None


# ============================================================================
# Metric Aggregator — consolidates periodic metrics into single rows
# ============================================================================

class MetricAggregator:
    """Accumulates periodic metric readings and flushes consolidated rows.

    Heap (~30s), lux (~5-10s), and stack (~33s) arrive at different intervals.
    We aggregate them into a single metrics row, flushing when heap data arrives
    (as it's the most periodic).
    """

    def __init__(self):
        self.current = {}
        self.last_flush_ts = None

    def update(self, metric_type, data):
        """Update current metrics with new data. Returns True if should flush."""
        self.current.update(data)
        # Flush on heap update (the ~30s heartbeat) or if we have all three
        return metric_type == 'heap'

    def harvest(self):
        """Return current metrics and reset."""
        data = self.current.copy()
        self.current.clear()
        return data


# ============================================================================
# Event Router — dispatches parsed events to DB
# ============================================================================

class EventRouter:
    """Routes parsed events to the correct DB table."""

    def __init__(self, db, verbose=False):
        self.db = db
        self.parser = LineParser()
        self.aggregator = MetricAggregator()
        self.verbose = verbose

        # Console display patterns for important events
        self._highlight = {
            'motion_events': (YELLOW + BOLD, 'MOTION'),
            'person_events': (GREEN + BOLD, 'PERSON'),
            'telegram_events': (CYAN, 'TELEGRAM'),
            'recording_events': (CYAN, 'RECORD'),
            'profile_changes': (GREEN, 'PROFILE'),
            'system_events': (RED, 'SYSTEM'),
            'stream_events': (DIM, 'STREAM'),
        }

    def process_line(self, ts, line):
        """Parse a line and route to the appropriate table."""
        # Strip emoji prefix for parsing
        clean = RE_EMOJI_PREFIX.sub('', line)

        result = self.parser.parse(clean)
        parsed = result is not None

        # Queue raw log
        self.db.queue_raw(ts, line, parsed)
        if not parsed:
            self.db.count_raw()  # count even if not stored
            return

        table, event_type, data = result

        # Handle metric aggregation
        if table.startswith('metric_'):
            should_flush = self.aggregator.update(table.split('_')[1], data)
            if should_flush:
                self._flush_metrics(ts)
            return

        # Route to event tables
        self._insert_event(ts, table, event_type, data)

        # Console output for important events
        if self.verbose:
            self._console_output(ts, table, event_type, data)

    def _flush_metrics(self, ts):
        """Flush aggregated metrics to DB."""
        data = self.aggregator.harvest()
        if not data:
            return

        self.db.queue(
            "INSERT INTO metrics (ts, session_id, heap_free, psram_free, rssi, "
            "uptime_s, lux, lux_raw, stack_free, frame_count, avg_brightness) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, self.db.session_id,
                data.get('heap_free'), data.get('psram_free'),
                data.get('rssi'), data.get('uptime_s'),
                data.get('lux'), data.get('lux_raw'),
                data.get('stack_free'), data.get('frame_count'),
                data.get('avg_brightness'),
            )
        )

    def _insert_event(self, ts, table, event_type, data):
        """Insert an event into its table."""
        sid = self.db.session_id

        if table == 'motion_events':
            self.db.queue(
                "INSERT INTO motion_events (ts, session_id, event_type, blocks_changed, "
                "blocks_total, motion_pct, threshold_pct, block_thresh, gain, brightness) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, sid, event_type,
                 data.get('blocks_changed'), data.get('blocks_total'),
                 data.get('motion_pct'), data.get('threshold_pct'),
                 data.get('block_thresh'), data.get('gain'),
                 data.get('brightness'))
            )

        elif table == 'person_events':
            self.db.queue(
                "INSERT INTO person_events (ts, session_id, event_type, confidence, "
                "detections, inference_ms, miss_count, miss_max, cooldown_remaining) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, sid, event_type,
                 data.get('confidence'), data.get('detections'),
                 data.get('inference_ms'), data.get('miss_count'),
                 data.get('miss_max'), data.get('cooldown_remaining'))
            )

        elif table == 'profile_changes':
            self.db.queue(
                "INSERT INTO profile_changes (ts, session_id, profile, lux, denoise) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, sid, event_type, data.get('lux'), data.get('denoise'))
            )
            # Also update the aggregator so the next metrics row knows the profile
            self.aggregator.current['profile'] = event_type

        elif table == 'stream_events':
            self.db.queue(
                "INSERT INTO stream_events (ts, session_id, event_type, stream_name, "
                "clients_current, clients_max, frames) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, sid, event_type,
                 data.get('stream_name'), data.get('clients_current'),
                 data.get('clients_max'), data.get('frames'))
            )

        elif table == 'telegram_events':
            self.db.queue(
                "INSERT INTO telegram_events (ts, session_id, event_type, bytes_size, "
                "filename, command) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, sid, event_type,
                 data.get('bytes_size'), data.get('filename'),
                 data.get('command'))
            )

        elif table == 'recording_events':
            self.db.queue(
                "INSERT INTO recording_events (ts, session_id, event_type, filename, "
                "width, height, fps, frames, audio_chunks, sd_usage_pct, "
                "sd_threshold_pct, sd_size_mb) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, sid, event_type,
                 data.get('filename'), data.get('width'),
                 data.get('height'), data.get('fps'),
                 data.get('frames'), data.get('audio_chunks'),
                 data.get('sd_usage_pct'), data.get('sd_threshold_pct'),
                 data.get('sd_size_mb'))
            )

        elif table == 'system_events':
            self.db.queue(
                "INSERT INTO system_events (ts, session_id, event_type, detail, value) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, sid, event_type, data.get('detail'), data.get('value'))
            )

    def _console_output(self, ts, table, event_type, data):
        """Print important events to console."""
        info = self._highlight.get(table)
        if not info:
            return
        color, label = info
        short_ts = ts[11:23] if len(ts) > 23 else ts  # HH:MM:SS.mmm
        parts = [f"{event_type}"]
        # Add key data points
        for key in ('lux', 'heap_free', 'confidence', 'blocks_changed',
                    'stream_name', 'filename', 'command', 'value', 'detail'):
            if key in data and data[key] is not None:
                parts.append(f"{key}={data[key]}")
        detail = " ".join(parts)
        print(f"{DIM}[{short_ts}]{RESET} {color}[{label}]{RESET} {detail}")


# ============================================================================
# Stats & Export
# ============================================================================

def show_stats(db_path):
    """Display statistics from the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"\n{BOLD}=== Telemetry Database: {db_path} ==={RESET}\n")

    # Sessions
    rows = conn.execute(
        "SELECT id, started_at, stopped_at, port, lines_total, lines_parsed FROM sessions "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    print(f"{BOLD}Sessions (last 5):{RESET}")
    for r in rows:
        duration = ""
        if r['stopped_at']:
            try:
                t0 = datetime.fromisoformat(r['started_at'])
                t1 = datetime.fromisoformat(r['stopped_at'])
                mins = (t1 - t0).total_seconds() / 60
                duration = f" ({mins:.0f}min)"
            except ValueError:
                pass
        parsed_pct = 0
        if r['lines_total'] and r['lines_total'] > 0:
            parsed_pct = (r['lines_parsed'] or 0) * 100 / r['lines_total']
        print(f"  #{r['id']}: {r['started_at']}{duration} — "
              f"{r['lines_total'] or 0} lines ({parsed_pct:.0f}% parsed), port={r['port']}")

    # Table counts
    tables = [
        'metrics', 'motion_events', 'person_events', 'profile_changes',
        'stream_events', 'telegram_events', 'recording_events', 'system_events', 'raw_log'
    ]
    print(f"\n{BOLD}Table row counts:{RESET}")
    for t in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:25s} {count:>8,}")
        except sqlite3.OperationalError:
            print(f"  {t:25s} {'(missing)':>8}")

    # Metrics summary
    row = conn.execute(
        "SELECT MIN(heap_free) as min_heap, AVG(heap_free) as avg_heap, "
        "MAX(heap_free) as max_heap, MIN(lux) as min_lux, AVG(lux) as avg_lux, "
        "MAX(lux) as max_lux, COUNT(*) as cnt FROM metrics"
    ).fetchone()
    if row and row['cnt'] > 0:
        print(f"\n{BOLD}Metrics summary ({row['cnt']} readings):{RESET}")
        if row['min_heap'] is not None:
            print(f"  Heap: {row['min_heap']:,} — {row['max_heap']:,} "
                  f"(avg {row['avg_heap']:,.0f}) bytes")
        if row['min_lux'] is not None:
            print(f"  Lux:  {row['min_lux']:.1f} — {row['max_lux']:.1f} "
                  f"(avg {row['avg_lux']:.1f})")

    # Event counts by type
    event_tables = [
        ('motion_events', 'Motion'),
        ('person_events', 'Person detection'),
        ('telegram_events', 'Telegram'),
        ('system_events', 'System'),
    ]
    for t, label in event_tables:
        try:
            rows = conn.execute(
                f"SELECT event_type, COUNT(*) as cnt FROM {t} GROUP BY event_type ORDER BY cnt DESC"
            ).fetchall()
            if rows:
                print(f"\n{BOLD}{label} events:{RESET}")
                for r in rows:
                    print(f"  {r['event_type']:25s} {r['cnt']:>6,}")
        except sqlite3.OperationalError:
            pass

    # Unparsed lines (top 10)
    try:
        rows = conn.execute(
            "SELECT line, COUNT(*) as cnt FROM raw_log WHERE parsed=0 "
            "GROUP BY line ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        if rows:
            total_unparsed = conn.execute(
                "SELECT COUNT(*) FROM raw_log WHERE parsed=0"
            ).fetchone()[0]
            print(f"\n{BOLD}Top unparsed lines ({total_unparsed} total):{RESET}")
            for r in rows:
                short = r['line'][:80] + ('...' if len(r['line']) > 80 else '')
                print(f"  {r['cnt']:>4}x  {short}")
    except sqlite3.OperationalError:
        pass

    conn.close()
    print()


def export_csv(db_path, prefix):
    """Export all tables to CSV files."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [
        'sessions', 'metrics', 'motion_events', 'person_events',
        'profile_changes', 'stream_events', 'telegram_events',
        'recording_events', 'system_events',
    ]

    for table in tables:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue

        filename = f"{prefix}_{table}.csv"
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(tuple(r))
        print(f"  {filename} ({len(rows)} rows)")

    conn.close()


# ============================================================================
# Serial Reader + Main Loop
# ============================================================================

def serial_loop(port, baud, db, router):
    """Main serial reading loop with reconnection."""
    while True:
        try:
            ser = serial.Serial(port, baud, timeout=1)
            print(f"{GREEN}[SERIAL] Connected to {port} @ {baud}{RESET}")

            while True:
                try:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode('utf-8', errors='replace').rstrip()
                    # Handle IR LED \\n bug: line may contain literal \n
                    line = line.replace('\\n', '')
                    if not line:
                        continue

                    ts = datetime.now().isoformat(timespec='milliseconds')
                    router.process_line(ts, line)

                except serial.SerialException:
                    print(f"{RED}[SERIAL] Disconnected{RESET}")
                    break
                except UnicodeDecodeError:
                    continue

        except serial.SerialException as e:
            print(f"{YELLOW}[SERIAL] Waiting for {port}... ({e}){RESET}")
            time.sleep(2)
        except KeyboardInterrupt:
            return


def main():
    parser = argparse.ArgumentParser(
        description="ESP32 Camera Serial Telemetry Logger → SQLite"
    )
    parser.add_argument('--port', default=SERIAL_PORT,
                        help=f"Serial port (default: {SERIAL_PORT})")
    parser.add_argument('--baud', type=int, default=BAUD_RATE,
                        help=f"Baud rate (default: {BAUD_RATE})")
    parser.add_argument('--db', default=DB_PATH,
                        help=f"SQLite database path (default: {DB_PATH})")
    parser.add_argument('--no-raw-log', action='store_true',
                        help="Don't store raw lines in raw_log table")
    parser.add_argument('--verbose', '-v', action='store_true',
                        help="Print parsed events to console")
    parser.add_argument('--stats', action='store_true',
                        help="Show statistics from existing DB and exit")
    parser.add_argument('--export-csv', metavar='PREFIX',
                        help="Export all tables to CSV files with given prefix and exit")
    args = parser.parse_args()

    # Stats mode — no serial needed
    if args.stats:
        if not os.path.exists(args.db):
            print(f"{RED}Database not found: {args.db}{RESET}")
            sys.exit(1)
        show_stats(args.db)
        return

    # Export mode
    if args.export_csv:
        if not os.path.exists(args.db):
            print(f"{RED}Database not found: {args.db}{RESET}")
            sys.exit(1)
        print(f"Exporting to {args.export_csv}_*.csv ...")
        export_csv(args.db, args.export_csv)
        return

    # Live logging mode — need pyserial
    if not HAS_SERIAL:
        print(f"{RED}pyserial is required: pip install pyserial{RESET}")
        sys.exit(1)

    # Initialize
    db = TelemetryDB(args.db, store_raw=not args.no_raw_log)
    session_id = db.start_session(args.port)
    router = EventRouter(db, verbose=args.verbose)

    print(f"{BOLD}ESP32 Serial Telemetry Logger v1.0{RESET}")
    print(f"Port: {args.port} @ {args.baud}")
    print(f"DB:   {args.db} (session #{session_id})")
    print(f"Raw log: {'OFF' if args.no_raw_log else 'ON'}")
    print(f"Press Ctrl+C to stop\n")

    # Graceful shutdown
    def shutdown(signum=None, frame=None):
        print(f"\n{BOLD}Stopping...{RESET}")
        db.close()
        print(f"Session #{session_id} saved. Lines: {db._lines_total} "
              f"({db._lines_parsed} parsed)")
        print(f"DB: {args.db}")
        print(f"Run: python3 {__file__} --stats --db {args.db}")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        serial_loop(args.port, args.baud, db, router)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()


if __name__ == '__main__':
    main()
