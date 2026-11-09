"""The M8-3 Lambda's logic: one diagnosed minute in, the incident records out.

incidents/handler.py gives it AWS (the table, S3, the lock, the explain Lambda); tests give it
fakes. `deps` needs: get_state() -> dict|None, put_state(state), get_record(started_at) ->
dict|None, put_record(record), read_window(key) -> dict, read_lock() -> dict|None, explain(input)
-> reply (raises ExplainError; explain(input, revise=...) for a retry), now() -> an aware datetime,
emit(line: dict).

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


def retry_line(rec, call_type, before, reply, error, kept=False):
    """One log line per retry: the flagged diagnosis's finding kinds, and the retry's (or its error),
    and whether its diagnosis replaced the flagged one (kept: record.retry_kept). M9-5 can count how
    often a retry helps."""
    line = {"event": "incident_retry", "incident_id": rec["incident_id"], "for_type": call_type,
            "flagged": sorted(before)}
    if error:
        return {**line, "kept": False, "error": error["type"]}
    return {**line, "kept": kept, "found": sorted(record.finding_kinds(reply.get("self_check")))}


def _explain(rec, state, baseline, window, prediction, deps, reason="type"):
    """Call Bedrock for the incident's current type and store what comes back. reason: record.call_reason's."""
    call_type = state["incident_type"]
    before = record.finding_kinds(rec.get("self_check"))
    error = reply = revise = None
    try:
        if reason == "retry":
            # The same input again, with the flagged answer and what its self-check found.
            evidence, revise = rec["diagnosis_input"], record.revision(rec)
        else:
            evidence = builder.build_input(state, baseline, window)
        contract.validate_input(evidence)
        if revise is not None:
            contract.validate_revision(revise, evidence)
        reply = deps.explain(evidence, revise=revise)
        if "diagnosis" not in reply:
            raise ExplainError("BadReply", "the reply has no diagnosis")
    except contract.BadInput as err:
        error = {"type": "BadInput", "message": str(err)}
    except ExplainError as err:
        error = {"type": err.kind, "message": err.message}
    stored, kept = None, False
    if error:
        rec = record.with_error(rec, call_type, error)
        if reason == "retry":
            rec = record.retried(rec, call_type)
    elif reason == "retry":
        kept = record.retry_kept(rec, reply)            # judged against the flagged record, before it changes
        stored = deps.now() if kept else None
        rec = record.with_retry(rec, call_type, reply, stored and _iso(stored), evidence)
    else:
        stored = deps.now()
        rec = record.with_diagnosis(rec, call_type, reply, _iso(stored), evidence)
    deps.put_record(rec)
    if stored is not None:
        deps.emit(metric_line(prediction, rec, stored, reply))
    if reason == "retry":
        deps.emit(retry_line(rec, call_type, before, reply, error, kept))
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
            rec = record.refresh(rec, new, affected.crossing(baseline, window))
            deps.put_record(rec)

    # On open, on a type change, and on the next counted minute after a failed call; and once per type
    # after a diagnosis flagged for something the input doesn't support or its wording (record.call_reason).
    reason = record.call_reason(rec, new) if rec is not None else None
    if reason:
        _explain(rec, new, baseline, window, prediction, deps, reason)

    recent = list(state["recent_normal"])
    if prediction.get("label") == "NORMAL" and not prediction.get("anomalous") and new["status"] != "OPEN":
        recent = (recent + [window])[-KEEP_NORMAL:]
    deps.put_state({"incident": new, "recent_normal": recent,
                    "baseline": baseline if new["status"] == "OPEN" else {}})
    return {"action": "processed", "changes": changes, "incident": new.get("started_at"),
            "window_from": window_from}