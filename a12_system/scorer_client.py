"""HTTP client for the shared YOLO scoring service."""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


def score_image(url, jpeg_bytes_or_path, timeout=10):
    """POST JPEG bytes (or a JPEG path) to the scorer; dict on success, None on failure."""
    try:
        if isinstance(jpeg_bytes_or_path, (bytes, bytearray, memoryview)):
            body = bytes(jpeg_bytes_or_path)
        else:
            path = os.fspath(jpeg_bytes_or_path)
            with open(path, "rb") as fh:
                body = fh.read()
    except OSError as e:
        log.warning("scorer: cannot read image: %s", e)
        return None

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "image/jpeg"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        try:
            return json.load(resp)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - remote scorer outage must never crash detection
        log.warning("scorer request failed: %s", e)
        return None
