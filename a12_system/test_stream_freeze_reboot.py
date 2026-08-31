"""Tests for the stream-freeze reboot escalation.

A "Stream frozen"/"stream_ended" break already forces a reconnect by
construction (the __main__ loop redials immediately), so this ladder only
decides whether a burst of freezes close together — the send_fail_count /
last_errno=104 pattern seen in a12.log, pointing at a wedged socket/heap state
on the ESP32 itself — warrants rebooting the camera over LAN.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.flat_episode import FlatEpisodeState
from a12_system.pipeline import DetectionPipeline


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_telegram(self, message, **kwargs):
        self.sent.append(message)


class _Clock:
    """A settable fake for time.time().

    Patching `time.time` on the shared `time` module also intercepts calls
    logging.critical() makes internally for the record timestamp, so a plain
    finite iterator gets consumed faster than the test expects. A clock that
    just returns its current value until explicitly advanced sidesteps that.
    """

    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now


def _pipeline(tmp_path, clock, **overrides):
    p = object.__new__(DetectionPipeline)
    p.log_prefix = "[test:cam]"
    p.telegram_label = ""
    p.notifier = _Notifier()
    p.shared_state = {}
    p.freeze_state = FlatEpisodeState(str(tmp_path / "stream_freeze_state.json"))
    p._freeze_reboot_after = overrides.get("reboot_after", 3)
    p._freeze_max_reboots = overrides.get("max_reboots", 2)
    p._freeze_healthy_gap = overrides.get("healthy_gap", 600.0)
    p._freeze_action_cooldown = overrides.get("cooldown", 0.0)
    p._freeze_notify_interval = overrides.get("notify_interval", 0.0)
    p._freeze_consecutive_count = 0
    p._last_freeze_time = 0.0
    p._last_freeze_action = 0.0
    return p


def test_isolated_freezes_never_reboot(tmp_path, monkeypatch):
    # Each freeze lands 601s after the previous one -> always a fresh episode
    # of size 1, well under reboot_after=3.
    clock = _Clock(1000.0)
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, clock, reboot_after=3, healthy_gap=600.0)
    for _ in range(10):
        p.note_stream_freeze("frozen")
        clock.now += 601.0
    assert "reboot_camera" not in p.shared_state


def test_burst_of_freezes_reboots_camera(tmp_path, monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, clock, reboot_after=3, healthy_gap=600.0, cooldown=0.0)
    p.note_stream_freeze("frozen")
    clock.now += 1.0
    p.note_stream_freeze("frozen")
    assert "reboot_camera" not in p.shared_state
    clock.now += 1.0
    p.note_stream_freeze("frozen")
    assert p.shared_state.get("reboot_camera") is True
    assert any("rebooting" in m.lower() for m in p.notifier.sent)


def test_reboot_budget_exhausted_gives_up(tmp_path, monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, clock, reboot_after=2, max_reboots=1, healthy_gap=600.0, cooldown=0.0)
    p.note_stream_freeze("frozen")
    clock.now += 1.0
    p.note_stream_freeze("frozen")  # -> reboot (1/1 used)
    assert p.shared_state.pop("reboot_camera") is True
    clock.now += 1.0
    p.note_stream_freeze("frozen")
    clock.now += 1.0
    p.note_stream_freeze("frozen")  # -> reboots exhausted -> giveup
    assert "reboot_camera" not in p.shared_state
    assert any("manual look" in m.lower() for m in p.notifier.sent)


def test_healthy_gap_resets_episode_and_notifies_recovery(tmp_path, monkeypatch):
    clock = _Clock(1000.0)
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, clock, reboot_after=2, healthy_gap=600.0, cooldown=0.0, notify_interval=0.0)
    p.note_stream_freeze("frozen")
    clock.now += 1.0
    p.note_stream_freeze("frozen")  # -> reboot, episode marked active
    assert p.shared_state.pop("reboot_camera") is True
    p.notifier.sent.clear()
    # Next freeze arrives 999s later — past the 600s healthy gap.
    clock.now += 999.0
    p.note_stream_freeze("stream_ended")
    assert p._freeze_consecutive_count == 1
    assert any("stable again" in m.lower() for m in p.notifier.sent)
