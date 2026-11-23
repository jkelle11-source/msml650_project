"""Tests for M9-5's scoring, comparison and blind sheet. No AWS, no Bedrock.

Run from AnomalyPulse/:  python -m pytest -q genai/m9_5
"""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

from genai.m9_5 import compare, score, sheet  # noqa: E402

INPUT = json.loads((Path(__file__).parents[1] / "samples" / "dependency_failure.json").read_text())
# A faithful diagnosis of the dependency-failure sample (genai/test_genai.py's GOOD), as the store keeps it.
GOOD = {
    "severity": "HIGH",
    "summary": ("Order requests are failing while calls to Payment have slowed sharply. Order's 5xx share "
                "is 16.7% and Payment's p95 latency is 2.9 s against 110 ms before the incident. Order "
                "failures began about 40 seconds after Payment latency rose (observed sequence from "
                "correlated timestamps)."),
    "root_cause": "A slow or failing payment dependency is the most likely root cause.",
    "evidence": ["Payment p95 latency rose 26x, from 110 ms to 2900 ms.",
                 "Order's 5xx share rose from 0% to 16.7%.",
                 "Order recorded 57 failed calls to Payment in the latest minute."],
    "recommended_actions": ["Inspect payment-service health and its recent errors.",
                            "Review the timeout and retry settings on Order's call to Payment.",
                            "Check whether Payment changed recently."],
}
CITES = [["payment-service.p95_latency_ms"], ["order-service.error_rate_5xx"],
         ["order-service.dependency_error_count"]]


def rec(phase="test", **diagnosis):
    return {"incident_id": "inc-1", "phase": phase, "labeled_type": "DEPENDENCY_FAILURE", "runbook": None,
            "diagnosis_input": copy.deepcopy(INPUT), "diagnosis": {**copy.deepcopy(GOOD), **diagnosis},
            "evidence_metrics": copy.deepcopy(CITES), "self_check": {"passed": True, "findings": []}}


# --- fidelity and hallucination ----------------------------------------------------------------

def test_a_faithful_diagnosis_has_every_claim_supported():
    s = score.score_record(rec())
    assert sum(s["claims"].values()) == 5 and not s["unsupported"] and not s["hallucinated"]
    assert s["mentions"]["service"] >= 4 and s["mentions"]["number"] >= 8


def test_restating_the_label_without_its_metric_is_unsupported_not_a_number():
    # Test set: DATABASE_THROTTLING handed over with only payment-service's metrics in the input.
    s = score.score_record(rec(root_cause="DynamoDB throttling is the most likely root cause."))
    assert s["unsupported"] == {"root_cause": 1}
    assert s["hallucinated"]["metric"] == 2          # 'throttl' and 'DynamoDB'
    assert s["unsupported_kinds"] == {"metric": 1}


def test_a_service_the_input_lacks_is_hallucinated():
    s = score.score_record(rec(summary=GOOD["summary"] + " Cart-service is unaffected."))
    assert s["unsupported"] == {"summary": 1} and s["hallucinated"]["service"] == 1


def test_a_real_number_on_the_wrong_metric_is_misattributed_not_hallucinated():
    r = rec()
    r["diagnosis"]["evidence"][1] = "Order's 5xx share rose to 16.7%, with 57 failed calls."
    s = score.score_record(r)
    assert s["unsupported"] == {"evidence": 1} and s["misattributed"] == 1 and not s["hallucinated"]


def test_a_bullet_citing_a_metric_the_input_lacks_is_unsupported():
    r = rec()
    r["evidence_metrics"][2] = ["order-service.db_latency_ms_p95"]
    assert score.score_record(r)["unsupported"] == {"evidence": 1}


def test_today_s_self_check_is_used_not_the_stored_one():
    r = rec(summary="Cart-service is failing.")
    r["self_check"] = {"passed": True, "findings": []}
    assert score.score_record(r)["unsupported"]


# --- completeness and the capacity slip ----------------------------------------------------------

def test_completeness_counts_moved_metrics_cited_and_services_named():
    c = score.completeness(rec())
    # Every sample metric moved; dependency_latency_ms_p95 and order p95 are not cited.
    assert c == {"moved": 5, "moved_cited": 3, "services": 2, "services_named": 2}


@pytest.mark.parametrize("action, slip", [
    ("Check provisioned capacity on the Carts and Orders tables.", True),
    ("Raise write capacity on the order-service DynamoDB table.", True),
    ("If throttling is confirmed on a DynamoDB table used by payment-service, increase provisioned throughput.", True),
    ("Check DynamoDB auto-scaling policies on the product-service, cart-service, and order-service tables.", True),
    ("Raise the Products table's provisioned read capacity.", False),
    ("Check payment-service reserved concurrency; add provisioned concurrency if throttled.", False),
    ("Review order-service DynamoDB write latency; write capacity consumption doubled.", False),
])
def test_the_capacity_slip(action, slip):
    assert bool(score.capacity_slips(rec(recommended_actions=[action]))) is slip


# --- the summary and the retries ----------------------------------------------------------------

def test_summarize_counts_rates_and_takes_severity_from_the_stored_check():
    bad = rec(root_cause="DynamoDB throttling is the most likely root cause.")
    bad["self_check"] = {"passed": False, "findings": ["severity: LOW, but the rubric gives HIGH"]}
    out = score.summarize([rec(), bad])
    assert out["fidelity"]["claims"] == 10 and out["fidelity"]["unsupported"] == 1
    assert out["fidelity"]["evidence_fidelity"] == 0.9
    assert out["fidelity"]["by_field"]["root_cause"] == {"claims": 2, "unsupported": 1, "rate": 0.5}
    assert out["severity_disagreements"] == 1


