"""Tests for M8-4: the stored diagnosis through the dashboard feed. No AWS.

Every saved Sonnet diagnosis goes the whole way: stored with its input, through DynamoDB's Decimal round
trip, record.to_incident, build_dashboard and the read API's JSON, then feed.check.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_feed.py -q
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from genai import contract, self_check
from incidents import feed, live_check, record

SAMPLES = Path(__file__).resolve().parent.parent / "genai" / "samples"
RUNS = ("global.anthropic.claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6+runbooks",
        "global.anthropic.claude-sonnet-4-6+runbooks-review")
SAVED = sorted((run, path.stem) for run in RUNS for path in (SAMPLES / "results" / run).glob("*.json"))
STARTED = "2026-10-30T14:32:00.000Z"
PREDICTION = {"latency": {"diagnosed_at": "2026-10-30T14:35:47.250Z", "since_window_end_s": 167.25}}
MODEL = {"estimator": "logistic_regression", "job": "job-1"}
NOW = datetime(2026, 10, 30, 14, 40, 0, tzinfo=timezone.utc)


def stored(run="global.anthropic.claude-sonnet-4-6", name="dependency_failure", **reply_changes):
    """A record holding one saved diagnosis and its input, as DynamoDB gives it back."""
    given = json.loads((SAMPLES / f"{name}.json").read_text())
    reply = {**json.loads((SAMPLES / "results" / run / f"{name}.json").read_text()), **reply_changes}
    state = {"status": "OPEN", "started_at": STARTED, "incident_type": given["incident_type"],
             "confidence": given["confidence"]}
    rec = record.open_record(state, PREDICTION, given["affected_services"], MODEL)
    rec = record.with_diagnosis(rec, given["incident_type"], reply, "2026-10-30T14:36:00.000Z", given)
    return record.from_dynamodb(record.to_dynamodb(rec))


def checked(rec):
    return feed.check(rec, feed.feed_view(rec, NOW))


def with_text(rec, **sections):
    """The record with some sections rewritten and its stored self-check clean, as if the checker had
    missed them: the feed's own check must still catch them."""
    new = copy.deepcopy(rec)
    new["diagnosis"].update(sections)
    new["self_check"] = {"passed": True, "findings": []}
    return new


# --- every saved diagnosis -------------------------------------------------------------------

@pytest.mark.parametrize("run,name", SAVED)
def test_every_saved_diagnosis_reaches_the_feed_intact_or_is_held_back(run, name):
    rec = stored(run, name)
    problems, notes = checked(rec)
    assert problems == []
    shown = feed.feed_view(rec, NOW)["diagnosis"]
    if record.held_back(rec):
        assert shown is None and any("held back" in n for n in notes)
    else:
        assert shown == rec["diagnosis"]                  # all five sections, byte for byte
        assert shown["evidence"] == rec["diagnosis"]["evidence"]
        assert shown["recommended_actions"] == rec["diagnosis"]["recommended_actions"]


def test_the_saved_runs_cover_both_outcomes():
    held = [s for s in SAVED if record.held_back(stored(*s))]
    assert 0 < len(held) < len(SAVED)


# --- what is held back -----------------------------------------------------------------------

def _flagged(rec, *findings):
    new = copy.deepcopy(rec)
    new["self_check"] = {"passed": False, "findings": list(findings)}
    return new


def test_causal_or_ordering_wording_is_held_back():
    for finding in ("summary: causal wording 'caused'; correlated timestamps are an observed sequence, "
                    "not proof of cause",
                    "summary: orders symptoms without saying 'observed sequence': 'A rose, then B.'"):
        rec = _flagged(stored(), finding)
        assert record.to_incident(rec)["diagnosis"] is None
        assert checked(rec)[0] == []


def test_a_citation_outside_the_input_is_held_back():
    rec = _flagged(stored(), "evidence cites cart-service.p95_latency_ms, which is not in the input")
    assert record.to_incident(rec)["diagnosis"] is None


