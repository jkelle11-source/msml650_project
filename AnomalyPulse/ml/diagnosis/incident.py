"""How classified minutes open and close an incident (M8-2's proposal for M8-3).

The Diagnosis Lambda is stateless: it answers each window it is given. Grouping minutes into
incidents needs the previous minute's state, which M8-3 keeps in the Incidents table, so the
rule is a pure function here that M8-3 calls with what it stored:

    state, changes = step(state, prediction)    # prediction: one item of the Lambda's reply

The rule, from what live windows do on validation (Docs/PIPELINE.md, "On the minutes only the
live path scores"):

- **A minute counts** when the detector flags it *and* the classifier names an incident
  (the prediction's incident_type is set). The detector alone keeps flagging ~3 minutes past a
  fault and flags the first minute after idle time; the classifier names an incident in 78 of
  112 first recovery minutes, 24 of 56 second ones and 2 of 138 third ones, and calls both
  after-idle minutes NORMAL.
- **Opens** on the first minute that counts. started_at is that window's window_start.
- **Stays open** through minutes that count, taking the latest minute's type and confidence:
  a cascade is first classified as its first fault's type, then CASCADING_FAILURE.
- **Closes** after CLOSE_AFTER minutes in a row that don't count, or when the next window
  comes CLOSE_AFTER or more minutes after the last one (the window builder announces no window
  with an empty value, so idle traffic leaves gaps). ended_at is the end of the last minute that
  counted.
- **Repeats and stragglers:** EventBridge may deliver a window twice and out of order. A window
  not newer than the last one applied is ignored, so apply a batch in window_start order.

`changes` holds "opened", "type_changed" or "closed": the first two are the moments
DIAGNOSIS_CONTRACT.md's open question 3 proposes calling Bedrock on.
"""
from datetime import datetime, timedelta

CLOSE_AFTER = 2
WINDOW = timedelta(minutes=1)


def _t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _iso(when):
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def counts(prediction):
    return bool(prediction.get("anomalous")) and prediction.get("incident_type") is not None


def step(state, prediction):
    """(new state, changes) after one window's prediction. `state` is None before the first
    incident, or the dict this returns; a closed incident's state is kept so the next window can
    be checked against last_window. `changes` lists what happened, in order: usually nothing or
    one change, but a gap can close one incident and the same window open the next."""
    at = _t(prediction["window_start"])
    if state and at <= _t(state["last_window"]):
        return state, []

    changes = []
    is_open = bool(state) and state["status"] == "OPEN"
    if is_open and at - _t(state["last_counted"]) > CLOSE_AFTER * WINDOW:
        # A gap: the minutes in between were never announced, so they can't hold the incident open.
        state, is_open = _closed(state), False
        changes.append("closed")

    if counts(prediction):
        if not is_open:
            return ({"status": "OPEN", "started_at": prediction["window_start"],
                     "last_window": prediction["window_start"], "last_counted": prediction["window_start"],
                     "incident_type": prediction["incident_type"], "confidence": prediction["confidence"],
                     "quiet": 0, "ended_at": None}, changes + ["opened"])
        changed = prediction["incident_type"] != state["incident_type"]
        return ({**state, "last_window": prediction["window_start"], "last_counted": prediction["window_start"],
                 "incident_type": prediction["incident_type"], "confidence": prediction["confidence"],
                 "quiet": 0}, ["type_changed"] if changed else [])

    if not is_open:
        return {**(state or {"status": "CLOSED"}), "last_window": prediction["window_start"]}, changes

    state = {**state, "last_window": prediction["window_start"], "quiet": state["quiet"] + 1}
    if state["quiet"] >= CLOSE_AFTER:
        return _closed(state), ["closed"]
    return state, []


def _closed(state):
    return {**state, "status": "CLOSED", "ended_at": _iso(_t(state["last_counted"]) + WINDOW)}