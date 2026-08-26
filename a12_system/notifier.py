"""Telegram notifications and media creation (GIF/MP4)."""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

import cv2
from PIL import Image

try:
    import telebot
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


class Notifier:
    def __init__(self, config: dict):
        self.config = config
        self.bot = None
        self.chat_id = None
        self.last_telegram_time = 0
        # send_telegram() is called from at least five threads (notification
        # worker, status monitor, audio monitor, HA callback, main loop). The
        # cooldown check was an unguarded read-modify-write, so two events could
        # both pass it and double-notify, or clobber each other's timestamp.
        self._send_lock = threading.Lock()
        # Set while a 429 backoff is in effect. Callers return immediately instead
        # of sleeping: the previous code slept up to 60 s *in the calling thread*,
        # which stalled the status monitor's watchdog or backed up the notification
        # queue until events were dropped.
        self._rate_limited_until = 0.0

        if config["telegram"]["enabled"] and TELEGRAM_AVAILABLE:
            try:
                self.bot = telebot.TeleBot(config["telegram"]["token"])
                self.chat_id = config["telegram"]["chat_id"]
                logging.info("Telegram bot initialized")
            except Exception as e:
                logging.error(f"Telegram init failed: {e}")

    def send_telegram(self, message: str, media_path: str = None, bypass_cooldown: bool = False) -> bool:
        """Send Telegram message/photo/video/GIF with cooldown.

        Returns True only when Telegram accepted the message. Callers that must not
        lose an event should check the result — a False here means nothing was
        delivered.
        """
        current_time = time.time()
        cooldown = self.config.get("telegram_cooldown_seconds", 60)

        with self._send_lock:
            if current_time < self._rate_limited_until:
                logging.warning(
                    f"Telegram rate-limited for another "
                    f"{self._rate_limited_until - current_time:.0f}s; dropping message"
                )
                return False

            if not bypass_cooldown and current_time - self.last_telegram_time < cooldown:
                logging.info(
                    f"Telegram cooldown active "
                    f"({current_time - self.last_telegram_time:.0f}s / {cooldown}s)"
                )
                return False

            if not self.bot or not self.chat_id:
                return False

            # Claim the slot before the network call so a concurrent caller cannot
            # slip through the same cooldown window.
            if not bypass_cooldown:
                self.last_telegram_time = current_time

        try:
            if media_path and os.path.exists(media_path):
                with open(media_path, "rb") as f:
                    if media_path.endswith(".gif"):
                        self.bot.send_animation(self.chat_id, f, caption=message)
                    elif media_path.endswith(".mp4"):
                        self.bot.send_video(self.chat_id, f, caption=message)
                    else:
                        self.bot.send_photo(self.chat_id, f, caption=message)
            else:
                self.bot.send_message(self.chat_id, message)

            logging.info(f"Telegram sent: {message[:50]}...")
            return True
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = 10
                try:
                    retry_after = int(e.result_json["parameters"]["retry_after"])
                except (KeyError, TypeError, ValueError):
                    pass
                retry_after = min(retry_after, 60)
                # Record the window instead of sleeping here: this method runs on
                # whatever thread had something to say, including the frame path and
                # the watchdog. Blocking one of those for a minute is worse than
                # dropping a notification, and the sleep did not retry anyway.
                logging.warning(f"Telegram 429: suppressing sends for {retry_after}s")
                with self._send_lock:
                    self._rate_limited_until = time.time() + retry_after
            else:
                logging.error(f"Telegram API error {e.error_code}: {e}")
        except Exception as e:
            logging.error(f"Telegram error: {e}")
        return False

    def create_gif(
        self,
        frames: list,
        output_path: str,
        fps: int = None,
        size: tuple[int, int] = (320, 240),
    ) -> bool:
        """Create GIF from frames."""
        if not frames:
            return False

        try:
            fps = max(1, int(fps or self.config["gif"]["fps"]))
            pil_frames = []
            for frame in frames:
                resized = cv2.resize(frame, size)
                pil_frames.append(Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)))
            duration = int(1000 / fps)
            pil_frames[0].save(
                output_path,
                save_all=True,
                append_images=pil_frames[1:],
                duration=duration,
                loop=0,
            )
            logging.info(f"GIF created: {output_path}")
            return True
        except Exception as e:
            logging.error(f"GIF creation error: {e}")
            return False

    def create_mp4(
        self,
        frames: list,
        audio_data: bytes | None,
        output_path: str,
        fps: int = None,
        size: tuple[int, int] = (640, 480),
        crf: int = 28,
        min_size: int = 10_000,
    ) -> bool:
        """Create MP4 from video frames, optionally with raw audio data."""
        if not frames:
            return False

        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp()

            # Save video frames
            for i, frame in enumerate(frames):
                cv2.imwrite(os.path.join(temp_dir, f"frame_{i:04d}.jpg"), frame)

            fps = fps or self.config["gif"]["fps"]
            has_audio = bool(audio_data and len(audio_data) > 1024)

            if has_audio:
                # Save audio data (16kHz, 16-bit PCM)
                audio_path = os.path.join(temp_dir, "audio.pcm")
                with open(audio_path, "wb") as f:
                    f.write(audio_data)

                cmd = [
                    "ffmpeg", "-y",
                    "-framerate", str(fps),
                    "-i", os.path.join(temp_dir, "frame_%04d.jpg"),
                    "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", audio_path,
                    "-vf", f"scale={size[0]}:{size[1]}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
                    "-c:a", "aac", "-b:a", "64k",
                    output_path,
                ]
            else:
                cmd = [
                    "ffmpeg", "-y",
                    "-framerate", str(fps),
                    "-i", os.path.join(temp_dir, "frame_%04d.jpg"),
                    "-vf", f"scale={size[0]}:{size[1]}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
                    output_path,
                ]

            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=30,
            )

            if os.path.getsize(output_path) < min_size:
                logging.error(f"MP4 too small ({os.path.getsize(output_path)}B), likely empty")
                return False

            logging.info(f"MP4 created: {output_path}")
            return True

        except subprocess.TimeoutExpired:
            logging.error("MP4 creation timed out (30s)")
            return False
        except Exception as e:
            logging.error(f"MP4 creation error: {e}")
            return False
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
