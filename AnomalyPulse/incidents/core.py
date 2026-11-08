"""The M8-3 Lambda's logic: one diagnosed minute in, the incident records out.

incidents/handler.py gives it AWS (the table, S3, the lock, the explain Lambda); tests give it
fakes. `deps` needs: get_state() -> dict|None, put_state(state), get_record(started_at) ->
dict|None, put_record(record), read_window(key) -> dict, read_lock() -> dict|None, explain(input)
-> reply (raises ExplainError), now() -> an aware datetime, emit(line: dict).

Per minute: the incident's record when it opens, then Bedrock, then the record again with the
diagnosis, then the state LAST. A crash before the state is written means the retry redoes the
minute (Bedrock may be called twice); a state written first would leave an incident with no
diagnosis and no retry.
"""
import json
from datetime import datetime, timedelta, timezone

from genai import affected, contract
from incidents import builder, record

try:
    from incidents import incident          # the Lambda bundle: the Makefile copies incident.py here
except ImportError:
    from ml.diagnosis import incident       # a checkout

KEEP_NORMAL = 5
FUNCTION = "anomalypulse-incidents"
NAMESPACE = "AnomalyPulse/Incidents"
UNITS = {"SinceWindowEndSeconds": "Seconds", "DiagnosisToStoredSeconds": "Seconds",
         "RoundTripSeconds": "Seconds", "BedrockMs": "Milliseconds"}


class ExplainError(Exception):
    """The explain Lambda failed: a FunctionError, a timeout or a throttle."""

    def __init__(self, kind, message=""):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _after_a_minute(text):
    return _iso(_parse(text) + timedelta(minutes=1))


def _window(detail, payload, deps):
    """The minute's feature window and where it came from: the request the Diagnosis Lambda
    answered ("event"), or its file in S3 ("s3")."""
    request = _json(detail.get("requestPayload"))
    windows = ((request or {}).get("detail") or {}).get("windows")
    if windows:
        return windows[0], "event"
    return deps.read_window(payload["key"]), "s3"


def _experiment(deps, now):
    """The running experiment's id, or None: no lock, or one a crashed run left behind."""
    lock = deps.read_lock()
    if lock and int(lock["expires_at"]) > int(now.timestamp()):
        return lock["experiment_id"]
    return None


def metric_line(prediction, rec, stored, reply):
    """One embedded-metric-format log line for a stored diagnosis: this call's own round trip, from
    the minute that triggered it (the record keeps only the first diagnosis's)."""
    lag = prediction.get("latency") or {}
    since_end = lag.get("since_window_end_s")
    to_stored = round((stored - _parse(lag["diagnosed_at"])).total_seconds(), 3)
    values = {"SinceWindowEndSeconds": since_end, "DiagnosisToStoredSeconds": to_stored,
              "RoundTripSeconds": None if since_end is None else round(since_end + to_stored, 3),
              "BedrockMs": reply.get("latency_ms")}
    values = {name: value for name, value in values.items() if value is not None}
    return {"_aws": {"Timestamp": int(stored.timestamp() * 1000),
                     "CloudWatchMetrics": [{"Namespace": NAMESPACE, "Dimensions": [["Function"]],
                                            "Metrics": [{"Name": n, "Unit": UNITS[n]} for n in values]}]},
            "Function": FUNCTION, "event": "incident_diagnosis", "incident_id": rec["incident_id"], **values}


def _explain(rec, state, baseline, window, prediction, deps):
    call_type = state["incident_type"]
    error = None
    try:
        evidence = builder.build_input(state, baseline, window)
        contract.validate_input(evidence)
        reply = deps.explain(evidence)
        if "diagnosis" not in reply:
            raise ExplainError("BadReply", "the reply has no diagnosis")
    except contract.BadInput as err:
        error = {"type": "BadInput", "message": str(err)}
    except ExplainError as err:
        error = {"type": err.kind, "message": err.message}
    stored = None
    if error:
        rec = record.with_error(rec, call_type, error)
    else:
        stored = deps.now()
        rec = record.with_diagnosis(rec, call_type, reply, _iso(stored))
    deps.put_record(rec)
    if stored is not None:
        deps.emit(metric_line(prediction, rec, stored, reply))
    return rec


def process(event, deps):
    """Handle one Diagnosis Lambda success event. Returns a short summary for the log."""
    detail = event["detail"]
    payload = _json(detail["responsePayload"])
    prediction = payload["predictions"][0]
    window, window_from = _window(detail, payload, deps)
    if window["window_start"] != prediction["window_start"]:
        raise ValueError(f"the window ({window['window_start']}) is not the prediction's "
                         f"({prediction['window_start']})")

    state = deps.get_state() or {"incident": None, "recent_normal": [], "baseline": {}}
    prev = state["incident"]
    if prev and prediction["window_start"] <= prev["last_window"]:
        return {"action": "ignored", "changes": [], "window_from": window_from}
    new, changes = incident.step(prev, prediction)
    now = deps.now()

    if "closed" in changes:
        # step returns only the NEW incident's state when the same window opens the next one,
        # so the old one is closed from the state loaded above.
        old = deps.get_record(prev["started_at"])
        if old is not None:
            deps.put_record(record.close(old, _after_a_minute(prev["last_counted"])))

    baseline = state.get("baseline") or {}
    rec = None
    if "opened" in changes:
        baseline = affected.baseline(state["recent_normal"])
        services = affected.affected_services(baseline, window)
        rec = record.open_record(new, prediction, services, payload["model"], _experiment(deps, now))
        deps.put_record(rec)
    elif new["status"] == "OPEN" and incident.counts(prediction):
        rec = deps.get_record(new["started_at"])
        if rec is not None:
            rec = record.refresh(rec, new, affected.affected_services(baseline, window))
            deps.put_record(rec)

    # On open, on a type change, and on the next counted minute after a failed call (record.wants_bedrock).
    if rec is not None and record.wants_bedrock(rec, new):
        _explain(rec, new, baseline, window, prediction, deps)

    recent = list(state["recent_normal"])
    if prediction.get("label") == "NORMAL" and not prediction.get("anomalous") and new["status"] != "OPEN":
        recent = (recent + [window])[-KEEP_NORMAL:]
    deps.put_state({"incident": new, "recent_normal": recent,
                    "baseline": baseline if new["status"] == "OPEN" else {}})
    return {"action": "processed", "changes": changes, "incident": new.get("started_at"),
            "window_from": window_from}