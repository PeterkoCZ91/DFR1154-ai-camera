"""Recovery must not rewrite exposure or count its own expected outages."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from a12_system import __main__ as entry
from a12_system.config import DEFAULT_CONFIG


@pytest.mark.parametrize('reason', ['frozen', 'stream_ended'])
def test_reboot_outage_is_recorded_without_escalation(monkeypatch, reason):
    app = object.__new__(entry.Application)
    app.pipeline = Mock()
    camera = Mock(last_freeze_summary='no bytes')
    camera.log_health_snapshot.return_value = {}
    monkeypatch.setattr(entry.time, 'time', lambda: 1050.0)
    app._record_stream_break(camera, reason, reboot_grace_until=1060.0)
    app.pipeline.note_stream_freeze.assert_not_called()
    assert 'expected_after_reboot' in app.pipeline.db.log_event.call_args.args[3]


def test_break_after_grace_still_escalates(monkeypatch):
    app = object.__new__(entry.Application)
    app.pipeline = Mock()
    camera = Mock(last_freeze_summary=None)
    camera.log_health_snapshot.return_value = {}
    monkeypatch.setattr(entry.time, 'time', lambda: 1060.0)
    app._record_stream_break(camera, 'frozen', reboot_grace_until=1060.0)
    app.pipeline.note_stream_freeze.assert_called_once_with('frozen')


def test_diagnostics_crossing_deadline_do_not_change_expected_outage(monkeypatch):
    app = object.__new__(entry.Application)
    app.pipeline = Mock()
    clock = SimpleNamespace(now=1059.0)
    monkeypatch.setattr(entry.time, 'time', lambda: clock.now)
    camera = Mock(last_freeze_summary=None)

    def slow_health(reason):
        clock.now = 1065.0
        return {}

    camera.log_health_snapshot.side_effect = slow_health
    app._record_stream_break(camera, 'frozen', reboot_grace_until=1060.0)
    app.pipeline.note_stream_freeze.assert_not_called()


@pytest.mark.parametrize('profiles_owned_by_a12', [False, True])
def test_connect_does_not_write_exposure_or_schedule_reset(tmp_path, monkeypatch, profiles_owned_by_a12):
    config = deepcopy(DEFAULT_CONFIG)
    config.update(audio_enabled=False, camera_init_settings={}, home_assistant_token='')
    config['camera_profiles']['apply_from_a12'] = profiles_owned_by_a12
    monkeypatch.setattr(entry, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(entry, 'load_config', lambda _: config)
    monkeypatch.setattr(entry, 'setup_logging', Mock())
    monkeypatch.setattr(entry.signal, 'signal', Mock())
    for name in ('Detector', 'Notifier', 'Statistics', 'EventDB', 'MQTTClient',
                 'StatusMonitor', 'DetectionPipeline'):
        monkeypatch.setattr(entry, name, Mock())
    entry.Detector.return_value.known_face_names = []
    camera = Mock()
    monkeypatch.setattr(entry, 'Camera', Mock(return_value=camera))
    timer = Mock(side_effect=AssertionError('reconnect must not schedule sensor writes'))
    monkeypatch.setattr(entry.threading, 'Timer', timer)
    app = entry.Application()

    def stop_after_connection(*args, **kwargs):
        app.running = False
        return 'interrupted'

    camera.process_stream.side_effect = stop_after_connection
    app.run()
    camera.set_camera_settings.assert_not_called()
    timer.assert_not_called()


# --- a reboot that never landed must be handed back ------------------------


def _camera(ok: bool):
    camera = Mock()
    camera.reboot.return_value = ok
    return camera


def test_a_failed_reboot_is_reported_back_to_the_ladder_that_charged_it():
    """Seen live 2026-09-15 11:45:14: the POST timed out and the ladder went to
    2/3 regardless. After the budget drains, A12 asks for a physical power
    cycle for a camera it never rebooted."""
    app = object.__new__(entry.Application)
    state = {"reboot_camera": "flat"}
    assert app._execute_reboot_request(_camera(False), state) is True
    assert state["reboot_failed"] == "flat"


def test_the_freeze_ladder_gets_its_own_attempt_back():
    """Two ladders, two budgets, one shared flag — the tag has to survive."""
    app = object.__new__(entry.Application)
    state = {"reboot_camera": "freeze"}
    app._execute_reboot_request(_camera(False), state)
    assert state["reboot_failed"] == "freeze"


def test_a_delivered_reboot_reports_nothing_back():
    app = object.__new__(entry.Application)
    state = {"reboot_camera": "flat"}
    assert app._execute_reboot_request(_camera(True), state) is True
    assert "reboot_failed" not in state


def test_no_request_means_no_reboot_and_no_grace_window():
    """The grace window suppresses STUCK alerts, so it may only open for an
    outage we actually caused."""
    app = object.__new__(entry.Application)
    camera = _camera(True)
    assert app._execute_reboot_request(camera, {}) is False
    camera.reboot.assert_not_called()
