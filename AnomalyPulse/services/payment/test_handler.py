"""Local unit tests for the Payment handler. No AWS needed.

Run from this folder:  python3 -m pytest -v
"""
import io
import json
import os
from contextlib import redirect_stdout

import handler as payment
from unittest import mock

SCHEMA_FIELDS = {
    "timestamp", "request_id", "service", "endpoint", "http_method",
    "status_code", "latency_ms", "lambda_duration_ms", "cold_start",
    "db_latency_ms", "db_consumed_capacity",
    "dependency", "dependency_latency_ms", "dependency_error",
}
TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}


def _event():
    return {"resource": "/payments", "httpMethod": "POST", "body": "{}"}


def _invoke():
    """Calls the handler and captures both its return value and its one log line."""
    out = io.StringIO()
    with redirect_stdout(out):
        resp = payment.handler(_event(), None)
    log_lines = out.getvalue().strip().splitlines()
    assert len(log_lines) == 1, "exactly one log line per request"
    return resp, json.loads(resp["body"]), json.loads(log_lines[0])


def setup_function():
    """Runs before every test: reset knobs to 'off' so tests don't leak into each other."""
    os.environ["PAYMENT_LATENCY_MS"] = "0"
    os.environ["PAYMENT_FAILURE_RATE"] = "0.0"
    os.environ["PAYMENT_TIMEOUT"] = "false"


def test_success_by_default():
    resp, body, log = _invoke()
    assert resp["statusCode"] == 200
    assert body["data"]["outcome"] == "success"
    assert body["error"] is None
    assert log["status_code"] == 200


def test_guaranteed_failure_is_5xx():
    # An injected fault must surface as a 5xx so the DEPENDENCY_FAILURE incident lands in the
    # 5xx-rate feature, not the distinct 4xx-rate one (PROJECT_PLAN Sections 3 and 5).
    os.environ["PAYMENT_FAILURE_RATE"] = "1.0"
    resp, body, log = _invoke()
    assert resp["statusCode"] == 503
    assert body["data"] is None
    assert body["error"]["code"] == "PAYMENT_UNAVAILABLE"
    assert log["status_code"] == 503


def test_guaranteed_success():
    os.environ["PAYMENT_FAILURE_RATE"] = "0.0"
    resp, body, _ = _invoke()
    assert resp["statusCode"] == 200
    assert body["data"]["outcome"] == "success"


def test_injected_latency_raises_wall_clock_not_cpu():
    # An injected latency fault is a sleep: it must show up in wall-clock latency_ms
    # but NOT in CPU lambda_duration_ms. That split is what distinguishes a
    # dependency-latency incident from LAMBDA_DEGRADATION (added computation).
    os.environ["PAYMENT_LATENCY_MS"] = "200"
    _, _, log = _invoke()
    assert log["latency_ms"] >= 200
    assert log["lambda_duration_ms"] < 200


def test_log_line_matches_schema_and_hides_tier2():
    _, _, log = _invoke()
    assert set(log) == SCHEMA_FIELDS
    assert not (set(log) & TIER2_FIELDS)
    assert log["service"] == "payment-service"
    assert log["dependency"] is None
    assert log["db_latency_ms"] is None

def test_timeout_knob_sleeps_30_seconds():
    os.environ["PAYMENT_TIMEOUT"] = "true"
    with mock.patch("handler.time.sleep") as fake_sleep:
        _invoke()
    fake_sleep.assert_any_call(30)


def test_unhandled_route_returns_501():
    out = io.StringIO()
    with redirect_stdout(out):
        resp = payment.handler({"resource": "/payments", "httpMethod": "GET"}, None)
    assert resp["statusCode"] == 501
    assert json.loads(resp["body"])["error"]["code"] == "NOT_IMPLEMENTED"
    # Still exactly one telemetry line, even on the rejected path.
    assert len(out.getvalue().strip().splitlines()) == 1


def test_unexpected_exception_returns_envelope_not_trace():
    # An unexpected error must yield a structured 500 (never a stack trace) and still emit
    # exactly one log line, so the "one JSON log line per request" invariant always holds.
    with mock.patch("handler.random.random", side_effect=RuntimeError("boom")):
        resp, body, log = _invoke()
    assert resp["statusCode"] == 500
    assert body["data"] is None
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in resp["body"]
    assert log["status_code"] == 500


def test_cold_start_only_first_invocation():
    payment._cold_start = True
    _, _, first = _invoke()
    _, _, second = _invoke()
    assert first["cold_start"] is True
    assert second["cold_start"] is False


def test_response_has_cors_header():
    # The dashboard is a cross-origin browser client, so every response
    # (including error envelopes) must carry Access-Control-Allow-Origin.
    resp, _, _ = _invoke()
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    os.environ["PAYMENT_FAILURE_RATE"] = "1.0"
    err, _, _ = _invoke()
    assert err["headers"]["Access-Control-Allow-Origin"] == "*"