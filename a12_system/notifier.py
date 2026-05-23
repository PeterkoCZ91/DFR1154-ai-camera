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

    def send_telegram(self, message: str, media_path: str = None, bypass_cooldown: bool = False) -> bool:
        """Send Telegram message/photo/video/GIF with cooldown."""
        current_time = time.time()
        cooldown = self.config.get("telegram_cooldown_seconds", 60)
        if not bypass_cooldown and current_time - self.last_telegram_time < cooldown:
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

            if not bypass_cooldown:
                self.last_telegram_time = current_time
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
                logging.warning(f"Telegram 429: backoff {retry_after}s")
                time.sleep(retry_after)
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
        if not GIF_AVAILABLE or not frames:
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
