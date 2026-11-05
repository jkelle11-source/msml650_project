"""Tests for M6-2's runbooks. No AWS or Bedrock: the client is faked.

Run from AnomalyPulse/:  python -m pytest -q genai
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from genai import contract, explain, prompt, runbooks, self_check
from genai.test_genai import DEP, SAMPLES, FakeBedrock

HERE = Path(__file__).parent
# explain.request(DEP), saved before M6-2 changed any code (2026-11-03).
OFF_FIXTURE = (HERE / "test_data" / "request_off_dependency_failure.json").read_text().strip()
THR = SAMPLES["database_throttling"]

# Every value the runbooks write, from the stack facts verified in Handoffs/M6-1_Handoff.md section 4.
# The self-check allows each of them in recommended_actions, so a new number is a deliberate change.
STACK_FACTS = {1000.0,   # concurrent Lambda executions, account-wide
               200.0, 5.0,   # Products table: provisioned read and write capacity units per second
               7.0,      # SDK attempts on a DynamoDB call
               1769.0, 10.0, 3.13,   # function memory (MB), timeout (s), Python version
               2.0, 6.0}  # Order -> Payment invoke: connect and read timeouts (s)

# Tier-2 and simulator vocabulary: none of it may reach a prompt.
FORBIDDEN_WORDS = {"inject", "injected", "injection", "injects", "injector", "simulator", "simulated",
                   "band", "bands", "experiment", "experiments", "fault-config"} | contract.FORBIDDEN_KEYS


def _off_request(**kwargs):
    return json.dumps(explain.request(DEP, **kwargs), sort_keys=True)


# --- the runbooks ----------------------------------------------------------------------------

@pytest.mark.parametrize("incident_type", contract.INCIDENT_TYPES)
def test_every_incident_type_has_a_runbook(incident_type):
    name, text = runbooks.runbook_for(incident_type)
    assert name == incident_type.lower()
    assert len(text.split()) >= 100


def test_the_name_is_the_lowercase_type():
    assert runbooks.runbook_for("DATABASE_THROTTLING")[0] == "database_throttling"


def test_no_runbook_for_normal_or_an_unknown_type():
    for bad in ("NORMAL", "database_throttling", ""):
        with pytest.raises(ValueError):
            runbooks.runbook_for(bad)


def test_a_missing_or_empty_runbook_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(runbooks, "RUNBOOK_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        runbooks.runbook_for("TRAFFIC_SPIKE")
    (tmp_path / "traffic_spike.md").write_text("  \n")
    with pytest.raises(ValueError, match="empty"):
        runbooks.runbook_for("TRAFFIC_SPIKE")


def test_one_runbook_file_per_type_and_no_others():
    assert sorted(p.stem for p in runbooks.RUNBOOK_DIR.glob("*.md")) == sorted(
        t.lower() for t in contract.INCIDENT_TYPES)


@pytest.mark.parametrize("incident_type", contract.INCIDENT_TYPES)
def test_no_simulator_or_tier2_words(incident_type):
    _, text = runbooks.runbook_for(incident_type)
    words = set(re.findall(r"[a-z][a-z0-9_-]*", text.lower()))
    assert not words & FORBIDDEN_WORDS


@pytest.mark.parametrize("incident_type", contract.INCIDENT_TYPES)
def test_runbooks_hold_no_causal_claims(incident_type):
    # Checks and actions, not causes: a "the cause is usually X" line would end up in root_cause.
    _, text = runbooks.runbook_for(incident_type)
    assert not re.search(r"\bcaus(e|es|ed|ing)\b|\bbecause\b|\bdue to\b|\bled to\b", text, re.IGNORECASE)


def test_every_runbook_number_is_a_verified_stack_fact():
    numbers = set().union(*(self_check._runbook_numbers(runbooks.runbook_for(t)[1])
                            for t in contract.INCIDENT_TYPES))
    assert numbers == STACK_FACTS


def test_list_markers_are_not_runbook_numbers():
    assert self_check._runbook_numbers("1. Check it.\n2. Then 7 attempts.\n  3. Done") == {7.0}


# --- the switch ------------------------------------------------------------------------------

def test_off_is_byte_for_byte_the_m6_1_request(monkeypatch):
    monkeypatch.setattr(explain, "RUNBOOKS", False)
    assert _off_request() == OFF_FIXTURE
    assert _off_request(runbooks=False) == OFF_FIXTURE


def test_the_fixture_catches_a_one_character_change(monkeypatch):
    monkeypatch.setattr(explain, "RUNBOOKS", False)
    monkeypatch.setattr(prompt, "SYSTEM_PROMPT", prompt.SYSTEM_PROMPT + " ")
    assert _off_request() != OFF_FIXTURE


def test_off_is_the_default():
    assert "ANOMALYPULSE_RUNBOOKS" not in __import__("os").environ or explain.RUNBOOKS is False
    template = (HERE.parent / "infra" / "genai" / "template.yaml").read_text()
    assert re.search(r"ANOMALYPULSE_RUNBOOKS: 'off'", template)


def test_the_switch_turns_runbooks_on(monkeypatch):
    monkeypatch.setattr(explain, "RUNBOOKS", True)
    assert _off_request() != OFF_FIXTURE
    assert _off_request(runbooks=False) == OFF_FIXTURE   # the parameter overrides the switch


def test_on_adds_only_the_user_turn_section():
    off, on = explain.request(DEP, runbooks=False), explain.request(DEP, runbooks=True)
    assert on["system"] == off["system"] == prompt.SYSTEM_PROMPT
    assert {k: v for k, v in on.items() if k != "messages"} == {k: v for k, v in off.items() if k != "messages"}
    off_text, on_text = off["messages"][0]["content"], on["messages"][0]["content"]
    _, runbook = runbooks.runbook_for("DEPENDENCY_FAILURE")
    assert on_text == f"{off_text}\n\n{prompt.RUNBOOK_INTRO}\n\n{runbook}"
    assert "Use it only for recommended_actions" in on_text


def test_each_incident_gets_its_own_types_runbook():
    for name, sample in SAMPLES.items():
        text = explain.request(sample, runbooks=True)["messages"][0]["content"]
        assert text.endswith(runbooks.runbook_for(sample["incident_type"])[1])


# --- the reply -------------------------------------------------------------------------------

def test_the_reply_names_the_runbook_when_on_and_none_when_off():
    fake = FakeBedrock()
    on = explain.diagnose(DEP, bedrock=fake, runbooks=True)
    assert on["runbook"] == "dependency_failure"
    assert runbooks.runbook_for("DEPENDENCY_FAILURE")[1] in fake.calls[0]["messages"][0]["content"]
    off = explain.diagnose(DEP, bedrock=FakeBedrock(), runbooks=False)
    assert off["runbook"] is None
    assert "runbook" not in on["diagnosis"]        # beside diagnosis: the dashboard contract is unchanged
    assert on["self_check"]["passed"] and off["self_check"]["passed"]


# --- the self-check --------------------------------------------------------------------------

def _diagnosis(evidence, *, summary="See the evidence.", actions=("Check the logs.", "Watch the metrics.")):
    m = evidence["metrics"][0]
    return {"severity": contract.severity(evidence), "summary": summary,
            "root_cause": "The classified fault is the most likely root cause.",
            "evidence": [{"statement": "See the metric.", "metrics": [f"{m['service']}.{m['metric']}"]}],
            "recommended_actions": list(actions)}


def _flags(evidence, diagnosis, runbook, number):
    return [f for f in self_check.check(evidence, diagnosis, runbook=runbook) if f"number {number} " in f]


def test_runbook_numbers_are_allowed_in_actions():
    _, runbook = runbooks.runbook_for("DATABASE_THROTTLING")
    action = _diagnosis(THR, actions=["Check whether reads exhaust their 7 SDK attempts.", "Watch p95."])
    assert _flags(THR, action, runbook, "7") == []
    assert _flags(THR, action, None, "7")


def test_runbook_numbers_stay_out_of_claims():
    _, runbook = runbooks.runbook_for("DATABASE_THROTTLING")
    claim = _diagnosis(THR, summary="Each read gets 7 SDK attempts.")
    assert _flags(THR, claim, runbook, "7")


def test_a_precise_runbook_number_needs_its_runbook():
    lam = SAMPLES["lambda_degradation"]
    _, runbook = runbooks.runbook_for("LAMBDA_DEGRADATION")
    action = _diagnosis(lam, actions=["Keep the memory at 1,769 MB.", "Profile the handler."])
    assert _flags(lam, action, runbook, "1,769") == []
    assert _flags(lam, action, None, "1,769")


def test_only_the_given_runbooks_numbers_are_allowed():
    # A precise number: a round one ("6") already matches the throttling sample's 6 ms p50.
    _, lambda_runbook = runbooks.runbook_for("LAMBDA_DEGRADATION")
    _, own = runbooks.runbook_for("DATABASE_THROTTLING")
    action = _diagnosis(THR, actions=["Keep the memory at 1,769 MB.", "Watch p95."])
    assert _flags(THR, action, lambda_runbook, "1,769") == []
    assert _flags(THR, action, own, "1,769")


def test_a_per_minute_count_may_be_written_per_second():
    # Sonnet with the throttling runbook (2026-11-03): 11,900 / 60 = 198.3.
    action = _diagnosis(THR, actions=["The current 11900 RCU/min is roughly 198 RCU/s.", "Watch p95."])
    assert _flags(THR, action, None, "198") == []
    wrong = _diagnosis(THR, actions=["The current 11900 RCU/min is roughly 240 RCU/s.", "Watch p95."])
    assert _flags(THR, wrong, None, "240")


def test_the_95th_percentile_is_a_name_not_a_value():
    lam = SAMPLES["lambda_degradation"]
    claim = _diagnosis(lam, summary="Latency rose at the median and the 95th percentile.")
    assert not any("number 95" in f for f in self_check.check(lam, claim))


# --- the build -------------------------------------------------------------------------------

def test_the_makefile_ships_the_runbooks():
    makefile = (HERE / "Makefile").read_text()
    assert "runbooks.py" in makefile
    assert re.search(r'cp -R runbooks "\$\(ARTIFACTS_DIR\)/genai/"', makefile)
