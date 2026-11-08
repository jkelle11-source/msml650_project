"""The stored record, read back by the dashboard's build_dashboard (M8-3 Definition of Done, item 3).

Loads dashboard/builder.py by its path under its own name, so it can't be mistaken for
incidents/builder.py. The window is the health item's shape (14 keys, with the system p95),
as the mock source uses.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from incidents import record

_path = Path(__file__).resolve().parent.parent / "dashboard" / "builder.py"
_spec = importlib.util.spec_from_file_location("dashboard_builder_under_test", _path)
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)

STARTED = "2026-10-30T14:32:00.000Z"
DETECTED = "2026-10-30T14:33:04.000Z"
NOW = datetime(2026, 10, 30, 14, 34, 5, tzinfo=timezone.utc)
MODEL = {"estimator": "logistic_regression", "job": "job-1"}
STATE = {"status": "OPEN", "started_at": STARTED, "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93}
PREDICTION = {"window_start": STARTED, "latency": {"diagnosed_at": DETECTED, "since_window_end_s": 167.25}}
DIAGNOSIS = {"severity": "HIGH", "summary": "s", "root_cause": "r", "evidence": ["e"],
             "recommended_actions": ["a"]}
HEALTH = {"window_start": "2026-10-30T14:33:00.000Z", "system_p95_latency_ms": 260.0,
          "product_request_rate": 15.0, "product_error_rate_5xx": 0.0, "product_p95_latency_ms": 41.0,
          "cart_request_rate": 2.0, "cart_error_rate_5xx": 0.0, "cart_p95_latency_ms": 38.0,
          "order_request_rate": 3.0, "order_error_rate_5xx": 30 / 180, "order_p95_latency_ms": 2950.0,
          "payment_request_rate": 1.0, "payment_error_rate_5xx": 0.5, "payment_p95_latency_ms": 2900.0}
MANIFEST = {"experiment_id": "exp-1", "status": "RUNNING", "incident_type": "DEPENDENCY_FAILURE",
            "severity": "HIGH", "started_at": "2026-10-30T14:29:55.000Z", "ended_at": None,
            "activations": [{"fault_id": "exp-1#0", "injector": "payment-failure", "target": "payment-service",
                             "intensity": 0.8, "band": None, "params": {}, "observed": None,
                             "active_from": "2026-10-30T14:32:05.000Z", "active_until": "2026-10-30T14:37:05.000Z"}]}


def stored(with_diagnosis=True):
    rec = record.open_record(STATE, PREDICTION, ["order-service", "payment-service"], MODEL)
    if with_diagnosis:
        rec = record.with_diagnosis(rec, "DEPENDENCY_FAILURE", {"diagnosis": DIAGNOSIS}, "2026-10-30T14:33:16.000Z")
    return rec


def test_the_dashboard_reads_a_stored_incident_without_errors():
    rec = stored()
    result = dash.build_dashboard(HEALTH, record.to_incident(rec), None, NOW)
    shown = result["incident"]
    assert shown["incident_id"] == rec["incident_id"] and shown["prediction"] == rec["prediction"]
    assert shown["diagnosis"] == DIAGNOSIS and shown["ground_truth"] is None
    statuses = {s["service"]: s["status"] for s in result["health"]["services"]}
    assert result["health"]["status"] == "INCIDENT"
    assert statuses["order-service"] == "DEGRADED" and statuses["payment-service"] == "DEGRADED"
    assert statuses["product-service"] == "HEALTHY"


def test_an_incident_still_waiting_for_bedrock_reads_with_a_null_diagnosis():
    result = dash.build_dashboard(HEALTH, record.to_incident(stored(False)), None, NOW)
    assert result["incident"]["diagnosis"] is None and result["incident"]["prediction"]["detected_at"] == DETECTED


def test_detection_latency_comes_from_the_stored_detected_at_and_the_manifest():
    result = dash.build_dashboard(HEALTH, record.to_incident(stored()), MANIFEST, NOW)
    assert result["incident"]["ground_truth"]["experiment_id"] == "exp-1"
    assert result["incident"]["comparison"]["detection_latency_s"] == 59.0