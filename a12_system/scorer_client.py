"""HTTP client for the shared YOLO scoring service."""

import json
import logging
import math
import os
import urllib.request

log = logging.getLogger(__name__)


def _score(value):
    """Return one finite confidence in the scorer's closed 0..1 range."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def validate_response(result):
    """Validate and normalize the shared scorer's public JSON response.

    The remote result is advisory: malformed data is treated as an unavailable scorer,
    allowing the detection layer to fall back locally.
    """
    if not isinstance(result, dict) or not isinstance(result.get("classes"), dict):
        return None

    classes = {}
    for label, confidence in result["classes"].items():
        if not isinstance(label, str) or not label:
            return None
        score = _score(confidence)
        if score is None:
            return None
        classes[label] = score

    normalized = dict(result)
    normalized["classes"] = classes
    box = result.get("box")
    if box is not None:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return None
        normalized_box = []
        for value in box:
            if isinstance(value, bool):
                return None
            try:
                value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            normalized_box.append(value)
        if normalized_box[0] >= normalized_box[2] or normalized_box[1] >= normalized_box[3]:
            return None
        normalized["box"] = normalized_box
    return normalized


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
            result = validate_response(json.load(resp))
            if result is None:
                log.warning("scorer returned a malformed response")
            return result
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception as e:  # noqa: BLE001 - remote scorer outage must never crash detection
        log.warning("scorer request failed: %s", e)
        return None
