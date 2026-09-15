"""The motion accuracy figure has to divide two comparable populations.

`record_motion_event` is called once per YOLO invocation — including `periodic`
sweeps and PIR-triggered checks, where no motion fired at all — and it always
incremented a true or a false positive. The denominator counted only the runs
where ESP32 or OpenCV motion actually fired. With the shipped
`MOTION_THRESHOLD=0` and firmware motion off, that denominator is permanently
zero, so the summary reported `accuracy: 0.0%` beside a false-positive count
accumulated from checks that had no motion to be wrong about. In a mixed
configuration the ratio could exceed 100%.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.stats import Statistics


def _stats(tmp_path):
    s = Statistics.__new__(Statistics)
    s.__init__(save_path=str(tmp_path / "stats.json"))
    s.running = False
    return s


def _motion(s):
    return s.get_summary()["motion_detection"]


def test_a_check_with_no_motion_is_not_a_motion_event(tmp_path):
    """The periodic sweep. It had nothing to be right or wrong about."""
    s = _stats(tmp_path)
    for _ in range(50):
        s.record_motion_event(esp32_detected=False, python_detected=False,
                              person_found=False)
    m = _motion(s)
    assert m["total_events"] == 0
    assert m["true_positives"] == 0
    assert m["false_positives"] == 0, (
        "a check with no motion was counted as a motion false positive"
    )


def test_the_populations_always_match(tmp_path):
    """The invariant the ratio depends on: every counted motion event lands in
    exactly one of the two outcome buckets."""
    s = _stats(tmp_path)
    for i in range(30):
        s.record_motion_event(
            esp32_detected=(i % 3 == 0),
            python_detected=(i % 3 == 1),
            person_found=(i % 2 == 0),
        )
    m = _motion(s)
    assert m["true_positives"] + m["false_positives"] == m["total_events"]


def test_accuracy_is_the_share_of_motion_events_that_found_somebody(tmp_path):
    s = _stats(tmp_path)
    for _ in range(3):
        s.record_motion_event(True, False, person_found=True)
    for _ in range(1):
        s.record_motion_event(True, False, person_found=False)
    assert _motion(s)["accuracy"] == "75.0%"


def test_accuracy_can_never_exceed_100_percent(tmp_path):
    """It could before: the numerator counted every check and the denominator
    only the ones motion triggered."""
    s = _stats(tmp_path)
    s.record_motion_event(True, False, person_found=True)
    for _ in range(20):
        s.record_motion_event(False, False, person_found=True)
    assert _motion(s)["accuracy"] == "100.0%"


def test_no_motion_at_all_reports_zero_not_a_division_error(tmp_path):
    m = _motion(_stats(tmp_path))
    assert m["accuracy"] == "0.0%"
    assert m["esp32_percentage"] == "0.0%"


def test_the_esp32_share_still_counts_only_motion_events(tmp_path):
    s = _stats(tmp_path)
    s.record_motion_event(True, False, person_found=True)
    s.record_motion_event(False, True, person_found=False)
    s.record_motion_event(False, False, person_found=True)   # periodic, ignored
    m = _motion(s)
    assert m["total_events"] == 2
    assert m["esp32_percentage"] == "50.0%"
