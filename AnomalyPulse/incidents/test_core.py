"""Tests for the M8-3 Lambda's logic, with every AWS call faked. No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_core.py -q
"""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from genai import contract
from incidents import builder, core, record

MODEL = {"task": "incident_type", "estimator": "logistic_regression", "job": "job-1", "version_id": "v1"}
REPLY = {"diagnosis": {"severity": "HIGH", "summary": "s", "root_cause": "r", "evidence": ["e"],
                       "recommended_actions": ["a"]},
         "evidence_metrics": [], "self_check": {"passed": True}, "runbook": ["x"], "latency_ms": 8200}


def ws(m):
    return f"2026-11-06T14:{m:02d}:00.000Z"


def diagnosed(m):
    return f"2026-11-06T14:{m + 3:02d}:47.250Z"


def key(m):
    return f"pipeline/windows/dt=2026-11-06/minute=14-{m:02d}.json"


def window(start, order_p95=100.0, order_errors=0.0, payment_p95=110.0, payment_errors=0.0):
    return {"window_start": start,
            "product_request_rate": 15.0, "product_p95_latency_ms": 20.0, "product_error_rate_5xx": 0.0,
            "cart_request_rate": 2.0, "cart_p95_latency_ms": 20.0, "cart_error_rate_5xx": 0.0,
            "order_request_rate": 3.0, "order_p95_latency_ms": order_p95, "order_error_rate_5xx": order_errors,
            "payment_request_rate": 1.0, "payment_p95_latency_ms": payment_p95,
            "payment_error_rate_5xx": payment_errors}


def failing(m):
    return window(ws(m), order_p95=2950.0, order_errors=0.167, payment_p95=2900.0, payment_errors=0.5)


def event(m, typ=None, win=None, request=True, as_string=False, flagged=None):
    """flagged: the detector's verdict when it differs from the classifier's (default: flagged
    exactly when typ is set)."""
    anomalous = bool(typ) if flagged is None else flagged
    prediction = {"window_start": ws(m), "anomalous": anomalous, "label": typ or "NORMAL",
                  "incident_type": typ, "confidence": 0.9 if typ else None,
                  "latency": {"window_end": ws(m + 1), "announced_at": diagnosed(m), "diagnosed_at": diagnosed(m),
                              "since_window_end_s": 167.25, "since_announced_s": 1.25}}
    payload = {"model": MODEL, "key": key(m), "predictions": [prediction]}
    detail = {"responsePayload": json.dumps(payload) if as_string else payload}
    if request:
        detail["requestPayload"] = {"detail": {"windows": [win or window(ws(m))], "key": key(m)}}
    return {"detail": detail}


class FakeDeps:
    def __init__(self):
        self.state, self.records, self.windows, self.lock = None, {}, {}, None
        self.fail, self.explain_calls, self.lines, self.ops = None, [], [], []
        self.reply = REPLY
        self.t = datetime(2026, 11, 6, 14, 5, 0, tzinfo=timezone.utc)

    def now(self):
        value = self.t
        self.t = self.t + timedelta(seconds=12)
        return value

    def get_state(self):
        return copy.deepcopy(self.state)

    def put_state(self, state):
        self.ops.append(("put_state",))
        self.state = copy.deepcopy(state)

    def get_record(self, started_at):
        return copy.deepcopy(self.records.get(started_at))

    def put_record(self, rec):
        self.ops.append(("put_record", rec["diagnosis"] is not None))
        self.records[rec["sk"]] = copy.deepcopy(rec)

    def read_window(self, k):
        return self.windows[k]

    def read_lock(self):
        return self.lock

    def explain(self, evidence):
        self.explain_calls.append(evidence)
        if self.fail:
            raise self.fail
        return self.reply

    def emit(self, line):
        self.lines.append(line)


def feed(deps, m, typ=None, win=None, **kw):
    deps.t = datetime(2026, 11, 6, 14, m + 3, 48, tzinfo=timezone.utc)
    return core.process(event(m, typ, win, **kw), deps)


def opened(deps, typ="DEPENDENCY_FAILURE"):
    feed(deps, 0)
    feed(deps, 1)
    feed(deps, 2, typ, failing(2))


def test_the_first_counted_minute_opens_an_incident_and_stores_the_diagnosis():
    deps = FakeDeps()
    opened(deps)
    rec = deps.records[ws(2)]
    assert rec["status"] == "OPEN" and rec["diagnosis"] == REPLY["diagnosis"]
    assert rec["bedrock_calls"] == 1 and rec["diagnosis_for_type"] == "DEPENDENCY_FAILURE"
    assert rec["prediction"]["detected_at"] == diagnosed(2)
    assert len(deps.explain_calls) == 1 and deps.state["incident"]["status"] == "OPEN"


