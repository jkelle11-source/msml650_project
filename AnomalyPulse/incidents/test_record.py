"""Tests for the stored incident record (M8-3). No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_record.py -q
"""
import copy
import json
from decimal import Decimal
from pathlib import Path

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
# A real input (M6-1's dependency-failure sample), as the call sent it.
INPUT = json.loads((Path(__file__).resolve().parent.parent / "genai" / "samples" / "dependency_failure.json").read_text())
NUMBER = "summary: the number 4.8 is not in or derivable from the input"
WORDING = "summary: causal wording 'caused'; correlated timestamps are an observed sequence, not proof of cause"
SEVERITY = "severity: MEDIUM, but the rubric gives HIGH"
SERVICE = "summary: names cart-service, which the input doesn't"
TIME = "summary: the time 14:29 is not in the input"
METRIC = "root_cause: claims 'throttling', but the input has no metric for it"
CITATION = "evidence cites cart-service.p95_latency_ms, which is not in the input"
THRESHOLD = "evidence[0]: mentions a threshold; the severity rubric's are never quoted"
SHAPE = "evidence has 7 bullets; 3 to 6 expected"
UNCITED = "evidence[0] cites no input metric"          # contract.check_output_shape's; a citation (M8-4)


def flagged(*findings, summary="s"):
    return {**REPLY, "diagnosis": {**REPLY["diagnosis"], "summary": summary},
            "self_check": {"passed": not findings, "findings": list(findings)}}


def prediction(diagnosed_at=DIAGNOSED):
    return {"window_start": STARTED, "anomalous": True, "label": "DEPENDENCY_FAILURE",
            "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93,
            "latency": {"window_end": "2026-10-30T14:33:00.000Z", "announced_at": "2026-10-30T14:35:46.000Z",
                        "diagnosed_at": diagnosed_at, "since_window_end_s": 167.25, "since_announced_s": 1.25}}


def new(experiment_id=None):
    return record.open_record(STATE, prediction(), ["order-service", "payment-service"], MODEL, experiment_id)


def test_an_opened_record_has_the_agreed_fields():
    assert set(new()) == {"pk", "sk", "incident_id", "status", "ended_at", "prediction", "diagnosis",
                          "diagnosis_input", "diagnosis_for_type", "bedrock_calls", "retried_for",
                          "diagnosis_error", "evidence_metrics", "self_check", "runbook", "latency"}
    r = new()
    assert (r["pk"], r["sk"], r["status"], r["ended_at"]) == ("INCIDENT", STARTED, "OPEN", None)
    assert r["incident_id"] == "inc-20261030T143200Z" and r["diagnosis"] is None and r["bedrock_calls"] == 0
    assert r["diagnosis_input"] is None and r["retried_for"] == []


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


def test_refresh_adds_services_in_service_order_and_never_removes_one():
    r = new()                                       # order-service, payment-service
    grown = record.refresh(r, STATE, ["product-service"])
    assert grown["prediction"]["affected_services"] == ["product-service", "order-service", "payment-service"]
    assert record.refresh(r, STATE, [])["prediction"]["affected_services"] == ["order-service", "payment-service"]
    assert record.refresh(r, STATE, ["order-service"])["prediction"]["affected_services"] == [
        "order-service", "payment-service"]
    assert record.refresh(grown, STATE, ["cart-service"])["prediction"]["affected_services"] == [
        "product-service", "cart-service", "order-service", "payment-service"]


def test_close_sets_status_and_end():
    r = new()
    closed = record.close(r, "2026-10-30T14:40:00.000Z")
    assert closed["status"] == "CLOSED" and closed["ended_at"] == "2026-10-30T14:40:00.000Z"
    assert r["status"] == "OPEN" and r["ended_at"] is None


def test_a_diagnosis_is_stored_with_its_type_the_call_count_and_latency():
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", REPLY, "2026-10-30T14:35:59.750Z", INPUT)
    assert r["diagnosis"] == REPLY["diagnosis"] and r["diagnosis_for_type"] == "DEPENDENCY_FAILURE"
    assert r["bedrock_calls"] == 1 and r["diagnosis_error"] is None
    assert r["evidence_metrics"] == REPLY["evidence_metrics"] and r["runbook"] == ["step"]
    assert r["diagnosis_input"] == INPUT
    assert r["latency"]["bedrock_ms"] == 8200 and r["latency"]["diagnosis_to_stored_s"] == 12.5