def test_a_bullet_citing_nothing_is_held_back():
    # The schema lets a bullet cite only the input's metrics but can't require one; the explain Lambda's own
    # checks (as explain.diagnose runs them) find the bullet citing nothing, and the feed must not show it.
    rec = stored()
    rec["evidence_metrics"][1] = []
    raw = feed.raw_diagnosis(rec["diagnosis"], rec["evidence_metrics"])
    found = contract.check_output_shape(raw) + self_check.check(rec["diagnosis_input"], raw)
    assert "evidence[1] cites no input metric" in found
    rec["self_check"] = {"passed": False, "findings": found}
    assert record.held_back(rec) and record.to_incident(rec)["diagnosis"] is None
    assert checked(rec)[0] == []


def test_the_failing_kinds_are_the_held_kinds():
    # What the feed holds back is exactly what its check must never find in shown text.
    assert set(feed.FAILING_KINDS) == set(record.HOLD_KINDS)


def changed_type(rec, to="CASCADING_FAILURE"):
    """The record after a type change, before the new type's diagnosis is stored (core.process writes the
    new type first and calls Bedrock second)."""
    return record.refresh(rec, {"incident_type": to, "confidence": 0.8})


def test_a_diagnosis_for_another_type_is_held_until_the_new_types_arrives():
    rec = changed_type(stored())
    assert record.for_another_type(rec) and record.to_incident(rec)["diagnosis"] is None
    problems, notes = checked(rec)
    assert problems == [] and notes == ["diagnosis made for DEPENDENCY_FAILURE, not CASCADING_FAILURE: "
                                        "the console shows Severity pending"]
    failed = record.with_error(rec, "CASCADING_FAILURE", {"type": "ThrottlingException", "message": ""})
    assert record.to_incident(failed)["diagnosis"] is None                # the old one is kept, not shown
    reply = json.loads((SAMPLES / "results" / RUNS[0] / "cascading_failure.json").read_text())
    given = json.loads((SAMPLES / "cascading_failure.json").read_text())
    done = record.with_diagnosis(failed, "CASCADING_FAILURE", reply, "2026-10-30T14:38:00.000Z", given)
    assert record.to_incident(done)["diagnosis"] == reply["diagnosis"]
    assert not record.for_another_type(record.open_record(
        {"started_at": STARTED, "incident_type": "TRAFFIC_SPIKE", "confidence": 0.9}, PREDICTION, [], MODEL))


def test_a_diagnosis_for_another_type_on_the_dashboard_fails():
    rec = changed_type(stored())
    shown = feed.feed_view(rec, NOW) | {"diagnosis": rec["diagnosis"]}
    assert feed.check(rec, shown)[0] == ["a diagnosis made for another type reached the dashboard"]


@pytest.mark.parametrize("finding", ["severity: HIGH, but the rubric gives MEDIUM",
                                     "summary: the number 15 is not in or derivable from the input",
                                     "root_cause: claims 'provisioned', but the input has no metric for it"])
def test_other_findings_are_shown(finding):
    rec = _flagged(stored(), finding)
    assert record.to_incident(rec)["diagnosis"] == rec["diagnosis"]


def test_a_clean_retry_replaces_a_held_diagnosis():
    given = json.loads((SAMPLES / "dependency_failure.json").read_text())
    rec = _flagged(stored(), "summary: causal wording 'led to'; correlated timestamps are an observed "
                             "sequence, not proof of cause")
    reply = json.loads((SAMPLES / "results" / RUNS[0] / "dependency_failure.json").read_text())
    rec = record.with_retry(rec, given["incident_type"], reply, "2026-10-30T14:37:00.000Z", given)
    assert record.to_incident(rec)["diagnosis"] == reply["diagnosis"]


def test_a_held_diagnosis_on_the_dashboard_fails():
    rec = _flagged(stored(), "summary: causal wording 'caused'; correlated timestamps are an observed "
                             "sequence, not proof of cause")
    shown = feed.feed_view(rec, NOW) | {"diagnosis": rec["diagnosis"]}
    assert feed.check(rec, shown)[0] == ["a held-back diagnosis reached the dashboard"]


def test_the_live_check_reads_a_held_record_back():
    rec = _flagged(stored(), "summary: causal wording 'caused'; correlated timestamps are an observed "
                             "sequence, not proof of cause")
    assert live_check.readback(rec, None, NOW) == ([], None)
    assert live_check.readback(changed_type(stored()), None, NOW) == ([], None)


def test_an_incident_waiting_for_bedrock_passes():
    rec = stored()
    rec.update(diagnosis=None, diagnosis_input=None, evidence_metrics=None, self_check=None)
    assert checked(rec) == ([], [])


