"""Tests for the diagnosis pipeline. No AWS or Bedrock: the client is faked.

Run from AnomalyPulse/:  python -m pytest -q genai
"""
import copy
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from genai import affected, contract, explain, handler, prompt, self_check

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
    # pairs the feature matrix doesn't have: Payment has no table, only Order calls a dependency
    (lambda d: d["metrics"][0].update(metric="db_latency_ms_mean"), "payment-service has no db_latency_ms_mean"),
    (lambda d: d["metrics"][2].update(service="product-service"),
     "product-service has no dependency_error_count"),
    # no free text reaches Bedrock
    (lambda d: d["observed_sequence"][0].update(observation="error_type was DynamoDBThrottle"),
     "observation must be one of"),
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


def test_each_service_has_the_metrics_the_feature_matrix_makes():
    # Docs/FEATURE_MATRIX.md is generated from features.build_matrix.columns(), and the features
    # tests fail when it isn't current; reading it keeps these tests free of the simulator's deps.
    doc = (Path(__file__).parent.parent.parent / "Docs" / "FEATURE_MATRIX.md").read_text()
    columns = re.findall(r"^\| `([a-z0-9_]+)` \|", doc, re.MULTILINE)
    temporal = ("_change", "_roll_mean_3", "_roll_std_3")
    for service, metrics in contract.SERVICE_METRICS.items():
        prefix = service.split("-")[0] + "_"
        made = {c[len(prefix):] for c in columns if c.startswith(prefix) and not c.endswith(temporal)}
        assert metrics == made, service


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
    assert "its numbers come from those metrics only" in p
    assert all(name in p for name in contract.METRICS)


def test_the_prompt_states_the_limits_and_rubric_the_code_checks():
    p = " ".join(prompt.SYSTEM_PROMPT.split())
    assert "recommended_actions: 2 to 5 concrete steps" in p
    assert "HIGH if any 5xx share is at least 0.05, or any p95 latency is at least 10x its baseline" in p
    assert ("MEDIUM if any 5xx share is at least 0.01, or any p95 latency is at least 2x its baseline, "
            "or any throttle or dependency-error count is above zero") in p


def test_the_request_holds_bedrock_to_the_schema():
    req = explain.request(DEP)
    assert req["model"] == "global.anthropic.claude-sonnet-4-6"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": contract.output_schema(DEP)}
    assert req["output_config"]["effort"] == "medium"
    # 4.6 models think only when asked; sampling parameters are never sent.
    assert req["thinking"] == {"type": "adaptive"}
    assert "temperature" not in req and "top_p" not in req
    assert explain.request(DEP, "us.anthropic.claude-sonnet-4-6")["model"] == "us.anthropic.claude-sonnet-4-6"
    sent = json.loads(req["messages"][0]["content"].split("\n\n", 1)[1])
    assert sent == {k: v for k, v in DEP.items() if k not in ("incident_id", "contract_version")}
    assert DEP["incident_id"] not in req["messages"][0]["content"]


def test_the_client_gives_up_inside_the_lambdas_timeout(monkeypatch):
    import anthropic
    made = {}
    monkeypatch.setattr(anthropic, "AnthropicBedrock", lambda **kw: made.update(kw) or "client")
    monkeypatch.setattr(explain, "_client", None)
    assert explain.client() == "client"
    assert made == {"aws_region": "us-east-2", "timeout": explain.TIMEOUT_S, "max_retries": explain.MAX_RETRIES}
    template = (Path(__file__).parent.parent / "infra" / "genai" / "template.yaml").read_text()
    function = template.split("FunctionName: anomalypulse-explain", 1)[1]
    lambda_timeout = int(re.search(r"\bTimeout: (\d+)", function).group(1))
    # every attempt, plus the SDK's backoff (under a second for one retry) and 15 s to spare
    assert explain.TIMEOUT_S * (explain.MAX_RETRIES + 1) + 15 <= lambda_timeout


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


def test_the_diagnosis_reports_the_rubrics_severity_and_the_check_keeps_bedrocks():
    # The first live spike: Bedrock said MEDIUM where the rubric gives LOW. Here it says MEDIUM where
    # the rubric gives HIGH (Order's 5xx share is 16.7%).
    result = explain.diagnose(DEP, bedrock=FakeBedrock(answer=_with(severity="MEDIUM")))
    assert contract.severity(DEP) == "HIGH"
    assert result["diagnosis"]["severity"] == "HIGH"
    assert result["self_check"] == {"passed": False, "findings": ["severity: MEDIUM, but the rubric gives HIGH"]}


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