def test_a_diagnosis_that_failed_its_self_check_is_stored_flagged_not_dropped():
    reply = {**REPLY, "self_check": {"passed": False, "findings": ["x"]}}
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", reply, DIAGNOSED, INPUT)
    assert r["diagnosis"] is not None and r["self_check"]["passed"] is False


def test_a_failed_call_is_counted_and_leaves_the_diagnosis_null():
    r = record.with_error(new(), "DEPENDENCY_FAILURE", {"type": "DiagnosisFailed", "message": "x" * 900})
    assert r["diagnosis"] is None and r["bedrock_calls"] == 1
    assert r["diagnosis_error"]["type"] == "DiagnosisFailed" and len(r["diagnosis_error"]["message"]) == 500
    assert r["diagnosis_error"]["for_type"] == "DEPENDENCY_FAILURE"


def test_a_failed_second_call_keeps_the_first_diagnosis():
    first = record.with_diagnosis(new(), "TRAFFIC_SPIKE", REPLY, DIAGNOSED, INPUT)
    second = record.with_error(first, "CASCADING_FAILURE", {"type": "Timeout", "message": "t"})
    assert second["diagnosis"] == REPLY["diagnosis"] and second["diagnosis_for_type"] == "TRAFFIC_SPIKE"
    assert second["bedrock_calls"] == 2 and second["diagnosis_error"]["for_type"] == "CASCADING_FAILURE"


def test_a_good_call_after_a_failed_one_clears_the_error():
    failed = record.with_error(new(), "TRAFFIC_SPIKE", {"type": "Timeout", "message": "t"})
    good = record.with_diagnosis(failed, "TRAFFIC_SPIKE", REPLY, DIAGNOSED, INPUT)
    assert good["diagnosis_error"] is None and good["bedrock_calls"] == 2


def test_bedrock_is_called_when_the_stored_diagnosis_is_not_for_the_current_type_up_to_the_cap():
    opened = new()
    assert record.wants_bedrock(opened, STATE)                                   # no diagnosis yet
    done = record.with_diagnosis(opened, "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, INPUT)
    assert not record.wants_bedrock(done, STATE)                                 # same type: no call
    assert record.wants_bedrock(done, {**STATE, "incident_type": "CASCADING_FAILURE"})   # type changed
    failed = record.with_error(opened, "DEPENDENCY_FAILURE", {"type": "Timeout", "message": "t"})
    assert record.wants_bedrock(failed, STATE)                                   # retry after a failure
    capped = {**failed, "bedrock_calls": record.MAX_CALLS}
    assert not record.wants_bedrock(capped, STATE)
    assert record.wants_bedrock({**failed, "bedrock_calls": record.MAX_CALLS - 1}, STATE)


def test_the_record_keeps_the_first_diagnosiss_latency_measured_from_detected_at():
    failed = record.with_error(new(), "TRAFFIC_SPIKE", {"type": "Timeout", "message": "t"})
    first = record.with_diagnosis(failed, "TRAFFIC_SPIKE", REPLY, "2026-10-30T14:37:00.250Z", INPUT)
    # detected_at 14:35:47.250 -> stored 14:37:00.250: the failed call's minute is counted in.
    assert first["latency"]["diagnosis_to_stored_s"] == 73.0 and first["latency"]["bedrock_ms"] == 8200
    second = record.with_diagnosis(first, "CASCADING_FAILURE", {**REPLY, "latency_ms": 31000},
                                   "2026-10-30T14:40:00.000Z", INPUT)
    assert second["latency"] == first["latency"] and second["diagnosis_for_type"] == "CASCADING_FAILURE"
    assert second["latency"]["window_end_to_diagnosis_s"] == 167.25


def test_floats_become_decimals_and_come_back_equal():
    r = record.with_diagnosis(new("exp-1"), "DEPENDENCY_FAILURE", REPLY, "2026-10-30T14:35:59.750Z", INPUT)
    stored = record.to_dynamodb(r)
    assert stored["prediction"]["confidence"] == Decimal("0.93")
    assert stored["latency"]["diagnosis_to_stored_s"] == Decimal("12.5")
    assert record.from_dynamodb(stored) == r


def test_the_dashboard_reads_the_incident_id_prediction_and_diagnosis():
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, INPUT)
    assert set(record.to_incident(r)) == {"incident_id", "prediction", "diagnosis"}
    assert record.to_incident(r)["diagnosis"] == REPLY["diagnosis"]

