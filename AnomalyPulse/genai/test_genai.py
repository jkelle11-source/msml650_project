"""Tests for the diagnosis pipeline. No AWS or Bedrock: the client is faked.

Run from AnomalyPulse/:  python -m pytest -q genai
"""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from genai import contract, explain, handler, prompt, self_check

SAMPLES = {f.stem: json.loads(f.read_text()) for f in (Path(__file__).parent / "samples").glob("*.json")}
DEP = SAMPLES["dependency_failure"]

# A faithful diagnosis of the dependency-failure sample.
GOOD = {
    "severity": "HIGH",
    "summary": ("Order requests are failing while calls to Payment have slowed sharply. Order's 5xx share "
                "is 16.7% and Payment's p95 latency is 2.9 s against 110 ms before the incident. Order "
                "failures began about 40 seconds after Payment latency rose (observed sequence from "
                "correlated timestamps)."),
    "root_cause": "A slow or failing payment dependency is the most likely root cause.",
    "evidence": [
        {"statement": "Payment p95 latency rose 26x, from 110 ms to 2900 ms.",
         "metrics": ["payment-service.p95_latency_ms"]},
        {"statement": "Order's 5xx share rose from 0% to 16.7%.", "metrics": ["order-service.error_rate_5xx"]},
        {"statement": "Order recorded 57 failed calls to Payment in the latest minute.",
         "metrics": ["order-service.dependency_error_count"]},
    ],
    "recommended_actions": ["Inspect payment-service health and its recent errors.",
                            "Review the timeout and retry settings on Order's call to Payment.",
                            "Check whether Payment changed recently."],
}


class FakeBedrock:
    def __init__(self, answer=GOOD, stop_reason="end_turn", text=True):
        self.answer, self.stop_reason, self.text, self.calls = answer, stop_reason, text, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = [SimpleNamespace(type="thinking", thinking="")]
        if self.text:
            content.append(SimpleNamespace(type="text", text=json.dumps(self.answer)))
        return SimpleNamespace(stop_reason=self.stop_reason, content=content,
                               stop_details=SimpleNamespace(category="cyber"),
                               usage=SimpleNamespace(input_tokens=1800, output_tokens=420))


def _with(**changes):
    out = copy.deepcopy(GOOD)
    out.update(changes)
    return out


# --- the input contract ------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_sample_meets_the_contract(name):
    assert contract.validate_input(SAMPLES[name])


def test_one_sample_per_incident_type():
    assert sorted(s["incident_type"] for s in SAMPLES.values()) == sorted(contract.INCIDENT_TYPES)


@pytest.mark.parametrize("change, problem", [
    (lambda d: d.update(incident_type="NORMAL"), "incident_type must be one of"),
    (lambda d: d.update(confidence=1.5), "confidence must be from 0 to 1"),
    (lambda d: d.pop("metrics"), "missing metrics"),
    (lambda d: d.update(extra=1), "not in the contract extra"),
    (lambda d: d["metrics"][0].update(metric="cpu_percent"), "not in the contract's metrics"),
    (lambda d: d["metrics"][0].update(unit="s"), "is in ms"),
    (lambda d: d["metrics"].append(dict(d["metrics"][0])), "twice"),
    (lambda d: d["affected_services"].append("search-service"), "affected_services must be"),
    (lambda d: d.update(started_at="2026-10-30 14:32"), "UTC time"),
    (lambda d: d["observed_sequence"].reverse(), "time order"),
    (lambda d: d["observed_sequence"][0].update(metric="request_rate"), "not in metrics"),
])
def test_a_bad_input_is_refused(change, problem):
    evidence = copy.deepcopy(DEP)
    change(evidence)
    with pytest.raises(contract.BadInput, match=problem):
        contract.validate_input(evidence)


@pytest.mark.parametrize("key", ["ground_truth", "fault_injection_params", "db_throttled", "experiment_id"])
def test_ground_truth_can_never_reach_bedrock(key):
    evidence = {**copy.deepcopy(DEP), key: {"anything": 1}}
    with pytest.raises(contract.BadInput, match="ground truth must never reach Bedrock"):
        contract.validate_input(evidence)