def test_a_number_from_another_metric_is_flagged_in_a_bullet():
    # 2950 ms is Order's p95; this bullet cites only Payment's.
    claim = _with(evidence=[{"statement": "Payment p95 latency is 2950 ms.",
                             "metrics": ["payment-service.p95_latency_ms"]}])
    assert self_check.check(DEP, claim) == [
        "evidence[0]: the number 2950 is not from the metrics it cites"]
    # Opus 4.6 quoted the rubric's threshold on the throttling sample (2026-11-01).
    claim = {**_with(severity="HIGH"), "evidence": [
        {"statement": "5xx error rate is 0.4%, below the 5% threshold but non-zero.",
         "metrics": ["product-service.error_rate_5xx"]}]}
    assert "evidence[0]: the number 5 is not from the metrics it cites" in self_check.check(
        SAMPLES["database_throttling"], claim)


def test_a_bullet_may_compare_the_metrics_it_cites():
    claim = _with(evidence=[{"statement": "Order's p95 is 2950 ms, 50 ms above Payment's 2900 ms.",
                             "metrics": ["order-service.p95_latency_ms", "payment-service.p95_latency_ms"]}])
    assert self_check.check(DEP, claim) == []


def test_status_codes_are_not_values():
    claim = _with(evidence=[{"statement": "Order's 5xx share rose from 0% to 16.7%, all 500 responses.",
                             "metrics": ["order-service.error_rate_5xx"]}])
    assert self_check.check(DEP, claim) == []


@pytest.mark.parametrize("written, ok", [
    ("2.9 s", True),        # 2900 or 2950 ms, to one decimal
    ("2,900 ms", True),
    ("2,950 ms", False),    # Order's p95, written exactly: not Payment's 2900
    ("26x", True),          # 26.36
    ("26.4x", True),
    ("27x", False),
    ("about 3,000 ms", True),
])
def test_a_number_is_held_to_the_precision_it_is_written_in(written, ok):
    claim = _with(evidence=[{"statement": f"Payment p95 latency is {written}.",
                             "metrics": ["payment-service.p95_latency_ms"]}])
    assert (self_check.check(DEP, claim) == []) is ok


LAM = SAMPLES["lambda_degradation"]   # p95 375 / 24 = 15.6x, p50 360 / 17 = 21.2x; no observed_sequence


def _lambda_summary(text, bullet=("Mean handler duration rose from 2 ms to 352 ms.", "lambda_duration_ms_mean")):
    """Findings on the Lambda-degradation sample for a summary and one evidence bullet (its statement
    and the order-service metric it cites), the rest of the diagnosis faithful."""
    return self_check.check(LAM, {
        "severity": contract.severity(LAM), "summary": text,
        "root_cause": "A slowdown inside the order-service handler is the most likely root cause.",
        "evidence": [{"statement": bullet[0], "metrics": [f"order-service.{bullet[1]}"]}],
        "recommended_actions": ["Inspect the handler's logs.", "Profile the handler."]})


@pytest.mark.parametrize("summary, flagged", [
    ("End-to-end p95 latency rose 15.6x.", []),
    ("End-to-end p95 latency rose 15x.", ["15"]),                 # 15.6 rounds to 16
    ("End-to-end p95 latency rose roughly 15x.", []),             # Sonnet 4.6, 2026-11-03: cut off, not rounded
    ("End-to-end p95 latency rose more than 15x.", []),           # Sonnet 4.6, 2026-11-05: 15.6 is above 15
    ("End-to-end p95 latency rose nearly 15x.", ["15"]),          # "nearly" is below 15, and 15.6 is above
    ("End-to-end p50 latency rose more than 21.2x.", ["21.2"]),   # 21.18 is below 21.2, though "21.2x" passes
    ("End-to-end latency rose roughly 15x to 22x.", []),          # the range's second number takes "roughly"
    ("End-to-end latency rose 15x to 22x.", ["15", "22"]),        # 22 doesn't round from 21.2 either
])
def test_a_hedge_or_bound_loosens_a_number_on_its_side(summary, flagged):
    findings = _lambda_summary(summary)
    assert [f.split("the number ")[1].split(" ")[0] for f in findings if "the number" in f] == flagged