# --- 1. every section intact -----------------------------------------------------------------

@pytest.mark.parametrize("change", [
    lambda d: d.update(summary=d["summary"][:-1]),
    lambda d: d.update(evidence=d["evidence"][::-1]),
    lambda d: d.update(recommended_actions=d["recommended_actions"][:-1]),
    lambda d: d.update(severity="LOW" if d["severity"] != "LOW" else "HIGH"),
    lambda d: d.update(extra="x"),
    lambda d: d.pop("root_cause"),
])
def test_a_section_changed_on_the_way_fails(change):
    rec = stored()
    shown = feed.feed_view(rec, NOW)
    change(shown["diagnosis"])
    assert feed.check(rec, shown)[0]


def test_a_changed_prediction_fails():
    rec = stored()
    shown = feed.feed_view(rec, NOW)
    shown["prediction"]["confidence"] = 0.5
    assert feed.check(rec, shown)[0] == ["the dashboard's incident_id or prediction is not the stored one"]


# --- 2. no ordering claim rendered as causation ----------------------------------------------

@pytest.mark.parametrize("summary", [
    "Payment latency rose, which caused order failures.",
    "Order 5xx errors began after payment latency rose.",
])
def test_causal_or_ordering_text_the_stored_check_missed_fails(summary):
    problems, _ = checked(with_text(stored(), summary=summary))
    assert any(p.startswith("an ordering claim rendered as causation") for p in problems)


def test_an_observed_sequence_is_not_causation():
    rec = with_text(stored(), summary="Payment p95 latency rose from 110 ms to 2900 ms.")
    assert checked(rec)[0] == []


# --- 3. every evidence bullet traces to an input metric --------------------------------------

def test_a_bullet_citing_a_metric_outside_the_input_fails():
    rec = stored()
    rec["evidence_metrics"][0] = ["cart-service.p95_latency_ms"]
    problems, _ = checked(rec)
    assert "evidence[0] cites cart-service.p95_latency_ms, which is not in the input" in problems


def test_a_bullet_citing_nothing_fails():
    rec = stored()
    rec["evidence_metrics"][1] = []
    assert "evidence[1] cites no metric" in checked(rec)[0]


def test_citations_out_of_step_with_the_bullets_fail():
    rec = stored()
    rec["evidence_metrics"] = rec["evidence_metrics"][:-1]
    problems, _ = checked(rec)
    assert problems and "evidence_metrics entries for" in problems[0]


def test_a_diagnosis_without_its_input_fails():
    rec = stored()
    rec["diagnosis_input"] = None
    assert checked(rec)[0] == ["no diagnosis_input: traceability and wording can't be checked again"]


def before_102(rec):
    """The record as M8-3 stored it before #102: no diagnosis_input and no retried_for."""
    return {k: v for k, v in rec.items() if k not in ("diagnosis_input", "retried_for")}


def test_a_record_stored_before_102_passes_with_a_note():
    # M8-3's two live runs (2026-11-08) were stored before #102 kept the input; live_check passes them too.
    rec = before_102(stored())
    problems, notes = checked(rec)
    assert problems == []
    assert notes == ["stored before #102 (no diagnosis_input): citations and wording not checked against the input"]


def test_a_record_stored_before_102_still_needs_every_section_and_a_citation_per_bullet():
    rec = before_102(stored())
    shown = feed.feed_view(rec, NOW)
    shown["diagnosis"]["summary"] = "changed on the way"
    assert feed.check(rec, shown)[0] == ["summary is not as stored"]
    rec["evidence_metrics"] = [[]] + rec["evidence_metrics"][1:]
    assert checked(rec)[0] == ["evidence[0] cites no metric"]
    rec["evidence_metrics"] = rec["evidence_metrics"][1:]
    assert "evidence bullets" in checked(rec)[0][0]


def test_the_runbook_text_is_used_for_action_numbers():
    # This answer's actions quote a number only its runbook has (10): allowed with the runbook's text,
    # a note without it.
    rec = stored("global.anthropic.claude-sonnet-4-6+runbooks", "lambda_degradation")
    assert rec["runbook"] == "lambda_degradation"
    assert not any("the number 10" in n for n in checked(rec)[1])
    assert any("the number 10 is not in or derivable" in n for n in checked({**rec, "runbook": None})[1])
