"""Interactive Telegram Bot Service for A12 Security System.

Provides bidirectional remote control and query interface for A12:
- /photo & /foto: Live snapshot from camera pipeline
- /unlock & /odemknout & /od: Remote door unlock via Nuki
- /lock & /zamknout & /za: Remote door lock via Nuki
- /status & /stav: System health, FPS, residents, events, mute status
- /mute [min] & /unmute: Temporary notification suppression
- Callback query handling for interactive inline buttons
"""

import logging
import threading
import time
from typing import Optional

import cv2


def _format_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


class _FallbackBotCommand:
    def __init__(self, command: str, description: str):
        self.command = command
        self.description = description

    def __repr__(self):
        return f"BotCommand('{self.command}', '{self.description}')"


try:
    import telebot
    from telebot.types import (
        BotCommand,
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    telebot = None
    TELEGRAM_AVAILABLE = False
    BotCommand = _FallbackBotCommand
    Message = object
    CallbackQuery = object
    InlineKeyboardMarkup = object
    InlineKeyboardButton = object


# An unlock button on an old notification opens nothing: whoever presses it
# hours later is answering a visitor who has long gone. /od is always fresh.
VISITOR_UNLOCK_TTL_SECONDS = 30 * 60
CB_UNLOCK_DOOR = "unlock_door"
# A stream frame older than this is not "current".
PHOTO_MAX_AGE_SECONDS = 15.0
MAX_MUTE_MINUTES = 24 * 60
BUSY_DOOR_TEXT = "⏳ Dveře se právě ovládají, výsledek přijde zprávou."


class TelegramBotService:
    """Background service providing interactive Telegram bot commands and callbacks."""

    def __init__(
        self,
        config: dict,
        notifier,
        pipeline=None,
        runtime_config=None,
    ):
        self.config = config or {}
        self.notifier = notifier
        self.pipeline = pipeline
        self.runtime_config = runtime_config
        self.bot = getattr(notifier, "bot", None)

        tg_cfg = self.config.get("telegram") or {}
        token = tg_cfg.get("token")
        if not self.bot and token and TELEGRAM_AVAILABLE:
            try:
                self.bot = telebot.TeleBot(token)
            except Exception as e:
                logging.error(f"Failed to create Telegram bot instance: {e}")

        # Parse allowed chat IDs
        self.allowed_chat_ids: set[str] = set()
        raw_chat_id = tg_cfg.get("chat_id")
        if raw_chat_id is not None:
            if isinstance(raw_chat_id, (list, tuple, set)):
                for cid in raw_chat_id:
                    if cid is not None:
                        self.allowed_chat_ids.add(str(cid).strip())
            elif isinstance(raw_chat_id, (int, float)):
                self.allowed_chat_ids.add(str(int(raw_chat_id)))
            elif isinstance(raw_chat_id, str):
                for cid in raw_chat_id.split(","):
                    if cid.strip():
                        self.allowed_chat_ids.add(cid.strip())

        self._running = False
        self._thread: Optional[threading.Thread] = None

        if self.bot:
            self._register_handlers()

    def _get_chat_and_user_id(self, message_or_call) -> tuple[Optional[str], Optional[str]]:
        chat_id = None
        user_id = None

        if hasattr(message_or_call, "chat") and message_or_call.chat:
            chat_id = getattr(message_or_call.chat, "id", None)
        elif (
            hasattr(message_or_call, "message")
            and message_or_call.message
            and hasattr(message_or_call.message, "chat")
            and message_or_call.message.chat
        ):
            chat_id = getattr(message_or_call.message.chat, "id", None)

        if hasattr(message_or_call, "from_user") and message_or_call.from_user:
            user_id = getattr(message_or_call.from_user, "id", None)

        if chat_id is None and isinstance(message_or_call, (int, str)):
            chat_id = message_or_call

        str_chat_id = str(chat_id).strip() if chat_id is not None else None
        str_user_id = str(user_id).strip() if user_id is not None else None
        return str_chat_id, str_user_id

    def _authorized(self, message_or_call) -> bool:
        """Security check: verify whether sender chat ID is in allowed_chat_ids."""
        str_chat_id, str_user_id = self._get_chat_and_user_id(message_or_call)

        target_id = str_chat_id if str_chat_id is not None else str_user_id
        if target_id and target_id in self.allowed_chat_ids:
            return True

        logging.warning(
            f"Unauthorized Telegram access attempt rejected: chat_id={str_chat_id}, user_id={str_user_id}"
        )
        return False

    def _safe_reply(self, message, text: str) -> None:
        """Best-effort plain-text reply used from error paths."""
        try:
            chat_id, _ = self._get_chat_and_user_id(message)
            if self.bot and chat_id:
                self.bot.send_message(chat_id, text)
        except Exception as e:
            logging.warning(f"Could not send Telegram error reply: {e}")

    def _answer(self, call, text: Optional[str] = None, show_alert: bool = False) -> None:
        if not hasattr(self.bot, "answer_callback_query"):
            return
        try:
            if text is None:
                self.bot.answer_callback_query(call.id)
            elif show_alert:
                self.bot.answer_callback_query(call.id, text, show_alert=True)
            else:
                self.bot.answer_callback_query(call.id, text)
        except Exception as e:
            logging.warning(f"Could not answer callback query: {e}")

    def _set_markup(self, call, markup) -> None:
        msg = getattr(call, "message", None)
        chat_id = getattr(getattr(msg, "chat", None), "id", None)
        message_id = getattr(msg, "message_id", None)
        if not (chat_id and message_id):
            return
        try:
            self.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=markup
            )
        except Exception as e:
            logging.warning(f"Could not update reply markup: {e}")

    def _set_status_markup(self, call, label: str) -> None:
        """Replace the buttons by a single inert label."""
        if not TELEGRAM_AVAILABLE:
            return
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton(label, callback_data="noop"))
        self._set_markup(call, markup)

    @staticmethod
    def _actor(message_or_call) -> str:
        """Who asked, for the door log and the result message."""
        user = getattr(message_or_call, "from_user", None)
        name = getattr(user, "first_name", None) or getattr(user, "username", None)
        return f"Telegram: {name}" if isinstance(name, str) and name else "Telegram"

    def _unlock_available(self) -> bool:
        return bool(
            self.pipeline
            and callable(getattr(self.pipeline, "_trigger_nuki_unlock", None))
        )

    def _register_handlers(self) -> None:
        if not self.bot or not hasattr(self.bot, "register_message_handler"):
            return

        self.bot.register_message_handler(
            self.handle_help,
            commands=["start", "help"],
        )
        self.bot.register_message_handler(
            self.handle_photo,
            commands=["photo", "foto"],
        )
        self.bot.register_message_handler(
            self.handle_unlock,
            commands=["unlock", "odemknout", "od"],
        )
        self.bot.register_message_handler(
            self.handle_lock,
            commands=["lock", "zamknout", "za"],
        )
        self.bot.register_message_handler(
            self.handle_status,
            commands=["status", "stav"],
        )
        self.bot.register_message_handler(
            self.handle_mute,
            commands=["mute"],
        )
        self.bot.register_message_handler(
            self.handle_unmute,
            commands=["unmute"],
        )
        if hasattr(self.bot, "register_callback_query_handler"):
            self.bot.register_callback_query_handler(
                self.handle_callback_query,
                func=lambda call: True,
            )

    def _register_commands_menu(self) -> None:
        """Register the official command list in Telegram UI command menu / popup picker."""
        if not self.bot or not hasattr(self.bot, "set_my_commands"):
            return

        try:
            from telebot.types import BotCommand as TelebotBotCommand

            cmd_cls = TelebotBotCommand
        except (ImportError, AttributeError):
            cmd_cls = BotCommand

        commands = [
            cmd_cls("od", "🔓 Odemknout dveře (střelka)"),
            cmd_cls("za", "🔒 Zamknout dveře"),
            cmd_cls("foto", "📷 Aktuální snímek z kamery"),
            cmd_cls("stav", "📊 Celkový stav systému"),
            cmd_cls("mute", "🔕 Ztišit notifikace"),
            cmd_cls("unmute", "🔔 Obnovit notifikace"),
            cmd_cls("help", "❓ Nápověda k příkazům"),
        ]

        try:
            self.bot.set_my_commands(commands)
            logging.info("Telegram command menu successfully registered via set_my_commands")
        except Exception as e:
            logging.warning(f"Failed to register Telegram command menu: {e}")

    def handle_help(self, message) -> None:
        """Handle /start and /help commands."""
        try:
            if not self._authorized(message):
                return

            help_text = (
                "🛡️ *A12 Bezpečnostní Systém - Příkazy:*\n\n"
                "📷 `/photo`, `/foto` - Aktuální snímek z kamery\n"
                "🔓 `/unlock`, `/odemknout`, `/od` - Odemknout vchodové dveře (Nuki)\n"
                "🔒 `/lock`, `/zamknout`, `/za` - Zamknout vchodové dveře (Nuki)\n"
                "📊 `/status`, `/stav` - Zobrazit celkový stav systému\n"
                "🔕 `/mute [minuty]` - Ztišit notifikace na danou dobu (výchozí 15 min)\n"
                "🔔 `/unmute` - Zrušit ztišení a obnovit notifikace\n"
                "❓ `/help`, `/start` - Zobrazit tuto nápovědu"
            )
            chat_id, _ = self._get_chat_and_user_id(message)
            self.bot.send_message(chat_id, help_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error handling /help: {e}")
            self._safe_reply(message, "⚠️ Nápovědu se nepodařilo zobrazit.")

    def handle_photo(self, message) -> None:
        """Handle /photo and /foto commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            frame = None
            caption = "📷 Aktuální snímek z kamery"

            if self.pipeline:
                frame = getattr(self.pipeline, "latest_frame", None)
                shared = getattr(self.pipeline, "shared_state", None) or {}
                frame_at = shared.get("last_frame") if isinstance(shared, dict) else None
                age = time.time() - frame_at if isinstance(frame_at, (int, float)) else None
                # The stream frame is never cleared, so a dead stream would
                # hand out an hours-old picture as "current".
                stale = frame is None or (age is not None and age > PHOTO_MAX_AGE_SECONDS)
                if stale:
                    cam = getattr(self.pipeline, "camera", None)
                    if cam and callable(getattr(cam, "snapshot", None)):
                        try:
                            fresh = cam.snapshot()
                            if fresh is not None:
                                frame, age, stale = fresh, None, False
                        except Exception as e:
                            logging.warning(f"Camera snapshot call failed: {e}")
                if frame is not None and stale and age is not None:
                    caption = f"📷 Poslední snímek, starý {_format_age(age)} — stream neběží?"

            if frame is not None:
                success, encoded = cv2.imencode(".jpg", frame)
                if success:
                    self.bot.send_photo(
                        chat_id,
                        encoded.tobytes(),
                        caption=caption,
                    )
                    return
                else:
                    self.bot.send_message(chat_id, "⚠️ Nepodařilo se zakódovat snímek.")
                    return

            self.bot.send_message(
                chat_id, "⚠️ Žádný snímek z kamery není momentálně k dispozici."
            )
        except Exception as e:
            logging.error(f"Error handling /photo: {e}")
            self._safe_reply(message, "⚠️ Snímek se nepodařilo odeslat.")

    def handle_unlock(self, message) -> None:
        """Handle /unlock, /odemknout, and /od commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            if self._unlock_available():
                if self.pipeline._trigger_nuki_unlock(self._actor(message), manual=True) is False:
                    self.bot.send_message(chat_id, BUSY_DOOR_TEXT)
                    return
                # The door is already moving: a failed acknowledgement must not
                # turn into "request failed".
                self._safe_reply(message, "⏳ Odemykám… (výsledek přijde zprávou)")
            else:
                self.bot.send_message(
                    chat_id, "⚠️ Funkce odemknutí dveří není v systému dostupná."
                )
        except Exception as e:
            logging.error(f"Error handling /unlock: {e}")
            self._safe_reply(message, "⚠️ Požadavek na odemknutí se nepodařilo zpracovat.")

    def handle_lock(self, message) -> None:
        """Handle /lock, /zamknout, and /za commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            if self.pipeline and callable(getattr(self.pipeline, "_trigger_nuki_lock", None)):
                if self.pipeline._trigger_nuki_lock(self._actor(message)) is False:
                    self.bot.send_message(chat_id, BUSY_DOOR_TEXT)
                    return
                # The door is already moving: a failed acknowledgement must not
                # turn into "request failed".
                self._safe_reply(message, "⏳ Zamykám… (výsledek přijde zprávou)")
            else:
                self.bot.send_message(
                    chat_id, "⚠️ Funkce zamknutí dveří není v systému dostupná."
                )
        except Exception as e:
            logging.error(f"Error handling /lock: {e}")
            self._safe_reply(message, "⚠️ Požadavek na zamknutí se nepodařilo zpracovat.")

    def handle_status(self, message) -> None:
        """Handle /status and /stav commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)

            # Pipeline state & FPS
            p_state = "neaktivní"
            fps = 0.0
            if self.pipeline:
                if getattr(self.pipeline, "running", False):
                    p_state = self.pipeline.shared_state.get("pipeline_state", "aktivní")
                else:
                    p_state = "zastaveno"
                fps = getattr(self.pipeline, "stream_fps", 0.0)

            # Known residents
            residents: list[str] = []
            if self.pipeline and getattr(self.pipeline, "detector", None):
                names = getattr(self.pipeline.detector, "known_face_names", [])
                residents = sorted(set(str(n) for n in names))
            elif self.runtime_config:
                residents = sorted(set(self.runtime_config.get("face_unlock.names", [])))

            residents_str = ", ".join(residents) if residents else "žádní"

            # Last event
            last_event_str = "žádná"
            if self.pipeline and getattr(self.pipeline, "db", None):
                try:
                    recent = self.pipeline.db.get_recent_events(limit=1)
                    if recent:
                        ev = recent[0]
                        ev_type = ev.get("event_type", "event")
                        label = ev.get("label", "")
                        ts = ev.get("timestamp", "")
                        last_event_str = f"{ev_type}: {label} ({ts})"
                except Exception as e:
                    logging.warning(f"Failed to query recent events: {e}")

            # Mute state
            if (
                self.notifier
                and callable(getattr(self.notifier, "is_muted", None))
                and self.notifier.is_muted()
            ):
                remaining = int(getattr(self.notifier, "_muted_until", 0.0) - time.time())
                mute_str = (
                    f"🔕 Ztišeno (zbývá {max(0, remaining // 60)}m {max(0, remaining % 60)}s)"
                )
            else:
                mute_str = "🔔 Aktivní (neztišeno)"

            # Plain text on purpose: values come from the DB and gallery, and a
            # stray Markdown character would make Telegram reject the message.
            status_msg = (
                "📊 Přehled stavu A12:\n"
                f"• Pipeline stav: {p_state}\n"
                f"• Kamera FPS: {fps}\n"
                f"• Obyvatelé (tváře): {residents_str}\n"
                f"• Poslední událost: {last_event_str}\n"
                f"• Notifikace: {mute_str}"
            )
            self.bot.send_message(chat_id, status_msg)
        except Exception as e:
            logging.error(f"Error handling /status: {e}")
            self._safe_reply(message, "⚠️ Stav systému se nepodařilo zjistit.")

    def handle_mute(self, message) -> None:
        """Handle /mute [minutes] command."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            minutes = 15
            text = (getattr(message, "text", None) or "").strip()
            parts = text.split()
            if len(parts) > 1:
                try:
                    minutes = min(max(1, int(parts[1])), MAX_MUTE_MINUTES)
                except ValueError:
                    minutes = 15

            if self.notifier and callable(getattr(self.notifier, "mute_until", None)):
                self.notifier.mute_until(time.time() + minutes * 60)
                self.bot.send_message(
                    chat_id, f"🔕 Notifikace byly ztišeny na {minutes} minut."
                )
            else:
                self.bot.send_message(chat_id, "⚠️ Notifikátor není dostupný.")
        except Exception as e:
            logging.error(f"Error handling /mute: {e}")
            self._safe_reply(message, "⚠️ Ztišení se nepodařilo nastavit.")

    def handle_unmute(self, message) -> None:
        """Handle /unmute command."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            if self.notifier and callable(getattr(self.notifier, "mute_until", None)):
                self.notifier.mute_until(0)
                self.bot.send_message(
                    chat_id, "🔔 Ztišení notifikací bylo zrušeno. Notifikace jsou aktivní."
                )
            else:
                self.bot.send_message(chat_id, "⚠️ Notifikátor není dostupný.")
        except Exception as e:
            logging.error(f"Error handling /unmute: {e}")
            self._safe_reply(message, "⚠️ Ztišení se nepodařilo zrušit.")

    def handle_callback_query(self, call) -> None:
        """Handle inline button callback queries."""
        try:
            if not self._authorized(call):
                self._answer(call, "Neautorizovaný přístup!", show_alert=True)
                return

            user_name = "uživatel"
            if getattr(call, "from_user", None) and call.from_user.first_name:
                user_name = call.from_user.first_name

            data = getattr(call, "data", "") or ""

            if data == CB_UNLOCK_DOOR:
                msg_date = getattr(getattr(call, "message", None), "date", None)
                if isinstance(msg_date, (int, float)) and (
                    time.time() - msg_date > VISITOR_UNLOCK_TTL_SECONDS
                ):
                    self._answer(call, "⌛ Tlačítko vypršelo, použij /od.", show_alert=True)
                    self._set_status_markup(call, "⌛ Tlačítko vypršelo")
                    return
                if not self._unlock_available():
                    self._answer(call, "⚠️ Funkce odemknutí dveří není dostupná.", show_alert=True)
                    return
                if self.pipeline._trigger_nuki_unlock(self._actor(call), manual=True) is False:
                    self._answer(call, BUSY_DOOR_TEXT, show_alert=True)
                    return
                self._answer(call, "⏳ Odemykám…")
                # The worker is fire-and-forget; the real outcome arrives as a
                # separate notifier message, so claim nothing here.
                self._set_status_markup(
                    call, f"⏳ Odemykám ({user_name})… výsledek přijde zprávou"
                )

            elif data == "lock_door":
                if self.pipeline and callable(
                    getattr(self.pipeline, "_trigger_nuki_lock", None)
                ):
                    if self.pipeline._trigger_nuki_lock(self._actor(call)) is False:
                        self._answer(call, BUSY_DOOR_TEXT, show_alert=True)
                        return
                    self._answer(call, "⏳ Zamykám…")
                    self._set_status_markup(
                        call, f"⏳ Zamykám ({user_name})… výsledek přijde zprávou"
                    )
                else:
                    self._answer(call, "⚠️ Funkce zamknutí dveří není dostupná.", show_alert=True)

            elif data == "mute_15m":
                if self.notifier and callable(getattr(self.notifier, "mute_until", None)):
                    self.notifier.mute_until(time.time() + 15 * 60)
                    self._answer(call, "🔕 Notifikace ztišeny na 15 minut.")
                    self._set_status_markup(call, "🔕 Ztišeno na 15m")
                else:
                    self._answer(call, "⚠️ Notifikátor není dostupný.", show_alert=True)

            elif data == "noop":
                self._answer(call)

            else:
                self._answer(call, "⚠️ Neznámé tlačítko.")
        except Exception as e:
            logging.error(f"Error handling callback query: {e}")
            self._answer(call, "⚠️ Akci se nepodařilo provést.", show_alert=True)

    def start(self) -> None:
        """Start polling loop in background daemon thread."""
        if not self.bot:
            logging.warning("Cannot start TelegramBotService: no bot instance")
            return
        if self._running:
            logging.info("TelegramBotService is already running")
            return

        self._register_commands_menu()

        self._running = True
        self._thread = threading.Thread(
            target=self._polling_worker,
            daemon=True,
            name="TelegramBotPolling",
        )
        self._thread.start()
        logging.info("TelegramBotService background thread started")

    def _polling_worker(self) -> None:
        logging.info("Starting Telegram bot infinity_polling...")
        try:
            if hasattr(self.bot, "infinity_polling"):
                self.bot.infinity_polling(
                    skip_pending=True,
                    timeout=20,
                    long_polling_timeout=20,
                )
        except Exception as e:
            if self._running:
                logging.error(f"Telegram infinity_polling error: {e}")
        finally:
            self._running = False
            logging.info("Telegram bot infinity_polling ended")

    def stop(self) -> None:
        """Stop polling loop gracefully."""
        self._running = False
        if self.bot and hasattr(self.bot, "stop_polling"):
            try:
                self.bot.stop_polling()
            except Exception as e:
                logging.warning(f"Error stopping Telegram polling: {e}")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logging.info("TelegramBotService stopped")
