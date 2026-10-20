"""Openings the lock made on someone else's say-so, read from its activity log.

A phone opens the door over Bluetooth straight to the lock: neither HA nor A12
sends anything. Unlatching an unlocked lock ends in the same `unlocked` state,
so the HA lock entity often never shows it. The lock's own activity log does:
Nuki Hub publishes it (needs the lock PIN and "Publish authorization log") as a
JSON list of entries such as

    {"authorizationName": "Phone", "type": "LockAction", "action": "Unlatch",
     "timeYear": 2026, "timeMonth": 10, "timeDay": 20,
     "timeHour": 18, "timeMinute": 30, "timeSecond": 55, ...}

with the time in UTC. Face unlock uses the latest such opening to stay quiet
while the resident walks in through a door their phone already opened.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

OPENING_ACTIONS = frozenset({"unlatch", "unlock", "locknagounlatch", "lockngounlatch"})


class NukiActivity:
    def __init__(self, own_names: Iterable[str] = ("NukiHub",)):
        # Openings A12 asked for go through Nuki Hub and carry its name; those
        # are A12's own and must not count as "someone else opened".
        self.own_names = frozenset(str(n).strip().lower() for n in own_names if str(n).strip())
        self._lock = threading.Lock()
        self._last: Optional[Tuple[float, str, str]] = None  # (epoch, who, action)
        # When the last log of any content arrived (our clock): lets a caller
        # that asked Nuki Hub for a fresh one see that it came.
        self.received_at = 0.0

    @staticmethod
    def _entry_time(entry: dict) -> Optional[float]:
        try:
            return datetime(
                int(entry["timeYear"]), int(entry["timeMonth"]), int(entry["timeDay"]),
                int(entry["timeHour"]), int(entry["timeMinute"]), int(entry["timeSecond"]),
                tzinfo=timezone.utc,
            ).timestamp()
        except (KeyError, TypeError, ValueError):
            return None

    def observe(self, payload: str, received_at: Optional[float] = None) -> None:
        """Take one published log (a JSON list, or a single entry)."""
        self.received_at = time.time() if received_at is None else received_at
        try:
            entries = json.loads(payload)
        except (TypeError, ValueError):
            return
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "LockAction":
                continue
            action = str(entry.get("action", "")).replace(" ", "").lower()
            who = str(entry.get("authorizationName", "")).strip()
            if action not in OPENING_ACTIONS or who.lower() in self.own_names:
                continue
            if str(entry.get("completionStatus", "success")).lower() != "success":
                continue
            at = self._entry_time(entry)
            if at is None:
                continue
            with self._lock:
                if self._last is None or at > self._last[0]:
                    self._last = (at, who or "?", entry.get("action", ""))
                    logging.info(
                        f"Nuki log: {self._last[2]} by '{self._last[1]}' at "
                        f"{datetime.fromtimestamp(at).strftime('%H:%M:%S')}"
                    )

    def recent_opening(self, now: float, within: float) -> Optional[Tuple[float, str]]:
        """(time, who) of the latest outside opening no older than `within`
        seconds, else None. A lock clock a little ahead of ours still counts."""
        with self._lock:
            last = self._last
        if last is None or within <= 0:
            return None
        if -60.0 <= now - last[0] <= within:
            return last[0], last[1]
        return None
