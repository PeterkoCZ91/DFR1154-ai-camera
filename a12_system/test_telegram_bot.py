"""Unit tests for TelegramBotService and Telegram visitor interaction."""

import os
import sys
import time
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system.notifier import Notifier, make_visitor_keyboard
from a12_system.telegram_bot import TelegramBotService


class FakeUser:
    def __init__(self, user_id: int = 12345, first_name: str = "TestUser"):
        self.id = user_id
        self.first_name = first_name


class FakeChat:
    def __init__(self, chat_id: int = 12345):
        self.id = chat_id


class FakeMessage:
    def __init__(
        self,
        text: str = "",
        chat_id: int = 12345,
        user_id: int = None,
        photo=None,
        reply_to_message=None,
        caption: str = None,
        message_id: int = 100,
    ):
        self.text = text
        self.caption = caption
        self.chat = FakeChat(chat_id)
        effective_user_id = user_id if user_id is not None else chat_id
        self.from_user = FakeUser(effective_user_id)
        self.photo = photo
        self.reply_to_message = reply_to_message
        self.message_id = message_id


class FakeCallbackQuery:
    def __init__(
        self,
        data: str = "",
        chat_id: int = 12345,
        user_id: int = None,
        message_id: int = 200,
        first_name: str = "Alice",
    ):
        self.id = "cb_query_1"
        self.data = data
        effective_user_id = user_id if user_id is not None else chat_id
        self.from_user = FakeUser(effective_user_id, first_name=first_name)
        self.message = FakeMessage(chat_id=chat_id, user_id=effective_user_id, message_id=message_id)


class FakeBot:
    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.answered_callbacks: list[dict] = []
        self.edited_markups: list[dict] = []
        self.files_downloaded: list[str] = []
        self.registered_handlers: list[dict] = []
        self.registered_callback_handlers: list[dict] = []
        self.registered_commands: list = []
        self.polling_started = False
        self.polling_stopped = False

    def set_my_commands(self, commands, **kwargs):
        self.registered_commands = commands
        return True

    def register_message_handler(self, handler, **kwargs):
        self.registered_handlers.append({"handler": handler, **kwargs})

    def register_callback_query_handler(self, handler, **kwargs):
        self.registered_callback_handlers.append({"handler": handler, **kwargs})

    def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return True

    def send_photo(self, chat_id, photo, **kwargs):
        self.sent_photos.append({"chat_id": chat_id, "photo": photo, **kwargs})
        return True

    def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        self.answered_callbacks.append({"id": callback_query_id, "text": text, **kwargs})
        return True

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        self.edited_markups.append(
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup, **kwargs}
        )
        return True

    def get_file(self, file_id):
        m = MagicMock()
        m.file_path = f"photos/{file_id}.jpg"
        return m

    def download_file(self, file_path):
        import cv2

        self.files_downloaded.append(file_path)
        dummy_img = np.zeros((100, 100, 3), dtype=np.uint8)
        _, buf = cv2.imencode(".jpg", dummy_img)
        return buf.tobytes()

    def infinity_polling(self, **kwargs):
        self.polling_started = True
        while not self.polling_stopped:
            time.sleep(0.01)

    def stop_polling(self):
        self.polling_stopped = True


def _make_service(chat_id=12345, bot=None, pipeline=None, notifier=None):
    cfg = {
        "telegram": {
            "enabled": True,
            "token": "fake_token",
            "chat_id": chat_id,
        }
    }
    if notifier is None:
        notifier = Notifier(cfg)
    if bot is None:
        bot = FakeBot()
    notifier.bot = bot
    notifier.chat_id = str(chat_id)
    service = TelegramBotService(cfg, notifier=notifier, pipeline=pipeline)
    service.bot = bot
    service._register_handlers()
    return service, bot, notifier


