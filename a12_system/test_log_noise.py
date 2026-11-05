import logging

import pytest

from a12_system.log_noise import EpisodeLogGate, TelebotNetworkFilter


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_gate_first_logged_repeats_suppressed_then_logged_after_window():
    c = Clock()
    g = EpisodeLogGate(interval=900, clock=c)
    assert g.note() == (1, 0.0)
    for _ in range(10):
        c.t += 30
        assert g.note() is None
    c.t += 900
    count, elapsed = g.note()
    assert count == 12 and elapsed == pytest.approx(1200)


def test_gate_recovery_line_once_and_resets():
    c = Clock()
    g = EpisodeLogGate(clock=c)
    assert g.recover() is None
    g.note()
    c.t += 60
    g.note()
    assert g.recover() == (2, 60)
    assert g.recover() is None
    assert g.note() == (1, 0.0)


def _setup(caplog):
    lg = logging.getLogger("TeleBot")
    f = TelebotNetworkFilter(grace=3600, clock=Clock())
    lg.addFilter(f)
    caplog.set_level(logging.DEBUG, logger="TeleBot")
    return lg, f


NET = "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded"


def test_telebot_network_outage_collapsed_and_summarised(caplog):
    lg, f = _setup(caplog)
    try:
        for _ in range(3):
            lg.error("Infinity polling exception: %s", NET)
            lg.error("Exception traceback:\nTraceback ... %s", NET)
        msgs = [r.getMessage() for r in caplog.records]
        assert len(msgs) == 2  # first exception + its traceback only
        assert msgs[0].startswith("Infinity polling exception")
        assert msgs[1].startswith("Exception traceback")
        f.flush()
        last = caplog.records[-1]
        assert last.levelno == logging.WARNING and "recovered after 3" in last.getMessage()
        # next outage is a new run: logged in full again
        lg.error("Infinity polling exception: %s", NET)
        assert caplog.records[-1].getMessage().startswith("Infinity polling exception")
    finally:
        lg.removeFilter(f)
        f.flush()


def test_telebot_non_network_error_still_logged(caplog):
    lg, f = _setup(caplog)
    try:
        lg.error("Infinity polling exception: %s", "KeyError: 'x'")
        lg.error("Exception traceback:\nKeyError: 'x'")
        lg.error("Infinity polling exception: %s", "KeyError: 'x'")
        assert len(caplog.records) == 3
    finally:
        lg.removeFilter(f)
