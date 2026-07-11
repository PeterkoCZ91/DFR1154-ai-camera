"""Persistent flat-episode state.

The self-heal ladder (reconnects + process self-restart) cycles every ~10 minutes
while the camera itself is wedged, and each process restart resets in-memory
notification state — the 2026-07-10 overnight episode produced 287 Telegram
messages. This state file (in /data, survives container restarts) rate-limits
episode notifications and lets the pipeline send a single "recovered" message.
"""

import json
import logging
import os

log = logging.getLogger(__name__)


class FlatEpisodeState:
    """Tiny JSON-file-backed state: per-key notify timestamps + episode flag."""

    def __init__(self, state_path: str):
        self.state_path = state_path
        # In-memory mirror of the latest writes. If /data becomes unwritable
        # (disk full — realistic during a long recording backlog), rate-limiting
        # must NOT silently degrade to "notify every time": the mirror keeps
        # this process honest even when _save() fails.
        self._mem: dict = {}

    def _load(self) -> dict:
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            disk = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            disk = {}
        # Mirror wins: it holds everything this process wrote, including writes
        # that never reached the disk.
        return {**disk, **self._mem}

    def _save(self, data: dict) -> None:
        self._mem = dict(data)
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("flat-episode state save failed: %s", e)

    def should_notify(self, key: str, now: float, min_interval: float) -> bool:
        """True at most once per ``min_interval`` seconds per key, across restarts."""
        data = self._load()
        last = None
        if ("notified_" + key) in data:
            try:
                last = float(data["notified_" + key])
            except (TypeError, ValueError):
                last = None
        if last is not None and now - last < min_interval:
            return False
        data["notified_" + key] = now
        self._save(data)
        return True

    def mark_active(self) -> None:
        data = self._load()
        if not data.get("episode_active"):
            data["episode_active"] = True
            self._save(data)

    def episode_active(self) -> bool:
        return bool(self._load().get("episode_active"))

    def record_reboot(self) -> int:
        """Count a commanded camera reboot; persisted so an A12 restart cannot
        re-arm the budget mid-episode (a crash-looping A12 must not reboot the
        camera all night). Returns the new total for this episode."""
        data = self._load()
        n = self._as_int(data.get("reboots")) + 1
        data["reboots"] = n
        self._save(data)
        return n

    def reboot_count(self) -> int:
        return self._as_int(self._load().get("reboots"))

    def set_gaveup(self) -> bool:
        """Latch the give-up terminal state. True exactly once per episode
        (persisted — an A12 restart must not re-send the give-up alert)."""
        data = self._load()
        if data.get("gaveup"):
            return False
        data["gaveup"] = True
        self._save(data)
        return True

    def clear(self) -> bool:
        """End the episode. True exactly once per episode (caller may notify recovery).

        A real recovery re-arms the ladder for the NEXT genuine hang: the reboot
        budget, the give-up latch and the onset-alert timestamp are dropped so a
        fresh episode acts and alerts immediately. Other notify timestamps are
        preserved so a flapping stream cannot re-fire messages on every
        healthy/flat transition.
        """
        data = self._load()
        if not data.get("episode_active"):
            return False
        data["episode_active"] = False
        data.pop("reboots", None)
        data.pop("gaveup", None)
        data.pop("notified_flat_alert", None)
        # _save replaces the mirror wholesale, so the dropped keys are forgotten
        # in memory as well — but only if the DISK copy also loses them. Rewrite
        # unconditionally; on write failure the mirror alone still drops them.
        self._save(data)
        return True

    @staticmethod
    def _as_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0


def prune_files(folder: str, prefix: str, keep: int) -> None:
    """Delete all but the ``keep`` newest ``prefix*`` files in ``folder``."""
    try:
        matches = [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.startswith(prefix)
        ]
        matches.sort(key=os.path.getmtime, reverse=True)
        for path in matches[keep:]:
            try:
                os.unlink(path)
            except OSError:
                pass
    except OSError as e:
        log.warning("prune of %s* failed: %s", prefix, e)