# 1. Test unauthorized chat ID rejection
def test_unauthorized_chat_id_rejection():
    service, bot, _ = _make_service(chat_id="12345")

    # Authorized message
    msg_auth = FakeMessage(text="/help", chat_id=12345)
    assert service._authorized(msg_auth) is True

    # Unauthorized message
    msg_unauth = FakeMessage(text="/help", chat_id=99999)
    assert service._authorized(msg_unauth) is False

    # Calling handler with unauthorized message should drop it
    service.handle_help(msg_unauth)
    assert len(bot.sent_messages) == 0

    service.handle_unlock(msg_unauth)
    assert len(bot.sent_messages) == 0

    service.handle_lock(msg_unauth)
    assert len(bot.sent_messages) == 0

    service.handle_photo(msg_unauth)
    assert len(bot.sent_photos) == 0
    assert len(bot.sent_messages) == 0


def test_allowed_chat_ids_formats():
    # Test int, list, and comma-separated string
    s1, _, _ = _make_service(chat_id=98765)
    assert "98765" in s1.allowed_chat_ids

    s2, _, _ = _make_service(chat_id=[111, "222", 333])
    assert s2.allowed_chat_ids == {"111", "222", "333"}

    s3, _, _ = _make_service(chat_id="444, 555, 666")
    assert s3.allowed_chat_ids == {"444", "555", "666"}


# 2. Test /help output
def test_help_output():
    service, bot, _ = _make_service()
    msg = FakeMessage(text="/help")
    service.handle_help(msg)

    assert len(bot.sent_messages) == 1
    sent = bot.sent_messages[0]
    assert sent["chat_id"] == "12345"
    assert "/photo" in sent["text"]
    assert "/unlock" in sent["text"]
    assert "/od" in sent["text"]
    assert "/lock" in sent["text"]
    assert "/za" in sent["text"]
    assert "/status" in sent["text"]
    assert "/mute" in sent["text"]
    assert "/unmute" in sent["text"]
    assert "/enroll" in sent["text"]


# 3. Test /photo sends image when frame is available
def test_photo_sends_image_when_frame_available():
    pipeline = MagicMock()
    dummy_frame = np.zeros((120, 160, 3), dtype=np.uint8)
    pipeline.latest_frame = dummy_frame

    service, bot, _ = _make_service(pipeline=pipeline)
    msg = FakeMessage(text="/photo")
    service.handle_photo(msg)

    assert len(bot.sent_photos) == 1
    photo_entry = bot.sent_photos[0]
    assert photo_entry["chat_id"] == "12345"
    assert isinstance(photo_entry["photo"], bytes)
    assert len(photo_entry["photo"]) > 0
    assert "Aktuální snímek z kamery" in photo_entry["caption"]


def test_photo_when_no_frame_available():
    pipeline = MagicMock()
    pipeline.latest_frame = None
    pipeline.camera = None

    service, bot, _ = _make_service(pipeline=pipeline)
    msg = FakeMessage(text="/photo")
    service.handle_photo(msg)

    assert len(bot.sent_photos) == 0
    assert len(bot.sent_messages) == 1
    assert "k dispozici" in bot.sent_messages[0]["text"]


# 4. Test /unlock, /odemknout, /od call _trigger_nuki_unlock
def test_unlock_calls_trigger_nuki_unlock():
    pipeline = MagicMock()
    service, bot, _ = _make_service(pipeline=pipeline)

    for cmd in ["/unlock", "/odemknout", "/od"]:
        pipeline.reset_mock()
        bot.sent_messages.clear()
        msg = FakeMessage(text=cmd)
        service.handle_unlock(msg)

        pipeline._trigger_nuki_unlock.assert_called_once_with("Telegram Admin")
        assert len(bot.sent_messages) == 1
        assert "odemknout" in bot.sent_messages[0]["text"].lower() or "odemknutí" in bot.sent_messages[0]["text"].lower()


