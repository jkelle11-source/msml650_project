"""Structured evidence -> Bedrock -> diagnosis (M6-1).

    diagnose(evidence) -> {"diagnosis": ..., "evidence_metrics": ..., "runbook": ..., "self_check": ...,
                           "model": ..., "usage": ..., "latency_ms": ...}

`diagnosis` is in the dashboard contract's shape (evidence as plain strings);
`evidence_metrics[i]` lists the input metrics evidence bullet i rests on, so M8-4
can show that every bullet traces to an input metric.

Calls Claude on Amazon Bedrock through the Anthropic SDK's Bedrock client
(bedrock-runtime), with structured outputs holding the answer to
contract.output_schema(). This account serves Claude through bedrock-runtime's
inference profiles, not the newer bedrock-mantle endpoint, which answered 404
for every model (2026-11-01).
"""
import json
import os
import time

from genai import contract, prompt, runbooks, self_check

# Claude Sonnet 4.6, chosen over Opus 4.6 on the five samples (2026-11-01: fewer self-check
# findings). Claude 4.7 and later have a Bedrock quota of 0 tokens per minute in this account
# (never raised), so they fail until AWS Support raises it. A global cross-region inference
# profile: Bedrock routes it to any region with capacity, at no premium over regional routing.
MODEL_ID = os.environ.get("ANOMALYPULSE_BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("ANOMALYPULSE_BEDROCK_REGION", "us-east-2")
# Adaptive thinking at medium effort. On 4.6 models thinking runs only when asked for, so the
# request always asks; on 5.x models it is on anyway and "adaptive" is the same as leaving it out.
EFFORT = os.environ.get("ANOMALYPULSE_BEDROCK_EFFORT", "medium")
MAX_TOKENS = 16000
# One attempt is cut off at TIMEOUT_S and retried MAX_RETRIES times, so a stalled call fails as
# an APITimeoutError inside the Lambda's 120 s (infra/genai/template.yaml) instead of the Lambda
# timing out. Diagnoses took 6-32 s on the samples (2026-11-01). The SDK's defaults (600 s, 2
# retries) could run 30 minutes. A throttled call the API asks to wait longer (retry-after) can
# still outlast the Lambda.
TIMEOUT_S = 50
MAX_RETRIES = 1
# M6-2: the runbook for the classified type goes into the user turn, for recommended_actions only.
# Off by default; M9-5's blind comparison of actions decides whether it is turned on.
RUNBOOKS = os.environ.get("ANOMALYPULSE_RUNBOOKS", "off") == "on"

_client = None


class DiagnosisFailed(RuntimeError):
    """Bedrock answered, but not with a usable diagnosis (a refusal, or a cut-off answer)."""


def client():
    # Created once per Lambda environment and reused. Credentials and region come from the
    # standard AWS chain (the Lambda role, or your profile locally).
    global _client
    if _client is None:
        from anthropic import AnthropicBedrock
        _client = AnthropicBedrock(aws_region=REGION, timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
    return _client


def _runbook(evidence: dict, use: bool | None):
    """(name, text) of the incident type's runbook when runbooks are on, else (None, None).
    `use` overrides the ANOMALYPULSE_RUNBOOKS switch."""
    if not (RUNBOOKS if use is None else use):
        return None, None
    return runbooks.runbook_for(evidence["incident_type"])


def request(evidence: dict, model: str | None = None, runbooks: bool | None = None,
            revise: dict | None = None) -> dict:
    """The Messages request for one diagnosis. Pure, so tests can check it without Bedrock.

    revise (contract.validate_revision): an earlier answer to this same input and its self-check
    findings. It goes back as the assistant's turn, and a user turn lists the findings and asks for
    the answer again (prompt.revision_message). The request still ends on a user turn, so this is
    not a prefill, and an assistant turn with no thinking blocks is fine on Sonnet 4.6."""
    _, runbook = _runbook(evidence, runbooks)
    messages = [{"role": "user", "content": prompt.user_message(evidence, runbook)}]
    if revise is not None:
        messages += [{"role": "assistant", "content": json.dumps(revise["answer"])},
                     {"role": "user", "content": prompt.revision_message(revise["findings"])}]
    return {
        "model": model or MODEL_ID,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": prompt.SYSTEM_PROMPT,
        "messages": messages,
        "output_config": {"effort": EFFORT,
                          "format": {"type": "json_schema", "schema": contract.output_schema(evidence)}},
    }


def diagnose(evidence: dict, *, bedrock=None, model: str | None = None,
             runbooks: bool | None = None, revise: dict | None = None) -> dict:
    """Validate the input, ask Bedrock, check the answer. Raises BadInput or DiagnosisFailed.
    `runbooks` overrides the ANOMALYPULSE_RUNBOOKS switch. `revise` asks Bedrock to fix an earlier
    answer to this input (request()); the new answer is checked as any other."""
    contract.validate_input(evidence)
    if revise is not None:
        contract.validate_revision(revise, evidence)
    model = model or MODEL_ID
    runbook_name, runbook_text = _runbook(evidence, runbooks)
    started = time.perf_counter()
    response = (bedrock or client()).messages.create(**request(evidence, model, runbooks, revise))
    latency_ms = round((time.perf_counter() - started) * 1000)

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise DiagnosisFailed(f"Bedrock declined to answer ({getattr(details, 'category', None)})")
    if response.stop_reason == "max_tokens":
        raise DiagnosisFailed(f"the answer was cut off at max_tokens ({MAX_TOKENS})")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise DiagnosisFailed(f"no text in the answer (stop_reason {response.stop_reason})")
    raw = json.loads(text)

    # The self-check judges Bedrock's own answer, so a severity off the rubric stays a finding (M9-5
    # counts them); the diagnosis reports the rubric's, which is computed from the input, not generated.
    findings = contract.check_output_shape(raw) + self_check.check(evidence, raw, runbook=runbook_text)
    return {
        "incident_id": evidence["incident_id"],
        "diagnosis": {
            "severity": contract.severity(evidence),
            "summary": raw["summary"],
            "root_cause": raw["root_cause"],
            "evidence": [item["statement"] for item in raw["evidence"]],
            "recommended_actions": raw["recommended_actions"],
        },
        "evidence_metrics": [item["metrics"] for item in raw["evidence"]],
        # Beside diagnosis, not in it, so the dashboard contract is unchanged. None when off.
        "runbook": runbook_name,
        "self_check": {"passed": not findings, "findings": findings},
        "model": {"id": model, "effort": EFFORT},
        "usage": {"input_tokens": response.usage.input_tokens,
                  "output_tokens": response.usage.output_tokens},
        "latency_ms": latency_ms,
    }