def test_the_baseline_is_the_mean_of_the_normal_windows_before_the_incident():
    deps = FakeDeps()
    feed(deps, 0, win=window(ws(0), order_p95=100.0))
    feed(deps, 1, win=window(ws(1), order_p95=120.0))
    feed(deps, 2, "DEPENDENCY_FAILURE", failing(2))
    entry = next(m for m in deps.explain_calls[0]["metrics"]
                 if m["service"] == "order-service" and m["metric"] == "p95_latency_ms")
    assert entry["baseline"] == 110.0 and entry["current"] == 2950.0
    assert deps.state["baseline"]["order_p95_latency_ms"] == 110.0


def test_only_the_last_five_normal_windows_are_kept():
    deps = FakeDeps()
    for m in range(7):
        feed(deps, m)
    assert [w["window_start"] for w in deps.state["recent_normal"]] == [ws(m) for m in range(2, 7)]


def test_the_incidents_own_minutes_never_enter_the_baseline_memory():
    deps = FakeDeps()
    opened(deps)
    assert [w["window_start"] for w in deps.state["recent_normal"]] == [ws(0), ws(1)]


def test_a_later_minute_of_the_same_type_refreshes_the_record_without_a_second_call():
    deps = FakeDeps()
    opened(deps)
    before = len(deps.ops)
    feed(deps, 3, "DEPENDENCY_FAILURE", failing(3))
    assert len(deps.explain_calls) == 1 and deps.records[ws(2)]["bedrock_calls"] == 1
    assert deps.ops[before:] == [("put_record", True), ("put_state",)]


def test_a_type_change_calls_bedrock_again_for_the_new_type():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "TRAFFIC_SPIKE", failing(1))
    result = feed(deps, 2, "CASCADING_FAILURE", failing(2))
    assert result["changes"] == ["type_changed"]
    rec = deps.records[ws(1)]
    assert len(deps.explain_calls) == 2 and rec["bedrock_calls"] == 2
    assert rec["diagnosis_for_type"] == "CASCADING_FAILURE"
    assert rec["prediction"]["incident_type"] == "CASCADING_FAILURE"


def test_the_call_cap_is_respected(monkeypatch):
    monkeypatch.setattr(record, "MAX_CALLS", 1)
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "TRAFFIC_SPIKE", failing(1))
    feed(deps, 2, "CASCADING_FAILURE", failing(2))
    assert len(deps.explain_calls) == 1


def test_two_quiet_minutes_close_the_incident_at_its_last_counted_minute():
    deps = FakeDeps()
    opened(deps)
    feed(deps, 3)
    assert deps.records[ws(2)]["status"] == "OPEN"
    feed(deps, 4)
    rec = deps.records[ws(2)]
    assert rec["status"] == "CLOSED" and rec["ended_at"] == ws(3)
    assert deps.state["incident"]["status"] == "CLOSED" and len(deps.explain_calls) == 1


def test_a_gap_closes_the_old_incident_and_opens_the_next_in_one_step():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "DEPENDENCY_FAILURE", failing(1))
    result = feed(deps, 9, "TRAFFIC_SPIKE", failing(9))
    assert result["changes"] == ["closed", "opened"]
    assert deps.records[ws(1)]["status"] == "CLOSED" and deps.records[ws(1)]["ended_at"] == ws(2)
    assert deps.records[ws(9)]["status"] == "OPEN" and len(deps.explain_calls) == 2


def test_a_repeated_event_changes_nothing():
    deps = FakeDeps()
    opened(deps)
    before = len(deps.ops)
    assert feed(deps, 2, "DEPENDENCY_FAILURE", failing(2))["action"] == "ignored"
    assert len(deps.ops) == before and len(deps.explain_calls) == 1


def test_a_straggler_is_ignored():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 5, "DEPENDENCY_FAILURE", failing(5))
    before = len(deps.ops)
    assert feed(deps, 3, "DEPENDENCY_FAILURE", failing(3))["action"] == "ignored"
    assert len(deps.ops) == before


def test_a_failed_explanation_is_stored_as_an_error_and_the_state_still_advances():
    deps = FakeDeps()
    deps.fail = core.ExplainError("DiagnosisFailed", "refused")
    opened(deps)
    rec = deps.records[ws(2)]
    assert rec["diagnosis"] is None and rec["bedrock_calls"] == 1
    assert rec["diagnosis_error"]["type"] == "DiagnosisFailed"
    assert deps.state["incident"]["status"] == "OPEN" and deps.lines == []


