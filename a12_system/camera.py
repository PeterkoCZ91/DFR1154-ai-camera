"""ESP32 MJPEG stream handler with Keep-Alive and frame processing."""

import logging
import queue
import threading
import time

import cv2
import numpy as np
import requests


class Camera:
    def __init__(self, config: dict, stream_port: int = 81, audio_port: int = 82):
        self.config = config
        self.stream_port = stream_port
        self.audio_port = audio_port

        self.session = requests.Session()

        # Parse base URL
        base_url = config["camera_url"]
        if "://" in base_url:
            protocol, address = base_url.split("://")
            host = address.split(":")[0]
        else:
            protocol = "http"
            host = base_url.split(":")[0]

        self.stream_base_url = f"{protocol}://{host}:{stream_port}"
        self.audio_base_url = f"{protocol}://{host}:{audio_port}"

        # Endpoints
        self.stream_url = f"{self.stream_base_url}/detection-stream"
        self.audio_stream_url = f"{self.audio_base_url}/audio.wav"

        # Control endpoints (port 80)
        self.status_url = f"{config['camera_url']}/health"
        self.settings_url = f"{config['camera_url']}/settings"
        self.ir_control_url = f"{config['camera_url']}/ir-control"
        self.ir_status_url = f"{config['camera_url']}/ir-status"

    def get_stream(self) -> requests.Response | None:
        """Connect to MJPEG stream with back-off retries."""
        logging.info(f"Connecting to {self.stream_url}...")
        retries = 0
        max_retries = 3
        base_delay = 2

        while retries < max_retries:
            try:
                response = self.session.get(self.stream_url, stream=True, timeout=5)
                if response.status_code != 200:
                    logging.error(f"Bad status: {response.status_code}")
                    response.close()
                    time.sleep(base_delay)
                    retries += 1
                    continue
                logging.info("Stream connected!")
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                logging.warning(f"Connection failed: {e}")
                time.sleep(base_delay)
                retries += 1
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
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
            logging.debug(f"Health check failed: {e}")
        return None

    def set_camera_settings(self, settings: dict, retries: int = 3) -> bool:
        """Configure camera settings via POST."""
        for attempt in range(retries):
            try:
                logging.info(f"Applying settings (attempt {attempt + 1})...")
                response = self.session.post(self.settings_url, json=settings, timeout=5)
                if response.status_code == 200:
                    logging.info("Settings applied")
                    return True
            except Exception as e:
                logging.warning(f"Settings failed: {e}")
                time.sleep(1)
        return False

    def set_ir_mode(self, auto_mode: bool = True, manual_state: bool = False) -> bool:
        """Control IR LED."""
        payload = {"auto_mode": auto_mode, "manual_state": manual_state}
        try:
            response = self.session.post(self.ir_control_url, json=payload, timeout=3)
            return response.status_code == 200
        except Exception as e:
            logging.error(f"IR Set Error: {e}")
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

    def process_stream(self, response: requests.Response, callback) -> None:
        """Read frames from MJPEG stream and call callback for each frame."""
        frame_queue: queue.Queue = queue.Queue(maxsize=1)
        stop_event = threading.Event()

        def reader_thread():
            buffer = b""
            logging.info("Reader thread started")
            try:
                for chunk in response.iter_content(chunk_size=4096):
                    if stop_event.is_set():
                        break
                    buffer += chunk

                    a = buffer.find(b"\xff\xd8")
                    b = buffer.find(b"\xff\xd9")

                    if a != -1 and b != -1:
                        jpg = buffer[a : b + 2]
                        buffer = buffer[b + 2 :]

                        try:
                            frame = cv2.imdecode(
                                np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR
                            )
                            if frame is not None and not frame_queue.full():
                                frame_queue.put(frame)
                        except Exception:
                            pass
            except Exception as e:
                logging.error(f"Stream error: {e}")
            finally:
                stop_event.set()
                response.close()
                logging.info("Reader thread stopped")

        t = threading.Thread(target=reader_thread, daemon=True)
        t.start()

        last_frame_time = time.time()

        try:
            while not stop_event.is_set():
                if time.time() - last_frame_time > 20:
                    logging.warning("Stream frozen (20s timeout). Reconnecting...")
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
