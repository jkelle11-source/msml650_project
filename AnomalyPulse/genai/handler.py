"""The explanation Lambda (M6-1): the seam M8-3 wires classifier -> Bedrock -> incident store.

Event: one input in the diagnosis contract (Docs/DIAGNOSIS_CONTRACT.md), optionally with `revise`
beside its fields: an earlier answer to this same input and its self-check findings, to be fixed
(M8-3's retry for a flagged diagnosis; contract.validate_revision).
Reply: explain.diagnose()'s result. A diagnosis that fails its self-check is
still returned, with self_check.passed false, so M8-3 can store it flagged
rather than lose it; a bad input or an unusable answer raises.
"""
from genai import explain


def handler(event, context):
    if isinstance(event, dict) and "revise" in event:
        evidence = {key: value for key, value in event.items() if key != "revise"}
        return explain.diagnose(evidence, revise=event["revise"])
    return explain.diagnose(event)