def test_an_input_the_contract_refuses_is_stored_as_an_error_and_never_sent(monkeypatch):
    monkeypatch.setattr(builder, "build_input", lambda *args: {"incident_id": "x"})
    deps = FakeDeps()
    opened(deps)
    assert deps.explain_calls == []
    assert deps.records[ws(2)]["diagnosis_error"]["type"] == "BadInput"


def test_a_running_experiment_is_stored_as_the_ground_truth_reference():
    deps = FakeDeps()
    deps.lock = {"experiment_id": "exp-1",
                 "expires_at": int(datetime(2026, 11, 6, 14, 5, 48, tzinfo=timezone.utc).timestamp()) + 3600}
    opened(deps)
    rec = deps.records[ws(2)]
    assert rec["experiment_id"] == "exp-1" and "experiment_id" not in rec["prediction"]


def test_a_lock_a_crashed_run_left_behind_is_not_a_reference():
    deps = FakeDeps()
    deps.lock = {"experiment_id": "exp-dead",
                 "expires_at": int(datetime(2026, 11, 6, 14, 5, 48, tzinfo=timezone.utc).timestamp()) - 3600}
    opened(deps)
    assert "experiment_id" not in deps.records[ws(2)]


def test_no_lock_means_a_real_incident_with_no_reference():
    deps = FakeDeps()
    opened(deps)
    assert "experiment_id" not in deps.records[ws(2)]


def test_the_reference_never_reaches_bedrocks_input():
    deps = FakeDeps()
    deps.lock = {"experiment_id": "exp-1",
                 "expires_at": int(datetime(2026, 11, 6, 14, 5, 48, tzinfo=timezone.utc).timestamp()) + 3600}
    opened(deps)
    text = json.dumps(deps.explain_calls[0])
    assert "experiment_id" not in text and "exp-1" not in text
    assert contract.FORBIDDEN_KEYS.isdisjoint(set(deps.explain_calls[0]))


def test_the_record_is_written_before_bedrock_and_again_after_and_the_state_last():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1)
    before = len(deps.ops)
    feed(deps, 2, "DEPENDENCY_FAILURE", failing(2))
    assert deps.ops[before:] == [("put_record", False), ("put_record", True), ("put_state",)]


def test_a_stored_diagnosis_logs_its_round_trip_latency():
    deps = FakeDeps()
    opened(deps)
    line = deps.lines[0]
    assert line["event"] == "incident_diagnosis" and line["Function"] == core.FUNCTION
    assert line["DiagnosisToStoredSeconds"] == 12.75 and line["RoundTripSeconds"] == 180.0
    assert line["SinceWindowEndSeconds"] == 167.25 and line["BedrockMs"] == 8200
    assert {m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]} == {
        "SinceWindowEndSeconds", "DiagnosisToStoredSeconds", "RoundTripSeconds", "BedrockMs"}


def test_the_window_is_read_from_s3_when_the_event_does_not_carry_it():
    deps = FakeDeps()
    deps.windows[key(2)] = failing(2)
    feed(deps, 0)
    feed(deps, 1)
    deps.t = datetime(2026, 11, 6, 14, 5, 48, tzinfo=timezone.utc)
    result = core.process(event(2, "DEPENDENCY_FAILURE", request=False), deps)
    assert deps.records[ws(2)]["status"] == "OPEN" and result["window_from"] == "s3"


def test_a_reply_payload_that_is_a_json_string_is_accepted():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "DEPENDENCY_FAILURE", failing(1), as_string=True)
    assert deps.records[ws(1)]["status"] == "OPEN"


def test_a_window_that_is_not_the_predictions_is_refused():
    deps = FakeDeps()
    with pytest.raises(ValueError):
        feed(deps, 1, win=window(ws(5)))


def test_the_summary_says_the_window_came_from_the_event():
    deps = FakeDeps()
    assert feed(deps, 0)["window_from"] == "event"


# ---- Added in review (PR #98) -------------------------------------------------------------------

