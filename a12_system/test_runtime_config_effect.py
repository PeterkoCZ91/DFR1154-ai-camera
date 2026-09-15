"""A runtime config change must actually change behaviour.

`update_from_mqtt` returns True and `__main__` publishes
`camera/config/status/last_update`, so an operator lowering a threshold from
Home Assistant sees a confirmation land. Until 2026-09-15 six of those keys were
read once into instance attributes in `DetectionPipeline.__init__` and never
read again, and two more (`telegram_cooldown`, `yolo_confidence`) reached their
consumers as a deep copy made at startup — so the confirmation was true about
the config and false about the system. Nothing here had a test.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.config import DEFAULT_CONFIG
from a12_system.pipeline import DetectionPipeline
from a12_system.runtime_config import RuntimeConfig


def _rc():
    return RuntimeConfig(copy.deepcopy(DEFAULT_CONFIG))


def _pipeline(rc):
    p = DetectionPipeline.__new__(DetectionPipeline)
    p.runtime_config = rc
    return p


def _set(rc, mqtt_key, value):
    assert rc.update_from_mqtt(f"camera/config/set/{mqtt_key}", str(value)), (
        f"{mqtt_key} is not in the MQTT key map"
    )


# --- the six the pipeline used to freeze at startup ------------------------


def test_notify_threshold_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "notify_threshold", 40)
    assert p.event_notify_threshold == 40


def test_local_record_threshold_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "local_record_threshold", 20)
    assert p.event_local_record_threshold == 20


def test_require_sensor_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    before = p.require_sensor_for_recording
    _set(rc, "require_sensor", not before)
    assert p.require_sensor_for_recording is (not before)


def test_detection_cooldown_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "detection_cooldown", 99)
    assert p.cooldown_seconds == 99


def test_pir_cooldown_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "pir_cooldown", 7)
    assert p.pir_recording_cooldown == 7


def test_periodic_yolo_interval_takes_effect():
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "periodic_yolo_interval", 600)
    assert p.periodic_yolo_interval == 600


def test_a_negative_pir_cooldown_is_still_floored():
    """The clamp lived in __init__ and has to survive the move."""
    rc = _rc()
    p = _pipeline(rc)
    _set(rc, "pir_cooldown", -5)
    assert p.pir_recording_cooldown == 0


# --- the two that reached their consumer as a startup snapshot -------------


def test_telegram_cooldown_reaches_the_notifier():
    from a12_system.notifier import Notifier

    rc = _rc()
    notifier = Notifier(rc.live())
    _set(rc, "telegram_cooldown", 123)
    assert notifier.config.get("telegram_cooldown_seconds") == 123


def test_yolo_confidence_reaches_the_detector():
    from a12_system.detection import Detector

    rc = _rc()
    det = Detector.__new__(Detector)
    det.config = rc.live()
    _set(rc, "yolo_confidence", 0.42)
    assert det.config["yolo"]["confidence_threshold"] == 0.42


# --- and the copy must stay a copy where one is wanted ---------------------


def test_get_all_still_hands_out_a_copy():
    """`live()` is the deliberate exception; `get_all()` must not become one,
    or a caller mutating its own snapshot would corrupt the real config."""
    rc = _rc()
    snapshot = rc.get_all()
    snapshot["yolo"]["confidence_threshold"] = 0.99
    assert rc.get("yolo.confidence_threshold") != 0.99


# --- and the wiring that hands it out --------------------------------------


def test_the_app_hands_consumers_the_live_config_not_a_snapshot():
    """The wiring. Every test above builds its consumer by hand, so swapping
    this one call back to `get_all()` left all of them green — which is a
    process where no runtime key reaches Camera, Detector or Notifier.
    """
    from a12_system import __main__ as entry

    app = object.__new__(entry.Application)
    rc = _rc()
    app.runtime_config = rc
    assert app._consumer_config() is rc.live(), (
        "consumers were handed a copy; runtime changes cannot reach them"
    )
