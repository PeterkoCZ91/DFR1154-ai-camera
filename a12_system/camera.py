"""ESP32 MJPEG stream handler with Keep-Alive and frame processing."""

import logging
from urllib.parse import urlsplit
import queue
import threading
import time

import cv2
import numpy as np
import requests

from .mdns_resolver import resolve_host, url_with_host

# How far back from a JPEG's SOI marker we look for the multipart part headers.
# The camera emits Content-Type, Content-Length and a couple of X- headers, so a
# few hundred bytes is plenty; the bound keeps a malformed stream from making us
# rescan the whole buffer on every chunk.
_PART_HEADER_LOOKBEHIND = 512


def _part_content_length(buffer: bytes, soi: int) -> int | None:
    """Content-Length declared for the JPEG part starting at `soi`, or None.

    MJPEG parts carry the exact payload size. Using it — instead of scanning for
    the next EOI marker — is what makes a truncated frame detectable: without it a
    cut-short frame is silently glued to the following one and decodes into a
    corrupt image that no error path catches.
    """
    start = max(0, soi - _PART_HEADER_LOOKBEHIND)
    headers = buffer[start:soi]
    key = headers.lower().rfind(b"content-length:")
    if key == -1:
        return None
    line_end = headers.find(b"\r\n", key)
    if line_end == -1:
        return None
    try:
        declared = int(headers[key + len(b"content-length:") : line_end].strip())
    except ValueError:
        return None
    # Sanity bound: a UXGA JPEG is a few hundred kB. Anything wilder means we
    # misread a header and should fall back to marker scanning.
    if declared <= 0 or declared > 2_000_000:
        return None
    return declared


