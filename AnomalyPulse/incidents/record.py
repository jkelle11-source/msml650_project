"""The stored incident record (M8-3). Pure: no AWS, no clock.

One item per incident in the Incidents table: pk "INCIDENT", sk started_at. Its shape is the one
posted on #30 and the dashboard's: `prediction` and `diagnosis` as build_dashboard reads them,
the explain Lambda's other outputs beside them, and the ground-truth reference
(`experiment_id`, Tier-2) at the top level, never inside `prediction`.

Every function returns a new record and leaves its argument alone.
"""
import copy
import json
from datetime import datetime
from decimal import Decimal

from genai import self_check
from genai.contract import MAX_FINDING_CHARS, MAX_REVISION_FINDINGS, SERVICES
from incidents.builder import incident_id

PK = "INCIDENT"
MAX_CALLS = 5
MAX_ERROR_CHARS = 500
# A stored diagnosis whose self-check found one of these is asked for again, once per incident type:
# anything it says that the input doesn't support, and causal or ordering wording. Not a severity
# disagreement (the rubric's severity is reported anyway), a quoted threshold, or a shape problem.
RETRY_KINDS = ("number", "time", "service", "metric", "citation", "wording")
# A stored diagnosis whose self-check found one of these is held back from the dashboard (M8-4): the
# console shows "Severity pending" instead. Causal or ordering wording would render an observed sequence
# as causation, and a citation outside the input breaks evidence-to-metric traceability. Other findings
# (a number rounded too far, say) are shown: M9-5 scores them.
HOLD_KINDS = ("wording", "citation")


def _t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def item_key(started_at):
    return {"pk": PK, "sk": started_at}


def open_record(state, prediction, services, model, experiment_id=None):
    """The item for an incident that just opened.

    state: incident.py's state; prediction: the first counted minute's prediction (the Diagnosis
    Lambda's predictions[0]); services: affected_services; model: the reply's `model` object."""
    latency = prediction.get("latency") or {}
    if not latency.get("diagnosed_at"):
        raise ValueError("the prediction has no latency.diagnosed_at, so detected_at is unknown")
    record = {
        "pk": PK,
        "sk": state["started_at"],
        "incident_id": incident_id(state["started_at"]),
        "status": "OPEN",
        "ended_at": None,
        "prediction": {
            "incident_type": state["incident_type"],
            "confidence": state["confidence"],
            "started_at": state["started_at"],
            "detected_at": latency["diagnosed_at"],
            "affected_services": list(services),
            "model": {"estimator": model["estimator"], "job": model["job"]},
        },
        "diagnosis": None,
        "diagnosis_input": None,
        "diagnosis_for_type": None,
        "bedrock_calls": 0,
        "retried_for": [],
        "diagnosis_error": None,
        "evidence_metrics": None,
        "self_check": None,
        "runbook": None,
        "latency": {"window_end_to_diagnosis_s": latency.get("since_window_end_s"),
                    "diagnosis_to_stored_s": None, "bedrock_ms": None},
    }
    if experiment_id:
        record["experiment_id"] = experiment_id
    return record


def refresh(record, state, services=()):
    """The record after a counted minute: the incident's current type and confidence, and its
    affected services so far, the stored ones plus `services` (the minute's services that cross
    the line, genai.affected.crossing), in SERVICES order. None is removed while the incident is
    open: its last counted minute is often the first one after the fault, when nothing crosses any
    more and affected_services would fall back to one service (the first live spike's record ended
    as ["order-service"] that way). detected_at and the diagnosis stay as they were."""
    new = copy.deepcopy(record)
    new["prediction"]["incident_type"] = state["incident_type"]
    new["prediction"]["confidence"] = state["confidence"]
    seen = set(record["prediction"]["affected_services"]) | set(services)
    new["prediction"]["affected_services"] = [s for s in SERVICES if s in seen]
    return new


def close(record, ended_at):
    new = copy.deepcopy(record)
    new["status"] = "CLOSED"
    new["ended_at"] = ended_at
    return new


def with_diagnosis(record, call_type, reply, stored_at, diagnosis_input):
    """Store the explain Lambda's reply. call_type: the incident type the call was made for;
    stored_at: now; diagnosis_input: the input the call sent, kept with the diagnosis it produced.

    `latency` keeps the FIRST diagnosis's numbers, so its stretches belong to one minute: the
    incident's first counted minute's end to its diagnosis (detected_at), then detected_at to the
    first diagnosis stored, which includes a retry after a failed first call. Each call's own
    numbers are in its log line (core.metric_line)."""
    new = copy.deepcopy(record)
    new["diagnosis"] = reply["diagnosis"]
    new["diagnosis_input"] = diagnosis_input
    new["diagnosis_for_type"] = call_type
    new["bedrock_calls"] = record["bedrock_calls"] + 1
    new["diagnosis_error"] = None
    new["evidence_metrics"] = reply.get("evidence_metrics")
    new["self_check"] = reply.get("self_check")
    new["runbook"] = reply.get("runbook")
    if record["latency"]["diagnosis_to_stored_s"] is None:
        detected = record["prediction"]["detected_at"]
        new["latency"]["bedrock_ms"] = reply.get("latency_ms")
        new["latency"]["diagnosis_to_stored_s"] = round((_t(stored_at) - _t(detected)).total_seconds(), 3)
    return new


