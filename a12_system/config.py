"""Configuration loading with environment variable overrides."""

import json
import os
from dotenv import load_dotenv


# --- Default Configuration ---

DEFAULT_CONFIG = {
    # Placeholder — override via ESP32_IP env var in config.env
    "camera_id": "esp32_cam",
    "camera_name": "ESP32 AI Camera",
    "camera_url": "http://192.168.1.100",
    "camera_http_user": "admin",
    "camera_http_pass": "admin",
    "yolo": {
        "enabled": True,
        "weights": "yolo11n.onnx",
        "names": "coco.names",
        "classes": ["person", "bird", "cat", "dog"],
        "confidence_threshold": 0.55,
        "notify_confidence_threshold": 0.55,
        "person_confirmations": 2,
        "pir_notify_confidence_threshold": 0.45,
        "pir_person_confirmations": 1,
        "check_interval": 5,
    },
    "motion": {
        "enabled": True,
        "threshold": 50,
        "min_contour_area": 500,
    },
    "face_recognition": {
        "enabled": False,
        "tolerance": 0.6,
        "known_faces_paths": ["known_faces.pkl"],
        # Names to skip Telegram notifications for (household members).
        # Populated at runtime from known_faces.pkl + user config.
        "whitelisted_names": [],
    },
    "telegram": {
        "enabled": False,
        "token": None,
        "chat_id": None,
        "camera_label": "ESP32 AI Camera",
        "media_mode": "mp4",  # mp4, preview_mp4, snapshot, text, none
        "preview_seconds": 4,
        "preview_fps": 4,
    },
    "telegram_cooldown_seconds": 60,
    "detection_cooldown_seconds": 5,
    "periodic_yolo_interval": 30,
    "external_trigger_yolo_window_seconds": 15.0,
    "external_trigger_yolo_interval_seconds": 2.0,
    "stream_idle_mode_enabled": False,
    "stream_idle_poll_seconds": 1.0,
    "stream_idle_decode_fps": 2,
    "stream_active_decode_fps": 10,
    "mqtt": {
        "enabled": True,
        # Placeholder — override via MQTT_BROKER env var in config.env
        "broker_url": "192.168.1.10",
        "port": 1883,
        "username": None,
        "password": None,
        "base_topic": "esp32_camera",
        "discovery": True,
        "discovery_prefix": "homeassistant",
        "discovery_node": None,
    },
    "audio_enabled": True,
    "audio": {
        "threshold_db": -25.0,
        "absolute_limit_db": -15.0,
        "cooldown_seconds": 60,
        "gain_reduction_db": 36,
        "buffer_seconds": 20,
        "analysis_interval_seconds": 0.25,
    },
    "gif": {
        "enabled": True,
        "duration_seconds": 5,
        "fps": 10,
    },
    "clip_pre_seconds": 5,
    "clip_post_seconds": 15,
    "clip_buffer_fps": 5,
    "clip_frame_width": 640,
    "clip_frame_height": 480,
    "require_sensor_for_recording": True,
    "pir_recording": {
        "enabled": True,
        "label": "motion",
        "send_telegram": True,
        "bypass_cooldown": True,
        "cooldown_seconds": 30,
    },
    "adaptive_clip": {
        "enabled": True,
        "idle_seconds": 10,
        "max_post_seconds": 60,
    },
    "event_scoring": {
        "enabled": True,
        "notify_threshold": 70,
        "local_record_threshold": 45,
        "yolo_person_score": 55,
        "sensor_active_score": 45,
        "pir_trigger_score": 15,
        "confirmation_score": 15,
        "confidence_bonus_score": 25,
    },
    "media_retention_days": 2,
    "media_cleanup_interval_seconds": 3600,
    "notification_queue_maxsize": 10,
    "ir_control": {
        "enabled": False,
        "mqtt_publish": True,
        "poll_interval": 10,
    },
    "home_assistant_url": "http://homeassistant.local:8123",
    "home_assistant_token": None,
    "home_assistant_entity_id": "binary_sensor.security_doorbell",
    "ha_monitored_sensors": [],
    "debug_detection": False,
    "logging": {"level": "INFO"},
    "security": {
        "mode": "MONITOR",
        "person_detection": True,
        "animal_detection": True,
        "require_sensor_confirmation": False,
        "sabotage_detection": False,
        "sabotage_timeout": 30.0,
        "sensor_fusion_window_seconds": 10.0,
    },
    "camera_profiles": {
        "apply_from_a12": False,
        "DAY": {
            "ae_level": 0,
            "agc": 1,
            "gainceiling": 3,   # enum: 0=2x,1=4x,2=8x,3=16x,4=32x,5=64x,6=128x
            "aec": 1,
            "awb": 1,
            "wb_mode": 0,
            "brightness": 0,
            "contrast": 0,
            "saturation": 1,
            "sharpness": 1,
            "denoise": 1,
            "lenc": 1,
        },
        "DUSK": {
            "ae_level": 5,
            "agc": 1,
            "gainceiling": 6,   # 128x — max gain ceiling for low light
            "aec": 1,
            "aec2": 1,
            "awb": 1,
            "wb_mode": 0,
            "brightness": 3,
            "contrast": 1,
            "saturation": 0,
            "sharpness": 0,
            "denoise": 4,
        },
        "NIGHT": {
            "ae_level": 5,
            "agc": 0,
            "gainceiling": 6,   # 128x — max gain ceiling for low light
            "aec": 1,
            "aec2": 1,
            "awb": 1,
            "wb_mode": 0,
            "brightness": 3,
            "contrast": 1,
            "saturation": 0,
            "sharpness": 0,
            "denoise": 4,
            "lenc": 0,
        },
        "thresholds": {
            "lux_day_threshold": 30.0,
            "lux_dusk_threshold": 2.0,
            "poll_interval": 60,
        },
    },
    "camera_init_settings": {
        "frame_size": 13,       # UXGA (1600x1200)
        "jpeg_quality": 8,      # lower = better quality; 8 gives YOLO better source data than 12
        "aec": 1,
        "awb": 1,
        "denoise": 1,
        "contrast": 0,
        "saturation": 0,
        "sharpness": 0,
        "brightness": 0,
    },
}