def test_a_hedged_number_in_a_bullet_still_comes_from_the_metrics_it_cites():
    bullet = "p95 latency rose roughly 15x."
    assert _lambda_summary("Handler duration rose 176x.", (bullet, "p95_latency_ms")) == []
    assert _lambda_summary("Handler duration rose 176x.", (bullet, "p50_latency_ms")) == [
        "evidence[0]: the number 15 is not from the metrics it cites"]   # p50 rose 21.2x


def test_a_hedge_does_not_excuse_a_wrong_number():
    # Sonnet 4.6 with the cascade runbook (2026-11-05) wrote 95 seconds for the 35 s from 17:04:05 to
    # 17:04:40. "About 95" is 94-96, and no gap in the input is.
    cascade = SAMPLES["cascading_failure"]
    for written, flagged in (("95", True), ("about 95", True), ("35", False), ("about 35", False)):
        text = f"Order 5xx rose {written} seconds after Payment latency (observed sequence)."
        number = written.split()[-1]
        assert any(f"the number {number} " in f for f in self_check.check(cascade, _with(summary=text))) is flagged


def test_followed_at_the_same_scale_orders_nothing():
    def ordering(text):
        return [f for f in _lambda_summary(text) if "observed sequence" in f or "ordering" in f]
    # Sonnet 4.6, runbooks off (2026-11-03): the two moving together.
    assert ordering("Mean handler duration rose from 2 ms to 352 ms (176x), and end-to-end latency at both "
                    "p50 and p95 followed at roughly the same scale.") == []
    assert ordering("End-to-end latency followed in step.") == []
    # Sonnet 4.6, runbooks on (2026-11-05): "has followed" reads as "rose afterwards", and this input
    # has no observed_sequence to order anything.
    assert ordering("End-to-end latency has followed, with p50 rising from 17 ms to 360 ms.") != []


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


@pytest.mark.parametrize("text", [
    "Read capacity must rise in order to stop the throttling.",          # "in order to" is no service
    "Over the 4-minute duration, read throttle events reached 1,400 per minute.",   # the input's duration
    "The first sign is p95 latency at 1,586 ms against 6 ms.",           # "first" orders nothing here
    "Request rate is flat at 15.1 rps against 15.0 before.",             # the prompt's own example
])
def test_faithful_wording_is_not_flagged(text):
    throttling = SAMPLES["database_throttling"]
    claim = {**GOOD, "summary": text, "root_cause": "DynamoDB read throttling is the most likely root cause.",
             "evidence": [{"statement": "Read throttle events reached 1,400 per minute.",
                           "metrics": ["product-service.read_throttle_events"]}]}
    assert self_check.check(throttling, claim) == []


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


def _cascade_links(evidence, root_cause):
    return [f for f in self_check.check(evidence, _with(root_cause=root_cause)) if "cascade" in f]


def test_a_cascades_root_cause_links_its_faults_causally_never():
    cascade = SAMPLES["cascading_failure"]
    # Sonnet 4.6 wrote this on the cascade sample (2026-11-01). The simulator injects the payment
    # fault on its own (Docs/CASCADE.md), and no metric says Payment was under load.
    wrote = ("A sharp traffic surge across product-service and order-service is the most likely root cause "
             "of this cascading failure: the increased order volume put payment-service under load, degrading "
             "its latency nearly 9.3x, which in turn produced a flood of dependency errors at order-service "
             "and pushed its 5xx rate to 25.0%.")
    assert [f.split("(")[-1] for f in _cascade_links(cascade, wrote)] == [
        "'under load')", "'in turn')", "'produced')", "'pushed')"]
    concurrent = ("A traffic surge and a slow payment dependency, active at the same time, are the most "
                  "likely root cause.")
    assert _cascade_links(cascade, concurrent) == []
    # Sonnet 4.6's answer to the revised prompt (2026-11-02): "root causes" is the noun.
    revised = ("A traffic surge on product-service and order-service, active at the same time as a severe "
               "payment-service latency degradation, are the most likely concurrent root causes.")
    assert _cascade_links(cascade, revised) == []
    assert _cascade_links(cascade, "The surge causes Payment's slowdown.") != []
    # Other incident types may connect symptoms along the call path in root_cause.
    along_the_path = ("A slow payment dependency is the most likely root cause; Order calls Payment on "
                      "every POST /orders, so it produced Order's 5xx.")
    assert _cascade_links(DEP, along_the_path) == []


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


