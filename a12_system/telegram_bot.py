"""Interactive Telegram Bot Service for A12 Security System.

Provides bidirectional remote control and query interface for A12:
- /photo & /foto: Live snapshot from camera pipeline
- /unlock & /odemknout: Remote door unlock via Nuki
- /status & /stav: System health, FPS, residents, events, mute status
- /mute [min] & /unmute: Temporary notification suppression
- /enroll <name>: One-shot face enrollment from photos
- Callback query handling for interactive inline buttons
"""

import logging
import os
import pickle
import threading
import time
from typing import Optional

import cv2
import numpy as np

from .face_backend import SFACE_BACKEND

try:
    import telebot
    from telebot.types import (
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
    )
    TELEGRAM_AVAILABLE = True
except ImportError:
    telebot = None
    TELEGRAM_AVAILABLE = False
    Message = object
    CallbackQuery = object
    InlineKeyboardMarkup = object
    InlineKeyboardButton = object


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
            commands=["unlock", "odemknout"],
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
        self.bot.register_message_handler(
            self.handle_enroll,
            func=lambda m: bool(
                m
                and (
                    getattr(m, "text", None)
                    or getattr(m, "caption", None)
                    or ""
                )
                .strip()
                .startswith("/enroll")
            ),
            content_types=["text", "photo"],
        )
        if hasattr(self.bot, "register_callback_query_handler"):
            self.bot.register_callback_query_handler(
                self.handle_callback_query,
                func=lambda call: True,
            )

    def handle_help(self, message) -> None:
        """Handle /start and /help commands."""
        try:
            if not self._authorized(message):
                return

            help_text = (
                "🛡️ *A12 Bezpečnostní Systém - Příkazy:*\n\n"
                "📷 `/photo`, `/foto` - Aktuální snímek z kamery\n"
                "🔓 `/unlock`, `/odemknout` - Odemknout vchodové dveře (Nuki)\n"
                "📊 `/status`, `/stav` - Zobrazit celkový stav systému\n"
                "🔕 `/mute [minuty]` - Ztišit notifikace na danou dobu (výchozí 15 min)\n"
                "🔔 `/unmute` - Zrušit ztišení a obnovit notifikace\n"
                "👤 `/enroll <jméno>` - Zaregistrovat obličej (přiložte fotku nebo odpovězte na fotku)\n"
                "❓ `/help`, `/start` - Zobrazit tuto nápovědu"
            )
            chat_id, _ = self._get_chat_and_user_id(message)
            self.bot.send_message(chat_id, help_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error handling /help: {e}")

    def handle_photo(self, message) -> None:
        """Handle /photo and /foto commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            frame = None

            if self.pipeline:
                frame = getattr(self.pipeline, "latest_frame", None)
                if frame is None:
                    cam = getattr(self.pipeline, "camera", None)
                    if cam and callable(getattr(cam, "snapshot", None)):
                        try:
                            frame = cam.snapshot()
                        except Exception as e:
                            logging.warning(f"Camera snapshot call failed: {e}")

            if frame is not None:
                success, encoded = cv2.imencode(".jpg", frame)
                if success:
                    self.bot.send_photo(
                        chat_id,
                        encoded.tobytes(),
                        caption="📷 Aktuální snímek z kamery",
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

    def handle_unlock(self, message) -> None:
        """Handle /unlock and /odemknout commands."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            if self.pipeline and callable(getattr(self.pipeline, "_trigger_nuki_unlock", None)):
                self.pipeline._trigger_nuki_unlock("Telegram Admin")
                self.bot.send_message(
                    chat_id, "🔓 Požadavek na odemknutí dveří (Telegram Admin) byl odeslán."
                )
            else:
                self.bot.send_message(
                    chat_id, "⚠️ Funkce odemknutí dveří není v systému dostupná."
                )
        except Exception as e:
            logging.error(f"Error handling /unlock: {e}")

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
                residents = sorted(set(names))
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

            status_msg = (
                "📊 *Přehled stavu A12:*\n"
                f"• Pipeline stav: `{p_state}`\n"
                f"• Kamera FPS: `{fps}`\n"
                f"• Obyvatelé (tváře): `{residents_str}`\n"
                f"• Poslední událost: `{last_event_str}`\n"
                f"• Notifikace: {mute_str}"
            )
            self.bot.send_message(chat_id, status_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Error handling /status: {e}")

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
                    minutes = max(1, int(parts[1]))
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

    def handle_enroll(self, message) -> None:
        """Handle /enroll <name> with photo or replied photo."""
        try:
            if not self._authorized(message):
                return

            chat_id, _ = self._get_chat_and_user_id(message)
            text = (
                getattr(message, "text", None)
                or getattr(message, "caption", None)
                or ""
            ).strip()

            parts = text.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else ""

            if not name:
                self.bot.send_message(
                    chat_id,
                    "⚠️ Uveďte prosím jméno osoby: `/enroll <jméno>` (přiložte fotku nebo odpovězte na fotku).",
                    parse_mode="Markdown",
                )
                return

            # Extract photo object
            photo_list = getattr(message, "photo", None)
            if not photo_list and getattr(message, "reply_to_message", None):
                photo_list = getattr(message.reply_to_message, "photo", None)

            if not photo_list:
                self.bot.send_message(
                    chat_id,
                    "⚠️ Přiložte fotografii s obličejem nebo odpovězte příkazem na fotku.",
                )
                return

            photo_obj = photo_list[-1]

            # Download photo bytes
            file_info = self.bot.get_file(photo_obj.file_id)
            downloaded = self.bot.download_file(file_info.file_path)
            img_np = np.frombuffer(downloaded, dtype=np.uint8)
            img = cv2.imdecode(img_np, cv2.IMREAD_COLOR)

            if img is None:
                self.bot.send_message(chat_id, "⚠️ Nepodařilo se načíst/dekódovat fotografii.")
                return

            # Detect & embed face
            detector = getattr(self.pipeline, "detector", None) if self.pipeline else None
            backend = getattr(detector, "face_backend", None) if detector else None
            if backend is None:
                self.bot.send_message(
                    chat_id,
                    "⚠️ Backend pro rozpoznávání obličejů (YuNet/SFace) není aktivní.",
                )
                return

            found = backend.detect_and_embed(img)
            if not found:
                self.bot.send_message(
                    chat_id, "⚠️ Na fotografii nebyl nalezen žádný obličej."
                )
                return
            if len(found) > 1:
                self.bot.send_message(
                    chat_id,
                    f"⚠️ Na fotografii bylo nalezeno {len(found)} obličejů. Pošlete fotografii pouze jedné osoby.",
                )
                return

            _, embedding = found[0]

            # Append to detector
            if not hasattr(detector, "known_face_encodings"):
                detector.known_face_encodings = []
            if not hasattr(detector, "known_face_names"):
                detector.known_face_names = []

            detector.known_face_encodings.append(embedding)
            detector.known_face_names.append(name)

            # Persist to gallery file if possible
            saved_path = None
            models_dir = getattr(
                detector, "models_dir", getattr(self.pipeline, "script_dir", ".")
            )
            gallery_paths = []
            if hasattr(detector, "_gallery_paths"):
                face_cfg = getattr(detector, "config", {}).get("face_recognition", {})
                gallery_paths = detector._gallery_paths(face_cfg, models_dir)

            target_gallery = (
                gallery_paths[0]
                if gallery_paths
                else os.path.join(models_dir, "known_faces_sface.pkl")
            )
            try:
                with open(target_gallery, "wb") as f:
                    pickle.dump(
                        {
                            "backend": SFACE_BACKEND,
                            "encodings": detector.known_face_encodings,
                            "names": detector.known_face_names,
                        },
                        f,
                    )
                saved_path = target_gallery
            except Exception as e:
                logging.error(f"Failed to save face gallery: {e}")

            # Update whitelist in runtime_config if available
            if self.runtime_config:
                try:
                    wl = list(
                        self.runtime_config.get("face_recognition.whitelisted_names", [])
                    )
                    if name not in wl:
                        wl.append(name)
                        self.runtime_config.set(
                            "face_recognition.whitelisted_names", sorted(wl)
                        )
                except Exception as e:
                    logging.warning(f"Could not update runtime whitelist: {e}")

            saved_note = (
                f" a uloženo do `{os.path.basename(saved_path)}`" if saved_path else ""
            )
            self.bot.send_message(
                chat_id,
                f"✅ Osoba *{name}* byla úspěšně zaregistrována{saved_note}!\n"
                f"Celkem uloženo v databázi: {len(detector.known_face_names)} vzorů.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.error(f"Error handling /enroll: {e}")

    def handle_callback_query(self, call) -> None:
        """Handle inline button callback queries."""
        try:
            if not self._authorized(call):
                if hasattr(self.bot, "answer_callback_query"):
                    self.bot.answer_callback_query(
                        call.id, "Neautorizovaný přístup!", show_alert=True
                    )
                return

            user_name = "uživatelem"
            if getattr(call, "from_user", None) and call.from_user.first_name:
                user_name = call.from_user.first_name

            msg = getattr(call, "message", None)
            chat_id = getattr(getattr(msg, "chat", None), "id", None)
            message_id = getattr(msg, "message_id", None)

            if call.data == "unlock_door":
                if self.pipeline and callable(
                    getattr(self.pipeline, "_trigger_nuki_unlock", None)
                ):
                    self.pipeline._trigger_nuki_unlock("Telegram Callback")

                if hasattr(self.bot, "answer_callback_query"):
                    self.bot.answer_callback_query(call.id, "🔓 Dveře se odemykají...")

                if TELEGRAM_AVAILABLE and chat_id and message_id:
                    try:
                        markup = InlineKeyboardMarkup()
                        markup.row(
                            InlineKeyboardButton(
                                f"✅ Odemčeno uživatelem {user_name}",
                                callback_data="noop",
                            )
                        )
                        self.bot.edit_message_reply_markup(
                            chat_id=chat_id,
                            message_id=message_id,
                            reply_markup=markup,
                        )
                    except Exception as e:
                        logging.warning(f"Could not update unlock reply markup: {e}")

            elif call.data == "mute_15m":
                if self.notifier and callable(getattr(self.notifier, "mute_until", None)):
                    self.notifier.mute_until(time.time() + 15 * 60)

                if hasattr(self.bot, "answer_callback_query"):
                    self.bot.answer_callback_query(
                        call.id, "🔕 Notifikace ztišeny na 15 minut."
                    )

                if TELEGRAM_AVAILABLE and chat_id and message_id:
                    try:
                        markup = InlineKeyboardMarkup()
                        markup.row(
                            InlineKeyboardButton(
                                "🔕 Ztišeno na 15m",
                                callback_data="noop",
                            )
                        )
                        self.bot.edit_message_reply_markup(
                            chat_id=chat_id,
                            message_id=message_id,
                            reply_markup=markup,
                        )
                    except Exception as e:
                        logging.warning(f"Could not update mute reply markup: {e}")

            elif call.data == "noop":
                if hasattr(self.bot, "answer_callback_query"):
                    self.bot.answer_callback_query(call.id)
        except Exception as e:
            logging.error(f"Error handling callback query: {e}")

    def start(self) -> None:
        """Start polling loop in background daemon thread."""
        if not self.bot:
            logging.warning("Cannot start TelegramBotService: no bot instance")
            return
        if self._running:
            logging.info("TelegramBotService is already running")
            return

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
