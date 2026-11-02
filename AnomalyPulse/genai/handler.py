"""The explanation Lambda (M6-1): the seam M8-3 wires classifier -> Bedrock -> incident store.

Event: one input in the diagnosis contract (Docs/DIAGNOSIS_CONTRACT.md).
Reply: explain.diagnose()'s result. A diagnosis that fails its self-check is
still returned, with self_check.passed false, so M8-3 can store it flagged
rather than lose it; a bad input or an unusable answer raises.
"""
from genai import explain


def handler(event, context):
    return explain.diagnose(event)