def test_whole_numbers_come_back_from_dynamodb_as_ints():
    back = record.from_dynamodb({"calls": Decimal("2"), "share": Decimal("0.5"), "list": [Decimal("3")]})
    assert back == {"calls": 2, "share": 0.5, "list": [3]}
    assert type(back["calls"]) is int and type(back["list"][0]) is int and type(back["share"]) is float


def test_a_clean_self_check_is_one_with_no_finding_or_only_bedrocks_own_severity():
    assert record.clean(None) and record.clean({"passed": True}) and record.clean({"passed": True, "findings": []})
    assert record.clean({"passed": False, "findings": [SEVERITY]})          # the rubric's is reported anyway
    assert not record.clean({"passed": False, "findings": [NUMBER]})
    assert not record.clean({"passed": False, "findings": [SEVERITY, SERVICE]})
    assert not record.clean({"passed": False, "findings": [THRESHOLD]})       # not retried, but not clean either
    assert not record.clean({"passed": False, "findings": ["a message nobody writes"]})
    assert record.finding_kinds({"findings": [NUMBER, WORDING, SEVERITY]}) == {"number", "wording", "severity"}


@pytest.mark.parametrize("findings, reason", [
    ([NUMBER], "retry"), ([TIME], "retry"), ([SERVICE], "retry"), ([METRIC], "retry"), ([CITATION], "retry"),
    ([WORDING], "retry"), ([SEVERITY, NUMBER], "retry"), ([THRESHOLD, SHAPE, CITATION], "retry"),
    ([UNCITED], "retry"),
    ([SEVERITY], None), ([THRESHOLD], None), ([SHAPE], None), ([SEVERITY, THRESHOLD, SHAPE], None),
    ([], None), (["a message nobody writes"], None)])
def test_a_diagnosis_flagged_for_an_unsupported_claim_or_wording_is_asked_for_again(findings, reason):
    done = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(*findings), DIAGNOSED, INPUT)
    assert record.call_reason(done, STATE) == reason
    assert record.wants_bedrock(done, STATE) is (reason is not None)


def test_a_retry_is_made_once_per_incident_type_and_never_past_the_cap():
    first = record.with_diagnosis(new(), "TRAFFIC_SPIKE", flagged(NUMBER), DIAGNOSED, INPUT)
    spike = {**STATE, "incident_type": "TRAFFIC_SPIKE"}
    assert record.call_reason(first, spike) == "retry"
    used = record.retried(first, "TRAFFIC_SPIKE")
    assert used["retried_for"] == ["TRAFFIC_SPIKE"] and record.call_reason(used, spike) is None
    assert record.call_reason(used, STATE) == "type"                        # a type change is still called
    second = record.with_diagnosis(used, "DEPENDENCY_FAILURE", flagged(WORDING), DIAGNOSED, INPUT)
    assert record.call_reason(second, STATE) == "retry"                     # the new type has its own retry
    assert record.call_reason({**first, "bedrock_calls": record.MAX_CALLS}, spike) is None
    assert record.call_reason({**first, "bedrock_calls": record.MAX_CALLS - 1}, spike) == "retry"
    older = {k: v for k, v in first.items() if k != "retried_for"}          # a record stored before retries
    assert record.call_reason(older, spike) == "retry"
    assert record.retried(older, "TRAFFIC_SPIKE")["retried_for"] == ["TRAFFIC_SPIKE"]


def test_a_clean_retry_replaces_the_flagged_diagnosis_and_its_input():
    first = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(NUMBER, summary="first"), DIAGNOSED, INPUT)
    later_input = {**INPUT, "window_start": "2026-10-30T14:33:00.000Z"}
    for reply in (flagged(summary="second"), flagged(SEVERITY, summary="second")):
        kept = record.with_retry(first, "DEPENDENCY_FAILURE", reply, "2026-10-30T14:37:00.000Z", later_input)
        assert kept["diagnosis"]["summary"] == "second" and kept["self_check"] == reply["self_check"]
        assert kept["diagnosis_input"] == later_input and kept["bedrock_calls"] == 2
        assert kept["retried_for"] == ["DEPENDENCY_FAILURE"] and kept["latency"] == first["latency"]
        assert record.call_reason(kept, STATE) is None


