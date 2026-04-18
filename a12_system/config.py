"""Configuration loading with environment variable overrides."""

import json
import os
from dotenv import load_dotenv


# --- Default Configuration ---

DEFAULT_CONFIG = {
    # Placeholder — override via ESP32_IP env var in config.env
    "camera_url": "http://192.168.1.100",
    "yolo": {
        "enabled": True,
        "weights": "yolo11n.onnx",
        "names": "coco.names",
        "classes": ["person", "bird", "cat", "dog"],
        "confidence_threshold": 0.3,
        "check_interval": 5,
    },
    "motion": {
        "enabled": True,
        "threshold": 50,
        "min_contour_area": 500,
    },
    "face_recognition": {
        "enabled": True,
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
    },
    "telegram_cooldown_seconds": 60,
    "detection_cooldown_seconds": 5,
    "periodic_yolo_interval": 30,
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
    },
    "audio_enabled": True,
    "audio": {
        "threshold_db": -25.0,
        "absolute_limit_db": -15.0,
        "cooldown_seconds": 60,
        "gain_reduction_db": 36,
    },
    "gif": {
        "enabled": True,
        "duration_seconds": 2,
        "fps": 10,
    },
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
        "DAY": {
            "ae_level": 0,
            "agc": 1,
            "gainceiling": 16,
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
            "gainceiling": 128,
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
            "gainceiling": 128,
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
            "lux_day_threshold": 250.0,
            "lux_dusk_threshold": 5.0,
            "poll_interval": 60,
        },
    },
    "camera_init_settings": {
        "frame_size": 13,       # UXGA (1600x1200)
        "jpeg_quality": 12,
        "aec": 1,
        "awb": 1,
        "denoise": 1,
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
    # Camera
    ("ESP32_IP", "camera_url", lambda v: f"http://{v}"),
    ("ESP32_STREAM_URL", "camera_url", lambda v: v.replace("/detection-stream", "").rstrip("/")),
    # YOLO
    ("YOLO_WEIGHTS", "yolo.weights", str),
    ("YOLO_CONFIDENCE_THRESHOLD", "yolo.confidence_threshold", _parse_float),
    ("YOLO_CLASSES", "yolo.classes", _parse_csv),
    # Motion
    ("MOTION_THRESHOLD", "motion.threshold", _parse_int),
    # MQTT
    ("MQTT_BROKER", "mqtt.broker_url", str),
    ("MQTT_PORT", "mqtt.port", _parse_int),
    ("MQTT_USERNAME", "mqtt.username", str),
    ("MQTT_PASSWORD", "mqtt.password", str),
    # Audio
    ("AUDIO_ENABLED", "audio_enabled", _parse_bool),
    # Home Assistant
    ("HOME_ASSISTANT_URL", "home_assistant_url", str),
    ("HOME_ASSISTANT_TOKEN", "home_assistant_token", str),
    # Debug
    ("DEBUG_DETECTION", "debug_detection", _parse_bool),
    ("LOG_LEVEL", "logging.level", lambda v: v.upper()),
    # Periodic YOLO
    ("PERIODIC_YOLO_INTERVAL", "periodic_yolo_interval", _parse_int),
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
