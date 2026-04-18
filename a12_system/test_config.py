#!/usr/bin/env python3
"""Smoke test for A12_System_v2 config loading."""

import os
import sys

# Add parent to path so we can import the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from A12_System_v2.config import load_config, DEFAULT_CONFIG


def test_defaults():
    """Verify DEFAULT_CONFIG has all expected keys."""
    required_top = [
        "camera_url", "yolo", "motion", "telegram", "mqtt", "audio",
        "security", "camera_profiles", "logging", "face_recognition",
        "gif", "ir_control", "camera_init_settings",
    ]
    for key in required_top:
        assert key in DEFAULT_CONFIG, f"Missing key: {key}"
    print(f"[OK] DEFAULT_CONFIG has all {len(required_top)} required top-level keys")


def test_yolo_defaults():
    """Verify YOLO defaults point to v11, not v4."""
    assert DEFAULT_CONFIG["yolo"]["weights"] == "yolo11n.onnx", "YOLO should default to v11"
    assert DEFAULT_CONFIG["yolo"]["confidence_threshold"] == 0.3
    print("[OK] YOLO defaults: yolo11n.onnx, threshold=0.3")


def test_no_duplicate_flat_keys():
    """Verify no backward-compat flat keys pollute the config."""
    forbidden = [
        "motion_threshold", "min_contour_area", "confidence_threshold",
        "nms_threshold", "gif_fps", "gif_duration_seconds", "gif_enabled",
        "yolov4_weights", "yolov4_config", "detection_classes",
        "video_enabled", "video_duration_seconds",
    ]
    for key in forbidden:
        assert key not in DEFAULT_CONFIG, f"Duplicate flat key should be removed: {key}"
    print(f"[OK] No duplicate flat keys ({len(forbidden)} checked)")


def test_security_config():
    """Verify security section has required fields."""
    sec = DEFAULT_CONFIG["security"]
    assert sec["mode"] == "MONITOR"
    assert "sabotage_timeout" in sec
    assert "sensor_fusion_window_seconds" in sec
    print("[OK] Security config has all fields")


def test_camera_profiles():
    """Verify 3 profiles exist with correct structure."""
    profiles = DEFAULT_CONFIG["camera_profiles"]
    assert "DAY" in profiles
    assert "DUSK" in profiles
    assert "NIGHT" in profiles
    assert "thresholds" in profiles
    assert profiles["DAY"]["brightness"] == 0
    assert profiles["DUSK"]["brightness"] == 3
    print("[OK] Camera profiles: DAY/DUSK/NIGHT with correct values")


def test_load_config_no_crash():
    """Verify load_config doesn't crash with empty directory."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        config = load_config(tmpdir)
        assert config["camera_url"] == "http://192.168.1.100"
        assert config["yolo"]["weights"] == "yolo11n.onnx"
        assert config["security"]["mode"] == "MONITOR"
    print("[OK] load_config works with empty directory (defaults only)")


if __name__ == "__main__":
    test_defaults()
    test_yolo_defaults()
    test_no_duplicate_flat_keys()
    test_security_config()
    test_camera_profiles()
    test_load_config_no_crash()
    print("\n=== All tests passed ===")