def _metric(service, metric, baseline, current):
    return {"service": service, "metric": metric, "baseline": baseline, "current": current,
            "unit": contract.METRICS[metric][0]}


@pytest.mark.parametrize("metrics, expected", [
    # 5xx share: 0.05 and over is HIGH, 0.01 and over MEDIUM
    ([_metric("order-service", "error_rate_5xx", 0.0, 0.05)], "HIGH"),
    ([_metric("order-service", "error_rate_5xx", 0.0, 0.049)], "MEDIUM"),
    ([_metric("order-service", "error_rate_5xx", 0.0, 0.01)], "MEDIUM"),
    ([_metric("order-service", "error_rate_5xx", 0.0, 0.009)], "LOW"),
    # p95 against its baseline: 10x is HIGH, 2x MEDIUM; no baseline, no ratio
    ([_metric("order-service", "p95_latency_ms", 100, 1000)], "HIGH"),
    ([_metric("order-service", "p95_latency_ms", 100, 999)], "MEDIUM"),
    ([_metric("order-service", "p95_latency_ms", 100, 200)], "MEDIUM"),
    ([_metric("order-service", "p95_latency_ms", 100, 199)], "LOW"),
    ([_metric("order-service", "p95_latency_ms", None, 5000)], "LOW"),
    # any throttle or dependency-error count above zero is MEDIUM
    ([_metric("order-service", "dependency_error_count", 0, 1)], "MEDIUM"),
    ([_metric("order-service", "lambda_throttle_count", 0, 3)], "MEDIUM"),
    ([_metric("order-service", "dependency_error_count", 0, 0)], "LOW"),
    # only the affected services count: Payment isn't affected here
    ([_metric("order-service", "p95_latency_ms", 100, 110),
      _metric("payment-service", "error_rate_5xx", 0.0, 0.5)], "LOW"),
])
def test_the_severity_rubric(metrics, expected):
    evidence = {**DEP, "affected_services": ["order-service"], "metrics": metrics}
    evidence.pop("observed_sequence")
    assert contract.severity(evidence) == expected


def test_the_samples_cover_each_severity_the_saved_diagnoses_gave():
    assert {name: contract.severity(s) for name, s in SAMPLES.items()} == {
        "cascading_failure": "HIGH", "database_throttling": "HIGH", "dependency_failure": "HIGH",
        "lambda_degradation": "HIGH", "traffic_spike": "LOW"}


def test_a_threshold_is_never_quoted():
    # Opus 4.6 wrote this on the Lambda-degradation sample (2026-11-01).
    claim = _with(evidence=[{"statement": "Payment p95 latency rose 26x, crossing the 10x threshold.",
                             "metrics": ["payment-service.p95_latency_ms"]}])
    assert "evidence[0]: mentions a threshold; the severity rubric's are never quoted" in self_check.check(
        DEP, claim)


def test_an_action_may_speak_of_capacity_thresholds():
    # Sonnet 4.6 on the traffic-spike sample (2026-11-02): DynamoDB's limits, not the rubric's.
    action = ("Check DynamoDB consumed capacity on all three tables against provisioned limits to ensure "
              "the tables are not approaching throttling thresholds.")
    assert self_check.check(DEP, _with(recommended_actions=[action, "Inspect Payment."])) == []


def test_a_severity_off_the_rubric_is_flagged():
    assert self_check.check(DEP, _with(severity="MEDIUM")) == ["severity: MEDIUM, but the rubric gives HIGH"]


@pytest.mark.parametrize("bullets, actions, problems", [
    (3, 2, []),
    (6, 5, []),
    (2, 2, ["evidence has 2 bullets; 3 to 6 expected"]),
    (7, 2, ["evidence has 7 bullets; 3 to 6 expected"]),
    (3, 1, ["recommended_actions has 1 items; 2 to 5 expected"]),
    (3, 6, ["recommended_actions has 6 items; 2 to 5 expected"]),
])
def test_shape_limits_match_the_prompt(bullets, actions, problems):
    raw = _with(evidence=[GOOD["evidence"][0]] * bullets, recommended_actions=["Inspect Payment."] * actions)
    assert contract.check_output_shape(raw) == problems


