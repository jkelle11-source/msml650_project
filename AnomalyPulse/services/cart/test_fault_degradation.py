"""Tests for Cart's lambda-degradation fault handling (M3-4).

Faults come from the control plane (fault_config.active_faults), so these tests
replace active_faults with a fake that returns the notices we choose. No AWS.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

# The handler reads TABLE_NAME at import and fault_config needs a region.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("TABLE_NAME", "test-cart-table")

# The telemetry layer is only importable locally once its python/ dir is on sys.path.
_LAYER_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "layers", "telemetry", "python")
sys.path.insert(0, os.path.abspath(_LAYER_PATH))

import handler as cart  # noqa: E402

TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}


class FakeTable:
    def query(self, **kwargs):
        return {"Items": [], "ConsumedCapacity": {"CapacityUnits": 0.5}}


def _get_event():
    return {"resource": "/cart/{user}", "httpMethod": "GET", "pathParameters": {"user": "u1"},
            "requestContext": {"requestId": "req-1"}}


def _bad_request_event():
    # Invalid JSON -> an early 400 return that never reaches DynamoDB.
    return {"resource": "/cart", "httpMethod": "POST", "pathParameters": None, "body": "{not json",
            "requestContext": {"requestId": "req-2"}}


def _invoke(monkeypatch, faults, event=None):
    monkeypatch.setattr(cart, "active_faults", lambda service: faults)
    monkeypatch.setattr(cart, "table", FakeTable())
    out = io.StringIO()
    with redirect_stdout(out):
        resp = cart.handler(event or _get_event(), None)
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 1, "exactly one log line per request"
    return resp, json.loads(lines[0])


def _degradation(ms):
    return {"injector": "lambda-degradation", "params": {"added_latency_ms": ms}}


def test_no_fault_is_fast(monkeypatch):
    resp, log = _invoke(monkeypatch, [])
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] < 100


def test_degradation_raises_cpu_time_and_still_succeeds(monkeypatch):
    resp, log = _invoke(monkeypatch, [_degradation(100)])
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] >= 100
    assert log["latency_ms"] >= 100


def test_degradation_also_applies_to_error_paths(monkeypatch):
    # Cart returns early on bad input; the fault must still apply, or a degraded
    # run would contain normal-looking requests.
    resp, log = _invoke(monkeypatch, [_degradation(100)], _bad_request_event())
    assert resp["statusCode"] == 400
    assert log["lambda_duration_ms"] >= 100


def test_unrelated_fault_is_ignored(monkeypatch):
    resp, log = _invoke(monkeypatch, [{"injector": "payment-failure", "params": {"timeout_ms": 3000}}])
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] < 100


def test_malformed_fault_does_not_crash(monkeypatch):
    bad = [{"injector": "lambda-degradation"}, {},
           {"injector": "lambda-degradation", "params": {"added_latency_ms": "abc"}}]
    resp, _ = _invoke(monkeypatch, bad)
    assert resp["statusCode"] == 200


def test_faults_are_read_on_every_request_for_this_service(monkeypatch):
    calls = []
    monkeypatch.setattr(cart, "active_faults", lambda service: calls.append(service) or [])
    monkeypatch.setattr(cart, "table", FakeTable())
    with redirect_stdout(io.StringIO()):
        cart.handler(_get_event(), None)
        cart.handler(_get_event(), None)
    assert calls == ["cart-service", "cart-service"]


def test_log_line_hides_the_fault(monkeypatch):
    _, log = _invoke(monkeypatch, [_degradation(20)])
    assert not (set(log) & TIER2_FIELDS)
    text = json.dumps(log)
    assert "added_latency_ms" not in text and "lambda-degradation" not in text