# 4b. Test /lock, /zamknout, /za call _trigger_nuki_lock
def test_lock_calls_trigger_nuki_lock():
    pipeline = MagicMock()
    service, bot, _ = _make_service(pipeline=pipeline)

    for cmd in ["/lock", "/zamknout", "/za"]:
        pipeline.reset_mock()
        bot.sent_messages.clear()
        msg = FakeMessage(text=cmd)
        service.handle_lock(msg)

        pipeline._trigger_nuki_lock.assert_called_once_with("Telegram Admin")
        assert len(bot.sent_messages) == 1
        assert "zamknutí" in bot.sent_messages[0]["text"].lower() or "zamknout" in bot.sent_messages[0]["text"].lower()


# 5. Test /mute and /unmute correctly update mute state
def test_mute_and_unmute_updates_mute_state():
    service, bot, notifier = _make_service()

    assert notifier.is_muted() is False

    # Default /mute -> 15 min
    msg_mute = FakeMessage(text="/mute")
    service.handle_mute(msg_mute)

    assert notifier.is_muted() is True
    assert notifier._muted_until > time.time() + 14 * 60
    assert len(bot.sent_messages) == 1
    assert "15 minut" in bot.sent_messages[0]["text"]

    # Explicit /mute 45 -> 45 min
    msg_mute_45 = FakeMessage(text="/mute 45")
    service.handle_mute(msg_mute_45)
    assert notifier._muted_until > time.time() + 44 * 60
    assert "45 minut" in bot.sent_messages[1]["text"]

    # /unmute
    msg_unmute = FakeMessage(text="/unmute")
    service.handle_unmute(msg_unmute)

    assert notifier.is_muted() is False
    assert len(bot.sent_messages) == 3
    assert "zrušeno" in bot.sent_messages[2]["text"]


# 6. Test callback query unlock_door and mute_15m
def test_callback_query_unlock_door():
    pipeline = MagicMock()
    service, bot, _ = _make_service(pipeline=pipeline)

    call = FakeCallbackQuery(data="unlock_door", first_name="Karel")
    service.handle_callback_query(call)

    pipeline._trigger_nuki_unlock.assert_called_once_with("Telegram Callback")
    assert len(bot.answered_callbacks) == 1
    assert "Dveře se odemykají" in bot.answered_callbacks[0]["text"]
    assert len(bot.edited_markups) == 1
    markup = bot.edited_markups[0]["reply_markup"]
    assert "Odemčeno uživatelem Karel" in str(markup.to_dict() if hasattr(markup, "to_dict") else markup)


def test_callback_query_lock_door():
    pipeline = MagicMock()
    service, bot, _ = _make_service(pipeline=pipeline)

    call = FakeCallbackQuery(data="lock_door", first_name="Karel")
    service.handle_callback_query(call)

    pipeline._trigger_nuki_lock.assert_called_once_with("Telegram Callback")
    assert len(bot.answered_callbacks) == 1
    assert "Dveře se zamykají" in bot.answered_callbacks[0]["text"]
    assert len(bot.edited_markups) == 1
    markup = bot.edited_markups[0]["reply_markup"]
    assert "Zamčeno uživatelem Karel" in str(markup.to_dict() if hasattr(markup, "to_dict") else markup)


def test_callback_query_mute_15m():
    service, bot, notifier = _make_service()

    call = FakeCallbackQuery(data="mute_15m")
    service.handle_callback_query(call)

    assert notifier.is_muted() is True
    assert len(bot.answered_callbacks) == 1
    assert "15 minut" in bot.answered_callbacks[0]["text"]
    assert len(bot.edited_markups) == 1


def test_callback_query_unauthorized():
    pipeline = MagicMock()
    service, bot, _ = _make_service(pipeline=pipeline, chat_id=12345)

    call_unauth = FakeCallbackQuery(data="unlock_door", chat_id=99999, user_id=99999)
    service.handle_callback_query(call_unauth)

    pipeline._trigger_nuki_unlock.assert_not_called()
    assert len(bot.answered_callbacks) == 1
    assert "Neautorizovaný" in bot.answered_callbacks[0]["text"]

    call_unauth_lock = FakeCallbackQuery(data="lock_door", chat_id=99999, user_id=99999)
    service.handle_callback_query(call_unauth_lock)
    pipeline._trigger_nuki_lock.assert_not_called()