def with_error(record, call_type, error):
    """A failed call: counted, its error stored, any earlier diagnosis kept. error: a dict with
    `type` and `message` (a FunctionError's errorType and errorMessage)."""
    new = copy.deepcopy(record)
    new["bedrock_calls"] = record["bedrock_calls"] + 1
    new["diagnosis_error"] = {"for_type": call_type, "type": str(error.get("type", "")),
                              "message": str(error.get("message", ""))[:MAX_ERROR_CHARS]}
    return new


def with_retry(record, call_type, reply, stored_at, diagnosis_input):
    """A retry's reply (call_reason "retry"). Its diagnosis replaces the flagged one only if its
    self-check is clean; otherwise the flagged one stays, for M8-4 to show as it decides, and only the
    call is counted. Either way the type's one retry is used."""
    if clean(reply.get("self_check")):
        return retried(with_diagnosis(record, call_type, reply, stored_at, diagnosis_input), call_type)
    new = copy.deepcopy(record)
    new["bedrock_calls"] = record["bedrock_calls"] + 1
    return retried(new, call_type)


def retried(record, call_type):
    """The record with call_type's one retry used (also when the retry's call failed)."""
    new = copy.deepcopy(record)
    new["retried_for"] = list(record.get("retried_for") or []) + [call_type]
    return new


def revision(record):
    """What a retry asks Bedrock to fix (genai.contract.validate_revision): the stored answer, as Bedrock
    gave it (the diagnosis's bullets with the metrics each cites), and its self-check findings, but not
    a severity disagreement: the stored severity is already the rubric's."""
    diagnosis = record["diagnosis"]
    answer = {"severity": diagnosis["severity"], "summary": diagnosis["summary"],
              "root_cause": diagnosis["root_cause"],
              "evidence": [{"statement": statement, "metrics": list(metrics)} for statement, metrics
                           in zip(diagnosis["evidence"], record.get("evidence_metrics") or [])],
              "recommended_actions": diagnosis["recommended_actions"]}
    findings = [finding[:MAX_FINDING_CHARS] for finding in (record.get("self_check") or {}).get("findings") or []
                if self_check.kind(finding) != "severity"]
    return {"answer": answer, "findings": findings[:MAX_REVISION_FINDINGS]}


def finding_kinds(check):
    """The kinds (genai.self_check.kind) of a self-check result's findings."""
    return {self_check.kind(finding) for finding in ((check or {}).get("findings") or [])}


def clean(check):
    """True when a self-check found nothing, or only that Bedrock's own severity wasn't the rubric's:
    the diagnosis reports the rubric's, so that finding changes nothing shown."""
    return finding_kinds(check) <= {"severity"}


def call_reason(record, state):
    """Why to call Bedrock on this counted minute of an open incident, or None. Never once MAX_CALLS
    calls have been made.

    "type": the stored diagnosis isn't for the incident's current type. That is when the incident
    opens (no diagnosis yet), when its type changes, and on the next counted minute after a failed
    call, so one failure doesn't leave the console on "Severity pending" for the rest of the incident.
    "retry": it is, but its self-check found one of RETRY_KINDS (a number, time, service, claim or
    citation the input doesn't support, or causal or ordering wording), and the type
    hasn't had its one retry; so retries can't use up the calls a later type change needs. A retry
    resends the diagnosis's own input with the answer and its findings (revision), so a record
    stored before inputs were kept (no diagnosis_input) gets none."""
    if record["bedrock_calls"] >= MAX_CALLS:
        return None
    if record["diagnosis_for_type"] != state["incident_type"]:
        return "type"
    if (state["incident_type"] not in (record.get("retried_for") or [])
            and record.get("diagnosis_input") is not None
            and finding_kinds(record.get("self_check")) & set(RETRY_KINDS)):
        return "retry"
    return None


def wants_bedrock(record, state):
    """True when call_reason gives a reason. Called only on counted minutes of an open incident."""
    return call_reason(record, state) is not None


def held_back(record):
    """True when the stored diagnosis's self-check found one of HOLD_KINDS. It stays held until a clean
    answer replaces it (a retry, or a call for a new type); if none does, the console shows "Severity
    pending" for the rest of the incident rather than the flagged text."""
    return record.get("diagnosis") is not None and bool(finding_kinds(record.get("self_check")) & set(HOLD_KINDS))


def shown_diagnosis(record):
    """The diagnosis the dashboard shows: the stored one, or None while it is held back."""
    return None if held_back(record) else record.get("diagnosis")


def to_incident(record):
    """What build_dashboard reads: incident_id, prediction, diagnosis (None while held back, M8-4)."""
    return {"incident_id": record["incident_id"], "prediction": record["prediction"],
            "diagnosis": shown_diagnosis(record)}


def to_dynamodb(item):
    """DynamoDB takes no floats: each goes in as a Decimal of its own text, as the window builder does."""
    return json.loads(json.dumps(item), parse_float=Decimal)


def from_dynamodb(value):
    """Decimals back to int or float, through nested dicts and lists."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: from_dynamodb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamodb(v) for v in value]
    return value