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


def test_the_runbook_text_is_used_for_action_numbers():
    rec = stored("global.anthropic.claude-sonnet-4-6+runbooks", "dependency_failure")
    assert rec["runbook"] == "dependency_failure"
    assert not any("the number 6" in n for n in checked(rec)[1])
