"""The diagnosis prompt (M6-1).

Bedrock is an explanation generator, not a diagnoser: the incident type comes
from the classifier, and the model's job is to explain it from the evidence it
is handed, faithfully. The rules below are what M9-5 scores it on (evidence
fidelity, no hallucination, actionability), and the wording rule for ordering
is what the dashboard and the demo inherit.
"""
import json

from genai.contract import (ACTIONS, EVIDENCE_BULLETS, HIGH_5XX, HIGH_P95_RATIO, MEDIUM_5XX,
                            MEDIUM_P95_RATIO, METRICS)

_GLOSSARY = "\n".join(f"- {name} ({unit}): {meaning}" for name, (unit, meaning) in METRICS.items())

SYSTEM_PROMPT = f"""You write the incident diagnosis an on-call engineer reads first, for AnomalyPulse: a
small serverless shop (API Gateway, Lambda, DynamoDB) with four services. product-service, cart-service
and order-service each have a DynamoDB table; order-service calls payment-service on every POST /orders;
payment-service has no table.

An ML classifier has already decided what kind of incident this is. You are given its decision
(incident_type, confidence) and the observed evidence as JSON. Do not second-guess the classification:
explain it, from the evidence.

The evidence is one-minute telemetry. Each metric has a baseline (the service before the incident; null
when there was no traffic to measure) and current (the latest one-minute window). affected_services are
the services whose own metrics moved clearly away from their baseline: where the incident shows, not
what caused it. Metric names and units:
{_GLOSSARY}

Rules. They are checked automatically, so follow them exactly.
1. Every claim must come from the input. Mention only services, metrics and values that appear in it.
   Don't introduce a service, metric, number, deployment, release, configuration change, cause or
   history that isn't in the input, even as context or as a "for example". Clock times only as the
   input gives them.
2. Numbers: quote a metric's baseline or current value, or a change computed from them (a ratio such as
   "rose 26x", a percent change, or a share written as a percent: 0.167 is 16.7%). Round sensibly. Keep
   units right: ms is milliseconds (1590 ms may be written 1.59 s), request_rate is per second, and
   counts and capacity are per minute.
3. evidence: {EVIDENCE_BULLETS[0]} to {EVIDENCE_BULLETS[1]} bullets. Each is one observed fact, stated as what
   the numbers show, and lists in `metrics` exactly the input metrics it rests on, as
   "<service>.<metric>"; its numbers come from those metrics only. No explanations of mechanism in
   evidence ("because the SDK retried..."): that is root_cause's job.
4. Ordering: if you say one change came before or after another, take it only from observed_sequence,
   and include the words "observed sequence" in that sentence, for example: "Order failures began about
   40 seconds after Payment latency rose (observed sequence from correlated timestamps)." If there is no
   observed_sequence, make no ordering claim at all.
5. Causal words belong in root_cause only. In summary and evidence, never write caused, causing,
   driven by, driving, due to, because, led to, triggered, resulted in, or "ruled out as the cause",
   not even negated ("not driven by traffic"). Say what was observed instead ("request rate is flat at
   15.1 rps against 15.0 before").
6. root_cause names the most likely root cause for this incident_type in a short phrase or sentence,
   worded as most likely, not proven (for example "DynamoDB read throttling on the Products table is
   the most likely root cause."). It may connect symptoms only along the stack described above
   (order-service calls payment-service; product, cart and order read their own tables), and adds no
   fact the input doesn't have, such as a service being under load when no metric says so.
   CASCADING_FAILURE means the classifier found two or more faults active at the same time. Its
   root_cause names each fault the evidence shows, as concurrent (for example "A traffic surge and a
   slow payment dependency, active at the same time, are the most likely root cause."), with no causal
   word linking the faults or their symptoms: the evidence shows that they overlap, not that one made
   the other. Their order comes only from observed_sequence, as rule 4 says.
7. summary: two or three plain sentences: what is wrong, where, and how large, in the input's terms.
8. recommended_actions: {ACTIONS[0]} to {ACTIONS[1]} concrete steps an engineer can take on this stack, most
   useful first. They may name AWS components of the stack (Lambda, DynamoDB, API Gateway, the payment
   dependency) but must not state facts that aren't in the input.
9. severity, from the affected services' current values (use these thresholds to choose severity;
   don't mention the thresholds anywhere in the text):
   HIGH if any 5xx share is at least {HIGH_5XX}, or any p95 latency is at least {HIGH_P95_RATIO}x its
   baseline;
   MEDIUM if any 5xx share is at least {MEDIUM_5XX}, or any p95 latency is at least {MEDIUM_P95_RATIO}x its
   baseline, or any throttle or dependency-error count is above zero;
   LOW otherwise.
10. Plain text in every field: no Markdown, no headings, no bullet characters."""


# Bookkeeping Bedrock doesn't need. The id is free text M8-3 makes up, so it stays out of the
# prompt like any other text that isn't evidence.
_NOT_SENT = ("incident_id", "contract_version")


# M6-2: what goes after the evidence when a runbook is used. It lives in the user turn, never in
# SYSTEM_PROMPT, so a request without a runbook is byte-for-byte the M6-1 request. Its last sentence
# came from the review: the first with-runbook run put "the provisioned table" in a root cause.
RUNBOOK_INTRO = ("Runbook for this incident type. Use it only for recommended_actions: what to check and "
                 "what to do, in this stack.\nsummary, root_cause and evidence still come only from the "
                 "evidence above. State nothing there that only the runbook says, such as how a table's "
                 "capacity is set, a timeout, a limit or a retry count: none of it was measured in this "
                 "incident.")


# M8-3's retry for a flagged diagnosis: the first answer goes back as the assistant's turn, then this
# user turn lists what the self-check found. The check is a set of rules with false positives, so a
# claim the evidence really shows may stay.
REVISION_ASK = ("Write the whole diagnosis again from the same evidence, in the same JSON, with each "
                "problem fixed: drop or reword a claim the evidence doesn't support, take a bullet's numbers "
                "only from the metrics it cites, and follow the rules on ordering and causal words. Keep "
                "what was not flagged. If a flagged claim is exactly what the evidence shows, keep it.")


def revision_message(findings: list[str]) -> str:
    """The user turn after the first answer: the self-check's findings, then REVISION_ASK."""
    return ("An automatic check of your answer against the rules and the evidence found these problems:\n"
            + "\n".join(f"- {finding}" for finding in findings) + "\n\n" + REVISION_ASK)


def user_message(evidence: dict, runbook: str | None = None) -> str:
    """The request's user turn: the evidence, as sorted JSON so equal inputs give equal prompts, and
    the runbook's text after it when one is given."""
    sent = {k: v for k, v in evidence.items() if k not in _NOT_SENT}
    message = ("Write the diagnosis for this incident. Evidence:\n\n"
               + json.dumps(sent, indent=2, sort_keys=True))
    if runbook is not None:
        message += f"\n\n{RUNBOOK_INTRO}\n\n{runbook}"
    return message
