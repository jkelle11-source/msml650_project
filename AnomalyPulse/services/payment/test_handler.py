"""Local unit tests for the Payment handler. No AWS needed.

Faults come from the control plane (fault_config.active_faults), so these tests
replace active_faults with a fake that returns the fault notices we choose.

Run from this folder:  python3 -m pytest -v
"""
import io
import json
import os
import uuid
from contextlib import redirect_stdout
from types import SimpleNamespace

# fault_config creates a boto3 client when imported, which needs a region. On AWS
# the region is set for us; locally we set it before importing the handler.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")

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


def _set_faults(monkeypatch, faults):
    """Make the handler see exactly these fault notices."""
    monkeypatch.setattr(payment, "active_faults", lambda service: faults)


def _payment_failure(timeout_ms):
    return {"injector": "payment-failure", "params": {"timeout_ms": timeout_ms}}


def _lambda_degradation(added_latency_ms):
    return {"injector": "lambda-degradation", "params": {"added_latency_ms": added_latency_ms}}


def _invoke():
    """Calls the handler and captures both its return value and its one log line."""
    out = io.StringIO()
    with redirect_stdout(out):
        resp = payment.handler(_event(), None)
    log_lines = out.getvalue().strip().splitlines()
    assert len(log_lines) == 1, "exactly one log line per request"
    return resp, json.loads(resp["body"]), json.loads(log_lines[0])


def _run(event, context=None):
    """Invoke the handler with a custom event/context; return its one log line."""
    out = io.StringIO()
    with redirect_stdout(out):
        payment.handler(event, context)
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1, "exactly one log line per request"
    return json.loads(lines[-1])


def test_success_by_default(monkeypatch):
    _set_faults(monkeypatch, [])
    resp, body, log = _invoke()
    assert resp["statusCode"] == 200
    assert body["data"]["outcome"] == "success"
    assert body["error"] is None
    assert log["status_code"] == 200


def test_payment_failure_fault_is_5xx(monkeypatch):
    # An injected fault must surface as a 5xx so the DEPENDENCY_FAILURE incident lands in the
    # 5xx-rate feature, not the distinct 4xx-rate one (PROJECT_PLAN Sections 3 and 5).
    _set_faults(monkeypatch, [_payment_failure(50)])
    resp, body, log = _invoke()
    assert resp["statusCode"] == 503
    assert body["data"] is None
    assert body["error"]["code"] == "PAYMENT_UNAVAILABLE"
    assert log["status_code"] == 503


def test_payment_failure_waits_timeout_ms_before_failing(monkeypatch):
    # The fault waits `timeout_ms` (a sleep) and then fails. time.sleep is mocked so the
    # test doesn't really wait 3.3 seconds.
    _set_faults(monkeypatch, [_payment_failure(3300)])
    with mock.patch("handler.time.sleep") as fake_sleep:
        resp, _, _ = _invoke()
    fake_sleep.assert_any_call(3.3)
    assert resp["statusCode"] == 503


def test_payment_failure_raises_wall_clock_not_cpu(monkeypatch):
    # A payment-failure fault is a wait: it must show up in wall-clock latency_ms but NOT in
    # CPU lambda_duration_ms. That split keeps DEPENDENCY_FAILURE distinct from
    # LAMBDA_DEGRADATION (added computation).
    _set_faults(monkeypatch, [_payment_failure(200)])
    _, _, log = _invoke()
    assert log["latency_ms"] >= 200
    assert log["lambda_duration_ms"] < 200


def test_lambda_degradation_raises_cpu_time(monkeypatch):
    # A lambda-degradation fault is real computation: it must raise lambda_duration_ms
    # (CPU time), and the request still succeeds.
    _set_faults(monkeypatch, [_lambda_degradation(100)])
    resp, _, log = _invoke()
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] >= 100
    assert log["latency_ms"] >= 100


def test_unrelated_fault_is_ignored(monkeypatch):
    # A fault for some other injector must not change payment's behavior.
    _set_faults(monkeypatch, [{"injector": "traffic-spike", "params": {"multiplier": 3}}])
    resp, _, log = _invoke()
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] < 100


def test_malformed_fault_does_not_crash(monkeypatch):
    # active_faults() never raises, but a notice with missing fields must not break a payment.
    _set_faults(monkeypatch, [{"injector": "payment-failure"}, {}])
    resp, _, _ = _invoke()
    assert resp["statusCode"] in (200, 503)


