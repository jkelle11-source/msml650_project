"""Tests for the stored incident record (M8-3). No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_record.py -q
"""
import copy
from decimal import Decimal

import pytest

from genai import contract
from incidents import record

STARTED = "2026-10-30T14:32:00.000Z"
DIAGNOSED = "2026-10-30T14:35:47.250Z"
MODEL = {"task": "incident_type", "estimator": "logistic_regression", "job": "job-1", "version_id": "v1"}
STATE = {"status": "OPEN", "started_at": STARTED, "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93}
REPLY = {"diagnosis": {"severity": "HIGH", "summary": "s", "root_cause": "r", "evidence": ["e"],
                       "recommended_actions": ["a"]},
         "evidence_metrics": [{"service": "order-service", "metric": "error_rate_5xx"}],
         "self_check": {"passed": True}, "runbook": ["step"], "usage": {"input_tokens": 10}, "latency_ms": 8200}


def prediction(diagnosed_at=DIAGNOSED):
    return {"window_start": STARTED, "anomalous": True, "label": "DEPENDENCY_FAILURE",
            "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93,
            "latency": {"window_end": "2026-10-30T14:33:00.000Z", "announced_at": "2026-10-30T14:35:46.000Z",
                        "diagnosed_at": diagnosed_at, "since_window_end_s": 167.25, "since_announced_s": 1.25}}


def new(experiment_id=None):
    return record.open_record(STATE, prediction(), ["order-service", "payment-service"], MODEL, experiment_id)


def test_an_opened_record_has_the_agreed_fields():
    assert set(new()) == {"pk", "sk", "incident_id", "status", "ended_at", "prediction", "diagnosis",
                          "diagnosis_for_type", "bedrock_calls", "diagnosis_error", "evidence_metrics",
                          "self_check", "runbook", "latency"}
    r = new()
    assert (r["pk"], r["sk"], r["status"], r["ended_at"]) == ("INCIDENT", STARTED, "OPEN", None)
    assert r["incident_id"] == "inc-20261030T143200Z" and r["diagnosis"] is None and r["bedrock_calls"] == 0


def test_prediction_is_exactly_the_dashboards_shape():
    assert set(new()["prediction"]) == {"incident_type", "confidence", "started_at", "detected_at",
                                        "affected_services", "model"}
    assert new()["prediction"]["model"] == {"estimator": "logistic_regression", "job": "job-1"}


def test_detected_at_is_the_first_counted_minutes_diagnosis_time():
    assert new()["prediction"]["detected_at"] == DIAGNOSED
    assert new()["latency"]["window_end_to_diagnosis_s"] == 167.25


def test_the_ground_truth_reference_sits_beside_the_prediction_never_inside_it():
    r = new("exp-1")
    assert r["experiment_id"] == "exp-1"
    assert "experiment_id" not in r["prediction"]
    assert contract.FORBIDDEN_KEYS.isdisjoint(set(r["prediction"]))
    assert "experiment_id" not in new()


def test_a_prediction_without_a_diagnosis_time_is_refused():
    p = prediction()
    del p["latency"]["diagnosed_at"]
    with pytest.raises(ValueError):
        record.open_record(STATE, p, ["order-service"], MODEL)


def test_refresh_updates_type_and_confidence_and_keeps_detected_at():
    r = new()
    before = copy.deepcopy(r)
    updated = record.refresh(r, {**STATE, "incident_type": "CASCADING_FAILURE", "confidence": 0.8})
    assert updated["prediction"]["incident_type"] == "CASCADING_FAILURE"
    assert updated["prediction"]["confidence"] == 0.8
    assert updated["prediction"]["detected_at"] == DIAGNOSED
    assert updated["prediction"]["affected_services"] == ["order-service", "payment-service"]
    assert r == before


def test_refresh_replaces_the_services_only_when_given():
    r = new()
    assert record.refresh(r, STATE, ["product-service"])["prediction"]["affected_services"] == ["product-service"]
    assert record.refresh(r, STATE, [])["prediction"]["affected_services"] == ["order-service", "payment-service"]


def test_close_sets_status_and_end():
    r = new()
    closed = record.close(r, "2026-10-30T14:40:00.000Z")
    assert closed["status"] == "CLOSED" and closed["ended_at"] == "2026-10-30T14:40:00.000Z"
    assert r["status"] == "OPEN" and r["ended_at"] is None


def test_a_diagnosis_is_stored_with_its_type_the_call_count_and_latency():
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, "2026-10-30T14:35:59.750Z")
    assert r["diagnosis"] == REPLY["diagnosis"] and r["diagnosis_for_type"] == "DEPENDENCY_FAILURE"
    assert r["bedrock_calls"] == 1 and r["diagnosis_error"] is None
    assert r["evidence_metrics"] == REPLY["evidence_metrics"] and r["runbook"] == ["step"]
    assert r["latency"]["bedrock_ms"] == 8200 and r["latency"]["diagnosis_to_stored_s"] == 12.5


