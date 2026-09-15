"""The daily summary may only be marked sent if it was sent.

`_check_daily_summary` persisted `_last_daily_date` regardless of what
`send_telegram` returned, and `Notifier.send_telegram` returns False for a whole
class of realistic cases — most relevantly inside its 429 rate-limit window. One
429 at 08:00 lost that day's summary permanently, with the state file on disk
asserting it had been delivered.
"""

import os
import sys
from datetime import date
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.status_monitor import StatusMonitor


def _monitor(tmp_path, sent_ok: bool):
    m = StatusMonitor.__new__(StatusMonitor)
    m.stats = Mock()
    m.stats.get_summary.return_value = {"session": {"uptime_formatted": "1h"}}
    m.db = None
    m.runtime_config = Mock()
    m.runtime_config.get.return_value = False   # face recognition off
    m.notifier = Mock()
    m.notifier.send_telegram.return_value = sent_ok
    m.log_prefix = "[test:cam]"
    m.telegram_label = ""
    m._last_daily_date = None
    m._daily_summary_state_path = str(tmp_path / "daily.txt")
    return m


def test_a_delivered_summary_is_marked_sent(tmp_path):
    m = _monitor(tmp_path, sent_ok=True)
    assert m._send_daily_summary() is True


def test_a_rejected_summary_is_not_marked_sent(tmp_path):
    """Returning False is what lets the caller try again on the next tick."""
    m = _monitor(tmp_path, sent_ok=False)
    assert m._send_daily_summary() is False


def test_a_failed_send_leaves_the_day_open_for_a_retry(tmp_path, monkeypatch):
    import a12_system.status_monitor as sm

    class _Now:
        hour = 9

    monkeypatch.setattr(sm, "datetime", Mock(now=lambda: _Now()))
    monkeypatch.setattr(sm, "date", Mock(today=lambda: date(2026, 9, 15)))

    m = _monitor(tmp_path, sent_ok=False)
    m._check_daily_summary()
    assert m._last_daily_date is None, "a lost summary was recorded as delivered"
    assert not os.path.exists(m._daily_summary_state_path)

    # The rate-limit window passes and the next tick gets it out.
    m.notifier.send_telegram.return_value = True
    m._check_daily_summary()
    assert m._last_daily_date == date(2026, 9, 15)


def test_a_delivered_summary_is_not_sent_twice(tmp_path, monkeypatch):
    import a12_system.status_monitor as sm

    class _Now:
        hour = 9

    monkeypatch.setattr(sm, "datetime", Mock(now=lambda: _Now()))
    monkeypatch.setattr(sm, "date", Mock(today=lambda: date(2026, 9, 15)))

    m = _monitor(tmp_path, sent_ok=True)
    m._check_daily_summary()
    m._check_daily_summary()
    assert m.notifier.send_telegram.call_count == 1
