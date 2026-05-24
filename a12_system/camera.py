"""ESP32 MJPEG stream handler with Keep-Alive and frame processing."""

import logging
import queue
import threading
import time

import cv2
import numpy as np
import requests


class Camera:
    def __init__(self, config: dict, stream_port: int | None = None, audio_port: int | None = None):
        self.config = config
        self.camera_id = str(config.get("camera_id", "esp32_cam")).strip() or "esp32_cam"
        self.camera_name = str(config.get("camera_name", self.camera_id)).strip() or self.camera_id
        self.log_prefix = f"[{self.camera_id}:{self.camera_name}]"
        self.stream_port = int(stream_port or config.get("stream_port", 81))
        self.audio_port = int(audio_port or config.get("audio_port", 82))

        self.session = requests.Session()
        user = config.get("camera_http_user", "admin")
        pwd = config.get("camera_http_pass", "admin")
        if user:
            self.session.auth = (user, pwd)

        base_url = config["camera_url"].rstrip("/")
        if "://" in base_url:
            protocol, address = base_url.split("://", 1)
            host = address.split(":")[0]
        else:
            protocol = "http"
            host = base_url.split(":")[0]
            base_url = f"{protocol}://{host}"

        self.stream_base_url = f"{protocol}://{host}:{self.stream_port}"
        self.audio_base_url = f"{protocol}://{host}:{self.audio_port}"

        self.stream_url = f"{self.stream_base_url}/detection-stream"
        self.audio_stream_url = f"{self.audio_base_url}/audio.wav"

        self.status_url = f"{base_url}/health"
        self.settings_url = f"{base_url}/settings"
        self.ir_control_url = f"{base_url}/ir-control"
        self.ir_status_url = f"{base_url}/ir-status"

    def get_stream(self) -> requests.Response | None:
        """Connect to MJPEG stream with back-off retries."""
        logging.info(f"{self.log_prefix} Connecting to {self.stream_url}...")
        retries = 0
        max_retries = 3
        base_delay = 2

        while retries < max_retries:
            try:
                response = self.session.get(self.stream_url, stream=True, timeout=(5, 15))
                if response.status_code != 200:
                    logging.error(f"{self.log_prefix} Bad stream status: {response.status_code}")
                    response.close()
                    time.sleep(base_delay)
                    retries += 1
                    continue
                logging.info(f"{self.log_prefix} Stream connected")
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                logging.warning(f"{self.log_prefix} Connection failed: {e}")
                time.sleep(base_delay)
                retries += 1
            except Exception as e:
                logging.error(f"{self.log_prefix} Unexpected stream error: {e}")
                return None
        return None

    def get_rssi(self) -> int | None:
        """Fetch RSSI (fast, non-blocking)."""
        try:
            response = self.session.get(self.status_url, timeout=2)
            if response.status_code == 200:
                return response.json().get("wifi_rssi")
        except Exception:
            pass
        return None

    def get_health(self) -> dict | None:
        """Fetch health status."""
        try:
            response = self.session.get(self.status_url, timeout=3)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logging.debug(f"{self.log_prefix} Health check failed: {e}")
        return None

    def set_camera_settings(self, settings: dict, retries: int = 3) -> bool:
        """Configure camera settings via POST."""
        for attempt in range(retries):
            try:
                logging.info(f"{self.log_prefix} Applying settings (attempt {attempt + 1})...")
                response = self.session.post(self.settings_url, json=settings, timeout=5)
                if response.status_code == 200:
                    logging.info(f"{self.log_prefix} Settings applied")
                    return True
            except Exception as e:
                logging.warning(f"{self.log_prefix} Settings failed: {e}")
                time.sleep(1)
        return False

    def set_ir_mode(self, auto_mode: bool = True, manual_state: bool = False) -> bool:
        """Control IR LED."""
        payload = {"auto_mode": auto_mode, "manual_state": manual_state}
        try:
            response = self.session.post(self.ir_control_url, json=payload, timeout=3)
            return response.status_code == 200
        except Exception as e:
            logging.error(f"{self.log_prefix} IR set error: {e}")
        return False

    def get_ir_status(self) -> dict | None:
        """Get IR status."""
        try:
            response = self.session.get(self.ir_status_url, timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        return None

    def process_stream(self, response: requests.Response, callback, target_fps_getter=None) -> None:
        """Read frames from MJPEG stream and call callback for each frame."""
        frame_queue: queue.Queue = queue.Queue(maxsize=1)
        stop_event = threading.Event()

        def reader_thread():
            buffer = b""
            last_decode_time = 0.0
            decode_errors = 0
            last_decode_error_log = 0.0

            def note_decode_error(reason: str) -> None:
                nonlocal decode_errors, last_decode_error_log
                decode_errors += 1
                now = time.time()
                if now - last_decode_error_log >= 60:
                    logging.warning(
                        f"{self.log_prefix} MJPEG decode errors: {decode_errors} "
                        f"(last={reason})"
                    )
                    last_decode_error_log = now

            logging.info(f"{self.log_prefix} Reader thread started")
            try:
                for chunk in response.iter_content(chunk_size=4096):
                    if stop_event.is_set():
                        break
                    buffer += chunk
                    if len(buffer) > 2_000_000:
                        logging.warning(f"{self.log_prefix} MJPEG buffer overflow; dropping stale bytes")
                        buffer = buffer[-200_000:]

                    a = buffer.find(b"\xff\xd8")
                    b = buffer.find(b"\xff\xd9")

                    if a != -1 and b != -1:
                        jpg = buffer[a : b + 2]
                        buffer = buffer[b + 2 :]

                        try:
                            target_fps = 0
                            if target_fps_getter:
                                target_fps = max(0, int(target_fps_getter()))
                            if target_fps > 0:
                                now = time.time()
                                if now - last_decode_time < 1.0 / target_fps:
                                    continue
                                last_decode_time = now

                            frame = cv2.imdecode(
                                np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR
                            )
                            if frame is None:
                                note_decode_error("imdecode returned None")
                            elif not frame_queue.full():
                                frame_queue.put(frame)
                        except Exception as e:
                            note_decode_error(str(e))
            except Exception as e:
                logging.error(f"{self.log_prefix} Stream error: {e}")
            finally:
                stop_event.set()
                response.close()
                logging.info(f"{self.log_prefix} Reader thread stopped")

        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()

        last_frame_time = time.time()

        try:
            while not stop_event.is_set():
                if time.time() - last_frame_time > 20:
                    logging.warning(f"{self.log_prefix} Stream frozen (20s timeout). Reconnecting...")
                    break

                try:
                    frame = frame_queue.get(timeout=1.0)
                    last_frame_time = time.time()
                    callback(frame)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            t.join(timeout=2)