def test_a_retry_that_is_flagged_too_keeps_the_first_flagged_diagnosis_and_its_input():
    first = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(NUMBER, summary="first"), DIAGNOSED, INPUT)
    later_input = {**INPUT, "window_start": "2026-10-30T14:33:00.000Z"}
    for reply in (flagged(WORDING, summary="second"), flagged(SERVICE, summary="second")):
        rejected = record.with_retry(first, "DEPENDENCY_FAILURE", reply, None, later_input)
        assert rejected["diagnosis"]["summary"] == "first" and rejected["self_check"] == first["self_check"]
        assert rejected["diagnosis_input"] == INPUT and rejected["bedrock_calls"] == 2
        assert rejected["retried_for"] == ["DEPENDENCY_FAILURE"] and record.call_reason(rejected, STATE) is None
    assert first["bedrock_calls"] == 1 and first["retried_for"] == []        # the argument is left alone


@pytest.mark.parametrize("held", [WORDING, UNCITED])
def test_a_retry_that_clears_what_held_the_diagnosis_back_is_kept_with_a_finding_the_feed_shows(held):
    # M8-4: an answer the console can show beats one it can't, so a retry that fixes the causal wording (or
    # the bullet citing nothing) but rounds a number too far replaces the held one, and M9-5 scores the number.
    first = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(held, summary="first"), DIAGNOSED, INPUT)
    assert record.held_back(first) and record.shown_diagnosis(first) is None
    for reply in (flagged(NUMBER, summary="second"), flagged(METRIC, SEVERITY, summary="second")):
        assert record.retry_kept(first, reply)
        kept = record.with_retry(first, "DEPENDENCY_FAILURE", reply, "2026-10-30T14:37:00.000Z", INPUT)
        assert kept["diagnosis"]["summary"] == "second" and kept["self_check"] == reply["self_check"]
        assert not record.held_back(kept) and record.shown_diagnosis(kept) == kept["diagnosis"]
        assert kept["retried_for"] == ["DEPENDENCY_FAILURE"] and record.call_reason(kept, STATE) is None
    for reply in (flagged(WORDING, summary="second"), flagged(NUMBER, UNCITED, summary="second")):
        assert not record.retry_kept(first, reply)                           # still held: the first stays
        assert record.with_retry(first, "DEPENDENCY_FAILURE", reply, None, INPUT)["diagnosis"]["summary"] == "first"


def test_a_retry_with_findings_does_not_replace_a_diagnosis_the_feed_already_shows():
    first = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(NUMBER, summary="first"), DIAGNOSED, INPUT)
    assert not record.held_back(first)
    assert not record.retry_kept(first, flagged(METRIC, summary="second"))
    assert record.retry_kept(first, flagged(SEVERITY, summary="second"))      # clean: the rubric's is reported


def test_the_input_survives_dynamodb():
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", REPLY, DIAGNOSED, INPUT)
    assert record.from_dynamodb(record.to_dynamodb(r))["diagnosis_input"] == INPUT


def test_a_retry_sends_the_stored_answer_as_bedrock_gave_it_and_its_findings_but_not_severity():
    reply = {**REPLY, "diagnosis": {**REPLY["diagnosis"], "evidence": ["e1", "e2"]},
             "evidence_metrics": [["payment-service.p95_latency_ms"], ["order-service.error_rate_5xx"]],
             "self_check": {"passed": False, "findings": [NUMBER, SEVERITY, WORDING]}}
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", reply, DIAGNOSED, INPUT)
    revise = record.revision(r)
    assert revise == {"answer": {"severity": "HIGH", "summary": "s", "root_cause": "r",
                                 "evidence": [{"statement": "e1", "metrics": ["payment-service.p95_latency_ms"]},
                                              {"statement": "e2", "metrics": ["order-service.error_rate_5xx"]}],
                                 "recommended_actions": ["a"]},
                      "findings": [NUMBER, WORDING]}
    assert contract.validate_revision(revise, r["diagnosis_input"]) is revise


def test_the_findings_a_retry_sends_are_bounded():
    many = [f"summary: the number {i} is not in or derivable from the input" for i in range(30)]
    long = "summary: orders symptoms without saying 'observed sequence': '" + "x" * 2000 + "'"
    r = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(long, *many), DIAGNOSED, INPUT)
    findings = record.revision(r)["findings"]
    assert len(findings) == contract.MAX_REVISION_FINDINGS and len(findings[0]) == contract.MAX_FINDING_CHARS


def test_a_record_with_no_stored_input_is_not_retried():
    done = record.with_diagnosis(new(), "DEPENDENCY_FAILURE", flagged(NUMBER), DIAGNOSED, INPUT)
    assert record.call_reason({**done, "diagnosis_input": None}, STATE) is None