def test_a_failed_first_call_is_retried_on_the_next_counted_minute():
    deps = FakeDeps()
    deps.fail = core.ExplainError("ThrottlingException", "slow down")
    opened(deps)
    deps.fail = None
    feed(deps, 3, "DEPENDENCY_FAILURE", failing(3))
    rec = deps.records[ws(2)]
    assert len(deps.explain_calls) == 2 and rec["bedrock_calls"] == 2
    assert rec["diagnosis"] == REPLY["diagnosis"] and rec["diagnosis_error"] is None
    assert rec["diagnosis_for_type"] == "DEPENDENCY_FAILURE"
    # The record's latency runs from detected_at (minute 2's diagnosis, 14:05:47.25) to the stored
    # retry (now() at minute 3's step: 14:06:48, then 14:07:00).
    assert rec["latency"]["diagnosis_to_stored_s"] == 72.75
    # The log line is the retry's own: from minute 3's diagnosis (14:06:47.25).
    assert deps.lines[-1]["DiagnosisToStoredSeconds"] == 12.75


def test_no_retry_once_a_diagnosis_for_the_current_type_is_stored():
    deps = FakeDeps()
    opened(deps)
    for m in (3, 4, 5):
        feed(deps, m, "DEPENDENCY_FAILURE", failing(m))
    assert len(deps.explain_calls) == 1


def test_a_failing_explanation_stops_at_the_cap():
    deps = FakeDeps()
    deps.fail = core.ExplainError("DiagnosisFailed", "refused")
    opened(deps)
    for m in range(3, 10):
        feed(deps, m, "DEPENDENCY_FAILURE", failing(m))
    assert len(deps.explain_calls) == record.MAX_CALLS
    assert deps.records[ws(2)]["bedrock_calls"] == record.MAX_CALLS


def test_a_failed_call_after_a_type_change_is_retried_for_the_new_type():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "TRAFFIC_SPIKE", failing(1))
    deps.fail = core.ExplainError("Timeout", "t")
    feed(deps, 2, "CASCADING_FAILURE", failing(2))
    assert deps.records[ws(1)]["diagnosis_for_type"] == "TRAFFIC_SPIKE"
    deps.fail = None
    feed(deps, 3, "CASCADING_FAILURE", failing(3))
    rec = deps.records[ws(1)]
    assert len(deps.explain_calls) == 3 and rec["diagnosis_for_type"] == "CASCADING_FAILURE"


def test_a_minute_the_detector_flagged_never_enters_the_baseline():
    deps = FakeDeps()
    feed(deps, 0, win=window(ws(0), order_p95=100.0))
    feed(deps, 1, win=window(ws(1), order_p95=900.0), flagged=True)    # NORMAL, but flagged
    feed(deps, 2, win=window(ws(2), order_p95=120.0))
    assert [w["window_start"] for w in deps.state["recent_normal"]] == [ws(0), ws(2)]
    feed(deps, 3, "DEPENDENCY_FAILURE", failing(3))
    assert deps.state["baseline"]["order_p95_latency_ms"] == 110.0


def test_quiet_minutes_of_an_open_incident_neither_enter_the_baseline_nor_rewrite_the_record():
    deps = FakeDeps()
    opened(deps)
    before = len(deps.ops)
    feed(deps, 3)
    assert [w["window_start"] for w in deps.state["recent_normal"]] == [ws(0), ws(1)]
    assert deps.ops[before:] == [("put_state",)]


def test_the_baseline_is_the_mean_of_the_last_five_normal_windows():
    deps = FakeDeps()
    for m, p95 in enumerate((1000.0, 100.0, 110.0, 120.0, 130.0, 140.0)):
        feed(deps, m, win=window(ws(m), order_p95=p95))
    feed(deps, 6, "DEPENDENCY_FAILURE", failing(6))
    assert deps.state["baseline"]["order_p95_latency_ms"] == 120.0


def test_a_counted_minute_refreshes_the_affected_services():
    deps = FakeDeps()
    opened(deps)
    assert deps.records[ws(2)]["prediction"]["affected_services"] == ["order-service", "payment-service"]
    spread = {**failing(3), "product_error_rate_5xx": 0.3}
    feed(deps, 3, "DEPENDENCY_FAILURE", spread)
    assert deps.records[ws(2)]["prediction"]["affected_services"] == [
        "product-service", "order-service", "payment-service"]

def test_each_calls_log_line_has_its_own_numbers_and_the_record_keeps_the_first():
    deps = FakeDeps()
    feed(deps, 0)
    feed(deps, 1, "TRAFFIC_SPIKE", failing(1))
    deps.reply = {**REPLY, "latency_ms": 31000}
    feed(deps, 2, "CASCADING_FAILURE", failing(2))
    assert [line["BedrockMs"] for line in deps.lines] == [8200, 31000]
    assert deps.records[ws(1)]["latency"]["bedrock_ms"] == 8200
