"""Tests for the live source (M8-5). No AWS: a fake Incidents table and a fake S3 client.

Items are stored as the incident store and the window builder store them (record.to_dynamodb: numbers as
Decimal), and read back as boto3's DynamoDB resource returns them.
"""
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import live_source
from builder import build_dashboard
from incidents import record

NOW = datetime(2026, 10, 30, 14, 34, 5, tzinfo=timezone.utc)
BUCKET = "anomalypulse-telemetry-test"
STARTED = "2026-10-30T14:32:00.000Z"
MODEL = {"estimator": "logistic_regression", "job": "job-1"}
DIAGNOSIS = {"severity": "HIGH", "summary": "Payment latency rose from 110 ms to 2.9 s.",
             "root_cause": "Payment dependency timeout", "evidence": ["Payment p95 latency rose 26x."],
             "recommended_actions": ["Inspect payment service health"]}
WORDING = "summary: causal wording 'caused'; correlated timestamps are an observed sequence, not proof of cause"
HEALTH = {"window_start": "2026-10-30T14:33:00.000Z", "system_p95_latency_ms": 260.0,
          "product_request_rate": 15.0, "product_error_rate_5xx": 0.0, "product_p95_latency_ms": 41.0,
          "cart_request_rate": 2.0, "cart_error_rate_5xx": 0.0, "cart_p95_latency_ms": 38.0,
          "order_request_rate": 3.0, "order_error_rate_5xx": 30 / 180, "order_p95_latency_ms": 2950.0,
          "payment_request_rate": 1.0, "payment_error_rate_5xx": 0.5, "payment_p95_latency_ms": 2900.0}
MANIFEST = {"experiment_id": "exp-1", "status": "RUNNING", "incident_type": "DEPENDENCY_FAILURE",
            "severity": "HIGH", "started_at": "2026-10-30T14:29:55.000Z", "ended_at": None,
            "activations": [{"fault_id": "exp-1#0", "injector": "payment-failure", "target": "payment-service",
                             "intensity": 0.8, "band": None, "params": {}, "observed": None,
                             "active_from": "2026-10-30T14:32:05.000Z",
                             "active_until": "2026-10-30T14:37:05.000Z"}]}


def health_item(**extra):
    return record.to_dynamodb({**HEALTH, **live_source.HEALTH_KEY, "telemetry_late": False, **extra})