# --- affected services ---------------------------------------------------------------------------

# A quiet minute: every service's request rate and p95, no errors or throttles.
QUIET = {"product_request_rate": 15.0, "product_p95_latency_ms": 6.0, "product_error_rate_5xx": 0.0,
         "cart_request_rate": 2.0, "cart_p95_latency_ms": 20.0, "cart_error_rate_5xx": 0.0,
         "order_request_rate": 2.9, "order_p95_latency_ms": 130.0, "order_error_rate_5xx": 0.0,
         "order_dependency_error_count": 0, "payment_request_rate": 1.0, "payment_p95_latency_ms": 110.0,
         "payment_error_rate_5xx": 0.0, "product_read_throttle_events": 0}


def _minute(**changes):
    return {**QUIET, **changes}


# Cart's 5xx at 0.02 crosses on its own, so the cases that use it never reach the fallback: a
# service on either side of a line shows whether that line is where the rule draws it.
@pytest.mark.parametrize("changes, expected", [
    # 5xx share: 0.01 is the line
    ({"order_error_rate_5xx": 0.01}, ["order-service"]),
    ({"order_error_rate_5xx": 0.009, "cart_request_rate": 2.1}, ["cart-service"]),   # none crosses: cart rose most
    # p95: 2x its baseline is the line (130 ms -> 260 ms)
    ({"order_p95_latency_ms": 260.0, "cart_error_rate_5xx": 0.02}, ["cart-service", "order-service"]),
    ({"order_p95_latency_ms": 259.0, "cart_error_rate_5xx": 0.02}, ["cart-service"]),
    ({"order_p95_latency_ms": 259.0, "product_request_rate": 15.1}, ["order-service"]),   # fallback: 1.99x > 1.01x
    # request rate: 1.75x (15 -> 26.25 rps)
    ({"product_request_rate": 26.25, "cart_error_rate_5xx": 0.02}, ["product-service", "cart-service"]),
    ({"product_request_rate": 26.1, "cart_error_rate_5xx": 0.02}, ["cart-service"]),
    ({"product_request_rate": 45.0, "cart_request_rate": 6.0, "order_request_rate": 8.7, "payment_request_rate": 3.0},
     ["product-service", "cart-service", "order-service", "payment-service"]),
    # any throttle or dependency-error count above zero
    ({"product_read_throttle_events": 1}, ["product-service"]),
    ({"order_dependency_error_count": 57, "payment_p95_latency_ms": 2900.0}, ["order-service", "payment-service"]),
    ({"payment_lambda_throttle_count": 2}, ["payment-service"]),
])
def test_affected_services(changes, expected):
    assert affected.affected_services(QUIET, _minute(**changes)) == expected


def test_a_quiet_minute_still_names_one_service():
    # Nothing moved; ties go to the first service.
    assert affected.affected_services(QUIET, QUIET) == ["product-service"]


def test_columns_a_service_has_no_are_ignored():
    # Payment has no table: a stray payment_read_throttle_events can't mark it.
    got = affected.affected_services(QUIET, _minute(payment_read_throttle_events=5, order_p95_latency_ms=400.0))
    assert got == ["order-service"]


def test_empty_cells_are_not_signals():
    # A service with no requests in a window has empty latency and error columns.
    current = _minute(cart_request_rate=0.0, cart_p95_latency_ms=None, cart_error_rate_5xx=None,
                      order_error_rate_5xx=0.2)
    assert affected.affected_services({**QUIET, "cart_p95_latency_ms": None}, current) == ["order-service"]


def test_the_baseline_is_the_mean_of_the_windows_before():
    windows = [{"window_start": "t0", "order_p95_latency_ms": 120.0, "order_dependency_latency_ms_p95": None},
               {"window_start": "t1", "order_p95_latency_ms": 140.0, "order_dependency_latency_ms_p95": None}]
    assert affected.baseline(windows) == {"order_p95_latency_ms": 130.0, "order_dependency_latency_ms_p95": None}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_each_samples_affected_services_follow_the_rule(name):
    sample = SAMPLES[name]
    base = {f"{m['service'].split('-')[0]}_{m['metric']}": m["baseline"] for m in sample["metrics"]}
    current = {f"{m['service'].split('-')[0]}_{m['metric']}": m["current"] for m in sample["metrics"]}
    assert affected.affected_services(base, current) == sample["affected_services"]
