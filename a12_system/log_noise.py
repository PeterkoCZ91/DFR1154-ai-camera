"""Throttles for two log sources that flood the log during long, harmless episodes.

Logging only: nothing here feeds back into recovery or alert behaviour.
"""

import logging
import threading
import time
from typing import Callable, Optional

_NETWORK_MARKERS = (
    "HTTPSConnectionPool",
    "HTTPConnectionPool",
    "ConnectionError",
    "Max retries exceeded",
    "Read timed out",
    "ReadTimeout",
    "ConnectTimeout",
    "Connection aborted",
    "Connection reset",
    "Name or service not known",
    "Temporary failure in name resolution",
    "Failed to resolve",
    "Network is unreachable",
)


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


class EpisodeLogGate:
    """First event of an episode passes, then at most one per `interval`."""

    def __init__(self, interval: float = 900.0, clock: Callable[[], float] = time.time):
        self.interval = interval
        self._clock = clock
        self._start: Optional[float] = None
        self._last_logged = 0.0
        self._count = 0

    def note(self) -> Optional[tuple]:
        """Register one event. Returns (count, elapsed_s) if it should be logged, else None."""
        now = self._clock()
        self._count += 1
        if self._start is None:
            self._start = self._last_logged = now
            return (self._count, 0.0)
        if now - self._last_logged >= self.interval:
            self._last_logged = now
            return (self._count, now - self._start)
        return None

    def recover(self) -> Optional[tuple]:
        """End the episode. Returns (count, elapsed_s) if one was open, else None."""
        if self._start is None:
            return None
        result = (self._count, self._clock() - self._start)
        self._start = None
        self._count = 0
        return result

    @staticmethod
    def describe(elapsed: float) -> str:
        return _fmt_duration(elapsed)


class TelebotNetworkFilter(logging.Filter):
    """Collapses repeated network-outage errors from the 'TeleBot' logger.

    The first failure of a run is logged in full. Repeats (and the traceback
    record that follows each) are dropped. When no failure has been seen for
    `grace` seconds, one WARNING line with count and duration is emitted.
    Non-network errors always pass.
    """

    def __init__(self, grace: float = 60.0, clock: Callable[[], float] = time.time,
                 out_logger: Optional[logging.Logger] = None):
        super().__init__()
        self.grace = grace
        self._clock = clock
        self._out = out_logger or logging.getLogger("TeleBot")
        self._lock = threading.Lock()
        self._first: Optional[float] = None
        self._last = 0.0
        self._count = 0
        self._fate: Optional[bool] = None
        self._timer: Optional[threading.Timer] = None

    @staticmethod
    def _is_network(text: str) -> bool:
        return any(m in text for m in _NETWORK_MARKERS)

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_log_noise_summary", False):
            return True
        try:
            text = record.getMessage()
        except Exception:
            return True
        is_traceback = text.startswith("Exception traceback:")
        if not self._is_network(text) and not (is_traceback and self._fate is not None):
            self._fate = None
            return True
        with self._lock:
            if is_traceback:
                # Same fate as the exception record it belongs to.
                return bool(self._fate)
            self._last = self._clock()
            self._count += 1
            first = self._first is None
            if first:
                self._first = self._last
            self._fate = first
            self._arm_timer()
            return first

    def _arm_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.grace, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def flush(self) -> None:
        """Close the outage run and log its summary (also called by the timer)."""
        with self._lock:
            if self._first is None:
                return
            count, duration = self._count, self._last - self._first
            self._first = None
            self._count = 0
            self._fate = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._out.warning(
            "Telegram polling recovered after %d network failures over %s",
            count, _fmt_duration(duration), extra={"_log_noise_summary": True},
        )
