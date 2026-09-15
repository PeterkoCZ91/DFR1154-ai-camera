"""Tests for telling a dark scene apart from a sensor that stopped reading out.

Both stream perfectly decodable JPEGs with near-zero standard deviation, so the
existing statistics cannot separate them, and on 2026-09-14 the pipeline spent
its whole AEC/AGC budget and then raised CRITICAL on a camera that was merely
looking at an unlit hallway.

The signal that does separate them is whether consecutive frames differ at all.
Measured that evening on this camera, downscaled to the 160x120 the heartbeat
already computes:

    state                     mean|d|   max|d|
    hung sensor                0.0000     0.00   <- every frame byte-identical
    dark, AGC off              0.0314     1.00
    dark, AGC on               0.1283     3.00
    dark, just after a reboot  1.7257     7.00
    lit hallway                1.3755     9.00

Silicon always has read noise, so a live sensor never repeats a frame exactly;
a hung one repeats it forever. The test is therefore equality, not a threshold —
the closest live case still sits a whole quantisation step away from zero.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.flat_episode import FlatEpisodeState
from a12_system.pipeline import DetectionPipeline, frames_are_identical


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_telegram(self, message, **kwargs):
        self.sent.append(message)


# --- the discriminator -----------------------------------------------------


def _frame(fill=40):
    return np.full((120, 160), fill, dtype=np.uint8)


def test_repeated_frame_is_identical():
    assert frames_are_identical(_frame(), _frame()) is True


def test_one_pixel_differing_by_one_level_is_not_identical():
    # The narrowest live margin measured: a dark hallway with AGC off still
    # moved a single pixel by one level between frames.
    changed = _frame()
    changed[0][0] = 41
    assert frames_are_identical(_frame(), changed) is False


def test_without_a_previous_frame_nothing_is_established():
    # The first heartbeat of a process has nothing to compare against, and
    # "unknown" must not read as "frozen" — that would reboot the camera on
    # every A12 start.
    assert frames_are_identical(None, _frame()) is False


def test_frames_of_different_shapes_are_not_identical():
    # A resolution change mid-episode is a real event (frame_size writes reboot
    # the camera); comparing across it must not raise or claim a freeze.
    assert frames_are_identical(_frame(), np.full((60, 80), 40, dtype=np.uint8)) is False


# --- the pipeline remembers the previous frame -----------------------------


def _pipeline(tmp_path, **overrides):
    p = object.__new__(DetectionPipeline)
    p.log_prefix = "[test:cam]"
    p.telegram_label = ""
    p.notifier = _Notifier()
    p.shared_state = {}
    p.flat_state = FlatEpisodeState(str(tmp_path / "flat_episode_state.json"))
    # Real production wiring, so a knob the config layer forgets cannot pass.
    p.configure_frame_health_watchdog({
        "flat_frame_reconnect_strikes": overrides.get("strikes", 5),
        "flat_frame_max_unwedge_attempts": overrides.get("max_attempts", 3),
        "flat_frame_unwedge_cooldown": overrides.get("cooldown", 0.0),
        "flat_frame_notify_interval": overrides.get("notify_interval", 0.0),
        "flat_frame_healthy_required": overrides.get("healthy_required", 3),
        "frozen_frame_strikes": overrides.get("frozen_strikes", 3),
        "frozen_frame_max_reboots": overrides.get("max_reboots", 2),
        "frozen_frame_reboot_cooldown": overrides.get("reboot_cooldown", 0.0),
    })
    # Production __init__ calls both; a harness that wires only one drifts
    # from it and passes while process_frame raises.
    p.configure_face_checks({})
    return p


def _spend_the_exposure_rewrite(p, when=1000.0, limit=40):
    """Drive the ladder to where the frozen path is allowed to act at all.

    Identical frames alone are ambiguous — a uniformly clipped frame encodes to
    identical JPEG bytes with the sensor reading out fine (measured on
    production 2026-09-15) — so the cheap exposure rewrite is spent first and
    the frozen run only counts repeats measured after it.
    """
    for _ in range(limit):
        if p.flat_state.unwedge_count() > 0:
            break
        p._note_flat_frame(when, frozen=True)
    else:
        raise AssertionError("the exposure rewrite was never attempted")
    p.shared_state.pop("unwedge_camera", None)
    assert "reboot_camera" not in p.shared_state, (
        "no reboot may be ordered before the cheap remedy has been tried"
    )


def test_first_frame_is_never_frozen(tmp_path):
    p = _pipeline(tmp_path)
    assert p._frame_is_frozen(_frame()) is False


def test_repeat_of_the_stored_frame_is_frozen(tmp_path):
    p = _pipeline(tmp_path)
    p._frame_is_frozen(_frame())
    assert p._frame_is_frozen(_frame()) is True


def test_a_changed_frame_replaces_the_stored_one(tmp_path):
    # Without storing the newest frame the comparison would drift back to a
    # stale baseline and call a live sensor frozen.
    p = _pipeline(tmp_path)
    p._frame_is_frozen(_frame(40))
    moved = _frame(40)
    moved[5][5] = 41
    assert p._frame_is_frozen(moved) is False
    assert p._frame_is_frozen(moved) is True


def test_frozen_frame_knobs_have_working_defaults():
    p = object.__new__(DetectionPipeline)
    p.configure_frame_health_watchdog({})
    assert p._frozen_strikes > 0
    assert p._frozen_max_reboots > 0


def test_frozen_frame_knobs_reach_the_ladder():
    p = object.__new__(DetectionPipeline)
    p.configure_frame_health_watchdog(
        {"frozen_frame_strikes": 9, "frozen_frame_max_reboots": 4}
    )
    assert p._frozen_strikes == 9
    assert p._frozen_max_reboots == 4


# --- what each diagnosis does ----------------------------------------------


def test_sustained_frozen_frames_reboot_the_camera(tmp_path):
    # A reboot is what actually restored readout on 2026-09-14; three AEC/AGC
    # rewrites did not.
    p = _pipeline(tmp_path, frozen_strikes=3, max_attempts=1)
    _spend_the_exposure_rewrite(p)
    for _ in range(3):
        p._note_flat_frame(1000.0, frozen=True)
    assert p.shared_state["reboot_camera"] is True


def test_the_reboot_request_also_tears_the_live_stream_down(tmp_path):
    """Measured on production 2026-09-15: the budget was spent and the camera
    never rebooted.

    __main__ only drains `reboot_camera` after `process_stream()` returns, and
    a hung sensor keeps serving perfectly valid MJPEG — uniform frames decode
    fine — so the stream never ends and the request is never acted on. The
    other ladder is safe because it only ever runs after a teardown already
    happened; this one runs on a live connection and has to cause one.
    """
    p = _pipeline(tmp_path, frozen_strikes=3, max_attempts=1)
    _spend_the_exposure_rewrite(p)
    for _ in range(3):
        p._note_flat_frame(1000.0, frozen=True)
    assert p.shared_state["reboot_camera"] is True
    assert p.shared_state.get("force_stream_reconnect") is True, (
        "reboot_camera alone is never consumed while the stream stays up"
    )


def test_nothing_tears_the_stream_down_without_an_escalation(tmp_path):
    """A forced reconnect costs a real outage, so it may only ride an escalation."""
    p = _pipeline(tmp_path, frozen_strikes=3)
    for _ in range(2):
        p._note_flat_frame(1000.0, frozen=True)
    assert "force_stream_reconnect" not in p.shared_state


def test_a_brief_frozen_run_reboots_nothing(tmp_path):
    p = _pipeline(tmp_path, frozen_strikes=3)
    for _ in range(2):
        p._note_flat_frame(1000.0, frozen=True)
    assert "reboot_camera" not in p.shared_state


def test_the_exposure_rewrite_comes_before_any_reboot(tmp_path):
    """Reversed on 2026-09-15. Identical frames were treated as proof of a
    stopped readout and jumped straight to a reboot; on production that was a
    camera clipped to min=max=40 by the firmware's NIGHT profile, reading out
    perfectly well. Two HTTP writes and no downtime settle it, so they go
    first."""
    p = _pipeline(tmp_path, strikes=1, frozen_strikes=1)
    p._note_flat_frame(1000.0, frozen=True)
    assert p.shared_state.get("unwedge_camera") is True
    assert p.flat_state.unwedge_count() == 1
    assert "reboot_camera" not in p.shared_state


def test_changing_flat_frames_never_reboot_the_camera(tmp_path):
    # A dark or featureless scene is still ambiguous evidence; only a repeated
    # frame proves a hang, so the conservative path must stay conservative.
    p = _pipeline(tmp_path, strikes=1, frozen_strikes=1)
    for _ in range(10):
        p._note_flat_frame(1000.0, frozen=False)
    assert "reboot_camera" not in p.shared_state


def test_a_single_changing_frame_breaks_the_frozen_run(tmp_path):
    p = _pipeline(tmp_path, frozen_strikes=3)
    p._note_flat_frame(1000.0, frozen=True)
    p._note_flat_frame(1000.0, frozen=True)
    p._note_flat_frame(1000.0, frozen=False)
    p._note_flat_frame(1000.0, frozen=True)
    p._note_flat_frame(1000.0, frozen=True)
    assert "reboot_camera" not in p.shared_state


def test_reboot_budget_is_bounded(tmp_path):
    p = _pipeline(tmp_path, frozen_strikes=1, max_reboots=2)
    for _ in range(2):
        p._note_flat_frame(1000.0, frozen=True)
        p.shared_state.pop("reboot_camera", None)
    p._note_flat_frame(1000.0, frozen=True)
    assert "reboot_camera" not in p.shared_state


def test_reboots_wait_out_the_cooldown(tmp_path):
    # Heartbeats are ~30s apart, so without the cooldown a frozen sensor would
    # be rebooted on every one of them and never get the time to come back.
    p = _pipeline(tmp_path, frozen_strikes=1, max_reboots=3, reboot_cooldown=120.0)
    _spend_the_exposure_rewrite(p)
    p._note_flat_frame(1000.0, frozen=True)
    assert p.shared_state.pop("reboot_camera") is True

    p._note_flat_frame(1060.0, frozen=True)
    assert "reboot_camera" not in p.shared_state

    p._note_flat_frame(1200.0, frozen=True)
    assert p.shared_state["reboot_camera"] is True


def test_a_frozen_episode_is_marked_active(tmp_path):
    # The budget is only re-armed by FlatEpisodeState.clear(), which does
    # nothing unless the episode was marked active — without this the camera
    # would get its reboots once and never again.
    p = _pipeline(tmp_path, frozen_strikes=1)
    _spend_the_exposure_rewrite(p)
    p._note_flat_frame(1000.0, frozen=True)
    assert p.flat_state.episode_active() is True
    assert p.flat_state.clear() is True
    assert p.flat_state.reboot_count() == 0


def test_frozen_giveup_asks_for_a_power_cycle(tmp_path):
    # A sensor that will not read out after repeated soft reboots is beyond
    # anything A12 can do over the LAN, and the operator has to be told which
    # physical action is left.
    p = _pipeline(tmp_path, frozen_strikes=1, max_reboots=1)
    _spend_the_exposure_rewrite(p)
    p._note_flat_frame(1000.0, frozen=True)
    p._note_flat_frame(1000.0, frozen=True)
    assert any("power" in m.lower() for m in p.notifier.sent)


def test_the_power_cycle_alert_is_sent_once_per_episode(tmp_path):
    # The give-up state is terminal and the heartbeat keeps arriving every ~30s;
    # without the latch this is a message every half minute until morning. A
    # 2026-07-10 episode produced 287 of them, which is why the state file
    # exists at all.
    p = _pipeline(tmp_path, frozen_strikes=1, max_reboots=1)
    _spend_the_exposure_rewrite(p)
    p._note_flat_frame(1000.0, frozen=True)
    for tick in range(1001, 1012):
        p._note_flat_frame(float(tick), frozen=True)
    assert len([m for m in p.notifier.sent if "power cycle" in m]) == 1


def test_the_two_ladders_keep_separate_giveup_latches(tmp_path):
    # The exposure ladder gives up on an episode long before a freeze appears
    # in it — the real 2026-09-14 state file still held gaveup=true hours
    # later. Sharing one latch would swallow the power-cycle alert, which is
    # the one message that asks for a physical action.
    p = _pipeline(tmp_path, strikes=1, max_attempts=1, frozen_strikes=1, max_reboots=1)
    p._note_flat_frame(1000.0, frozen=False)
    p._note_flat_frame(1001.0, frozen=False)
    assert any("still has no detail" in m for m in p.notifier.sent)

    p._note_flat_frame(1002.0, frozen=True)
    p._note_flat_frame(1003.0, frozen=True)
    assert any("power cycle" in m for m in p.notifier.sent)


def test_recovery_clears_both_giveup_latches(tmp_path):
    state = FlatEpisodeState(str(tmp_path / "flat.json"))
    state.mark_active()
    state.set_gaveup()
    state.set_gaveup("gaveup_frozen")
    state.clear()
    assert state.set_gaveup() is True
    assert state.set_gaveup("gaveup_frozen") is True


def test_frozen_reboot_budget_persists_across_restarts(tmp_path):
    # A crash-looping A12 must not reboot the camera all night.
    first = _pipeline(tmp_path, frozen_strikes=1, max_reboots=1)
    first._note_flat_frame(1000.0, frozen=True)

    second = _pipeline(tmp_path, frozen_strikes=1, max_reboots=1)
    second._note_flat_frame(1000.0, frozen=True)
    assert "reboot_camera" not in second.shared_state


# --- the wiring into the heartbeat -----------------------------------------


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def time(self) -> float:
        return self.now


def _heartbeat_pipeline(tmp_path, clock, monkeypatch):
    """The real process_frame path, so a missing call cannot pass unnoticed."""
    import queue
    from unittest.mock import Mock

    monkeypatch.setattr("a12_system.pipeline.time.time", clock.time)
    p = _pipeline(tmp_path, strikes=5, frozen_strikes=3, max_reboots=2,
                  max_attempts=1)
    p.running = True
    p.frame_count = 0
    p.last_heartbeat = 0
    p.heartbeat_interval = 30
    p.frame_buffer = []
    p.notification_queue = queue.Queue()
    p.status_monitor = None
    p.mqtt_client = Mock()
    p.runtime_config = Mock()
    p.runtime_config.get.return_value = 50
    p.freeze_state = FlatEpisodeState(str(tmp_path / "stream_freeze_state.json"))
    p._freeze_consecutive_count = 0
    p._freeze_healthy_frames = 0
    p._last_freeze_time = 0.0
    p._last_freeze_action = 0.0
    p._freeze_reboot_after = 3
    p._freeze_max_reboots = 2
    p._freeze_healthy_gap = 600.0
    p._freeze_action_cooldown = 0.0
    p._freeze_notify_interval = 0.0
    return p


def _dark_night_frames(count):
    """Dark frames as a real sensor produces them: uniform apart from read noise.

    A synthetic constant fill would be bit-identical, which is the signature of
    the fault this feature detects — not of a dark hallway.
    """
    rng = np.random.default_rng(7)
    return [
        np.clip(
            rng.integers(0, 2, size=(120, 160, 1), endpoint=False).astype(np.int16) + 5,
            0, 255,
        ).astype(np.uint8).repeat(3, axis=2)
        for _ in range(count)
    ]


def test_heartbeat_reboots_on_a_repeated_frame(tmp_path, monkeypatch):
    # The wiring itself. Without this test the frozen check can be dropped from
    # process_frame and every other test here still passes, leaving the camera
    # blind exactly as it was on 2026-09-14.
    import pytest
    from unittest.mock import Mock

    clock = _Clock()
    p = _heartbeat_pipeline(tmp_path, clock, monkeypatch)

    class EndOfWatchdog(Exception):
        pass

    p.detector = Mock()
    p.detector.detect_motion.side_effect = EndOfWatchdog

    # 5 flat heartbeats spend the exposure rewrite, then 3 more repeats
    # measured after it are what actually orders the reboot.
    frame = np.full((120, 160, 3), 40, dtype=np.uint8)
    for _ in range(9):
        clock.now += 31
        with pytest.raises(EndOfWatchdog):
            p.process_frame(frame)

    assert p.flat_state.unwedge_count() == 1, "the cheap remedy was skipped"
    assert p.shared_state.get("reboot_camera") is True


def test_heartbeat_does_not_reboot_on_a_dark_but_live_sensor(tmp_path, monkeypatch):
    import pytest
    from unittest.mock import Mock

    clock = _Clock()
    p = _heartbeat_pipeline(tmp_path, clock, monkeypatch)

    class EndOfWatchdog(Exception):
        pass

    p.detector = Mock()
    p.detector.detect_motion.side_effect = EndOfWatchdog

    for frame in _dark_night_frames(20):
        clock.now += 31
        with pytest.raises(EndOfWatchdog):
            p.process_frame(frame)

    assert not p.shared_state.get("reboot_camera")
    assert p.flat_state.reboot_count() == 0


def test_giveup_on_changing_frames_rules_out_a_hung_sensor(tmp_path):
    # The frames were moving the whole time, so the readout is demonstrably
    # alive and the message must not leave a sensor hang on the table.
    p = _pipeline(tmp_path, strikes=1, max_attempts=1)
    p._note_flat_frame(1000.0, frozen=False)
    p.shared_state.pop("unwedge_camera", None)
    p._note_flat_frame(1000.0, frozen=False)
    giveup = [m for m in p.notifier.sent if "still has no detail" in m]
    assert giveup, "the exhausted ladder must still report"
    assert "new frames" in giveup[0]


# --- identical frames are not proof on their own --------------------------
#
# Measured on production 2026-09-15: the camera served min=max=40, std=0.00,
# byte-identical frames — and the sensor was reading out the whole time. The
# firmware was holding PROFILE_NIGHT (AGC off) because the enclosure seals the
# LTR-308, and a uniformly clipped frame encodes to identical JPEG bytes. One
# exposure write turned it into std=14.5, min=0, max=188.
#
# So the cheap, no-downtime exposure rewrite has to be spent BEFORE a reboot is
# ordered. Only frames that stay identical after that say "the readout stopped".


def test_identical_frames_alone_do_not_reboot(tmp_path):
    """The clipped-exposure case. Rebooting here is wrong and it cost a real
    camera three reboots and a pointless request to pull the plug."""
    p = _pipeline(tmp_path, frozen_strikes=3, strikes=5)
    for _ in range(5):
        p._note_flat_frame(1000.0, frozen=True)
        assert "reboot_camera" not in p.shared_state
    assert p.shared_state.get("unwedge_camera") is True, (
        "the cheap exposure rewrite must be tried first"
    )


def test_frames_still_identical_after_the_exposure_rewrite_do_reboot(tmp_path):
    """Once the exposure has been rewritten and the bytes still do not move,
    the readout really has stopped and only a reboot is left."""
    p = _pipeline(tmp_path, frozen_strikes=3, strikes=5, max_attempts=1, cooldown=0.0)
    for _ in range(10):
        p._note_flat_frame(1000.0, frozen=True)
        p.shared_state.pop("unwedge_camera", None)
    assert p.flat_state.unwedge_count() >= 1, "exposure budget was never spent"
    assert p.shared_state.get("reboot_camera") is True
    assert p.shared_state.get("force_stream_reconnect") is True