# 7. Test status overview
def test_status_overview():
    pipeline = MagicMock()
    pipeline.running = True
    pipeline.shared_state = {"pipeline_state": "idle"}
    pipeline.stream_fps = 9.8
    pipeline.detector.known_face_names = ["Alice", "Bob"]
    pipeline.db.get_recent_events.return_value = [
        {"event_type": "motion", "label": "door", "timestamp": "2026-10-18 12:00:00"}
    ]

    service, bot, notifier = _make_service(pipeline=pipeline)
    msg = FakeMessage(text="/status")
    service.handle_status(msg)

    assert len(bot.sent_messages) == 1
    status_text = bot.sent_messages[0]["text"]
    assert "idle" in status_text
    assert "9.8" in status_text
    assert "Alice" in status_text
    assert "Bob" in status_text
    assert "motion: door" in status_text
    assert "Aktivní" in status_text


# 8. Test /enroll face
def test_enroll_face_workflow(tmp_path):
    pipeline = MagicMock()
    pipeline.script_dir = str(tmp_path)
    detector = MagicMock()
    detector.models_dir = str(tmp_path)
    detector.known_face_encodings = []
    detector.known_face_names = []
    detector._gallery_paths.return_value = [str(tmp_path / "known_faces_sface.pkl")]

    face_backend = MagicMock()
    dummy_face = np.array([0, 0, 100, 100, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.95])
    dummy_embed = np.ones(128, dtype=np.float32)
    face_backend.detect_and_embed.return_value = [(dummy_face, dummy_embed)]
    detector.face_backend = face_backend
    pipeline.detector = detector

    service, bot, _ = _make_service(pipeline=pipeline)

    photo_item = MagicMock()
    photo_item.file_id = "test_photo_id"
    msg = FakeMessage(text="/enroll Eva", photo=[photo_item])

    service.handle_enroll(msg)

    assert len(detector.known_face_names) == 1
    assert detector.known_face_names[0] == "Eva"
    assert len(detector.known_face_encodings) == 1
    assert os.path.exists(tmp_path / "known_faces_sface.pkl")
    assert len(bot.sent_messages) == 1
    assert "Eva" in bot.sent_messages[0]["text"]


# 9. Test make_visitor_keyboard and send_telegram reply_markup
def test_make_visitor_keyboard_and_reply_markup():
    kb = make_visitor_keyboard()
    assert kb is not None
    d = kb.to_dict()
    buttons = [btn for row in d["inline_keyboard"] for btn in row]
    callbacks = [b["callback_data"] for b in buttons]
    assert "unlock_door" in callbacks
    assert "mute_15m" in callbacks


def test_notifier_mute_suppresses_send_telegram():
    fake_bot = FakeBot()
    cfg = {"telegram": {"enabled": True, "chat_id": "12345"}}
    notifier = Notifier(cfg)
    notifier.bot = fake_bot
    notifier.chat_id = "12345"

    # Send while not muted
    assert notifier.send_telegram("Alert 1", bypass_cooldown=True) is True
    assert len(fake_bot.sent_messages) == 1

    # Mute for 60s
    notifier.mute_until(time.time() + 60)
    assert notifier.is_muted() is True

    # Send while muted should be suppressed
    assert notifier.send_telegram("Alert 2", bypass_cooldown=True) is False
    assert len(fake_bot.sent_messages) == 1

    # Unmute
    notifier.mute_until(0)
    assert notifier.is_muted() is False
    assert notifier.send_telegram("Alert 3", bypass_cooldown=True) is True
    assert len(fake_bot.sent_messages) == 2


# 10. Test start and stop thread management
def test_service_start_and_stop():
    service, bot, _ = _make_service()
    service.start()
    assert service._running is True
    assert bot.polling_started is True
    service.stop()
    assert service._running is False
    assert bot.polling_stopped is True