def test_faults_are_read_on_every_request(monkeypatch):
    # active_faults must be called whether or not a fault exists, so its cost is not a
    # fingerprint of incident runs.
    calls = []
    monkeypatch.setattr(payment, "active_faults", lambda service: calls.append(service) or [])
    _invoke()
    _invoke()
    assert calls == ["payment-service", "payment-service"]


def test_log_line_hides_the_fault(monkeypatch):
    # Even while a fault is active, the log line has exactly the schema fields and never
    # mentions the injected values (Tier-2).
    _set_faults(monkeypatch, [_payment_failure(50), _lambda_degradation(20)])
    _, _, log = _invoke()
    assert set(log) == SCHEMA_FIELDS
    assert not (set(log) & TIER2_FIELDS)
    text = json.dumps(log)
    assert "timeout_ms" not in text and "added_latency_ms" not in text
    assert "payment-failure" not in text and "lambda-degradation" not in text


def test_log_line_matches_schema_and_hides_tier2(monkeypatch):
    _set_faults(monkeypatch, [])
    _, _, log = _invoke()
    assert set(log) == SCHEMA_FIELDS
    assert not (set(log) & TIER2_FIELDS)
    assert log["service"] == "payment-service"
    assert log["dependency"] is None
    assert log["db_latency_ms"] is None


def test_unhandled_route_returns_501(monkeypatch):
    _set_faults(monkeypatch, [])
    out = io.StringIO()
    with redirect_stdout(out):
        resp = payment.handler({"resource": "/payments", "httpMethod": "GET"}, None)
    assert resp["statusCode"] == 501
    assert json.loads(resp["body"])["error"]["code"] == "NOT_IMPLEMENTED"
    # Still exactly one telemetry line, even on the rejected path.
    assert len(out.getvalue().strip().splitlines()) == 1


def test_unexpected_exception_returns_envelope_not_trace(monkeypatch):
    # An unexpected error must yield a structured 500 (never a stack trace) and still emit
    # exactly one log line, so the "one JSON log line per request" invariant always holds.
    monkeypatch.setattr(payment, "_read_faults", mock.Mock(side_effect=RuntimeError("boom")))
    resp, body, log = _invoke()
    assert resp["statusCode"] == 500
    assert body["data"] is None
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in resp["body"]
    assert log["status_code"] == 500


def test_cold_start_only_first_invocation(monkeypatch):
    _set_faults(monkeypatch, [])
    payment._cold_start = True
    _, _, first = _invoke()
    _, _, second = _invoke()
    assert first["cold_start"] is True
    assert second["cold_start"] is False


def test_response_has_cors_header(monkeypatch):
    # The dashboard is a cross-origin browser client, so every response
    # (including error envelopes) must carry Access-Control-Allow-Origin.
    _set_faults(monkeypatch, [])
    resp, _, _ = _invoke()
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    _set_faults(monkeypatch, [_payment_failure(10)])
    err, _, _ = _invoke()
    assert err["headers"]["Access-Control-Allow-Origin"] == "*"


def test_telemetry_never_crashes_on_invalid_record(monkeypatch, capsys):
    # If the schema validator ever rejects a record, the handler must log a
    # telemetry_error and continue -- never raise and break the actual payment response.
    import handler as handler_module

    def fake_validate(record):
        from jsonschema.exceptions import ValidationError
        return False, [ValidationError("simulated validation failure")]

    _set_faults(monkeypatch, [])
    monkeypatch.setattr(handler_module, "validate", fake_validate)
    resp = handler_module.handler(_event(), None)

    assert resp["statusCode"] == 200
    printed = capsys.readouterr().out.strip()
    log = json.loads(printed)
    assert "telemetry_error" in log


def test_request_id_prefers_request_context(monkeypatch):
    # Correlation contract (PROJECT_PLAN Section 3): payment must stamp the caller's
    # requestContext.requestId on its telemetry line, winning over the Lambda context id.
    _set_faults(monkeypatch, [])
    log = _run({**_event(), "requestContext": {"requestId": "apigw-req-1"}},
               SimpleNamespace(aws_request_id="lambda-req-1"))
    assert log["request_id"] == "apigw-req-1"


def test_request_id_falls_back_to_lambda_context(monkeypatch):
    _set_faults(monkeypatch, [])
    log = _run(_event(), SimpleNamespace(aws_request_id="lambda-req-1"))
    assert log["request_id"] == "lambda-req-1"


def test_request_id_falls_back_to_uuid_and_is_never_null(monkeypatch):
    _set_faults(monkeypatch, [])
    first = _run(_event(), None)
    second = _run(_event(), None)
    assert uuid.UUID(first["request_id"]) and uuid.UUID(second["request_id"])
    assert first["request_id"] != second["request_id"]