def test_the_schema_only_lets_evidence_cite_input_metrics():
    schema = contract.output_schema(DEP)
    refs = schema["properties"]["evidence"]["items"]["properties"]["metrics"]["items"]["enum"]
    assert refs == [f"{m['service']}.{m['metric']}" for m in DEP["metrics"]]
    assert schema["properties"]["severity"]["enum"] == ["LOW", "MEDIUM", "HIGH"]
    assert schema["additionalProperties"] is False


# --- the prompt and the request ----------------------------------------------------------------

def test_the_prompt_carries_the_fidelity_and_wording_rules():
    p = prompt.SYSTEM_PROMPT
    assert "observed sequence" in p and "Causal words belong in root_cause only" in p
    assert "Mention only services, metrics and values that appear in it" in p
    assert "evidence: 3 to 6 bullets" in p and "don't mention the thresholds" in p
    assert all(name in p for name in contract.METRICS)


def test_the_request_holds_bedrock_to_the_schema():
    req = explain.request(DEP)
    assert req["model"] == "global.anthropic.claude-sonnet-4-6"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": contract.output_schema(DEP)}
    assert req["output_config"]["effort"] == "medium"
    # 4.6 models think only when asked; sampling parameters are never sent.
    assert req["thinking"] == {"type": "adaptive"}
    assert "temperature" not in req and "top_p" not in req
    assert explain.request(DEP, "us.anthropic.claude-sonnet-4-6")["model"] == "us.anthropic.claude-sonnet-4-6"
    assert json.loads(req["messages"][0]["content"].split("\n\n", 1)[1]) == DEP


def test_equal_inputs_give_equal_prompts():
    shuffled = dict(reversed(list(copy.deepcopy(DEP).items())))
    assert prompt.user_message(shuffled) == prompt.user_message(DEP)


# --- diagnose() --------------------------------------------------------------------------------

def test_a_faithful_diagnosis_comes_back_in_the_dashboard_shape():
    result = explain.diagnose(DEP, bedrock=FakeBedrock())
    assert set(result["diagnosis"]) == {"severity", "summary", "root_cause", "evidence",
                                        "recommended_actions"}
    assert result["diagnosis"]["evidence"][0] == GOOD["evidence"][0]["statement"]
    assert result["evidence_metrics"] == [e["metrics"] for e in GOOD["evidence"]]
    assert result["self_check"] == {"passed": True, "findings": []}
    assert result["usage"] == {"input_tokens": 1800, "output_tokens": 420}


def test_a_refusal_or_a_cut_off_answer_raises():
    with pytest.raises(explain.DiagnosisFailed, match="declined"):
        explain.diagnose(DEP, bedrock=FakeBedrock(stop_reason="refusal"))
    with pytest.raises(explain.DiagnosisFailed, match="max_tokens"):
        explain.diagnose(DEP, bedrock=FakeBedrock(stop_reason="max_tokens"))
    with pytest.raises(explain.DiagnosisFailed, match="no text"):
        explain.diagnose(DEP, bedrock=FakeBedrock(text=False))


def test_a_bad_input_never_reaches_bedrock():
    fake = FakeBedrock()
    with pytest.raises(contract.BadInput):
        explain.diagnose({**DEP, "incident_type": "NORMAL"}, bedrock=fake)
    assert fake.calls == []


def test_the_lambda_handler_is_diagnose(monkeypatch):
    monkeypatch.setattr(explain, "client", lambda: FakeBedrock())
    assert handler.handler(DEP, None)["self_check"]["passed"] is True


def test_a_flagged_diagnosis_is_returned_not_raised():
    bad = _with(summary="Payment latency reached 9.4 s.")
    result = explain.diagnose(DEP, bedrock=FakeBedrock(bad))
    assert result["self_check"]["passed"] is False
    assert result["diagnosis"]["summary"] == "Payment latency reached 9.4 s."


# --- the self-check ----------------------------------------------------------------------------

def test_the_faithful_diagnosis_has_no_findings():
    assert self_check.check(DEP, GOOD) == []


def test_an_invented_number_is_flagged():
    findings = self_check.check(DEP, _with(summary="Payment p95 latency rose to 4.8 s."))
    assert findings == ["summary: the number 4.8 is not in or derivable from the input"]


