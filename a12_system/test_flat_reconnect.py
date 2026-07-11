import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.pipeline import flat_recovery_action


def _action(**kw):
    base = dict(
        consecutive_flat=5, reconnect_strikes=5,
        forced_reconnects=0, reboots=0,
        reconnect_before_reboot=3, max_reboots=5,
        now=1000.0, last_action=0.0, cooldown=120.0,
    )
    base.update(kw)
    return flat_recovery_action(**base)


def test_below_strike_threshold_does_nothing():
    assert _action(consecutive_flat=2) == "none"


def test_first_escalation_reconnects():
    assert _action(forced_reconnects=0) == "reconnect"


def test_within_cooldown_does_nothing():
    assert _action(forced_reconnects=1, now=1030.0, last_action=1000.0) == "none"


def test_reconnect_allowed_again_after_cooldown():
    assert _action(forced_reconnects=1, now=1200.0, last_action=1000.0) == "reconnect"


def test_reboots_camera_once_reconnects_exhausted():
    # 3 forced reconnects did not recover — a soft camera reboot over LAN clears
    # the OV3660 wedge (verified 2026-07-11).
    assert _action(forced_reconnects=3, reboots=0) == "reboot"


def test_gives_up_after_max_reboots():
    # Even camera reboots did not help — likely dead hardware; stop rebooting and
    # alert instead of looping forever.
    assert _action(forced_reconnects=3, reboots=5) == "giveup"


def test_reboot_also_respects_cooldown():
    assert _action(forced_reconnects=3, reboots=1, now=1030.0, last_action=1000.0) == "none"
