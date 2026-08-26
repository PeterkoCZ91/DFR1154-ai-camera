import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a12_system import scorer_client


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self, *_args):
        return self._body

    def close(self):
        pass


class _RawResponse(_Response):
    def __init__(self, body):
        self._body = body


def test_score_metrics_count_success_without_retaining_payload(monkeypatch):
    metrics = scorer_client.ScorerMetrics()
    monkeypatch.setattr(
        scorer_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"classes": {"person": 0.81}}),
    )

    result = scorer_client.score_image(
        "http://scorer.example/score", b"jpeg", metrics=metrics
    )

    assert result["classes"]["person"] == 0.81
    snapshot = metrics.snapshot()
    assert {key: snapshot[key] for key in (
        "requests", "completed", "successes", "malformed_responses",
        "transport_failures", "image_read_failures", "fallbacks", "failure_reasons",
    )} == {
        "requests": 1,
        "completed": 1,
        "successes": 1,
        "malformed_responses": 0,
        "transport_failures": 0,
        "image_read_failures": 0,
        "fallbacks": 0,
        "failure_reasons": {},
    }
    assert snapshot["request_seconds_p50"] >= 0.0
    assert snapshot["request_seconds_p95"] >= snapshot["request_seconds_p50"]
    assert snapshot["request_seconds_max"] >= snapshot["request_seconds_p95"]


def test_score_metrics_classify_malformed_and_transport_failures(monkeypatch):
    metrics = scorer_client.ScorerMetrics()
    responses = iter([_Response({"wrong": True}), OSError("unreachable")])

    def urlopen(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(scorer_client.urllib.request, "urlopen", urlopen)

    assert scorer_client.score_image("http://scorer.example/score", b"jpeg", metrics=metrics) is None
    assert scorer_client.score_image("http://scorer.example/score", b"jpeg", metrics=metrics) is None
    metrics.record_fallback()

    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 2
    assert snapshot["completed"] == 2
    assert snapshot["successes"] == 0
    assert snapshot["malformed_responses"] == 1
    assert snapshot["transport_failures"] == 1
    assert snapshot["fallbacks"] == 1
    assert snapshot["failure_reasons"] == {
        "malformed_response": 1,
        "transport_error": 1,
    }
    # The exception class name survives; its text (which can carry the URL) does not.
    assert snapshot["error_kinds"] == {"invalid_schema": 1, "OSError": 1}


def test_score_metrics_keep_http_status_out_of_transport_failures(monkeypatch):
    """A 503 means the scorer answered. Counting it as a transport failure would
    hide scorer-side breakage behind an apparent network outage."""
    metrics = scorer_client.ScorerMetrics()

    def urlopen(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://scorer.example/score", 503, "busy", {}, None
        )

    monkeypatch.setattr(scorer_client.urllib.request, "urlopen", urlopen)

    assert scorer_client.score_image(
        "http://scorer.example/score", b"jpeg", metrics=metrics
    ) is None

    snapshot = metrics.snapshot()
    assert snapshot["http_errors"] == 1
    assert snapshot["transport_failures"] == 0
    assert snapshot["failure_reasons"] == {"http_error": 1}
    assert snapshot["error_kinds"] == {"HTTP 503": 1}


def test_score_metrics_count_an_unparseable_body_as_malformed(monkeypatch):
    metrics = scorer_client.ScorerMetrics()
    monkeypatch.setattr(
        scorer_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _RawResponse(b"<html>gateway error</html>"),
    )

    assert scorer_client.score_image(
        "http://scorer.example/score", b"jpeg", metrics=metrics
    ) is None

    snapshot = metrics.snapshot()
    assert snapshot["malformed_responses"] == 1
    assert snapshot["transport_failures"] == 0
    assert snapshot["failure_reasons"] == {"malformed_response": 1}


def test_error_kinds_stay_bounded():
    metrics = scorer_client.ScorerMetrics()
    for index in range(scorer_client.ERROR_KIND_LIMIT + 5):
        metrics.transport_failure(0.0, f"Kind{index}")

    kinds = metrics.snapshot()["error_kinds"]
    assert len(kinds) == scorer_client.ERROR_KIND_LIMIT + 1
    assert kinds["other"] == 5


def test_score_metrics_report_latency_and_send_source_id(monkeypatch):
    metrics = scorer_client.ScorerMetrics()
    seen = []
    monkeypatch.setattr(
        scorer_client.urllib.request,
        "urlopen",
        lambda request, **_kwargs: seen.append(
            request.get_header("X-tapo-source-id")
        ) or _Response({"classes": {"person": 0.4}}),
    )

    scorer_client.score_image(
        "http://scorer.example/score",
        b"jpeg",
        metrics=metrics,
        source_id="0123456789abcdef",
    )

    snapshot = metrics.snapshot()
    assert seen == ["0123456789abcdef"]
    assert snapshot["request_seconds_p50"] >= 0.0
    assert snapshot["request_seconds_p95"] >= snapshot["request_seconds_p50"]


def test_default_source_id_is_hashed_and_used_when_none_is_passed(monkeypatch):
    monkeypatch.setenv("YOLO_SCORER_SOURCE_ID", "a12-camera")
    monkeypatch.delenv("CAMERA_ID", raising=False)
    expected = scorer_client.source_id_for("a12-camera")
    seen = []
    monkeypatch.setattr(
        scorer_client.urllib.request,
        "urlopen",
        lambda request, **_kwargs: seen.append(request.get_header("X-tapo-source-id"))
        or _Response({"classes": {"person": 0.4}}),
    )

    scorer_client.score_image(
        "http://scorer.example/score", b"jpeg", metrics=scorer_client.ScorerMetrics()
    )

    assert seen == [expected]
    assert len(expected) == 16 and "a12-camera" not in expected


def test_score_metrics_count_image_read_failure(tmp_path):
    metrics = scorer_client.ScorerMetrics()

    assert scorer_client.score_image(
        "http://scorer.example/score", tmp_path / "missing.jpg", metrics=metrics
    ) is None

    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 0
    assert snapshot["image_read_failures"] == 1
    assert snapshot["failure_reasons"] == {"image_read": 1}
    assert snapshot["error_kinds"] == {"FileNotFoundError": 1}
