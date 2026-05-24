#!/usr/bin/env python3
"""Smoke test for A12_System_v2 config loading."""

import os
import sys

# Add parent to path so we can import the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from A12_System_v2.config import load_config, DEFAULT_CONFIG
except ModuleNotFoundError:
    from a12_system.config import load_config, DEFAULT_CONFIG


def test_defaults():
    """Verify DEFAULT_CONFIG has all expected keys."""
    required_top = [
        "camera_id", "camera_name", "camera_url", "yolo", "motion", "telegram", "mqtt", "audio",
        "security", "camera_profiles", "logging", "face_recognition",
        "gif", "clip_pre_seconds", "clip_post_seconds", "ir_control",
        "camera_init_settings", "external_trigger_yolo_window_seconds",
        "external_trigger_yolo_interval_seconds", "media_retention_days",
        "media_cleanup_interval_seconds", "notification_queue_maxsize", "adaptive_clip", "event_scoring",
    ]
    for key in required_top:
        assert key in DEFAULT_CONFIG, f"Missing key: {key}"
    print(f"[OK] DEFAULT_CONFIG has all {len(required_top)} required top-level keys")


def test_clip_defaults():
    """Verify media clip defaults are long enough for stair movement."""
    assert DEFAULT_CONFIG["clip_pre_seconds"] == 5
    assert DEFAULT_CONFIG["clip_post_seconds"] == 15
    assert DEFAULT_CONFIG["audio"]["buffer_seconds"] >= 20
    print("[OK] Clip defaults: 5s pre + 15s post")


def test_external_trigger_defaults():
    """Verify PIR/HA trigger opens a short repeated YOLO window."""
    assert DEFAULT_CONFIG["external_trigger_yolo_window_seconds"] == 15.0
    assert DEFAULT_CONFIG["external_trigger_yolo_interval_seconds"] == 2.0
    assert DEFAULT_CONFIG["yolo"]["pir_notify_confidence_threshold"] == 0.45
    assert DEFAULT_CONFIG["yolo"]["pir_person_confirmations"] == 1
    print("[OK] External trigger defaults: 15s window, 2s interval, PIR fast path")


def test_event_scoring_defaults():
    """Verify event scoring keeps PIR fast and camera-only conservative."""
    scoring = DEFAULT_CONFIG["event_scoring"]
    assert scoring["enabled"] is True
    assert scoring["notify_threshold"] == 70
    assert scoring["local_record_threshold"] == 45
    assert scoring["sensor_active_score"] + scoring["yolo_person_score"] >= scoring["notify_threshold"]
    print("[OK] Event scoring defaults: PIR-assisted notify, weak events local-only")


def test_adaptive_clip_defaults():
    """Verify clips can extend while activity continues."""
    adaptive = DEFAULT_CONFIG["adaptive_clip"]
    assert adaptive["enabled"] is True
    assert adaptive["idle_seconds"] == 10
    assert adaptive["max_post_seconds"] == 60
    assert adaptive["max_post_seconds"] >= DEFAULT_CONFIG["clip_post_seconds"]
    print("[OK] Adaptive clip defaults: idle=10s, max_post=60s")


def test_media_cleanup_defaults():
    """Verify media retention keeps disk usage bounded."""
    assert DEFAULT_CONFIG["media_retention_days"] == 2
    assert DEFAULT_CONFIG["media_cleanup_interval_seconds"] == 3600
    assert DEFAULT_CONFIG["notification_queue_maxsize"] == 10
    print("[OK] Media cleanup defaults: 2d retention, hourly check")


def test_yolo_defaults():
    """Verify YOLO defaults point to v11, not v4."""
    assert DEFAULT_CONFIG["yolo"]["weights"] == "yolo11n.onnx", "YOLO should default to v11"
    assert DEFAULT_CONFIG["yolo"]["confidence_threshold"] == 0.55
    assert DEFAULT_CONFIG["yolo"]["notify_confidence_threshold"] == 0.55
    assert DEFAULT_CONFIG["yolo"]["person_confirmations"] == 2
    assert DEFAULT_CONFIG["yolo"]["pir_notify_confidence_threshold"] == 0.45
    assert DEFAULT_CONFIG["yolo"]["pir_person_confirmations"] == 1
    print("[OK] YOLO defaults: camera confirmations=2, PIR confirmations=1")


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
    assert profiles["apply_from_a12"] is False
    assert profiles["DAY"]["brightness"] == 0
    assert profiles["DUSK"]["brightness"] == 3
    print("[OK] Camera profiles: DAY/DUSK/NIGHT with correct values")


def test_camera_instance_env_overrides():
    """Verify one codebase can run as distinct camera instances."""
    import tempfile

    env = {
        "CAMERA_ID": "yard",
        "CAMERA_NAME": "Yard Camera",
        "CAMERA_LABEL": "Yard",
        "ESP32_IP": "192.168.1.101",
        "MQTT_BASE_TOPIC": "esp32_camera_yard",
        "MQTT_DISCOVERY_NODE": "esp32_camera_yard",
        "ESP32_MQTT_DEVICE": "ESP32-Camera-Yard",
    }
    old = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(tmpdir)
        assert config["camera_id"] == "yard"
        assert config["camera_name"] == "Yard Camera"
        assert config["telegram"]["camera_label"] == "Yard"
        assert config["camera_url"] == "http://192.168.1.101"
        assert config["mqtt"]["base_topic"] == "esp32_camera_yard"
        assert config["mqtt"]["discovery_node"] == "esp32_camera_yard"
        assert config["esp32_mqtt_device"] == "ESP32-Camera-Yard"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("[OK] Camera instance env overrides work")


def test_load_config_no_crash():
    """Verify load_config doesn't crash with empty directory."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        config = load_config(tmpdir)
        assert config["camera_url"] == "http://192.168.1.100"
        assert config["yolo"]["weights"] == "yolo11n.onnx"
        assert config["security"]["mode"] == "MONITOR"
        assert config["camera_id"] == "esp32_cam"
        assert config["camera_name"] == "ESP32 AI Camera"
    print("[OK] load_config works with empty directory (defaults only)")


if __name__ == "__main__":
    test_defaults()
    test_clip_defaults()
    test_external_trigger_defaults()
    test_event_scoring_defaults()
    test_adaptive_clip_defaults()
    test_media_cleanup_defaults()
    test_yolo_defaults()
    test_no_duplicate_flat_keys()
    test_security_config()
    test_camera_profiles()
    test_camera_instance_env_overrides()
    test_load_config_no_crash()
    print("\n=== All tests passed ===")
