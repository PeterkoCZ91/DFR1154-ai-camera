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
    p._freeze_healthy_frames = 0
    p._flat_healthy_required = 3
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


def test_quiet_gap_does_not_prove_recovery(tmp_path, monkeypatch):
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
    assert not p.notifier.sent
    assert p.freeze_state.reboot_count() == 1
    assert p.freeze_state.episode_active()


def test_only_sustained_healthy_frames_restore_budget(tmp_path):
    p = _pipeline(tmp_path, _Clock())
    p.freeze_state.mark_active()
    p.freeze_state.record_reboot()
    for now in range(1000, 1020):
        p._note_stream_frame_health(False, now)
    assert p.freeze_state.reboot_count() == 1
    assert not p.notifier.sent
    p._note_stream_frame_health(True, 1021)
    p._note_stream_frame_health(False, 1022)
    p._note_stream_frame_health(True, 1023)
    p._note_stream_frame_health(True, 1024)
    assert p.freeze_state.reboot_count() == 1
    p._note_stream_frame_health(True, 1025)
    assert p.freeze_state.reboot_count() == 0
    assert len(p.notifier.sent) == 1


def test_uniform_night_frames_never_command_disruptive_recovery(tmp_path, monkeypatch):
    """Uniform frames may trigger an AEC/AGC rewrite (cheap, no outage) but must
    never reboot the camera or tear down the stream: darkness still looks
    exactly like a fault, and a dark night is not a hardware failure.

    The frames carry read noise, because a real sensor's do. This fixture used
    a constant fill, which is bit-identical frame to frame — since 2026-09-14
    that is the signature of a sensor that has stopped reading out, and it now
    routes to a reboot (see test_frozen_sensor.py). The claim under test is
    unchanged; only the model of darkness was wrong.
    """
    import queue
    from unittest.mock import Mock

    import numpy as np
    import pytest

    clock = _Clock()
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, clock)
    p.running = True
    p.frame_count = 0
    p.last_heartbeat = 0
    p.heartbeat_interval = 30
    p.frame_buffer = []
    p.notification_queue = queue.Queue()
    p.status_monitor = None  # no lux telemetry must not imply sensor failure
    p.mqtt_client = Mock()
    p.configure_frame_health_watchdog({
        "brightness_watchdog_threshold": 30,
        "brightness_watchdog_strikes": 1,
        "flat_frame_std_threshold": 1,
        "flat_frame_reconnect_strikes": 5,
        "flat_frame_notify_interval": 3600,
        "flat_frame_max_unwedge_attempts": 3,
        "flat_frame_unwedge_cooldown": 0.0,
        "flat_frame_healthy_required": 3,
    })
    p.flat_state = FlatEpisodeState(str(tmp_path / "flat.json"))
    p.runtime_config = Mock()
    p.runtime_config.get.return_value = 50

    class EndOfWatchdog(Exception):
        pass

    # Stop at the motion boundary, after the real heartbeat/watchdog code ran.
    p.detector = Mock()
    p.detector.detect_motion.side_effect = EndOfWatchdog
    rng = np.random.default_rng(11)
    for level in (0, 40, 65):
        for _ in range(120):
            # Uniform to within one level — still "flat" by std, but never the
            # same frame twice, which is what a live sensor always gives.
            noise = rng.integers(0, 2, size=(120, 160, 1), dtype=np.uint8)
            frame = (np.full((120, 160, 1), level, dtype=np.uint8) + noise).repeat(3, axis=2)
            clock.now += 31
            with pytest.raises(EndOfWatchdog):
                p.process_frame(frame)
            assert not p.shared_state.get("reboot_camera")
            assert not p.shared_state.get("force_stream_reconnect")
    assert p.flat_state.reboot_count() == 0
    assert not any("hardware" in m or "hung" in m for m in p.notifier.sent)
    # The non-disruptive rewrite IS allowed on this evidence, and is bounded:
    # it must stop at the budget rather than write every heartbeat all night.
    # Pinned to the exact budget, not "<= 3": that weaker form also passes when
    # the rewrite never fires at all, so it could not fail either way.
    assert p.flat_state.unwedge_count() == 3
