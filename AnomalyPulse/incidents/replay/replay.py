"""The incident store over validation's runs as the live stream (Docs/INCIDENT_STORE.md, "Checked on validation").

    cd AnomalyPulse
    python3 incidents/replay/replay.py <dir> <out.json> [--fail-first | --flag-first]
    python3 incidents/replay/analyze.py <out.json>

<dir> is what download.sh fetched. Needs Python 3.13 with the Diagnosis Lambda's libraries
(ml/requirements-image.txt) plus boto3 and jsonschema. Reaches no AWS. About a minute.

1. The stream. datasets/v1's validation runs (slots 561-700), in the order they ran, each built as the
   window builder builds a live window (Docs/PIPELINE.md, "How it was measured"): build_matrix over the
   run's records and snapshots, plus the previous run's when it ran right before, with no bounds and no
   look-back limit. Only complete windows are kept, the only ones the window builder announces. Each
   goes through the Diagnosis Lambda's own detect_then_classify (ml/diagnosis/handler.py) with the
   promoted classifier and forest, and gets a diagnosis time 170 s after its minute ends.
2. The incident store. Each window and its prediction, in window order, wrapped as the event the
   incident store's rule delivers (detail.requestPayload, detail.responsePayload), through
   incidents.core.process with fakes for its AWS calls:
   - the table: every item goes through record.to_dynamodb and boto3's TypeSerializer, so a float
     fails as it would in DynamoDB, and the largest item's size is kept;
   - the lock: held by the run whose started_at-ended_at holds the minute's diagnosis time;
   - the explanation Lambda: validate_input again, then a canned reply (with --fail-first, every
     incident's first call fails, as a throttle would; with --flag-first, the first answer for each
     of an incident's types is flagged for a wrong number, and the retry, which resends that input
     with the answer and the finding, comes back clean).
   Then each stored record is read back through the dashboard's build_dashboard with its run's
   manifest.

Ground truth (manifests, labels) builds no window and no event: the lock's fake uses run times only,
and analyze.py scores against the rest.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]     # AnomalyPulse/
if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] not in ("--fail-first", "--flag-first")):
    sys.exit(__doc__.split("\n\n")[1])
DIR, OUT = Path(sys.argv[1]), Path(sys.argv[2])
FAIL_FIRST, FLAG_FIRST = sys.argv[3:] == ["--fail-first"], sys.argv[3:] == ["--flag-first"]
FLAG = "summary: the number 99 is not in or derivable from the input"
DATA = DIR / "data"
sys.path[:0] = [str(ROOT), str(ROOT / "ml" / "diagnosis")]
os.environ["ANOMALYPULSE_MODEL_DIR"] = str(DIR / "models" / "lr")
os.environ["ANOMALYPULSE_DETECTOR_DIR"] = str(DIR / "models" / "if")
os.environ.pop("ANOMALYPULSE_MODEL_POINTER", None)
os.environ.pop("ANOMALYPULSE_DETECTOR_POINTER", None)

from boto3.dynamodb.types import TypeSerializer  # noqa: E402
from features.build_matrix import build_matrix  # noqa: E402
import handler as diagnosis  # noqa: E402  (ml/diagnosis/handler.py, the Diagnosis Lambda)
from genai import contract  # noqa: E402
from incidents import core, record  # noqa: E402

_spec = importlib.util.spec_from_file_location("dashboard_builder", ROOT / "dashboard" / "builder.py")
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)

MINUTE = timedelta(minutes=1)
DIAGNOSED_AFTER = timedelta(seconds=170)        # a live window's minute end to its diagnosis
SERIALIZER = TypeSerializer()


def t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def floor(moment):
    return moment.replace(second=0, microsecond=0)


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# ---- 1. the stream -------------------------------------------------------------------------------
# In the order they ran, not slot order: slot 608 was re-run after slot 700.
runs = sorted((r for r in jsonl(DATA / "experiments.jsonl") if 561 <= r["slot"] <= 700),
              key=lambda r: r["generated_at"])
for r in runs:
    eid = r["experiment_id"]
    r["manifest"] = json.loads((DATA / f"tier2/manifests/experiment_id={eid}/manifest.json").read_text())
    r["records"] = jsonl(DATA / f"tier1/requests/experiment_id={eid}/requests.jsonl")
    r["snapshots"] = jsonl(DATA / f"tier1/metrics/experiment_id={eid}/metrics.jsonl")

stream, previous = [], None
for k, r in enumerate(runs):
    m = r["manifest"]
    start, end = t(m["started_at"]), t(m["ended_at"])
    back_to_back = previous is not None and start - t(previous["manifest"]["ended_at"]) < 2 * MINUTE
    matrix = build_matrix((previous["records"] if back_to_back else []) + r["records"],
                          (previous["snapshots"] if back_to_back else []) + r["snapshots"])
    first, last = floor(start), floor(end - timedelta(milliseconds=1))
    # The minute two back-to-back runs share is scored once, by the later run, which has both runs' records.
    following = runs[k + 1]["manifest"] if k + 1 < len(runs) else None
    if following and t(following["started_at"]) - end < 2 * MINUTE and floor(t(following["started_at"])) <= last:
        last = floor(t(following["started_at"])) - MINUTE
    windows, minute = [], first
    while minute <= last:
        row = matrix.get(iso(minute))
        if row is not None and all(v is not None for v in row.values()):
            windows.append({"window_start": iso(minute), **row})
        minute += MINUTE
    if windows:
        with contextlib.redirect_stdout(io.StringIO()):
            reply = diagnosis.detect_then_classify(windows, None, None)
        for window, p in zip(windows, reply["predictions"]):
            end_w = t(window["window_start"]) + MINUTE
            p["latency"] = {"window_end": iso(end_w), "announced_at": iso(end_w + DIAGNOSED_AFTER - timedelta(seconds=5)),
                            "diagnosed_at": iso(end_w + DIAGNOSED_AFTER),
                            "since_window_end_s": DIAGNOSED_AFTER.total_seconds(), "since_announced_s": 5.0}
            stream.append((r, window, p, {key: reply[key] for key in ("model", "detector")}))
    previous = r
stream.sort(key=lambda s: s[1]["window_start"])     # live, it is one stream in window order


# ---- 2. the incident store, with fakes -----------------------------------------------------------
class Fakes:
    """core.process's dependencies (see core.py's docstring)."""

    def __init__(self):
        self.items, self.calls, self.lines, self.failed, self.flagged = {}, [], [], set(), set()
        self.revised = []
        self.clock, self.largest = None, (0, None)

    def _put(self, key, item):
        stored = record.to_dynamodb(item)
        size = len(json.dumps(SERIALIZER.serialize(stored), default=str))    # raises on a float
        self.largest = max(self.largest, (size, list(key)))
        self.items[key] = stored

    def get_state(self):
        item = self.items.get(("INCIDENT_STATE", "CURRENT"))
        return None if item is None else record.from_dynamodb(item["state"])

    def put_state(self, state):
        self._put(("INCIDENT_STATE", "CURRENT"), {"pk": "INCIDENT_STATE", "sk": "CURRENT", "state": state})

    def get_record(self, started_at):
        item = self.items.get(("INCIDENT", started_at))
        return None if item is None else record.from_dynamodb(item)

    def put_record(self, rec):
        self._put((rec["pk"], rec["sk"]), rec)

    def read_window(self, key):
        raise AssertionError("every event carries its window")

    def read_lock(self):
        for r in runs:
            if t(r["manifest"]["started_at"]) <= self.clock < t(r["manifest"]["ended_at"]):
                return {"experiment_id": r["experiment_id"], "expires_at": int(self.clock.timestamp()) + 3600}
        return None

    def explain(self, evidence, revise=None):
        contract.validate_input(evidence)
        if revise is not None:
            contract.validate_revision(revise, evidence)
        self.calls.append(evidence)
        self.revised.append(revise is not None)
        if FAIL_FIRST and evidence["incident_id"] not in self.failed:
            self.failed.add(evidence["incident_id"])
            raise core.ExplainError("ThrottlingException", "replay: every incident's first call fails")
        check = {"passed": True, "findings": []}
        if FLAG_FIRST and revise is None and (evidence["incident_id"], evidence["incident_type"]) not in self.flagged:
            self.flagged.add((evidence["incident_id"], evidence["incident_type"]))
            check = {"passed": False, "findings": [FLAG]}
        return {"diagnosis": {"severity": contract.severity(evidence), "summary": "s", "root_cause": "r",
                              "evidence": ["a", "b", "c"], "recommended_actions": ["x", "y"]},
                "evidence_metrics": [], "self_check": check, "runbook": None, "usage": {},
                "latency_ms": 20000}

    def now(self):
        return self.clock + timedelta(seconds=1)

    def emit(self, line):
        self.lines.append(line)


fakes, steps, calls, errors = Fakes(), [], [], []
for r, window, p, models in stream:
    fakes.clock = t(p["latency"]["diagnosed_at"])
    event = {"source": "lambda", "detail-type": "Lambda Function Invocation Result - Success",
             "detail": {"requestPayload": {"source": "anomalypulse.pipeline", "detail-type": "WindowReady",
                                           "time": p["latency"]["announced_at"],
                                           "detail": {"windows": [window], "key": "replay"}},
                        "responsePayload": {**models, "key": "replay", "predictions": [p]}}}
    before = fakes.get_state()
    made = len(fakes.calls)
    try:
        out = core.process(json.loads(json.dumps(event)), fakes)
    except Exception as err:  # noqa: BLE001 - every failure is a finding
        errors.append([window["window_start"], repr(err)])
        continue
    steps.append([window["window_start"], r["slot"], out["changes"]])
    state = fakes.get_state()
    for evidence, revised in zip(fakes.calls[made:], fakes.revised[made:]):
        calls.append({"slot": r["slot"], "evidence": evidence, "revised": revised, "window": window,
                      "baseline": state["baseline"],
                      "opened": "opened" in out["changes"],
                      "baseline_windows": [w["window_start"] for w in (before or {}).get("recent_normal", [])]})

records = [record.from_dynamodb(v) for k, v in sorted(fakes.items.items()) if k[0] == "INCIDENT"]
manifests = {r["experiment_id"]: r["manifest"] for r in runs}
for rec in records:
    detected = t(rec["prediction"]["detected_at"])
    health = {"window_start": iso(floor(detected) - MINUTE), "system_p95_latency_ms": 10.0,
              **{f"{s}_{m}": v for s in ("product", "cart", "order", "payment")
                 for m, v in (("request_rate", 10.0), ("error_rate_5xx", 0.0), ("p95_latency_ms", 10.0))}}
    try:
        shown = dashboard.build_dashboard(health, record.to_incident(rec), manifests.get(rec.get("experiment_id")),
                                          detected)["incident"]
        assert shown["prediction"] == rec["prediction"] and shown["diagnosis"] == rec["diagnosis"]
        if rec.get("experiment_id"):
            assert shown["ground_truth"]["experiment_id"] == rec["experiment_id"]
    except Exception as err:  # noqa: BLE001
        errors.append(["read-back " + rec["sk"], repr(err)])

OUT.write_text(json.dumps({
    "fail_first": FAIL_FIRST, "flag_first": FLAG_FIRST, "minutes": len(stream), "errors": errors,
    "largest_item": fakes.largest, "metric_lines": sum(line.get("event") == "incident_diagnosis" for line in fakes.lines),
    "retry_lines": [line for line in fakes.lines if line.get("event") == "incident_retry"], "records": records, "calls": calls, "steps": steps,
    "runs": [{"slot": r["slot"], "experiment_id": r["experiment_id"], "incident_type": r["manifest"]["incident_type"],
              "started_at": r["manifest"]["started_at"], "ended_at": r["manifest"]["ended_at"],
              "activations": r["manifest"].get("activations") or []} for r in runs]}, default=str))
print(f"{len(stream)} minutes, {len(errors)} errors, {len(records)} incidents, {len(fakes.calls)} Bedrock calls "
      f"-> {OUT}")
