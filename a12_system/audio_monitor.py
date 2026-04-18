"""Audio level monitoring via ESP32 WAV stream with adaptive thresholds."""

import logging
import math
import struct
import threading
import time

import requests


class AudioMonitor:
    def __init__(self, config: dict, callback=None):
        self.config = config
        self.callback = callback
        self.running = False
        self.thread = None
        self.lock = threading.Lock()

        # Network
        self.session = requests.Session()
        self.audio_port = config.get("audio_port", 82)
        host = config["camera_url"].split("://")[-1].split(":")[0]
        self.stream_url = f"http://{host}:{self.audio_port}/audio.wav"

        # Settings (dBFS)
        audio_cfg = config.get("audio", {})
        self.threshold_db = audio_cfg.get("threshold_db", -30.0)
        self.absolute_limit_db = audio_cfg.get("absolute_limit_db", -6.0)
        self.cooldown = audio_cfg.get("cooldown_seconds", 60)
        self.gain_reduction_db = audio_cfg.get("gain_reduction_db", 0)

        # Warm-up
        self.warmup_seconds = 15
        self.start_time = 0
        self.is_learning = True

        # State
        self.background_db = -60.0
        self.alpha_normal = 0.05
        self.alpha_fast = 0.2
        self.last_trigger_time = 0
        self.current_db = -90.0
        self.connected = False

        # Buffer (5 seconds @ 16kHz 16-bit)
        self.buffer_size = 16000 * 5 * 2
        self.audio_buffer = bytearray()

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.start_time = time.time()
        self.is_learning = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        logging.info(f"Audio monitor started (Warm-up: {self.warmup_seconds}s)")

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        logging.info("Audio monitor stopped")

    def get_audio_data(self) -> bytes:
        with self.lock:
            return bytes(self.audio_buffer)

    def _monitor_loop(self) -> None:
        chunk_size = 1024

        while self.running:
            response = None
            try:
                logging.debug(f"Connecting to {self.stream_url}")
                response = self.session.get(self.stream_url, stream=True, timeout=5)

                if response.status_code != 200:
                    logging.warning(f"Audio stream failed: {response.status_code}")
                    time.sleep(5)
                    continue

                self.connected = True
                logging.info("Audio stream connected")

                # Skip WAV header (44 bytes)
                header_read = 0
                HEADER_SIZE = 44
                header_buffer = bytearray()

                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not self.running:
                        break
                    if not chunk:
                        continue

                    if header_read < HEADER_SIZE:
                        needed = HEADER_SIZE - header_read
                        if len(chunk) <= needed:
                            header_buffer.extend(chunk)
                            header_read += len(chunk)
                            continue
                        else:
                            header_buffer.extend(chunk[:needed])
                            chunk = chunk[needed:]
                            header_read = HEADER_SIZE

                            if header_buffer[0:4] == b"RIFF" and header_buffer[8:12] == b"WAVE":
                                logging.info("Valid WAV header detected")
                            else:
                                logging.warning(f"Invalid WAV header: {header_buffer[0:12]}")

                    self._process_chunk(chunk)

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                time.sleep(5)
            except Exception as e:
                logging.error(f"Audio monitor error: {e}")
                time.sleep(5)
            finally:
                self.connected = False
                if response:
                    response.close()

    def _process_chunk(self, chunk: bytes) -> None:
        # Update buffer
        with self.lock:
            self.audio_buffer.extend(chunk)
            if len(self.audio_buffer) > self.buffer_size:
                self.audio_buffer = self.audio_buffer[-self.buffer_size:]

        # Calculate RMS & dB
        count = len(chunk) // 2
        if count == 0:
            return

        try:
            samples = struct.unpack(f"<{count}h", chunk[: count * 2])
            sum_sq = sum(s * s for s in samples)
            rms = math.sqrt(sum_sq / count)

            if rms > 0:
                db = 20 * math.log10(rms / 32768.0)
            else:
                db = -90.0

            if self.gain_reduction_db > 0:
                db -= self.gain_reduction_db

            self.current_db = db

            # Learning phase
            elapsed = time.time() - self.start_time
            if self.is_learning:
                if elapsed >= self.warmup_seconds:
                    self.is_learning = False
                    logging.info(f"Audio learning complete. Background: {self.background_db:.1f} dB")
                else:
                    self.background_db = (self.alpha_fast * db) + ((1 - self.alpha_fast) * self.background_db)
                    return

            # Adaptive background (only update if not loud)
            if db < self.background_db + 10:
                self.background_db = (self.alpha_normal * db) + ((1 - self.alpha_normal) * self.background_db)

            # Trigger logic
            adaptive_threshold = max(self.background_db + 15, self.threshold_db)

            trigger_type = None
            if db > self.absolute_limit_db:
                trigger_type = "EXPLOSION/CRASH"
            elif db > adaptive_threshold:
                trigger_type = "LOUD_NOISE"

            if trigger_type:
                self._trigger(db, trigger_type, adaptive_threshold)

        except struct.error:
            pass

    def _trigger(self, db: float, trigger_type: str, threshold: float) -> None:
        now = time.time()
        if now - self.last_trigger_time <= self.cooldown:
            return

        self.last_trigger_time = now
        msg = f"Audio {trigger_type}! Level: {db:.1f} dB (Bg: {self.background_db:.1f}, Thresh: {threshold:.1f})"
        logging.info(msg)

        if self.callback:
            try:
                self.callback(db, msg)
            except Exception:
                pass
