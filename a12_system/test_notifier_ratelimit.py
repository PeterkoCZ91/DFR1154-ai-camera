"""Tests for Telegram cooldown and 429 handling.

Two regressions are covered:

* The cooldown check was an unguarded read-modify-write while send_telegram() is
  called from five threads, so two events could both pass it and double-notify.
* A 429 slept up to 60 s *in the calling thread*. When that thread was the status
  monitor, its watchdog stopped running; when it was the notification worker, the
  queue backed up until events were dropped.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.notifier import Notifier

telebot = pytest.importorskip("telebot", reason="telebot needed for the 429 path")


class _FakeApiException(telebot.apihelper.ApiTelegramException):
    """A real ApiTelegramException, built without an HTTP round trip.

    The class matters: notifier.py catches ApiTelegramException specifically, and a
    look-alike would fall through to the generic handler and never open the
    rate-limit window.
    """

    def __init__(self, error_code, retry_after=None):
        Exception.__init__(self, f"api error {error_code}")
        self.error_code = error_code
        self.function_name = "sendMessage"
        self.description = f"api error {error_code}"
        self.result_json = (
            {"parameters": {"retry_after": retry_after}} if retry_after else {}
        )


class _FakeBot:
    def __init__(self, raises=None):
        self.raises = raises
        self.sent = []
        self._lock = threading.Lock()

    def send_message(self, chat_id, message):
        if self.raises:
            raise self.raises
        with self._lock:
            self.sent.append(message)


def _notifier(bot, cooldown=60):
    n = Notifier({"telegram": {"enabled": False}, "telegram_cooldown_seconds": cooldown})
    n.bot = bot
    n.chat_id = "123"
    return n


def test_cooldown_blocks_second_message():
    bot = _FakeBot()
    n = _notifier(bot)
    assert n.send_telegram("first") is True
    assert n.send_telegram("second") is False
    assert bot.sent == ["first"]


def test_bypass_cooldown_still_sends():
    bot = _FakeBot()
    n = _notifier(bot)
    assert n.send_telegram("first") is True
    assert n.send_telegram("urgent", bypass_cooldown=True) is True
    assert bot.sent == ["first", "urgent"]


def test_bypass_does_not_consume_the_cooldown_slot():
    # An urgent message must not reset the cooldown clock for routine ones.
    bot = _FakeBot()
    n = _notifier(bot)
    n.send_telegram("routine")
    before = n.last_telegram_time
    n.send_telegram("urgent", bypass_cooldown=True)
    assert n.last_telegram_time == before


def test_concurrent_sends_only_one_wins():
    bot = _FakeBot()
    n = _notifier(bot)
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        results.append(n.send_telegram(f"msg{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 1, results
    assert len(bot.sent) == 1


def test_429_does_not_block_the_caller():
    bot = _FakeBot(raises=_FakeApiException(429, retry_after=30))
    n = _notifier(bot)

    started = time.monotonic()
    assert n.send_telegram("boom", bypass_cooldown=True) is False
    elapsed = time.monotonic() - started

    # The old implementation slept retry_after seconds right here.
    assert elapsed < 1.0, f"send blocked for {elapsed:.1f}s"
    assert n._rate_limited_until > time.time()


def test_sends_are_suppressed_during_the_429_window():
    bot = _FakeBot(raises=_FakeApiException(429, retry_after=30))
    n = _notifier(bot)
    n.send_telegram("boom", bypass_cooldown=True)

    # Even bypass_cooldown must not hammer a rate-limited API.
    bot.raises = None
    assert n.send_telegram("after", bypass_cooldown=True) is False
    assert bot.sent == []


def test_window_expires():
    bot = _FakeBot(raises=_FakeApiException(429, retry_after=30))
    n = _notifier(bot)
    n.send_telegram("boom", bypass_cooldown=True)

    n._rate_limited_until = time.time() - 1   # pretend the window passed
    bot.raises = None
    assert n.send_telegram("after", bypass_cooldown=True) is True
    assert bot.sent == ["after"]


def test_non_429_api_error_does_not_start_a_window():
    bot = _FakeBot(raises=_FakeApiException(400))
    n = _notifier(bot)
    assert n.send_telegram("bad request", bypass_cooldown=True) is False
    assert n._rate_limited_until == 0.0


def test_mute_drops_routine_messages_even_without_cooldown():
    # PIR clips go out with bypass_cooldown=True by default; mute must still
    # silence them, or the "mute 15m" button does nothing.
    bot = _FakeBot()
    n = _notifier(bot)
    n.mute_until(time.time() + 60)
    assert n.send_telegram("visitor clip", bypass_cooldown=True) is False
    assert n.send_telegram("motion") is False
    assert bot.sent == []


def test_critical_message_goes_out_while_muted():
    bot = _FakeBot()
    n = _notifier(bot)
    n.mute_until(time.time() + 60)
    assert n.send_telegram("Latch opening confirmed.", critical=True) is True
    assert n.send_telegram("SABOTAGE!", bypass_cooldown=True, critical=True) is True
    assert bot.sent == ["Latch opening confirmed.", "SABOTAGE!"]


def test_critical_message_skips_cooldown():
    bot = _FakeBot()
    n = _notifier(bot)
    assert n.send_telegram("first") is True
    assert n.send_telegram("door opened", critical=True) is True
    assert bot.sent == ["first", "door opened"]


def test_critical_message_still_respects_rate_limit_while_muted():
    n = _notifier(_FakeBot(raises=_FakeApiException(429, retry_after=30)))
    n.mute_until(time.time() + 60)
    assert n.send_telegram("boom", critical=True) is False
    assert n._rate_limited_until > time.time()
    # Mute must neither hide the backoff nor let a critical send hammer the API.
    n.bot = _FakeBot()
    assert n.send_telegram("after", critical=True) is False
    assert n.bot.sent == []
    n._rate_limited_until = 0.0
    assert n.send_telegram("after", critical=True) is True
    assert n.bot.sent == ["after"]