def test_a_diagnosis_that_failed_its_self_check_is_stored_flagged_not_dropped():
    reply = {**REPLY, "self_check": {"passed": False, "findings": ["x"]}}
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", reply, DIAGNOSED, DIAGNOSED)
    assert r["diagnosis"] is not None and r["self_check"]["passed"] is False


def test_a_failed_call_is_counted_and_leaves_the_diagnosis_null():
    r = record.with_error(new(), "DEPENDENCY_FAILURE", {"type": "DiagnosisFailed", "message": "x" * 900})
    assert r["diagnosis"] is None and r["bedrock_calls"] == 1
    assert r["diagnosis_error"]["type"] == "DiagnosisFailed" and len(r["diagnosis_error"]["message"]) == 500
    assert r["diagnosis_error"]["for_type"] == "DEPENDENCY_FAILURE"


def test_a_failed_second_call_keeps_the_first_diagnosis():
    first = record.with_diagnosis(new(), "TRAFFIC_SPIKE", REPLY, DIAGNOSED, DIAGNOSED)
    second = record.with_error(first, "CASCADING_FAILURE", {"type": "Timeout", "message": "t"})
    assert second["diagnosis"] == REPLY["diagnosis"] and second["diagnosis_for_type"] == "TRAFFIC_SPIKE"
    assert second["bedrock_calls"] == 2 and second["diagnosis_error"]["for_type"] == "CASCADING_FAILURE"


def test_a_good_call_after_a_failed_one_clears_the_error():
    failed = record.with_error(new(), "TRAFFIC_SPIKE", {"type": "Timeout", "message": "t"})
    good = record.with_diagnosis(failed, "TRAFFIC_SPIKE", REPLY, DIAGNOSED, DIAGNOSED)
    assert good["diagnosis_error"] is None and good["bedrock_calls"] == 2


def test_bedrock_is_called_on_open_and_type_change_up_to_the_cap():
    assert record.wants_bedrock(["opened"], 0)
    assert record.wants_bedrock(["type_changed"], 3)
    assert record.wants_bedrock(["closed", "opened"], 0)
    assert not record.wants_bedrock(["closed"], 1)
    assert not record.wants_bedrock([], 1)
    assert not record.wants_bedrock(["type_changed"], record.MAX_CALLS)


def test_floats_become_decimals_and_come_back_equal():
    r = record.with_diagnosis(new("exp-1"), "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, "2026-10-30T14:35:59.750Z")
    stored = record.to_dynamodb(r)
    assert stored["prediction"]["confidence"] == Decimal("0.93")
    assert stored["latency"]["diagnosis_to_stored_s"] == Decimal("12.5")
    assert record.from_dynamodb(stored) == r


def test_the_dashboard_reads_the_incident_id_prediction_and_diagnosis():
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, DIAGNOSED)
    assert set(record.to_incident(r)) == {"incident_id", "prediction", "diagnosis"}
    assert record.to_incident(r)["diagnosis"] == REPLY["diagnosis"]