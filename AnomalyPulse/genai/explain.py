"""Structured evidence -> Bedrock -> diagnosis (M6-1).

    diagnose(evidence) -> {"diagnosis": ..., "evidence_metrics": ..., "self_check": ..., "model": ...,
                           "usage": ..., "latency_ms": ...}

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

from genai import contract, prompt, self_check

# Claude Sonnet 4.6, chosen over Opus 4.6 on the five samples (2026-11-01: fewer self-check
# findings); the account has no access to Claude 4.7 or later. A global cross-region inference
# profile: Bedrock routes it to any region with capacity, at no premium over regional routing.
MODEL_ID = os.environ.get("ANOMALYPULSE_BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")
REGION = os.environ.get("ANOMALYPULSE_BEDROCK_REGION", "us-east-2")
# Adaptive thinking at medium effort. On 4.6 models thinking runs only when asked for, so the
# request always asks; on 5.x models it is on anyway and "adaptive" is the same as leaving it out.
EFFORT = os.environ.get("ANOMALYPULSE_BEDROCK_EFFORT", "medium")
MAX_TOKENS = 16000

_client = None


class DiagnosisFailed(RuntimeError):
    """Bedrock answered, but not with a usable diagnosis (a refusal, or a cut-off answer)."""


def client():
    # Created once per Lambda environment and reused. Credentials and region come from the
    # standard AWS chain (the Lambda role, or your profile locally).
    global _client
    if _client is None:
        from anthropic import AnthropicBedrock
        _client = AnthropicBedrock(aws_region=REGION)
    return _client


def request(evidence: dict, model: str | None = None) -> dict:
    """The Messages request for one diagnosis. Pure, so tests can check it without Bedrock."""
    return {
        "model": model or MODEL_ID,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "system": prompt.SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt.user_message(evidence)}],
        "output_config": {"effort": EFFORT,
                          "format": {"type": "json_schema", "schema": contract.output_schema(evidence)}},
    }


def diagnose(evidence: dict, *, bedrock=None, model: str | None = None) -> dict:
    """Validate the input, ask Bedrock, check the answer. Raises BadInput or DiagnosisFailed."""
    contract.validate_input(evidence)
    model = model or MODEL_ID
    started = time.perf_counter()
    response = (bedrock or client()).messages.create(**request(evidence, model))
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

    findings = contract.check_output_shape(raw) + self_check.check(evidence, raw)
    return {
        "incident_id": evidence["incident_id"],
        "diagnosis": {
            "severity": raw["severity"],
            "summary": raw["summary"],
            "root_cause": raw["root_cause"],
            "evidence": [item["statement"] for item in raw["evidence"]],
            "recommended_actions": raw["recommended_actions"],
        },
        "evidence_metrics": [item["metrics"] for item in raw["evidence"]],
        "self_check": {"passed": not findings, "findings": findings},
        "model": {"id": model, "effort": EFFORT},
        "usage": {"input_tokens": response.usage.input_tokens,
                  "output_tokens": response.usage.output_tokens},
        "latency_ms": latency_ms,
    }