# --- Environment Variable Override Mapping ---

def _parse_bool(v: str) -> bool:
    return v.lower() in ("true", "1", "yes", "on")

def _parse_int(v: str) -> int:
    return int(v)

def _parse_float(v: str) -> float:
    return float(v)

def _parse_csv(v: str) -> list:
    return [c.strip() for c in v.split(",") if c.strip()]


# (env_var, config_path, parser)
ENV_OVERRIDES = [
    # Telegram
    ("TELEGRAM_ENABLED", "telegram.enabled", _parse_bool),
    ("TELEGRAM_BOT_TOKEN", "telegram.token", str),
    ("TELEGRAM_CHAT_ID", "telegram.chat_id", str),
    ("TELEGRAM_MEDIA_MODE", "telegram.media_mode", str),
    ("TELEGRAM_PREVIEW_SECONDS", "telegram.preview_seconds", _parse_int),
    ("TELEGRAM_PREVIEW_FPS", "telegram.preview_fps", _parse_int),
    ("TELEGRAM_PREVIEW_WIDTH", "telegram_preview_width", _parse_int),
    ("TELEGRAM_PREVIEW_HEIGHT", "telegram_preview_height", _parse_int),
    # Camera
    ("CAMERA_ID", "camera_id", str),
    ("CAMERA_NAME", "camera_name", str),
    ("CAMERA_LABEL", "telegram.camera_label", str),
    ("ESP32_IP", "camera_url", lambda v: f"http://{v}"),
    ("ESP32_HTTP_USER", "camera_http_user", str),
    ("ESP32_HTTP_PASS", "camera_http_pass", str),
    ("ESP32_MQTT_DEVICE", "esp32_mqtt_device", str),
    # YOLO
    ("YOLO_WEIGHTS", "yolo.weights", str),
    ("YOLO_CONFIDENCE_THRESHOLD", "yolo.confidence_threshold", _parse_float),
    ("YOLO_NOTIFY_CONFIDENCE_THRESHOLD", "yolo.notify_confidence_threshold", _parse_float),
    ("YOLO_PERSON_CONFIRMATIONS", "yolo.person_confirmations", _parse_int),
    ("YOLO_PIR_NOTIFY_CONFIDENCE_THRESHOLD", "yolo.pir_notify_confidence_threshold", _parse_float),
    ("YOLO_PIR_PERSON_CONFIRMATIONS", "yolo.pir_person_confirmations", _parse_int),
    ("YOLO_CLASSES", "yolo.classes", _parse_csv),
    # Motion
    ("MOTION_THRESHOLD", "motion.threshold", _parse_int),
    # Face recognition
    ("FACE_RECOGNITION_ENABLED", "face_recognition.enabled", _parse_bool),
    # MQTT
    ("MQTT_BROKER", "mqtt.broker_url", str),
    ("MQTT_PORT", "mqtt.port", _parse_int),
    ("MQTT_USERNAME", "mqtt.username", str),
    ("MQTT_PASSWORD", "mqtt.password", str),
    ("MQTT_BASE_TOPIC", "mqtt.base_topic", str),
    ("MQTT_DISCOVERY_NODE", "mqtt.discovery_node", str),
    # Audio
    ("AUDIO_ENABLED", "audio_enabled", _parse_bool),
    ("AUDIO_BUFFER_SECONDS", "audio.buffer_seconds", _parse_int),
    ("AUDIO_ANALYSIS_INTERVAL_SECONDS", "audio.analysis_interval_seconds", _parse_float),
    # Media clips
    ("GIF_DURATION_SECONDS", "gif.duration_seconds", _parse_int),
    ("GIF_FPS", "gif.fps", _parse_int),
    ("CLIP_PRE_SECONDS", "clip_pre_seconds", _parse_int),
    ("CLIP_POST_SECONDS", "clip_post_seconds", _parse_int),
    ("CLIP_BUFFER_FPS", "clip_buffer_fps", _parse_int),
    ("CLIP_FRAME_WIDTH", "clip_frame_width", _parse_int),
    ("CLIP_FRAME_HEIGHT", "clip_frame_height", _parse_int),
    ("REQUIRE_SENSOR_FOR_RECORDING", "require_sensor_for_recording", _parse_bool),
    ("PIR_RECORDING_ENABLED", "pir_recording.enabled", _parse_bool),
    ("PIR_RECORDING_LABEL", "pir_recording.label", str),
    ("PIR_RECORDING_SEND_TELEGRAM", "pir_recording.send_telegram", _parse_bool),
    ("PIR_RECORDING_BYPASS_COOLDOWN", "pir_recording.bypass_cooldown", _parse_bool),
    ("PIR_RECORDING_COOLDOWN_SECONDS", "pir_recording.cooldown_seconds", _parse_int),
    ("ADAPTIVE_CLIP_ENABLED", "adaptive_clip.enabled", _parse_bool),
    ("ADAPTIVE_CLIP_IDLE_SECONDS", "adaptive_clip.idle_seconds", _parse_int),
    ("ADAPTIVE_CLIP_MAX_POST_SECONDS", "adaptive_clip.max_post_seconds", _parse_int),
    ("EVENT_SCORE_NOTIFY_THRESHOLD", "event_scoring.notify_threshold", _parse_int),
    ("EVENT_SCORE_LOCAL_RECORD_THRESHOLD", "event_scoring.local_record_threshold", _parse_int),
    ("MEDIA_RETENTION_DAYS", "media_retention_days", _parse_float),
    ("MEDIA_CLEANUP_INTERVAL_SECONDS", "media_cleanup_interval_seconds", _parse_int),
    ("NOTIFICATION_QUEUE_MAXSIZE", "notification_queue_maxsize", _parse_int),
    # Home Assistant
    ("HOME_ASSISTANT_URL", "home_assistant_url", str),
    ("HOME_ASSISTANT_TOKEN", "home_assistant_token", str),
    # Debug
    ("DEBUG_DETECTION", "debug_detection", _parse_bool),
    ("LOG_LEVEL", "logging.level", lambda v: v.upper()),
    # Periodic YOLO
    ("PERIODIC_YOLO_INTERVAL", "periodic_yolo_interval", _parse_int),
    ("EXTERNAL_TRIGGER_YOLO_WINDOW_SECONDS", "external_trigger_yolo_window_seconds", _parse_float),
    ("EXTERNAL_TRIGGER_YOLO_INTERVAL_SECONDS", "external_trigger_yolo_interval_seconds", _parse_float),
    ("STREAM_IDLE_MODE_ENABLED", "stream_idle_mode_enabled", _parse_bool),
    ("STREAM_IDLE_POLL_SECONDS", "stream_idle_poll_seconds", _parse_float),
    ("STREAM_IDLE_DECODE_FPS", "stream_idle_decode_fps", _parse_int),
    ("STREAM_ACTIVE_DECODE_FPS", "stream_active_decode_fps", _parse_int),
    ("A12_APPLY_CAMERA_PROFILES", "camera_profiles.apply_from_a12", _parse_bool),
]


