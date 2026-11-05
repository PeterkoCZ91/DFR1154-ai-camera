"""Daily process-memory sample: logged once a day, one warning above the limit."""

import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.database import EventDB
from a12_system.messages import translate_outgoing
from a12_system.status_monitor import StatusMonitor


def _monitor(tmp_path, limit=800.0):
    sent = []
    m = StatusMonitor.__new__(StatusMonitor)
    m.db = EventDB(str(tmp_path / "events.db"))
    m.runtime_config = SimpleNamespace(
        get=lambda key, default=None: limit if key == "memory_warn_mib" else default
    )
    m.notifier = SimpleNamespace(
        send_telegram=lambda msg, **_kw: sent.append(msg) or True,
        language=lambda: "en",
    )
    m._last_memory_date = None
    m._last_memory_mib = None
    return m, sent


def _mem(monkeypatch, mib):
    monkeypatch.setattr(StatusMonitor, "read_process_memory_mib", staticmethod(lambda: mib))


def _rows(m):
    return m.db.conn.execute(
        "SELECT label, value FROM events WHERE type='health' AND label='memory'"
    ).fetchall()


def test_below_threshold_logs_but_does_not_warn(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path)
    _mem(monkeypatch, 799.0)
    m._check_memory()
    assert sent == []
    assert [tuple(r) for r in _rows(m)] == [("memory", 799.0)]


def test_above_threshold_warns_once_per_day(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path)
    _mem(monkeypatch, 900.0)
    m._check_memory()
    m._check_memory()
    assert len(sent) == 1 and "900" in sent[0] and "800" in sent[0]
    assert len(_rows(m)) == 1


def test_next_day_samples_again(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path)
    _mem(monkeypatch, 900.0)
    m._check_memory()
    m._last_memory_date = date.today() - timedelta(days=1)
    m._check_memory()
    assert len(sent) == 2 and len(_rows(m)) == 2


def test_zero_limit_disables_warning(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path, limit=0.0)
    _mem(monkeypatch, 5000.0)
    m._check_memory()
    assert sent == [] and len(_rows(m)) == 1


def test_unreadable_memory_is_silent(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path)
    _mem(monkeypatch, None)
    m._check_memory()
    assert sent == [] and _rows(m) == []


def test_czech_warning(tmp_path, monkeypatch):
    m, sent = _monitor(tmp_path)
    _mem(monkeypatch, 900.0)
    m._check_memory()
    rendered = translate_outgoing(sent[0], "cz")
    assert rendered != sent[0] and "900" in rendered and "800" in rendered


def test_reader_returns_positive_number_here():
    assert StatusMonitor.read_process_memory_mib() > 0
