"""A PIR that went "unavailable" while on must not stay on for ever: a stuck
sensor keeps the PIR window open and lets face checks run with nobody there."""

import time

from a12_system.ha_monitor import HAMonitor


def monitor():
    m = HAMonitor.__new__(HAMonitor)
    m.sensor_states = {}
    m.sensor_last_active = {}
    m.callback = None
    m.running = True
    m.poll_interval = 0.0
    m.monitored_sensors = ["binary_sensor.pir"]
    m._last_ok_at = time.monotonic()
    m.max_state_age_seconds = 30.0
    return m


def poll_once(m, states):
    """Run one pass of the run() loop body with canned HA answers."""
    calls = {"n": 0}

    def fake_states():
        calls["n"] += 1
        if calls["n"] > 2:
            m.running = False
        return states.pop(0) if states else {}

    m._get_sensor_states = fake_states
    m.run()


def st(state):
    return {"binary_sensor.pir": {"entity_id": "binary_sensor.pir", "state": state, "attributes": {}}}


def test_on_then_unavailable_is_no_longer_active():
    m = monitor()
    poll_once(m, [st("on"), st("on"), st("unavailable")])
    assert m.sensor_states["binary_sensor.pir"] == "unavailable"
    assert m.is_any_sensor_active() is False
    assert m.get_active_sensors() == []


def test_a_sensor_that_is_on_counts_while_ha_answers():
    m = monitor()
    poll_once(m, [st("on"), st("on"), st("on")])
    assert m.is_any_sensor_active() is True
    assert m.get_active_sensors() == ["binary_sensor.pir"]


def test_on_is_dropped_when_ha_stops_answering():
    m = monitor()
    m.sensor_states["binary_sensor.pir"] = "on"
    m._last_ok_at = time.monotonic() - 31.0
    assert m.is_any_sensor_active() is False
    assert m.get_active_sensors() == []


def test_a_lost_sensor_is_not_an_event():
    m = monitor()
    seen = []
    m.callback = lambda **kw: seen.append(kw["new_state"])
    poll_once(m, [st("off"), st("unavailable"), st("on")])
    assert seen == ["on"]