def test_service_start_registers_commands_menu():
    service, bot, _ = _make_service()
    service.start()
    try:
        assert len(bot.registered_commands) == 7
        expected = [
            ("od", "🔓 Odemknout dveře (střelka)"),
            ("za", "🔒 Zamknout dveře"),
            ("foto", "📷 Aktuální snímek z kamery"),
            ("stav", "📊 Celkový stav systému"),
            ("mute", "🔕 Ztišit notifikace"),
            ("unmute", "🔔 Obnovit notifikace"),
            ("help", "❓ Nápověda k příkazům"),
        ]
        actual = [(cmd.command, cmd.description) for cmd in bot.registered_commands]
        assert actual == expected
    finally:
        service.stop()


def test_service_register_commands_menu_handles_exception():
    service, bot, _ = _make_service()
    bot.set_my_commands = MagicMock(side_effect=RuntimeError("API network error"))
    # Should not raise exception
    service._register_commands_menu()


# 11. Test graceful error handling
def test_graceful_handling_when_pipeline_is_none():
    service, bot, _ = _make_service(pipeline=None)

    # None of these should raise
    service.handle_photo(FakeMessage(text="/photo"))
    assert len(bot.sent_messages) == 1
    assert "k dispozici" in bot.sent_messages[0]["text"]

    service.handle_unlock(FakeMessage(text="/unlock"))
    assert len(bot.sent_messages) == 2
    assert "není" in bot.sent_messages[1]["text"]

    service.handle_lock(FakeMessage(text="/lock"))
    assert len(bot.sent_messages) == 3
    assert "není" in bot.sent_messages[2]["text"]

    service.handle_status(FakeMessage(text="/status"))
    assert len(bot.sent_messages) == 4
    assert "neaktivní" in bot.sent_messages[3]["text"]


def test_graceful_handling_when_telebot_raises_error():
    service, bot, _ = _make_service()

    # Configure bot to raise exception on calls
    bot.send_message = MagicMock(side_effect=RuntimeError("Telegram network failure"))
    bot.send_photo = MagicMock(side_effect=RuntimeError("Telegram photo upload timeout"))
    bot.answer_callback_query = MagicMock(side_effect=RuntimeError("Telegram callback expired"))

    # Handlers should catch the exceptions and log error without bubbling up
    service.handle_help(FakeMessage(text="/help"))
    service.handle_status(FakeMessage(text="/status"))
    service.handle_unlock(FakeMessage(text="/unlock"))
    service.handle_lock(FakeMessage(text="/lock"))
    service.handle_mute(FakeMessage(text="/mute"))
    service.handle_unmute(FakeMessage(text="/unmute"))
    service.handle_callback_query(FakeCallbackQuery(data="unlock_door"))
    service.handle_callback_query(FakeCallbackQuery(data="lock_door"))


def test_registered_command_aliases():
    bot = FakeBot()
    service, _, _ = _make_service(bot=bot)
    commands_map = {tuple(h["commands"]): h["handler"] for h in bot.registered_handlers if "commands" in h}

    unlock_cmds = next(cmds for cmds in commands_map if "unlock" in cmds)
    assert set(unlock_cmds) == {"unlock", "odemknout", "od"}
    assert commands_map[unlock_cmds] == service.handle_unlock

    lock_cmds = next(cmds for cmds in commands_map if "lock" in cmds)
    assert set(lock_cmds) == {"lock", "zamknout", "za"}
    assert commands_map[lock_cmds] == service.handle_lock


def test_send_telegram_passes_reply_markup():
    fake_bot = FakeBot()
    cfg = {"telegram": {"enabled": True, "chat_id": "12345"}}
    notifier = Notifier(cfg)
    notifier.bot = fake_bot
    notifier.chat_id = "12345"

    kb = make_visitor_keyboard()
    notifier.send_telegram("Doorbell alert", bypass_cooldown=True, reply_markup=kb)

    assert len(fake_bot.sent_messages) == 1
    assert fake_bot.sent_messages[0]["reply_markup"] is kb

