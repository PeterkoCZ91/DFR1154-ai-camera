"""Small failure modes that used to go quiet: a token in a log line, a frame
size change that killed motion detection for good, an untyped MQTT value, a
failed send that ate the cooldown."""

import copy
import json
import logging
import math

import numpy as np
import pytest

from a12_system.config import DEFAULT_CONFIG
from a12_system.detection import crop_person_box_with_origin
from a12_system.logging_setup import _redact_bot_token
from a12_system.notifier import Notifier
from a12_system.runtime_config import RuntimeConfig
from a12_system.stats import Statistics
from a12_system.test_telegram_bot import FakeMessage, _make_service


# --- the bot token must not reach a log file ---------------------------------


def _record(msg, exc=None):
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, (), exc)
    _redact_bot_token(rec)
    return rec


def test_the_bot_token_is_redacted_from_the_message():
    rec = _record("Telegram error: HTTPSConnectionPool /bot123456:ABC-def_GH/sendMessage?chat_id=1")
    assert "ABC-def_GH" not in rec.getMessage()
    assert "bot<redacted>/sendMessage" in rec.getMessage()


def test_the_bot_token_is_redacted_from_a_traceback():
    try:
        raise RuntimeError("GET /bot123456:ABC-def_GH/getMe failed")
    except RuntimeError:
        import sys

        rec = _record("boom", sys.exc_info())
    assert "ABC-def_GH" not in logging.Formatter().format(rec)


# --- motion detection survives a resolution change ---------------------------


def test_motion_detection_recovers_after_a_resolution_change():
    from a12_system.detection import Detector

    d = Detector.__new__(Detector)
    d.config = {"motion": {"enabled": True, "threshold": 25, "min_contour_area": 10,
                           "min_consecutive_frames": 1}}
    d.previous_frame_gray = None
    d.motion_streak = 0
    small = np.zeros((240, 320, 3), np.uint8)
    large = np.zeros((480, 640, 3), np.uint8)
    d.detect_motion(small)
    d.detect_motion(large)  # used to raise cv2.error, and again on every later frame
    d.detect_motion(large)
    assert d.previous_frame_gray.shape == (480, 640)


def test_an_infinite_scorer_box_falls_back_to_the_whole_frame():
    frame = np.zeros((100, 100, 3), np.uint8)
    crop, origin = crop_person_box_with_origin(frame, (0, 0, math.inf, 90))
    assert crop is frame and origin == (0, 0)


# --- MQTT values are untyped -------------------------------------------------


def _rc():
    return RuntimeConfig(copy.deepcopy(DEFAULT_CONFIG))


@pytest.mark.parametrize(
    "key, payload",
    [
        ("yolo_confidence", "NaN"),
        ("motion_sensitivity", "Infinity"),
        ("motion_sensitivity", "-5"),
        ("yolo_confidence", "abc"),
        ("telegram_cooldown", "abc"),
        ("telegram_cooldown", "true"),
        ("person_detection", "maybe"),
        ("person_detection", "5"),
    ],
)
def test_untyped_mqtt_values_are_rejected_and_change_nothing(key, payload):
    rc = _rc()
    before = rc.get_all()
    assert rc.update_from_mqtt(f"camera/config/set/{key}", payload) is False
    assert rc.get_all() == before


def test_sane_mqtt_values_still_apply():
    rc = _rc()
    assert rc.update_from_mqtt("camera/config/set/yolo_confidence", "0.5")
    assert rc.update_from_mqtt("camera/config/set/person_detection", "off")
    assert rc.get("yolo.confidence_threshold") == 0.5
    assert rc.get("security.person_detection") is False


# --- /mute has a ceiling -----------------------------------------------------


def test_mute_is_capped_at_a_day():
    service, bot, notifier = _make_service()
    import time

    service.handle_mute(FakeMessage(text="/mute 99999999"))
    assert notifier._muted_until <= time.time() + 24 * 3600 + 5
    assert "1440" in bot.sent_messages[-1]["text"]


# --- stats.json is replaced, not truncated ----------------------------------


def test_stats_save_leaves_no_partial_file(tmp_path):
    path = tmp_path / "stats.json"
    s = Statistics(save_path=str(path), auto_save_interval=3600)
    assert s.save() is True
    assert json.loads(path.read_text())["cumulative"]
    assert not (tmp_path / "stats.json.tmp").exists()


# --- a failed send does not eat the cooldown ---------------------------------


class _Bot:
    def __init__(self, fail_media=False, fail_all=False):
        self.fail_media, self.fail_all = fail_media, fail_all
        self.texts, self.photos = [], []

    def send_message(self, chat_id, text, **kw):
        if self.fail_all:
            raise RuntimeError("network down")
        self.texts.append(text)

    def send_photo(self, chat_id, f, caption=None, **kw):
        if self.fail_media or self.fail_all:
            raise RuntimeError("rejected")
        self.photos.append(caption)


def _notifier(bot):
    n = Notifier({"telegram": {"enabled": False}, "telegram_cooldown_seconds": 60})
    n.bot, n.chat_id = bot, "1"
    return n


def test_a_failed_send_does_not_start_the_cooldown():
    bot = _Bot(fail_all=True)
    n = _notifier(bot)
    assert n.send_telegram("first") is False
    bot.fail_all = False
    assert n.send_telegram("second") is True


def test_rejected_media_still_delivers_the_text(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    bot = _Bot(fail_media=True)
    n = _notifier(bot)
    assert n.send_telegram("Person at the door", media_path=str(img)) is True
    assert bot.texts == ["Person at the door"]


def test_long_text_and_captions_are_cut_to_telegram_limits(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")
    bot = _Bot()
    n = _notifier(bot)
    n.send_telegram("x" * 5000, bypass_cooldown=True)
    n.send_telegram("y" * 2000, media_path=str(img), bypass_cooldown=True)
    assert len(bot.texts[0]) == 4096
    assert len(bot.photos[0]) == 1024
