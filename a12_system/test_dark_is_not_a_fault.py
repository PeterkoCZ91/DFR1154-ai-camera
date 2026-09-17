"""Darkness is not evidence of a wedged exposure loop.

Measured over the night of 2026-09-16/17 on the production camera: 1397 dark
frames at a median brightness of 4.7, 20 AEC/AGC rewrites and 13 Telegram
alerts, none of which changed anything — the episode ended at dawn, when
brightness climbed from 10.3 (06:57) to 29.9 (08:14) on its own. The last
rewrite was at 05:09, an hour before the last "recovered".

`classify_frame_health()` had already labelled every one of those frames
"dark" rather than "flat". The verdict was used for the wording of the log line
and then discarded: the ladder was entered on standard deviation alone, so a
black image took the same path as a wedged grey one. They are opposite
situations — brightness ~64 with no detail is the exposure loop holding its
target with no signal under it, and rewriting the registers is the right
remedy; brightness ~5 is no light, and no register can conjure photons.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.pipeline import classify_frame_health, route_low_detail_frame

DARK, FLAT_STD = 30.0, 1.0


def _route(brightness, std):
    fault = classify_frame_health(brightness, std, DARK, FLAT_STD)
    return route_low_detail_frame(fault, std, FLAT_STD)


def test_uniform_grey_still_reaches_the_ladder():
    """The wedge signature measured on 2026-09-15: mid-grey, no detail."""
    assert _route(64.4, 0.5) == "unwedge"


def test_a_black_featureless_frame_is_held_not_treated_as_a_wedge():
    """The exact numbers logged 1397 times overnight."""
    assert _route(4.7, 0.6) == "hold"


def test_a_dark_but_textured_frame_still_counts_as_recovery():
    """Dawn: brightness 10.3, std 6.3. Detail is health even before sunrise."""
    assert _route(10.3, 6.3) == "healthy"


def test_a_normal_frame_counts_as_recovery():
    assert _route(120.0, 25.0) == "healthy"


def test_the_boundary_belongs_to_the_ladder_not_to_darkness():
    """Just above the dark threshold with no detail is still a wedge candidate."""
    assert _route(30.0, 0.5) == "unwedge"
    assert _route(29.9, 0.5) == "hold"


# --- end to end: the routing above must actually be what process_frame uses ---

import queue  # noqa: E402
from unittest.mock import Mock  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from a12_system.flat_episode import FlatEpisodeState  # noqa: E402
from a12_system.pipeline import DetectionPipeline  # noqa: E402


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_telegram(self, message, **kwargs):
        self.sent.append(message)
        return True


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now


class _EndOfWatchdog(Exception):
    pass


def _watchdog_pipeline(tmp_path, clock):
    p = object.__new__(DetectionPipeline)
    p.log_prefix = "[test:cam]"
    p.telegram_label = ""
    p.notifier = _Notifier()
    p.shared_state = {}
    p.running = True
    p.frame_count = 0
    p.last_heartbeat = 0
    p.heartbeat_interval = 30
    p.frame_buffer = []
    p.notification_queue = queue.Queue()
    p.status_monitor = None
    p.mqtt_client = Mock()
    p.configure_frame_health_watchdog({
        "brightness_watchdog_threshold": 30,
        "brightness_watchdog_strikes": 1,
        "flat_frame_std_threshold": 1,
        "flat_frame_reconnect_strikes": 5,
        "flat_frame_notify_interval": 0,
        "flat_frame_max_unwedge_attempts": 3,
        "flat_frame_unwedge_cooldown": 0.0,
        "flat_frame_healthy_required": 3,
    })
    p.configure_stream_freeze_ladder({})
    p.configure_face_checks({})
    p.flat_state = FlatEpisodeState(str(tmp_path / "flat.json"))
    p.freeze_state = FlatEpisodeState(str(tmp_path / "freeze.json"))
    p.runtime_config = Mock()
    p.runtime_config.get.return_value = 50
    p.detector = Mock()
    p.detector.detect_motion.side_effect = _EndOfWatchdog
    return p


def _feed(p, clock, level, count, rng):
    for _ in range(count):
        # Read noise, because a real sensor's frames are never identical — an
        # identical repeat is the signature of a stopped readout instead.
        noise = rng.integers(0, 2, size=(120, 160, 1), dtype=np.uint8)
        frame = (np.full((120, 160, 1), level, dtype=np.uint8) + noise).repeat(3, axis=2)
        clock.now += 31
        with pytest.raises(_EndOfWatchdog):
            p.process_frame(frame)


def test_a_whole_night_of_black_frames_writes_nothing_and_says_nothing(tmp_path, monkeypatch):
    """The reproduction of 2026-09-16: 120 black heartbeats, an hour of them."""
    clock = _Clock()
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _watchdog_pipeline(tmp_path, clock)

    _feed(p, clock, level=4, count=120, rng=np.random.default_rng(11))

    assert p.flat_state.unwedge_count() == 0
    assert "unwedge_camera" not in p.shared_state
    assert not p.shared_state.get("reboot_camera")
    assert not p.shared_state.get("force_stream_reconnect")
    assert p.notifier.sent == []


def test_uniform_grey_still_spends_its_budget_and_warns(tmp_path, monkeypatch):
    """The guard above must not be a way of switching the ladder off."""
    clock = _Clock()
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _watchdog_pipeline(tmp_path, clock)

    _feed(p, clock, level=64, count=120, rng=np.random.default_rng(11))

    assert p.flat_state.unwedge_count() == 3
    assert p.shared_state.get("unwedge_camera") is True
    assert any("no detail" in m for m in p.notifier.sent)


def test_darkness_does_not_close_an_open_grey_episode(tmp_path, monkeypatch):
    """Nightfall over a wedged camera must not read as a recovery.

    A dark frame is no more evidence that the camera healed than that it broke,
    so it must not feed the healthy run — otherwise sunset would silently clear
    a real wedge and announce "frames are healthy again".
    """
    clock = _Clock()
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _watchdog_pipeline(tmp_path, clock)
    rng = np.random.default_rng(3)

    _feed(p, clock, level=64, count=40, rng=rng)
    assert p.flat_state.episode_active()

    _feed(p, clock, level=4, count=60, rng=rng)

    assert p.flat_state.episode_active(), "darkness closed the episode"
    assert not any("healthy again" in m for m in p.notifier.sent)


def test_dawn_closes_the_episode(tmp_path, monkeypatch):
    """Texture ends it, whatever the brightness — 06:57 read b=10.3, std=6.3."""
    clock = _Clock()
    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _watchdog_pipeline(tmp_path, clock)
    rng = np.random.default_rng(5)

    _feed(p, clock, level=64, count=40, rng=rng)
    assert p.flat_state.episode_active()

    for _ in range(10):
        clock.now += 31
        frame = rng.integers(2, 20, size=(120, 160, 3), dtype=np.uint8)
        with pytest.raises(_EndOfWatchdog):
            p.process_frame(frame)

    assert not p.flat_state.episode_active()
    assert any("healthy again" in m for m in p.notifier.sent)
