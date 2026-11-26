"""M9-6's latency chain on the live path: from a fault's real start to its diagnosis stored, split into the
steps that make it up. Offline: it reads M9-5's incident records and retry log lines, and the manifests
report/fetch.py exported.

    cd AnomalyPulse
    python -m report.latency

Writes report/results/latency_m9_6.json and prints a summary. The steps, for each fault run's detection:

- window: the fault's start to the end of the incident's first minute. The live path judges a minute only once it
  ends (plan section 16), so this is the aggregation window's share. It splits into the rest of the minute the
  fault started in, and the whole minute after it when that minute was the first one flagged.
- pipeline_wait: that minute's end to its classification (the record's latency.window_end_to_diagnosis_s). It
  splits in two (Docs/PIPELINE.md, "When a minute's window is ready"):
  - cloudwatch_settle: a fixed 120 s, set by configuration, not measured. The metrics extractor reads a minute
    only once 2 more minutes have passed, so CloudWatch has settled it (METRICS_LAG_MINUTES = 2,
    infra/shared/template.yaml), and the window needs that snapshot.
  - build_and_classify: the rest. Waiting for the window builder's next run (once a minute, at about :45),
    building the full window, and classifying it.
- diagnosis: the classification to the first diagnosis stored (latency.diagnosis_to_stored_s), of which
  bedrock is the kept Bedrock call (latency.bedrock_ms).
- end_to_end: the fault's start to the first diagnosis stored. The console then shows it at its next poll,
  within 15 s, which no record measures, or about a minute later if that diagnosis was held back.

Rules:
- A run's fault start is its first fault's active_from in the manifest. M9-2 starts its clock at the first labeled
  window instead, which for DB throttling is label_from, after the injector has started.
- An incident belongs to the run that started last at or before its first minute began. The record's own
  experiment_id is not used: the store reads it from the experiment lock when the incident opens, about 4
  minutes after its first minute starts, by when a short fault's run has often ended and the next one holds it.
- An incident open when a fault starts, whose first minute ended at or before the start, covered the fault from
  before it, whichever run it belongs to: the run is "covered", but its steps are left out of the timings.
- Otherwise a run's detection is its first own incident whose first minute ends after the fault's start.
- A run's own incident that opened and closed before its fault's start is a false incident, as is any incident
  of a NORMAL run.
- A fault run that is neither covered nor detected is missed.
- A detection's first diagnosis was held back when the first retry the store logged for its incident was for
  wording or citation findings. The console showed it after the retry, on the next minute.
- Every timing is runbooks off: all of M9-1's runs predate the runbooks deploy (2026-11-23).
- The test set and the stress slice are summarized apart.
"""
import bisect
import hashlib
import json
import math
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "genai" / "m9_5" / "data" / "records.json"
RETRIES = ROOT / "genai" / "m9_5" / "data" / "retries.jsonl"
MANIFESTS = Path(__file__).parent / "data" / "manifests.json"
RESULTS = Path(__file__).parent / "results" / "latency_m9_6.json"
CLOUDWATCH_SETTLE_S = 120.0     # METRICS_LAG_MINUTES (2) x 60
HOLD_KINDS = ("wording", "citation")    # incidents.record.HOLD_KINDS: findings that hold a diagnosis back
STEPS = ("window", "window_onset_minute", "window_extra_minutes", "pipeline_wait", "cloudwatch_settle",
         "build_and_classify", "diagnosis", "bedrock", "end_to_end")
CHAIN = ("window", "cloudwatch_settle", "build_and_classify", "diagnosis")   # the steps end_to_end is the sum of
TYPES = ("TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION", "DEPENDENCY_FAILURE", "CASCADING_FAILURE")
PHASES = ("test", "stress")
RUNBOOKS = "off: every M9-1 run predates the runbooks deploy (2026-11-23T05:49Z)"


