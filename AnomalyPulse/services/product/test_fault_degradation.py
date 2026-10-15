"""Tests for Product's lambda-degradation fault handling (M3-4).

Faults come from the control plane (fault_config.active_faults), so these tests
replace active_faults with a fake that returns the notices we choose. No AWS.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

# fault_config creates a boto3 client when imported, which needs a region.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")

# The telemetry layer (schema_validator, fault_config) is only importable once
# its python/ dir is on sys.path locally; on AWS the layer provides it.
_LAYER_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "layers", "telemetry", "python")
sys.path.insert(0, os.path.abspath(_LAYER_DIR))

import handler as product  # noqa: E402

TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}


class FakeTable:
    def scan(self, ReturnConsumedCapacity=None, ExclusiveStartKey=None):
        return {"Items": [], "ConsumedCapacity": {"CapacityUnits": 0.5}}


def _event():
    return {"resource": "/products", "httpMethod": "GET", "requestContext": {"requestId": "req-1"}}


def _invoke(monkeypatch, faults):
    monkeypatch.setattr(product, "active_faults", lambda service: faults)
    monkeypatch.setattr(product, "_table", lambda: FakeTable())
    out = io.StringIO()
    with redirect_stdout(out):
        resp = product.handler(_event(), None)
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


def test_unrelated_fault_is_ignored(monkeypatch):
    resp, log = _invoke(monkeypatch, [{"injector": "payment-failure", "params": {"timeout_ms": 3000}}])
    assert resp["statusCode"] == 200
    assert log["lambda_duration_ms"] < 100


def test_malformed_fault_does_not_crash(monkeypatch):
    resp, _ = _invoke(monkeypatch, [{"injector": "lambda-degradation"}, {}, {"injector": "lambda-degradation", "params": {"added_latency_ms": "abc"}}])
    assert resp["statusCode"] == 200


def test_faults_are_read_on_every_request_for_this_service(monkeypatch):
    calls = []
    monkeypatch.setattr(product, "active_faults", lambda service: calls.append(service) or [])
    monkeypatch.setattr(product, "_table", lambda: FakeTable())
    out = io.StringIO()
    with redirect_stdout(out):
        product.handler(_event(), None)
        product.handler(_event(), None)
    assert calls == ["product-service", "product-service"]


def test_log_line_hides_the_fault(monkeypatch):
    _, log = _invoke(monkeypatch, [_degradation(20)])
    assert not (set(log) & TIER2_FIELDS)
    text = json.dumps(log)
    assert "added_latency_ms" not in text and "lambda-degradation" not in text