def _deep_update(base: dict, override: dict) -> dict:
    """Deep merge override into base dict."""
    for k, v in override.items():
        if isinstance(v, dict):
            base[k] = _deep_update(base.get(k, {}), v)
        else:
            base[k] = v
    return base


def _set_nested(config: dict, path: str, value) -> None:
    """Set a value in a nested dict using dot-notation path."""
    keys = path.split(".")
    d = config
    for k in keys[:-1]:
        if k not in d:
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def load_config(script_dir: str) -> dict:
    """Load configuration from config.json + environment variables.

    Priority: ENV > config.json > DEFAULT_CONFIG
    """
    # 1. Load .env file
    for env_name in (".env", "config.env"):
        env_file = os.path.join(script_dir, env_name)
        if os.path.exists(env_file):
            load_dotenv(env_file)
            print(f"Loaded environment from {env_file}")
            break

    # 2. Start with defaults
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)

    # 3. Merge config.json if exists
    config_path = os.path.join(script_dir, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                loaded = json.load(f)
            _deep_update(config, loaded)
            print(f"Config loaded from {config_path}")
        except Exception as e:
            print(f"Error loading config.json: {e}")

    # 4. Apply environment variable overrides
    for env_var, config_path_str, parser in ENV_OVERRIDES:
        env_val = os.environ.get(env_var)
        if env_val is not None:
            try:
                _set_nested(config, config_path_str, parser(env_val))
            except (ValueError, TypeError) as e:
                print(f"Warning: Failed to parse {env_var}={env_val}: {e}")

    # 5. Auto-enable Telegram if token+chat_id provided
    if config["telegram"]["token"] and config["telegram"]["chat_id"]:
        config["telegram"]["enabled"] = True

    return config
