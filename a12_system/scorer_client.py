"""HTTP client for the shared YOLO scoring service."""

import json
import hashlib
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque

log = logging.getLogger(__name__)

LATENCY_SAMPLE_LIMIT = 256
# Distinct exception/status names kept before everything else buckets into
# "other" — the set urllib can raise is small, this only bounds the outlier.
ERROR_KIND_LIMIT = 16


def source_id_for(value):
    """Return a stable pseudonymous 16-hex source ID without sending the input."""
    return hashlib.sha256(f"a12-source:{value}".encode("utf-8")).hexdigest()[:16]


def default_source_id():
    """Use an explicit source label or stable camera ID, never send either raw."""
    value = os.environ.get("YOLO_SCORER_SOURCE_ID") or os.environ.get("CAMERA_ID")
    return source_id_for(value) if value else None


def _percentile(samples, fraction):
    if not samples:
        return 0.0
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class ScorerMetrics:
    """Thread-safe aggregate scorer telemetry; never stores image data or URLs.

    Latency covers every attempt that reached a verdict, failures included: a
    request that burned the whole deadline is exactly what the timeout knob has
    to be chosen against, so hiding it from the tail would defeat the purpose.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = 0
        self.completed = 0
        self.successes = 0
        self.malformed_responses = 0
        self.transport_failures = 0
        self.http_errors = 0
        self.image_read_failures = 0
        self.fallbacks = 0
        self.failure_reasons = {}
        self.error_kinds = {}
        self._latency_samples = deque(maxlen=LATENCY_SAMPLE_LIMIT)
        self._latency_max = 0.0

    def _failure(self, reason):
        self.completed += 1
        self.failure_reasons[reason] = self.failure_reasons.get(reason, 0) + 1

    def _kind(self, kind):
        """Count one exception class name (or HTTP status) behind a failure.

        Names only, never exception text — that can carry the scorer URL. This
        is what tells a timeout apart from a refused connection after the fact.
        """
        if not kind:
            return
        if kind not in self.error_kinds and len(self.error_kinds) >= ERROR_KIND_LIMIT:
            kind = "other"
        self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1

    def _observe(self, elapsed):
        if elapsed is None:
            return
        self._latency_samples.append(elapsed)
        self._latency_max = max(self._latency_max, elapsed)

    def begin(self):
        with self._lock:
            self.requests += 1

    def success(self, elapsed=None):
        with self._lock:
            self.completed += 1
            self.successes += 1
            self._observe(elapsed)

    def malformed(self, elapsed=None, kind=None):
        with self._lock:
            self.malformed_responses += 1
            self._failure("malformed_response")
            self._kind(kind)
            self._observe(elapsed)

    def transport_failure(self, elapsed=None, kind=None):
        with self._lock:
            self.transport_failures += 1
            self._failure("transport_error")
            self._kind(kind)
            self._observe(elapsed)

    def http_error(self, status, elapsed=None):
        """A scorer that answered non-2xx is reachable — not a transport failure.

        Collapsing the two hides scorer-side breakage behind "network trouble".
        """
        with self._lock:
            self.http_errors += 1
            self._failure("http_error")
            self._kind(f"HTTP {status}")
            self._observe(elapsed)

    def image_read_failure(self, kind=None):
        with self._lock:
            self.image_read_failures += 1
            self.failure_reasons["image_read"] = self.failure_reasons.get("image_read", 0) + 1
            self._kind(kind)

    def record_fallback(self):
        with self._lock:
            self.fallbacks += 1

    def snapshot(self):
        with self._lock:
            return {
                "requests": self.requests,
                "completed": self.completed,
                "successes": self.successes,
                "malformed_responses": self.malformed_responses,
                "transport_failures": self.transport_failures,
                "http_errors": self.http_errors,
                "image_read_failures": self.image_read_failures,
                "fallbacks": self.fallbacks,
                "failure_reasons": dict(self.failure_reasons),
                "error_kinds": dict(self.error_kinds),
                "request_seconds_p50": round(_percentile(self._latency_samples, 0.50), 6),
                "request_seconds_p95": round(_percentile(self._latency_samples, 0.95), 6),
                "request_seconds_max": round(self._latency_max, 6),
            }


METRICS = ScorerMetrics()


def metrics_snapshot():
    """Return aggregate scorer-client telemetry with no request payload data."""
    return METRICS.snapshot()


def record_fallback():
    """Count a caller-side local-detector fallback after scorer unavailability."""
    METRICS.record_fallback()


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


def score_image(url, jpeg_bytes_or_path, timeout=10, *, metrics=None, source_id=None):
    """POST JPEG bytes (or a JPEG path) to the scorer; dict on success, None on failure."""
    metrics = metrics or METRICS
    try:
        if isinstance(jpeg_bytes_or_path, (bytes, bytearray, memoryview)):
            body = bytes(jpeg_bytes_or_path)
        else:
            path = os.fspath(jpeg_bytes_or_path)
            with open(path, "rb") as fh:
                body = fh.read()
    except OSError as e:
        metrics.image_read_failure(type(e).__name__)
        log.warning("scorer: cannot read image (%s)", type(e).__name__)
        return None

    metrics.begin()
    started = time.monotonic()
    headers = {"Content-Type": "image/jpeg"}
    source_id = source_id or default_source_id()
    if isinstance(source_id, str) and len(source_id) == 16 and all(
        character in "0123456789abcdef" for character in source_id.lower()
    ):
        headers["X-Tapo-Source-ID"] = source_id.lower()
    req = urllib.request.Request(url, data=body, headers=headers)

    # Each failure class is separated deliberately: a timeout, a refused
    # connection, a 500 from the scorer and an unparseable body all used to
    # land in one bucket, which made a scorer outage indistinguishable from a
    # scorer bug. Only exception class names are logged, never their text —
    # that can carry the scorer URL.
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        metrics.http_error(e.code, time.monotonic() - started)
        log.warning("scorer request failed: http_status=%s", e.code)
        return None
    except Exception as e:  # noqa: BLE001 - remote scorer outage must never crash detection
        metrics.transport_failure(time.monotonic() - started, type(e).__name__)
        log.warning("scorer request failed: transport_error=%s", type(e).__name__)
        return None

    try:
        payload = json.load(resp)
    except Exception as e:  # noqa: BLE001 - a broken body must not crash detection either
        metrics.malformed(time.monotonic() - started, type(e).__name__)
        log.warning("scorer returned an unparseable body (%s)", type(e).__name__)
        return None
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()

    result = validate_response(payload)
    if result is None:
        metrics.malformed(time.monotonic() - started, "invalid_schema")
        log.warning("scorer returned a malformed response")
    else:
        metrics.success(time.monotonic() - started)
    return result