def test_derived_numbers_are_accepted():
    # ratio, percent of a share, ms as seconds, confidence, the sequence gap
    text = ("Payment p95 rose about 26x to 2.9 s; Order 5xx is 16.7%; confidence 93%. "
            "Order failures began 40 seconds later (observed sequence).")
    assert self_check.check(DEP, _with(summary=text)) == []


def test_metric_names_and_times_are_not_read_as_values():
    text = "Payment p95 and p99 latency and 5xx share at 2026-10-30T14:32:00Z were checked."
    assert not any("number" in f for f in self_check.check(DEP, _with(summary=text)))


def test_a_service_absent_from_the_input_is_flagged():
    findings = self_check.check(DEP, _with(summary="Cart-service latency also rose."))
    assert "summary: names cart-service, which the input doesn't" in findings


def test_a_metric_family_absent_from_the_input_is_flagged():
    findings = self_check.check(DEP, _with(root_cause="DynamoDB throttling is the most likely root cause."))
    assert any("'DynamoDB'" in f for f in findings) and any("'throttl'" in f for f in findings)


def test_retries_need_a_db_latency_metric():
    claim = _with(evidence=[{"statement": "Order retried each failed call.",
                             "metrics": ["order-service.dependency_error_count"]}])
    assert any("'retried'" in f for f in self_check.check(DEP, claim))
    throttling = SAMPLES["database_throttling"]   # has db_latency_ms_p95, which includes SDK retries
    claim = {**GOOD, "summary": "DynamoDB call latency, SDK retries included, rose 395x.",
             "evidence": [{"statement": "Product read throttle events reached 1400 per minute.",
                           "metrics": ["product-service.read_throttle_events"]}]}
    assert not any("retr" in f for f in self_check.check(throttling, claim))


def test_a_deployment_is_never_supported():
    claim = _with(root_cause="A new deployment of payment-service is the most likely root cause.")
    assert any("'deploy'" in f for f in self_check.check(DEP, claim))


def test_actions_may_name_components_and_services_without_metrics():
    actions = ["Inspect DynamoDB capacity on the Orders table.", "Check for throttling.",
               "Check the cart-service logs too."]
    assert self_check.check(DEP, _with(recommended_actions=actions)) == []


def test_input_clock_times_are_accepted_and_others_flagged():
    ok = "Payment latency rose at 14:32:00 and Order errors at 14:32:40 (observed sequence)."
    assert self_check.check(DEP, _with(summary=ok)) == []
    findings = self_check.check(DEP, _with(summary="Payment latency rose at 14:29."))
    assert findings == ["summary: the time 14:29 is not in the input"]


def test_one_in_n_restates_a_share():
    claim = _with(summary="Roughly 1 in 6 order requests fails with a 5xx (16.7%).")
    assert self_check.check(DEP, claim) == []


def test_causal_wording_is_flagged_outside_root_cause():
    findings = self_check.check(DEP, _with(summary="Slow Payment calls caused Order to fail."))
    assert any("causal wording 'caused'" in f for f in findings)
    assert not any("causal" in f for f in self_check.check(
        DEP, _with(root_cause="A failing payment dependency is the most likely cause.")))


def test_ruling_out_is_causal_wording_whatever_follows():
    # Sonnet 4.6 wrote this on the Lambda-degradation sample (2026-11-01).
    claim = _with(evidence=[{"statement": "Request rate is flat at 2.9 rps, ruling out a traffic surge "
                                          "as an explanation.", "metrics": ["order-service.p95_latency_ms"]}])
    assert any("causal wording 'ruling out'" in f for f in self_check.check(DEP, claim))


def test_ordering_must_be_marked_as_an_observed_sequence():
    findings = self_check.check(DEP, _with(summary="Order failures began after Payment slowed."))
    assert any("without saying 'observed sequence'" in f for f in findings)


def test_ordering_needs_a_sequence_in_the_input():
    evidence = {k: v for k, v in DEP.items() if k != "observed_sequence"}
    text = "Order failures began after Payment slowed (observed sequence)."
    findings = self_check.check(evidence, _with(summary=text))
    assert any("no observed_sequence" in f for f in findings)


def test_shape_problems_are_reported():
    assert contract.check_output_shape(_with(evidence=[], recommended_actions=[])) == [
        "evidence has 0 bullets; 1 to 6 expected", "recommended_actions has 0 items; 1 to 6 expected"]