def parse(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def minute_end(moment):
    return moment.replace(second=0, microsecond=0) + timedelta(minutes=1)


def fault_start(manifest):
    starts = [parse(f["active_from"]) for f in manifest["faults"] if f.get("active_from")]
    return min(starts) if starts else None


def first_minute(incident):
    return parse(incident["prediction"]["started_at"])


def first_minute_end(incident):
    return first_minute(incident) + timedelta(minutes=1)


def open_at(incident, moment):
    return not incident.get("ended_at") or parse(incident["ended_at"]) > moment


def chain(onset, incident):
    """The steps, in seconds, from a fault's start to its detection's first diagnosis stored."""
    end = first_minute_end(incident)
    window = (end - onset).total_seconds()
    onset_minute = (minute_end(onset) - onset).total_seconds()
    lat = incident["latency"]
    pipeline_wait = (parse(incident["prediction"]["detected_at"]) - end).total_seconds()
    diagnosis = lat["diagnosis_to_stored_s"]
    return {
        "window": round(window, 3),
        "window_onset_minute": round(min(onset_minute, window), 3),
        "window_extra_minutes": round(max(window - onset_minute, 0.0), 3),
        "pipeline_wait": round(pipeline_wait, 3),
        "cloudwatch_settle": CLOUDWATCH_SETTLE_S,
        "build_and_classify": round(pipeline_wait - CLOUDWATCH_SETTLE_S, 3),
        "diagnosis": diagnosis,
        "bedrock": round(lat["bedrock_ms"] / 1000, 3),
        "end_to_end": round(window + pipeline_wait + diagnosis, 3),
    }


def owners(manifests, records):
    """incident_id -> the experiment_id of the run that started last at or before the incident's first minute
    began (None before the first run)."""
    starts = sorted((parse(m["started_at"]), m["experiment_id"]) for m in manifests)
    times = [t for t, _ in starts]
    out = {}
    for r in records:
        k = bisect.bisect_right(times, first_minute(r)) - 1
        out[r["incident_id"]] = starts[k][1] if k >= 0 else None
    return out


def held_back(retries):
    """The incident_ids whose first stored diagnosis was held back: the first retry the store logged for the
    incident (lines are in log order) was for wording or citation findings. A later type's held diagnosis is
    not counted: the first one was shown on time."""
    first = {}
    for line in retries:
        first.setdefault(line["incident_id"], line)
    return {iid for iid, line in first.items() if set(line.get("flagged") or ()) & set(HOLD_KINDS)}


def runs(manifests, records, retries=()):
    """One row per run: what happened to it and, for a detected fault, its chain."""
    owner = owners(manifests, records)
    held = held_back(retries)
    ordered = sorted(records, key=first_minute)
    by_run = {}
    for r in ordered:
        by_run.setdefault(owner[r["incident_id"]], []).append(r)
    rows = []
    for m in manifests:
        eid = m["experiment_id"]
        incidents = by_run.get(eid, [])
        row = {"experiment_id": eid, "slot": m["slot"], "phase": m["phase"], "labeled_type": m["labeled_type"],
               "incidents": len(incidents), "false_incidents": 0,
               "tagged_to_another_run": sum(i.get("experiment_id") != eid for i in incidents)}
        onset = fault_start(m)
        if onset is None:                                  # a NORMAL run: any incident of its own is a false one
            row.update(outcome="normal", false_incidents=len(incidents))
            rows.append(row)
            continue
        covering = [i for i in ordered if first_minute_end(i) <= onset and open_at(i, onset)]
        before = [i for i in incidents if first_minute_end(i) <= onset and i not in covering]
        after = [i for i in incidents if first_minute_end(i) > onset]
        row["false_incidents"] = len(before)
        row["fault_start"] = onset.isoformat().replace("+00:00", "Z")
        if covering:
            first = covering[0]
            row.update(outcome="covered", incident_id=first["incident_id"],
                       shown_type=first["prediction"]["incident_type"],
                       incident_from="this run" if owner[first["incident_id"]] == eid else "an earlier run",
                       opened_before_s=round((onset - first_minute_end(first)).total_seconds(), 3))
        elif not after:
            row["outcome"] = "missed"
        else:
            first = after[0]
            row.update(outcome="detected", incident_id=first["incident_id"],
                       shown_type=first["prediction"]["incident_type"], steps=chain(onset, first),
                       held_back=first["incident_id"] in held)
        rows.append(row)
    return rows


def stats(values):
    values = sorted(values)
    if not values:
        return {"n": 0}

    def pct(q):     # nearest rank: the smallest value at least a share q of the runs are within
        return round(values[max(0, math.ceil(q * len(values)) - 1)], 1)
    return {"n": len(values), "median": round(statistics.median(values), 1), "mean": round(statistics.fmean(values), 1),
            "p90": pct(0.9), "min": round(values[0], 1), "max": round(values[-1], 1)}


def extra_minute(rows):
    """How many detections waited for the minute after the fault's first one, by how much of that first minute
    the fault was active: at least half (30 s or more) or less."""
    out = {}
    for name, keep in (("onset_minute_half_or_more", lambda s: s >= 30), ("onset_minute_under_half", lambda s: s < 30)):
        mine = [r for r in rows if keep(r["steps"]["window_onset_minute"])]
        out[name] = {"runs": len(mine), "extra_minute": sum(r["steps"]["window_extra_minutes"] > 0 for r in mine)}
    return out


def summarize(rows):
    detected = [r for r in rows if r["outcome"] == "detected"]
    steps = {s: stats([r["steps"][s] for r in detected]) for s in STEPS}
    total = sum(steps[s]["mean"] for s in CHAIN) if detected else 0
    by_type = {}
    for t in TYPES:
        mine = [r for r in detected if r["labeled_type"] == t]
        by_type[t] = {s: stats([r["steps"][s] for r in mine]) for s in ("window", "pipeline_wait", "diagnosis",
                                                                         "end_to_end")}
        by_type[t]["extra_minute"] = sum(r["steps"]["window_extra_minutes"] > 0 for r in mine)
    return {
        "runs": len(rows),
        "fault_runs": sum(r["outcome"] != "normal" for r in rows),
        "detected": len(detected),
        "covered": [{"experiment_id": r["experiment_id"], "slot": r["slot"], "incident_id": r["incident_id"],
                     "incident_from": r["incident_from"], "opened_before_s": r["opened_before_s"]}
                    for r in rows if r["outcome"] == "covered"],
        "missed": [r["experiment_id"] for r in rows if r["outcome"] == "missed"],
        "false_incidents": {"before_fault_start": sum(r["false_incidents"] for r in rows if r["outcome"] != "normal"),
                            "normal_runs": sum(r["false_incidents"] for r in rows if r["outcome"] == "normal")},
        "tagged_to_another_run": sum(r["tagged_to_another_run"] for r in rows),
        "held_back": sum(r["held_back"] for r in detected),
        "extra_minute": extra_minute(detected),
        "steps_s": steps,
        # Shares of the mean end-to-end time: means add up, medians don't.
        "share_of_mean": {s: round(steps[s]["mean"] / total, 3) for s in CHAIN} if total else {},
        "by_type": by_type,
    }


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def git_state():
    """The commit, and whether any tracked file other than this script's output differed from it."""
    commit, status = git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=no")
    if commit is None or status is None:
        return {"git_commit": None, "git_dirty": None}
    out = RESULTS.relative_to(ROOT.parent).as_posix()
    changed = [line[3:] for line in status.splitlines() if line[3:] != out]
    return {"git_commit": commit.strip(), "git_dirty": bool(changed)}


def build(manifests, records, retries=()):
    rows = runs(manifests, records, retries)
    return {"summary": {p: summarize([r for r in rows if r["phase"] == p]) for p in PHASES}, "runs": rows}


def main(argv=None):
    manifests = json.loads(MANIFESTS.read_text())
    records = json.loads(RECORDS.read_text())
    retries = [json.loads(line) for line in RETRIES.read_text().splitlines() if line.strip()]
    result = build(manifests, records, retries)
    result = {"provenance": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             **git_state(), "runbooks": RUNBOOKS,
                             "cloudwatch_settle_s": CLOUDWATCH_SETTLE_S,
                             "inputs": {str(p.relative_to(ROOT)): sha256(p) for p in (RECORDS, RETRIES, MANIFESTS)},
                             "rules": __doc__.split("Rules:\n", 1)[1].strip()},
              **result}
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(result, indent=1) + "\n")
    test = result["summary"]["test"]
    s = test["steps_s"]
    print(f"test set: {test['fault_runs']} fault runs: {test['detected']} timed, {len(test['covered'])} covered "
          f"(an incident already open at the fault's start), {len(test['missed'])} missed; "
          f"false incidents: {test['false_incidents']}")
    for step in STEPS:
        print(f"  {step:22s} median {s[step]['median']:6.1f} s   p90 {s[step]['p90']:6.1f} s")
    print(f"  shares of the mean: {test['share_of_mean']}")
    print(f"written to {RESULTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