def incident(started_at=STARTED, experiment_id="exp-1", diagnosed=True, closed=False, findings=(),
             diagnosis_for="DEPENDENCY_FAILURE"):
    """An incident record as the store keeps it."""
    state = {"status": "OPEN", "started_at": started_at, "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93}
    prediction = {"window_start": started_at,
                  "latency": {"diagnosed_at": "2026-10-30T14:33:04.000Z", "since_window_end_s": 4.0}}
    rec = record.open_record(state, prediction, ["order-service", "payment-service"], MODEL, experiment_id)
    if diagnosed:
        reply = {"diagnosis": DIAGNOSIS, "self_check": {"passed": not findings, "findings": list(findings)},
                 "evidence_metrics": [{"service": "payment-service", "metric": "p95_latency_ms"}]}
        rec = record.with_diagnosis(rec, diagnosis_for, reply, "2026-10-30T14:33:16.000Z", {"incident_id": "x"})
    if closed:
        rec = record.close(rec, "2026-10-30T14:40:00.000Z")
    return rec


class FakeTable:
    """get_item and query as boto3's Table resource answers them. query sorts the INCIDENT items by sk."""

    def __init__(self, health=None, incidents=()):
        self.health = health
        self.incidents = [record.to_dynamodb(r) for r in incidents]
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        found = kwargs.get("Key") == live_source.HEALTH_KEY and self.health is not None
        return {"Item": self.health} if found else {}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        items = sorted(self.incidents, key=lambda i: i["sk"], reverse=not kwargs.get("ScanIndexForward", True))
        return {"Items": items[:kwargs.get("Limit", len(items))], "Count": min(len(items), kwargs.get("Limit", 99))}


class FakeS3:
    def __init__(self, objects=None, error_code=None):
        self.objects = objects or {}
        self.error_code = error_code
        self.calls = []

    def get_object(self, Bucket, Key):
        self.calls.append((Bucket, Key))
        code = self.error_code or (None if Key in self.objects else "NoSuchKey")
        if code:
            raise ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


def s3_with_manifest():
    return FakeS3({live_source.manifest_key("exp-1"): MANIFEST})


def has_decimal(value):
    if isinstance(value, Decimal):
        return True
    if isinstance(value, dict):
        return any(has_decimal(v) for v in value.values())
    if isinstance(value, list):
        return any(has_decimal(v) for v in value)
    return False


# ---- health -----------------------------------------------------------------------------

def test_no_health_item_yet_is_no_window():
    assert live_source.latest_health(FakeTable()) is None


def test_the_health_item_is_read_by_its_key():
    table = FakeTable(health=health_item())
    live_source.latest_health(table)
    assert table.calls == [("get_item", {"Key": {"pk": "HEALTH", "sk": "LATEST"}})]


def test_the_health_item_comes_back_without_decimals():
    window = live_source.latest_health(FakeTable(health=health_item()))
    assert not has_decimal(window)
    assert window["window_start"] == HEALTH["window_start"]
    assert window["order_error_rate_5xx"] == 30 / 180 and window["product_request_rate"] == 15.0


def test_telemetry_late_is_kept_so_the_builder_can_say_no_data():
    window = live_source.latest_health(FakeTable(health=health_item(telemetry_late=True)))
    assert window["telemetry_late"] is True
    assert build_dashboard(window, None, None, NOW)["health"]["status"] == "NO_DATA"


# ---- the open incident --------------------------------------------------------------------

def test_no_incident_ever_is_none():
    assert live_source.open_incident(FakeTable()) is None


def test_incidents_are_one_query_newest_first_one_item():
    table = FakeTable(incidents=[incident()])
    live_source.open_incident(table)
    (call, kwargs), = table.calls
    assert call == "query"
    assert kwargs["KeyConditionExpression"] == Key("pk").eq("INCIDENT")
    assert kwargs["ScanIndexForward"] is False and kwargs["Limit"] == 1


def test_an_open_newest_record_comes_back_as_stored_without_decimals():
    stored = incident()
    found = live_source.open_incident(FakeTable(incidents=[incident("2026-10-30T13:00:00.000Z", closed=True), stored]))
    assert found == stored and not has_decimal(found)


def test_a_closed_newest_record_is_no_incident():
    table = FakeTable(incidents=[incident("2026-10-30T13:00:00.000Z", closed=True), incident(closed=True)])
    assert live_source.open_incident(table) is None


# ---- the manifest -------------------------------------------------------------------------

def test_the_manifest_key_is_the_simulator_s():
    manifest = pytest.importorskip("simulator.manifest")
    assert live_source.manifest_key("exp-1") == manifest.manifest_key("exp-1")


def test_the_manifest_is_read_from_the_bucket_by_experiment_id():
    s3 = s3_with_manifest()
    assert live_source.manifest_for(incident(), s3, BUCKET) == MANIFEST
    assert s3.calls == [(BUCKET, "experiments/exp-1/manifest.json")]


def test_a_real_incident_has_no_manifest_and_reads_nothing():
    s3 = s3_with_manifest()
    assert live_source.manifest_for(incident(experiment_id=None), s3, BUCKET) is None
    assert s3.calls == []


def test_a_missing_manifest_is_none():
    assert live_source.manifest_for(incident(), FakeS3(), BUCKET) is None


def test_any_other_s3_error_raises():
    # Without s3:ListBucket, S3 answers AccessDenied even for a missing key: that must not read as "no ground truth".
    with pytest.raises(ClientError):
        live_source.manifest_for(incident(), FakeS3(error_code="AccessDenied"), BUCKET)


# ---- read: what build_dashboard gets ----------------------------------------------------

def test_read_with_no_open_incident_reads_no_manifest():
    s3 = s3_with_manifest()
    window, shown, manifest = live_source.read(FakeTable(health_item(), [incident(closed=True)]), s3, BUCKET)
    assert window is not None and shown is None and manifest is None
    assert s3.calls == []


def test_read_passes_to_incident_never_the_raw_record():
    _, shown, manifest = live_source.read(FakeTable(health_item(), [incident()]), s3_with_manifest(), BUCKET)
    assert set(shown) == {"incident_id", "prediction", "diagnosis"}
    assert shown["diagnosis"] == DIAGNOSIS and manifest == MANIFEST


def test_a_diagnosis_held_back_for_its_wording_reads_as_pending():
    _, shown, _ = live_source.read(FakeTable(health_item(), [incident(findings=[WORDING])]), s3_with_manifest(),
                                   BUCKET)
    assert shown["diagnosis"] is None


def test_a_diagnosis_made_for_an_earlier_type_reads_as_pending():
    rec = incident(diagnosis_for="TRAFFIC_SPIKE")
    _, shown, _ = live_source.read(FakeTable(health_item(), [rec]), s3_with_manifest(), BUCKET)
    assert shown["diagnosis"] is None


def test_an_open_incident_waiting_for_bedrock_reads_as_pending():
    _, shown, _ = live_source.read(FakeTable(health_item(), [incident(diagnosed=False)]), s3_with_manifest(), BUCKET)
    assert shown["diagnosis"] is None


def test_end_to_end_an_injected_incident_is_a_json_reply_with_ground_truth():
    reply = build_dashboard(*live_source.read(FakeTable(health_item(), [incident()]), s3_with_manifest(), BUCKET), NOW)
    reply = json.loads(json.dumps(reply))          # what the handler sends: no Decimal may be left
    assert reply["health"]["status"] == "INCIDENT" and reply["health"]["active_incidents"] == 1
    assert reply["incident"]["prediction"]["incident_type"] == "DEPENDENCY_FAILURE"
    assert reply["incident"]["diagnosis"] == DIAGNOSIS
    assert reply["incident"]["ground_truth"]["experiment_id"] == "exp-1"
    assert reply["incident"]["comparison"] == {"detection_latency_s": 59.0}


def test_end_to_end_after_the_incident_closes_the_system_reads_healthy():
    reply = build_dashboard(*live_source.read(FakeTable(health_item(), [incident(closed=True)]), FakeS3(), BUCKET),
                            NOW)
    assert reply["health"]["status"] == "HEALTHY" and reply["incident"] is None


def test_end_to_end_a_real_incident_shows_no_ground_truth():
    reply = build_dashboard(*live_source.read(FakeTable(health_item(), [incident(experiment_id=None)]), FakeS3(),
                                              BUCKET), NOW)
    assert reply["incident"]["ground_truth"] is None and reply["incident"]["comparison"] is None
