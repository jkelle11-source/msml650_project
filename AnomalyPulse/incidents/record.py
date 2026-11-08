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

from incidents.builder import incident_id

PK = "INCIDENT"
MAX_CALLS = 5
MAX_ERROR_CHARS = 500


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
        "diagnosis_for_type": None,
        "bedrock_calls": 0,
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


def refresh(record, state, services=None):
    """The record after a counted minute: the incident's current type and confidence, and the
    affected services if given. detected_at and the diagnosis stay as they were."""
    new = copy.deepcopy(record)
    new["prediction"]["incident_type"] = state["incident_type"]
    new["prediction"]["confidence"] = state["confidence"]
    if services:
        new["prediction"]["affected_services"] = list(services)
    return new


def close(record, ended_at):
    new = copy.deepcopy(record)
    new["status"] = "CLOSED"
    new["ended_at"] = ended_at
    return new


def with_diagnosis(record, call_type, reply, stored_at):
    """Store the explain Lambda's reply. call_type: the incident type the call was made for;
    stored_at: now.

    `latency` keeps the FIRST diagnosis's numbers, so its stretches belong to one minute: the
    incident's first counted minute's end to its diagnosis (detected_at), then detected_at to the
    first diagnosis stored, which includes a retry after a failed first call. Each call's own
    numbers are in its log line (core.metric_line)."""
    new = copy.deepcopy(record)
    new["diagnosis"] = reply["diagnosis"]
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


def wants_bedrock(record, state):
    """True when the stored diagnosis isn't for the incident's current type and the cap isn't
    reached. That is when the incident opens (no diagnosis yet), when its type changes, and on the
    next counted minute after a failed call, so one failure doesn't leave the console on
    "Severity pending" for the rest of the incident. Called only on counted minutes of an open
    incident."""
    return record["bedrock_calls"] < MAX_CALLS and record["diagnosis_for_type"] != state["incident_type"]


def to_incident(record):
    """What build_dashboard reads: incident_id, prediction, diagnosis."""
    return {"incident_id": record["incident_id"], "prediction": record["prediction"],
            "diagnosis": record["diagnosis"]}


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