class Camera:
    # Last raw JPEG delivered by the stream — forensic evidence for flat-frame
    # episodes (a second stream client starves, so this is the only way to see
    # the actual bytes A12 received).
    last_raw_jpg: bytes | None = None

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
        if "://" not in base_url:
            base_url = f"http://{base_url}"

        parsed = urlsplit(base_url)
        self._protocol = parsed.scheme or "http"
        self._configured_host = parsed.hostname or base_url.split("://", 1)[-1].split(":", 1)[0]
        self._base_url = base_url
        self._resolved_host: str | None = None
        self._refresh_urls(force=True)

    def _refresh_urls(self, *, force: bool = False) -> None:
        host = resolve_host(self._configured_host, force=force)
        if host != self._resolved_host:
            self._resolved_host = host
            if host != self._configured_host:
                logging.info(f"{self.log_prefix} Camera host {self._configured_host} resolved to {host}")

        base_url = url_with_host(self._base_url, host)
        self.stream_base_url = f"{self._protocol}://{host}:{self.stream_port}"
        self.audio_base_url = f"{self._protocol}://{host}:{self.audio_port}"

        self.stream_url = f"{self.stream_base_url}/detection-stream"
        self.audio_stream_url = f"{self.audio_base_url}/audio.wav"

        self.status_url = f"{base_url}/health"
        self.reboot_url = f"{base_url}/reboot"
        self.settings_url = f"{base_url}/settings"
        self.ir_control_url = f"{base_url}/ir-control"
        self.ir_status_url = f"{base_url}/ir-status"

    def reset_session(self) -> None:
        """Discard the pooled HTTP session and start a clean one.

        The 2026-07-10 hung-gray episode survived every in-process reconnect that
        reused this session's connection pool; only a fresh process (fresh session)
        recovered. Rebuilding the session removes any stale pooled-connection state
        as a reconnect variable.
        """
        old = self.session
        self.session = requests.Session()
        self.session.auth = old.auth
        try:
            old.close()
        except Exception as e:
            logging.debug(f"{self.log_prefix} Old session close failed: {e}")

    def reboot(self) -> bool:
        """Ask the camera to soft-reboot over the LAN (POST /reboot).

        A soft ESP.restart clears the OV3660 hung-sensor (uniform gray) state
        without a physical power-cycle (verified 2026-07-11). The camera drops
        offline for ~15-25s; the stream reconnect loop reconnects to the fresh boot.
        """
        try:
            self._refresh_urls()
            resp = self.session.post(self.reboot_url, timeout=5)
            ok = resp.status_code == 200
            logging.warning(
                f"{self.log_prefix} Camera reboot requested via {self.reboot_url} "
                f"(HTTP {resp.status_code})"
            )
            return ok
        except Exception as e:
            # A reboot that closes the connection mid-response can surface as an
            # exception even though the device is rebooting — treat as best-effort.
            logging.warning(f"{self.log_prefix} Camera reboot request error: {e}")
            return False

    def get_stream(self) -> requests.Response | None:
        """Connect to MJPEG stream with back-off retries."""
        self.reset_session()
        self._refresh_urls(force=True)
        logging.info(f"{self.log_prefix} Connecting to {self.stream_url}...")
        retries = 0
        max_retries = 3
        base_delay = 2

        while retries < max_retries:
            try:
                self._refresh_urls(force=retries > 0)
                response = self.session.get(self.stream_url, stream=True, timeout=(5, 30))
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
            self._refresh_urls()
            response = self.session.get(self.status_url, timeout=2)
            if response.status_code == 200:
                return response.json().get("wifi_rssi")
        except Exception:
            pass
        return None

    def get_health(self) -> dict | None:
        """Fetch health status."""
        try:
            self._refresh_urls()
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
                self._refresh_urls(force=attempt > 0)
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
            self._refresh_urls()
            response = self.session.post(self.ir_control_url, json=payload, timeout=3)
            return response.status_code == 200
        except Exception as e:
            logging.error(f"{self.log_prefix} IR set error: {e}")
        return False

    def get_ir_status(self) -> dict | None:
        """Get IR status."""
        try:
            self._refresh_urls()
            response = self.session.get(self.ir_status_url, timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        return None

    def process_stream(
        self, response: requests.Response, callback, target_fps_getter=None,
        reconnect_requested=None,
    ) -> None:
        """Read frames from MJPEG stream and call callback for each frame."""
        raw_queue: queue.Queue[bytes] = queue.Queue(maxsize=3)
        frame_queue: queue.Queue = queue.Queue(maxsize=1)
        stop_event = threading.Event()

        def put_drop_oldest(target_queue: queue.Queue, item) -> None:
            try:
                target_queue.put_nowait(item)
                return
            except queue.Full:
                pass
            try:
                target_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                target_queue.put_nowait(item)
            except queue.Full:
                pass

        def drain_thread():
            buffer = b""
            frame_count = 0
            truncated_frames = 0

            logging.info(f"{self.log_prefix} Stream drain thread started")
            try:
                for chunk in response.iter_content(chunk_size=8192):
                    if stop_event.is_set():
                        break
                    if not chunk:
                        continue
                    buffer += chunk
                    if len(buffer) > 2_000_000:
                        logging.warning(f"{self.log_prefix} MJPEG buffer overflow; dropping stale bytes")
                        buffer = buffer[-200_000:]

                    while True:
                        a = buffer.find(b"\xff\xd8")
                        if a == -1:
                            # No SOI in sight; keep only a tail in case one is split
                            # across the chunk boundary.
                            if len(buffer) > 4:
                                buffer = buffer[-4:]
                            break

                        # Prefer the part's Content-Length over hunting for EOI.
                        # Scanning for FFD9 alone silently concatenates a truncated
                        # frame with the next one (the camera's socket send timeout
                        # does cut frames short), and OpenCV happily decodes the
                        # mangled result — which then feeds the flat/dark watchdog
                        # and can trigger a spurious camera reboot.
                        declared = _part_content_length(buffer, a)
                        if declared is not None:
                            if len(buffer) - a < declared:
                                buffer = buffer[a:]   # wait for the rest
                                break
                            jpg = buffer[a : a + declared]
                            buffer = buffer[a + declared :]
                            if not (jpg.startswith(b"\xff\xd8") and jpg.endswith(b"\xff\xd9")):
                                truncated_frames += 1
                                continue          # drop it, do not hand on a partial JPEG
                            frame_count += 1
                            put_drop_oldest(raw_queue, jpg)
                            continue

                        # No usable Content-Length: fall back to marker scanning.
                        b = buffer.find(b"\xff\xd9", a + 2)
                        if b == -1:
                            if a > 0:
                                buffer = buffer[a:]
                            break
                        jpg = buffer[a : b + 2]
                        buffer = buffer[b + 2 :]
                        frame_count += 1
                        put_drop_oldest(raw_queue, jpg)
            except Exception as e:
                logging.error(f"{self.log_prefix} Stream drain error: {e}")
            finally:
                stop_event.set()
                response.close()
                logging.info(
                    f"{self.log_prefix} Stream drain thread stopped "
                    f"(raw_frames={frame_count}, truncated={truncated_frames})"
                )

        def decode_thread():
            last_decode_time = 0.0
            decode_errors = 0
            last_decode_error_log = 0.0
            decoded_frames = 0

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

            logging.info(f"{self.log_prefix} Decode thread started")
            while not stop_event.is_set():
                try:
                    jpg = raw_queue.get(timeout=1.0)
                except queue.Empty:
                    continue

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
                    else:
                        decoded_frames += 1
                        self.last_raw_jpg = jpg
                        put_drop_oldest(frame_queue, frame)
                except Exception as e:
                    note_decode_error(str(e))
            logging.info(
                f"{self.log_prefix} Decode thread stopped "
                f"(decoded_frames={decoded_frames}, decode_errors={decode_errors})"
            )

        drain = threading.Thread(target=drain_thread, daemon=True)
        decoder = threading.Thread(target=decode_thread, daemon=True)
        drain.start()
        decoder.start()

        last_frame_time = time.time()

        try:
            while not stop_event.is_set():
                if reconnect_requested is not None and reconnect_requested():
                    logging.warning(
                        f"{self.log_prefix} Forced stream reconnect "
                        f"(flat-frame watchdog). Rebuilding connection."
                    )
                    break
                if time.time() - last_frame_time > 20:
                    logging.warning(f"{self.log_prefix} Stream frozen (20s timeout). Reconnecting...")
                    break

                try:
                    frame = frame_queue.get(timeout=1.0)
                    last_frame_time = time.time()
                    callback(frame)
                except queue.Empty:
                    if not drain.is_alive() or not decoder.is_alive():
                        break
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            # Close the response BEFORE joining. The drain thread is parked inside
            # response.iter_content() and only notices stop_event after the next
            # chunk arrives — up to the 30 s read timeout. Until it returns, the
            # socket stays open and the camera still counts us as a connected
            # detection client, so the immediate reconnect below is rejected
            # (the camera caps that route) and we blame the camera for a stall we
            # are causing ourselves. Session.close() does not help here: it only
            # closes pooled connections, not the one currently checked out.
            try:
                response.close()
            except Exception as e:
                logging.debug(f"{self.log_prefix} Stream response close failed: {e}")
            drain.join(timeout=2)
            decoder.join(timeout=2)
