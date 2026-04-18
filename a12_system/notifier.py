"""Telegram notifications and media creation (GIF/MP4)."""

import logging
import os
import shutil
import subprocess
import tempfile
import time

import cv2
from PIL import Image

try:
    import telebot
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

try:
    import imageio
    GIF_AVAILABLE = True
except ImportError:
    GIF_AVAILABLE = False


class Notifier:
    def __init__(self, config: dict):
        self.config = config
        self.bot = None
        self.chat_id = None
        self.last_telegram_time = 0

        if config["telegram"]["enabled"] and TELEGRAM_AVAILABLE:
            try:
                self.bot = telebot.TeleBot(config["telegram"]["token"])
                self.chat_id = config["telegram"]["chat_id"]
                logging.info("Telegram bot initialized")
            except Exception as e:
                logging.error(f"Telegram init failed: {e}")

    def send_telegram(self, message: str, media_path: str = None) -> bool:
        """Send Telegram message/photo/video/GIF with cooldown."""
        current_time = time.time()
        cooldown = self.config.get("telegram_cooldown_seconds", 60)
        if current_time - self.last_telegram_time < cooldown:
            logging.info(
                f"Telegram cooldown active "
                f"({current_time - self.last_telegram_time:.0f}s / {cooldown}s)"
            )
            return False

        if not self.bot or not self.chat_id:
            return False

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

            self.last_telegram_time = current_time
            logging.info(f"Telegram sent: {message[:50]}...")
            return True
        except Exception as e:
            logging.error(f"Telegram error: {e}")
        return False

    def create_gif(self, frames: list, output_path: str) -> bool:
        """Create GIF from frames."""
        if not GIF_AVAILABLE or not frames:
            return False

        try:
            pil_frames = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames]
            duration = int(1000 / self.config["gif"]["fps"])
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

    def create_mp4(self, frames: list, audio_data: bytes, output_path: str) -> bool:
        """Create MP4 from video frames and raw audio data using ffmpeg."""
        if not frames:
            return False

        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp()

            # Save video frames
            for i, frame in enumerate(frames):
                cv2.imwrite(os.path.join(temp_dir, f"frame_{i:04d}.jpg"), frame)

            # Save audio data (16kHz, 16-bit PCM)
            audio_path = os.path.join(temp_dir, "audio.pcm")
            with open(audio_path, "wb") as f:
                f.write(audio_data)

            fps = self.config["gif"]["fps"]
            cmd = [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", os.path.join(temp_dir, "frame_%04d.jpg"),
                "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", audio_path,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                output_path,
            ]

            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=30,
            )
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
