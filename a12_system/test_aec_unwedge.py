"""Tests for AEC/AGC unwedge recovery on a sustained low-detail episode.

A wedged OV3660 auto-exposure loop keeps streaming perfectly decodable JPEGs
that are a uniform field, so the stream-freeze watchdog never trips and the
camera stays blind indefinitely (2026-09-11 21:00 -> 2026-09-12 08:57: 12h).
Re-applying the day/night profile does NOT clear it — the 06:00 NIGHT->DUSK
switch that night changed brightness 5.0 -> 64.1 without restoring any detail.
Explicitly toggling AEC/AGC off and back on does clear it, and unlike a LAN
reboot it costs two HTTP POSTs and no downtime.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.flat_episode import FlatEpisodeState
from a12_system.pipeline import DetectionPipeline, aec_unwedge_action
from a12_system.status_monitor import StatusMonitor


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_telegram(self, message, **kwargs):
        self.sent.append(message)


# --- the escalation ladder -------------------------------------------------


def _action(**kw):
    base = dict(
        consecutive_flat=5, strikes=5, attempts=0, max_attempts=3,
        now=1000.0, last_action=0.0, cooldown=300.0,
    )
    base.update(kw)
    return aec_unwedge_action(**base)


def test_short_flat_run_does_nothing():
    assert _action(consecutive_flat=4) == "none"


def test_sustained_flat_run_unwedges():
    assert _action() == "unwedge"


def test_second_attempt_waits_out_the_cooldown():
    assert _action(attempts=1, now=1100.0, last_action=1000.0) == "none"


def test_second_attempt_allowed_after_cooldown():
    assert _action(attempts=1, now=1400.0, last_action=1000.0) == "unwedge"


def test_gives_up_once_the_attempt_budget_is_spent():
    assert _action(attempts=3) == "giveup"


# --- persisted attempt budget ----------------------------------------------


def test_unwedge_attempts_persist_across_restarts(tmp_path):
    path = str(tmp_path / "flat.json")
    first = FlatEpisodeState(path)
    first.record_unwedge()
    first.record_unwedge()
    # A crash-looping A12 must not re-arm a fresh budget every process start.
    assert FlatEpisodeState(path).unwedge_count() == 2


def test_recovery_clears_the_unwedge_budget(tmp_path):
    state = FlatEpisodeState(str(tmp_path / "flat.json"))
    state.mark_active()
    state.record_unwedge()
    state.clear()
    assert state.unwedge_count() == 0


# --- pipeline wiring -------------------------------------------------------


def _pipeline(tmp_path, **overrides):
    p = object.__new__(DetectionPipeline)
    p.log_prefix = "[test:cam]"
    p.telegram_label = ""
    p.notifier = _Notifier()
    p.shared_state = {}
    p.flat_state = FlatEpisodeState(str(tmp_path / "flat_episode_state.json"))
    # Real production wiring, not a hand-copied attribute list: a knob the
    # config layer forgets can then no longer slip past the suite.
    p.configure_frame_health_watchdog({
        "flat_frame_reconnect_strikes": overrides.get("strikes", 5),
        "flat_frame_max_unwedge_attempts": overrides.get("max_attempts", 3),
        "flat_frame_unwedge_cooldown": overrides.get("cooldown", 0.0),
        "flat_frame_notify_interval": overrides.get("notify_interval", 0.0),
        "flat_frame_healthy_required": overrides.get("healthy_required", 3),
    })
    return p


def test_config_knobs_reach_the_ladder():
    p = object.__new__(DetectionPipeline)
    p.configure_frame_health_watchdog(
        {"flat_frame_max_unwedge_attempts": 7, "flat_frame_unwedge_cooldown": 42.0}
    )
    assert p._flat_max_unwedge_attempts == 7
    assert p._flat_unwedge_cooldown == 42.0


def test_frame_health_knobs_have_working_defaults():
    p = object.__new__(DetectionPipeline)
    p.configure_frame_health_watchdog({})
    assert p._flat_max_unwedge_attempts > 0
    assert p._flat_unwedge_cooldown > 0


def test_brief_low_detail_blip_requests_nothing(tmp_path):
    p = _pipeline(tmp_path, strikes=5)
    for _ in range(4):
        p._note_flat_frame(1000.0)
    assert "unwedge_camera" not in p.shared_state


def test_sustained_low_detail_requests_an_unwedge(tmp_path):
    p = _pipeline(tmp_path, strikes=5)
    for _ in range(5):
        p._note_flat_frame(1000.0)
    assert p.shared_state["unwedge_camera"] is True


def test_flat_alert_does_not_blame_low_light(tmp_path):
    # brightness=64 uniform grey is NOT darkness (real darkness reads ~5), and
    # the old wording sent the operator looking for a lighting problem.
    p = _pipeline(tmp_path, strikes=1)
    p._note_flat_frame(1000.0)
    assert p.notifier.sent, "a sustained low-detail episode must alert"
    assert "low light is possible" not in p.notifier.sent[0]


def test_alert_says_recovery_is_being_attempted(tmp_path):
    p = _pipeline(tmp_path, strikes=1)
    p._note_flat_frame(1000.0)
    assert "reboot skipped" not in p.notifier.sent[0]


def test_stops_requesting_once_the_budget_is_spent(tmp_path):
    p = _pipeline(tmp_path, strikes=1, max_attempts=2)
    for _ in range(2):
        p._note_flat_frame(1000.0)
        p.shared_state.pop("unwedge_camera", None)
    p._note_flat_frame(1000.0)
    assert "unwedge_camera" not in p.shared_state


def test_textured_frames_re_arm_the_budget(tmp_path):
    p = _pipeline(tmp_path, strikes=1, max_attempts=1, healthy_required=1)
    p._note_flat_frame(1000.0)
    p.shared_state.pop("unwedge_camera", None)
    p._flat_ladder_note_nonflat(1000.0)

    p._note_flat_frame(2000.0)
    assert p.shared_state["unwedge_camera"] is True


# --- executor --------------------------------------------------------------


class _Camera:
    """Stands in for the camera client, which owns the authenticated session.

    /settings is behind basic auth (401 without it), and only the camera client
    carries credentials — the status monitor's plain session does not.
    """

    def __init__(self, ok=True):
        self.writes = []
        self.ok = ok

    def set_camera_settings(self, settings, retries=3):
        self.writes.append(settings)
        return self.ok


def _monitor(camera):
    m = object.__new__(StatusMonitor)
    m.camera = camera
    m.shared_state = {}
    m._unwedge_settle_seconds = 0.0
    return m


def test_unwedge_toggles_aec_and_agc_off_then_back_on():
    cam = _Camera()
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True

    m._service_unwedge_request()

    assert len(cam.writes) == 2, "expected an off write then an on write"
    off, on = cam.writes
    assert off["aec"] == 0 and off["agc"] == 0
    assert on["aec"] == 1 and on["agc"] == 1


def test_unwedge_goes_through_the_authenticated_client():
    # Writing with the status monitor's own session gets a 401 from /settings.
    cam = _Camera()
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True
    m._service_unwedge_request()
    assert cam.writes, "the write must go through the camera client"


def test_unwedge_request_is_consumed_once():
    cam = _Camera()
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True

    m._service_unwedge_request()
    m._service_unwedge_request()

    assert len(cam.writes) == 2, "a single request must not toggle twice"


def test_no_request_means_no_writes():
    cam = _Camera()
    m = _monitor(cam)
    m._service_unwedge_request()
    assert cam.writes == []


def test_missing_camera_client_is_reported_not_swallowed():
    # Silently returning here would reproduce the exact failure this feature
    # exists to prevent: a recovery that looks wired up and does nothing.
    m = _monitor(None)
    m.shared_state["unwedge_camera"] = True

    m._service_unwedge_request()

    assert m.shared_state.get("unwedge_write_failed") is True


def test_failed_write_is_reported_back():
    # Port 80 is starved while port 81 streams (measured: 8s, 3.5s, 1.2s for
    # /status), so these writes really do time out.
    cam = _Camera(ok=False)
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True

    m._service_unwedge_request()

    assert m.shared_state.get("unwedge_write_failed") is True


def test_successful_write_reports_no_failure():
    cam = _Camera(ok=True)
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True
    m._service_unwedge_request()
    assert not m.shared_state.get("unwedge_write_failed")


# --- the budget must count real rewrites, not failed attempts --------------


def test_failed_write_does_not_burn_the_budget(tmp_path):
    # Otherwise a camera that is merely unreachable exhausts the budget and the
    # give-up alert blames the exposure loop for a write that never landed.
    p = _pipeline(tmp_path, strikes=1, max_attempts=2)
    p._note_flat_frame(1000.0)
    p.shared_state["unwedge_write_failed"] = True

    p._note_flat_frame(1001.0)

    assert p.flat_state.unwedge_count() == 1


def test_pipeline_arms_the_unwedge_cooldown(tmp_path):
    # The ladder's cooldown is only real if the pipeline records when it last
    # fired; without that it rewrites the sensor on every single heartbeat.
    p = _pipeline(tmp_path, strikes=1, max_attempts=5, cooldown=300.0)
    p._note_flat_frame(1000.0)
    assert p.shared_state.pop("unwedge_camera", None) is True

    p._note_flat_frame(1001.0)

    assert "unwedge_camera" not in p.shared_state


def test_off_write_pins_a_manual_exposure():
    # Without agc_gain/aec_value the sensor keeps the very auto values it is
    # wedged at. Pinning a manual exposure is the write that produced detail on
    # the live camera (2026-09-12: std 0.34 -> 7.59 on this write alone).
    cam = _Camera()
    m = _monitor(cam)
    m.shared_state["unwedge_camera"] = True

    m._service_unwedge_request()

    off = cam.writes[0]
    assert "aec_value" in off and "agc_gain" in off


def test_run_loop_services_the_unwedge_request(monkeypatch):
    # The wiring itself. Without this test the call can be deleted from run()
    # and every other test still passes — the recovery silently does nothing,
    # which is the exact failure shape this whole feature exists to fix.
    now = 1_000_000.0
    monkeypatch.setattr("a12_system.status_monitor.time.time", lambda: now)

    cam = _Camera()
    m = _monitor(cam)
    m.running = True

    # Stop from the loop's own sleep, never from the camera call: if the
    # service call is missing the test must FAIL, not spin forever.
    ticks = {"n": 0}

    def _tick(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            m.running = False

    monkeypatch.setattr("a12_system.status_monitor.time.sleep", _tick)
    # Park every interval-gated task; only the unwedge service is unconditional.
    for attr in ("last_heartbeat", "last_status", "last_adaptive",
                 "last_resource_check", "last_health"):
        setattr(m, attr, now)
    for attr in ("heartbeat_interval", "status_interval", "adaptive_interval",
                 "resource_check_interval", "health_interval"):
        setattr(m, attr, 10_000)

    m.shared_state["unwedge_camera"] = True

    m.run()

    assert cam.writes, "run() must service the unwedge request"


def test_the_giveup_alert_is_sent_once_per_episode(tmp_path):
    # Give-up is terminal but the heartbeat keeps arriving every ~30s, so
    # without the latch this is a Telegram message every half minute until the
    # scene changes — the 287-message shape the state file exists to prevent.
    p = _pipeline(tmp_path, strikes=1, max_attempts=1)
    for tick in range(1000, 1012):
        p._note_flat_frame(float(tick))
    assert len([m for m in p.notifier.sent if "still has no detail" in m]) == 1


def test_failed_write_alerts_that_it_could_not_be_applied(tmp_path):
    # Saying nothing would look identical to a rewrite that landed and did not
    # help, which is the one conclusion the evidence does not support.
    p = _pipeline(tmp_path, strikes=1, max_attempts=2)
    p._note_flat_frame(1000.0)
    p.notifier.sent.clear()
    p.shared_state["unwedge_write_failed"] = True

    p._note_flat_frame(1001.0)

    assert any("could not be applied" in m for m in p.notifier.sent)
