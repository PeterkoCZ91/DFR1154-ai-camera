"""Tests for the clip post-window clamp and the flat-ladder budget gate.

Both cover regressions where the code silently did something other than what it
claimed:

* The adaptive post window could wait up to `adaptive_clip.max_post_seconds` (60 s
  by default) and then read post frames from the *live* rolling deque, which only
  retains `clip_pre + clip_post + 2` seconds. Everything older had been evicted, so
  the clip was pre-frames plus the last ~22 s with tens of seconds missing in the
  middle — and nothing reported it.
* The flat-frame ladder re-armed its reconnect budget on every single healthy
  heartbeat, so a sensor flapping around the std threshold never reached the reboot
  step. The module docstring said explicitly that this must not happen.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.pipeline import DetectionPipeline


class _Cfg:
    """Minimal runtime-config stand-in: dotted keys with defaults."""

    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _pipeline(**cfg):
    """A DetectionPipeline with only the attributes these tests touch.

    __init__ builds threads, folders and a notification worker, so bypass it —
    the two behaviours under test are pure arithmetic over instance state.
    """
    p = object.__new__(DetectionPipeline)
    p.runtime_config = _Cfg(cfg)
    p.log_prefix = "[test]"
    p._logged_post_clamp = False
    return p


# ── post-window clamp ────────────────────────────────────────────────────────

def test_clamped_to_what_the_buffer_retains():
    # defaults: pre 5 + post 15 + 2 = 22 s of history, so 21 s is recoverable
    p = _pipeline()
    p._max_retainable_post_seconds = 21
    assert p._effective_max_post_seconds(15) == 21


def test_configured_maximum_wins_when_the_buffer_is_big_enough():
    p = _pipeline(**{"adaptive_clip.max_post_seconds": 30})
    p._max_retainable_post_seconds = 120
    assert p._effective_max_post_seconds(15) == 30


def test_never_below_the_base_post_window():
    # base_post_seconds is the floor of `configured`; the clamp may still cut it,
    # but a tiny max_post_seconds must not shrink the window below the base.
    p = _pipeline(**{"adaptive_clip.max_post_seconds": 1})
    p._max_retainable_post_seconds = 21
    assert p._effective_max_post_seconds(15) == 15


def test_clamp_is_logged_once(caplog):
    p = _pipeline(**{"adaptive_clip.max_post_seconds": 60})
    p._max_retainable_post_seconds = 21
    with caplog.at_level("INFO"):
        p._effective_max_post_seconds(15)
        p._effective_max_post_seconds(15)
        p._effective_max_post_seconds(15)
    assert sum(1 for r in caplog.records if "post window capped" in r.message) == 1


def test_no_log_when_nothing_is_clamped(caplog):
    p = _pipeline(**{"adaptive_clip.max_post_seconds": 10})
    p._max_retainable_post_seconds = 120
    with caplog.at_level("INFO"):
        p._effective_max_post_seconds(10)
    assert not [r for r in caplog.records if "post window capped" in r.message]


# ── flat-ladder budget gate ──────────────────────────────────────────────────

def _ladder(healthy_required=10):
    p = object.__new__(DetectionPipeline)
    p.log_prefix = "[test]"
    p._flat_healthy_required = healthy_required
    p._flat_nonflat_count = 0
    p._flat_forced_reconnects = 2          # two reconnects already spent
    p._flat_notify_interval = 3600
    p.notifier = types.SimpleNamespace(send_telegram=lambda *a, **k: True)
    p._telegram_message = lambda m: m
    p.flat_state = types.SimpleNamespace(
        clear=lambda: False,               # already clear → no notification
        should_notify=lambda *a, **k: False,
    )
    return p


def test_single_healthy_frame_does_not_refund_the_budget():
    p = _ladder()
    p._flat_ladder_note_nonflat(1000.0)
    assert p._flat_forced_reconnects == 2, "one healthy heartbeat must not re-arm"


def test_budget_survives_a_flapping_sensor():
    # nine healthy frames, still one short of the requirement
    p = _ladder(healthy_required=10)
    for _ in range(9):
        p._flat_ladder_note_nonflat(1000.0)
    assert p._flat_forced_reconnects == 2


def test_sustained_health_refunds_the_budget():
    p = _ladder(healthy_required=10)
    for _ in range(10):
        p._flat_ladder_note_nonflat(1000.0)
    assert p._flat_forced_reconnects == 0


def test_escalation_is_reachable_when_health_never_sustains():
    """The point of the gate: a flapping sensor must still reach "reboot"."""
    from a12_system.pipeline import flat_recovery_action

    p = _ladder(healthy_required=10)
    now, last_action = 1000.0, 0.0
    for _ in range(3):                     # one healthy frame between wedges
        p._flat_ladder_note_nonflat(now)
    action = flat_recovery_action(
        consecutive_flat=5, reconnect_strikes=5,
        forced_reconnects=p._flat_forced_reconnects + 1,
        reboots=0, reconnect_before_reboot=3, max_reboots=5,
        now=now, last_action=last_action, cooldown=120.0,
    )
    assert action == "reboot"
