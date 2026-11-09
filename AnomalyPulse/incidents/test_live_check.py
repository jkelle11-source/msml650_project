"""Tests for M8-3's live-check logic (the judging, not the AWS reads). No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_live_check.py -q
"""
import copy
from datetime import datetime, timezone

from incidents import live_check, record

STARTED = "2026-11-06T14:02:00.000Z"
DIAGNOSED = "2026-11-06T14:05:47.250Z"
STATE = {"status": "OPEN", "started_at": STARTED, "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.93}
PREDICTION = {"latency": {"diagnosed_at": DIAGNOSED, "since_window_end_s": 167.25}}
REPLY = {"diagnosis": {"severity": "HIGH", "summary": "s", "root_cause": "r", "evidence": ["e"],
                       "recommended_actions": ["a"]},
         "evidence_metrics": [], "self_check": {"passed": True}, "runbook": [], "latency_ms": 8200}
NOW = datetime(2026, 11, 6, 14, 30, 0, tzinfo=timezone.utc)
INPUT = {"incident_id": "inc-20261106T140200Z", "metrics": []}


def good(experiment_id="exp-1"):
    rec = record.open_record(STATE, PREDICTION, ["order-service", "payment-service"],
                             {"estimator": "e", "job": "j"}, experiment_id)
    rec = record.with_diagnosis(rec, "DEPENDENCY_FAILURE", REPLY, "2026-11-06T14:05:59.750Z", INPUT)
    return record.close(rec, "2026-11-06T14:08:00.000Z")


def run(records, want="DEPENDENCY_FAILURE"):
    return live_check.check_run(records, want, "exp-1")


def test_a_good_fault_run_passes():
    assert run([good()]) == ([], [])


def test_a_normal_run_with_no_incident_passes():
    assert run([], "NORMAL") == ([], [])


def test_a_normal_run_that_opened_an_incident_fails():
    assert "opened 1 incident" in run([good()], "NORMAL")[0][0]


def test_a_fault_run_needs_exactly_one_incident():
    assert "got 0" in run([])[0][0]
    assert "got 2" in run([good(), good()])[0][0]


def test_the_wrong_incident_type_fails():
    assert any("expected CASCADING_FAILURE" in p for p in run([good()], "CASCADING_FAILURE")[0])


def test_an_incident_that_never_closed_fails():
    rec = good()
    rec["status"], rec["ended_at"] = "OPEN", None
    assert "never closed" in run([rec])[0][0]


def test_a_missing_or_wrong_reference_fails():
    assert any("experiment_id" in p for p in run([good("exp-other")])[0])
    rec = good()
    del rec["experiment_id"]
    assert any("experiment_id" in p for p in run([rec])[0])


def test_a_reference_inside_the_prediction_fails():
    rec = good()
    rec["prediction"]["experiment_id"] = "exp-1"
    assert any("ground-truth key" in p for p in run([rec])[0])


def test_a_missing_diagnosis_fails_and_names_the_error():
    rec = record.close(record.with_error(good("exp-1") | {"diagnosis": None, "bedrock_calls": 0},
                                         "DEPENDENCY_FAILURE", {"type": "DiagnosisFailed", "message": "x"}),
                       "2026-11-06T14:08:00.000Z")
    problems, _ = run([rec])
    assert any("no diagnosis was stored (DiagnosisFailed)" in p for p in problems)


def test_the_call_count_must_be_one_to_five():
    rec = good()
    rec["bedrock_calls"] = 6
    assert any("bedrock_calls is 6" in p for p in run([rec])[0])
    rec["bedrock_calls"] = 0
    assert any("bedrock_calls is 0" in p for p in run([rec])[0])


def test_a_failed_self_check_is_a_note_not_a_failure():
    rec = good()
    rec["self_check"] = {"passed": False}
    problems, notes = run([rec])
    assert problems == [] and any("failed its self-check" in n for n in notes)


def test_a_diagnosis_for_an_earlier_type_is_a_note():
    rec = good()
    rec["diagnosis_for_type"] = "TRAFFIC_SPIKE"
    problems, notes = run([rec])
    assert problems == [] and any("made for TRAFFIC_SPIKE" in n for n in notes)


def test_log_lines_that_are_not_json_are_skipped():
    assert live_check.parse_lines(['{"event": "incident_step"}', "START RequestId: x", "[1, 2]", ""]) == [
        {"event": "incident_step"}]


def test_steps_are_counted_by_where_the_window_came_from():
    lines = [{"event": "incident_step", "changes": ["opened"], "window_from": "event"},
             {"event": "incident_step", "changes": ["type_changed"], "window_from": "event"},
             {"event": "incident_step", "changes": ["closed", "opened"], "window_from": "s3"},
             {"event": "incident_diagnosis"}]
    assert live_check.summarize_steps(lines) == {"steps": 3, "window_from": {"event": 2, "s3": 1},
                                                 "opened": 2, "type_changed": 1, "closed": 1}


def test_latency_is_summarized_from_the_metric_lines():
    lines = [{"event": "incident_diagnosis", "RoundTripSeconds": 180.0, "BedrockMs": 8000},
             {"event": "incident_diagnosis", "RoundTripSeconds": 200.0, "BedrockMs": 12000},
             {"event": "incident_step"}]
    out = live_check.summarize_latency(lines)
    assert out["diagnoses"] == 2 and out["RoundTripSeconds"] == {"median": 190.0, "max": 200.0}
    assert out["SinceWindowEndSeconds"] is None


def test_latency_counts_only_the_runs_own_incidents():
    lines = [{"event": "incident_diagnosis", "incident_id": "inc-a", "RoundTripSeconds": 180.0},
             {"event": "incident_diagnosis", "incident_id": "inc-next-run", "RoundTripSeconds": 200.0}]
    assert live_check.summarize_latency(lines, {"inc-a"})["RoundTripSeconds"] == {"median": 180.0, "max": 180.0}
    assert live_check.summarize_latency(lines, set()) == {
        "diagnoses": 0, "SinceWindowEndSeconds": None, "DiagnosisToStoredSeconds": None,
        "RoundTripSeconds": None, "BedrockMs": None}


def test_the_record_range_ends_at_the_run_end():
    # The live noop (21:42:44.489 to 21:58:44.511) and the cascade chained after it: the cascade's incident
    # (22:04) is not the noop's.
    noop = live_check.record_range(datetime(2026, 11, 8, 21, 42, 44, 489000, tzinfo=timezone.utc),
                                   datetime(2026, 11, 8, 21, 58, 44, 511000, tzinfo=timezone.utc))
    assert noop == ("2026-11-08T21:41:44.489Z", "2026-11-08T21:58:44.511Z")
    lo, hi = noop
    assert lo <= "2026-11-08T21:42:00.000Z" <= hi          # the run's first minute
    assert lo <= "2026-11-08T21:58:00.000Z" <= hi          # its last
    assert not lo <= "2026-11-08T22:04:00.000Z" <= hi      # the next run's incident


def test_a_stored_record_reads_back_through_the_dashboard():
    problems, latency = live_check.readback(good(), None, NOW)
    assert problems == [] and latency is None


def test_a_record_the_dashboard_cannot_read_is_reported():
    rec = copy.deepcopy(good())
    del rec["prediction"]["affected_services"]
    problems, _ = live_check.readback(rec, None, NOW)
    assert problems and "could not read the record" in problems[0]


def test_a_diagnosis_stored_without_its_input_fails():
    rec = good()
    rec["diagnosis_input"] = None
    problems, _ = run([rec])
    assert problems == ["no diagnosis_input was stored with the diagnosis"]


def test_a_record_stored_before_retries_needs_no_input():
    rec = {k: v for k, v in good().items() if k not in ("diagnosis_input", "retried_for")}
    assert run([rec]) == ([], [])


def test_a_retry_is_a_note():
    rec = good()
    rec["retried_for"] = ["DEPENDENCY_FAILURE"]
    problems, notes = run([rec])
    assert problems == [] and notes == ["a flagged diagnosis was asked for again for DEPENDENCY_FAILURE"]
