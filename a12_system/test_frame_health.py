import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.pipeline import classify_frame_health


def test_normal_frame_is_healthy():
    assert classify_frame_health(120.0, 45.0, 30, 1.0) is None


def test_dark_frame_detected():
    assert classify_frame_health(5.0, 3.0, 30, 1.0) == "dark"


def test_flat_gray_frame_detected():
    # hung OV3660 sensor: uniform mid-gray (~65) with near-zero variance,
    # bright enough to evade the dark-frame check
    assert classify_frame_health(65.0, 0.1, 30, 1.0) == "flat"


def test_dark_and_flat_reports_dark():
    assert classify_frame_health(2.0, 0.05, 30, 1.0) == "dark"


def test_noisy_night_scene_is_dark_not_flat():
    assert classify_frame_health(10.0, 8.0, 30, 1.0) == "dark"


def test_low_contrast_but_real_scene_is_healthy():
    # fog / plain wall: low-ish variance but well above the flat threshold
    assert classify_frame_health(90.0, 6.0, 30, 1.0) is None