def test_the_report_keeps_the_stress_slice_apart():
    out = score.report([rec(), rec(phase="stress")], [])
    assert out["test"]["diagnoses"] == 1 and out["stress"]["diagnoses"] == 1
    assert out["test_when_the_type_agrees_with_the_run"]["agrees"]["diagnoses"] == 1


def test_retry_summary():
    lines = [{"incident_id": "a", "for_type": "X", "flagged": ["metric"], "kept": False, "found": ["metric"]},
             {"incident_id": "b", "for_type": "X", "flagged": ["number"], "kept": True, "found": []}]
    assert score.retry_summary(lines) == {"retries": 2, "kept": 1, "errors": 0,
                                          "flagged_for": {"metric": 1, "number": 1},
                                          "kept_still_found": {}, "dropped_found": {"metric": 1}}


def test_the_runbook_sample_scores_a_runbook_only_fact_in_a_claim_as_unsupported(tmp_path):
    on = _reply(actions=["Raise the Products table's provisioned read capacity."])
    on["diagnosis"]["root_cause"] = "The provisioned table is the most likely root cause."
    (tmp_path / "inc-1.json").write_text(json.dumps({**on, "calls": [on]}))
    out = score.runbook_sample([rec()], tmp_path)
    assert out["without"]["fidelity"]["unsupported"] == 0
    assert out["with"]["fidelity"]["by_field"]["root_cause"]["unsupported"] == 1
    assert out["bedrock_calls"] == 1


# --- the comparison ----------------------------------------------------------------------------

def _reply(findings=(), actions=("Check the Products table.",)):
    return {"diagnosis": {**GOOD, "recommended_actions": list(actions)}, "evidence_metrics": CITES,
            "runbook": "dependency_failure", "self_check": {"passed": not findings, "findings": list(findings)}}


def test_with_runbooks_retries_a_flagged_answer_once_and_keeps_a_clean_retry():
    replies = [_reply(["summary: the number 9 is not in or derivable from the input"]), _reply()]
    seen = []

    def diagnose(evidence, runbooks, revise=None):
        seen.append((runbooks, revise))
        return replies.pop(0)
    out = compare.with_runbooks(INPUT, diagnose)
    assert [r for r, _ in seen] == [True, True] and seen[1][1]["findings"]
    assert out["self_check"]["passed"] and len(out["calls"]) == 2


def test_with_runbooks_keeps_the_first_answer_when_the_retry_is_no_better():
    first = _reply(["summary: the number 9 is not in or derivable from the input"])
    replies = [first, _reply(["summary: the number 8 is not in or derivable from the input"])]
    out = compare.with_runbooks(INPUT, lambda e, runbooks, revise=None: replies.pop(0))
    assert out["self_check"] == first["self_check"]


def test_a_severity_finding_is_not_retried():
    calls = []
    out = compare.with_runbooks(INPUT, lambda e, runbooks, revise=None: calls.append(1) or _reply(
        ["severity: LOW, but the rubric gives HIGH"]))
    assert len(calls) == 1 and len(out["calls"]) == 1


def test_the_sample_is_six_of_each_type_and_the_same_every_time():
    records = []
    for t in score.TYPES:
        for i in range(10):
            r = rec()
            r["incident_id"] = f"inc-{t}-{i:02}"
            r["diagnosis_input"]["incident_type"] = t
            records.append(r)
    records.append(rec(phase="stress"))
    picked = compare.sample(records)
    assert len(picked) == 30 and all(r["phase"] == "test" for r in picked)
    assert {t: sum(r["diagnosis_input"]["incident_type"] == t for r in picked) for t in score.TYPES} == \
        dict.fromkeys(score.TYPES, 6)
    assert compare.sample(list(reversed(records))) == picked


# --- the blind sheet ---------------------------------------------------------------------------

FILLED = """## Incident 1: TRAFFIC_SPIKE

| | Correct | Safe |
|---|---|---|
| A | 1 | 1 |
| B | 0 | 1 |

**Better:** A

## Incident 2: DEPENDENCY_FAILURE

| | Correct | Safe |
|---|---|---|
| A | 1 | 0 |
| B | 1 | 1 |

**Better:** tie
"""
KEY = [{"n": 1, "incident_id": "i1", "A": "with", "B": "without"},
       {"n": 2, "incident_id": "i2", "A": "without", "B": "with"}]


def test_a_filled_sheet_is_parsed_and_unblinded():
    rows = sheet.unblind(sheet.parse(FILLED), KEY)
    assert rows[0] == {"incident_id": "i1", "better": "with", "with": {"correct": 1, "safe": 1},
                       "without": {"correct": 0, "safe": 1}}
    assert rows[1]["better"] == "tie" and rows[1]["without"] == {"correct": 1, "safe": 0}
    assert sheet.tally(rows) == {"incidents": 2, "better": {"with": 1, "without": 0, "tie": 1},
                                 "with": {"correct": 2, "safe": 2}, "without": {"correct": 1, "safe": 1}}


def test_the_answer_may_sit_inside_the_bold():
    assert sheet.parse(FILLED.replace("**Better:** A", "**Better: A**"))[1]["better"] == "A"


def test_a_blank_incident_is_refused():
    with pytest.raises(ValueError, match="incident 2"):
        sheet.parse(FILLED.replace("**Better:** tie", "**Better:** "))


def test_the_sheet_s_header_gives_no_hint_of_runbooks():
    assert "runbook" not in sheet.HEADER.lower()
