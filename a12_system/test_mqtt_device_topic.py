#!/usr/bin/env python3
"""The ESP32 MQTT topic must come from the camera, not from a guessed default."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system import mqtt_client
from a12_system.mqtt_client import MQTTClient


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _client(**extra):
    config = {"camera_url": "http://192.0.2.10", "mqtt": {"enabled": False}}
    config.update(extra)
    return MQTTClient(config)


def _no_dns(monkeypatch):
    monkeypatch.setattr(mqtt_client, "resolve_camera_url", lambda url, **kw: url)


def test_explicit_device_wins_and_camera_is_not_asked(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not call the camera when the name is configured")

    _no_dns(monkeypatch)
    monkeypatch.setattr(mqtt_client.requests, "get", boom)
    assert _client(esp32_mqtt_device="ESP32-Camera-Yard")._esp32_device_name() == "ESP32-Camera-Yard"


def test_device_name_is_read_from_health(monkeypatch):
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return _Resp({"device_name": "ESP32-Camera-a1b2c3d4"})

    _no_dns(monkeypatch)
    monkeypatch.setattr(mqtt_client.requests, "get", fake_get)
    assert _client()._esp32_device_name() == "ESP32-Camera-a1b2c3d4"
    assert seen["url"] == "http://192.0.2.10/health"


def test_falls_back_and_warns_when_camera_gives_no_name(monkeypatch, caplog):
    _no_dns(monkeypatch)
    monkeypatch.setattr(mqtt_client.requests, "get", lambda url, timeout: _Resp({"status": "ok"}))
    with caplog.at_level("WARNING"):
        assert _client()._esp32_device_name() == "ESP32-Camera"
    assert "ESP32_MQTT_DEVICE" in caplog.text


def test_falls_back_when_camera_is_unreachable(monkeypatch, caplog):
    def down(url, timeout):
        raise OSError("unreachable")

    _no_dns(monkeypatch)
    monkeypatch.setattr(mqtt_client.requests, "get", down)
    with caplog.at_level("WARNING"):
        assert _client()._esp32_device_name() == "ESP32-Camera"
    assert "did not report" in caplog.text
