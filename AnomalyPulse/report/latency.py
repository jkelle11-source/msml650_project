"""M9-6's latency chain on the live path: from a fault's real start to its diagnosis stored, split into the
steps that make it up. Offline: it reads M9-5's incident records and the manifests report/fetch.py exported.

    cd AnomalyPulse
    python -m report.latency

Writes report/results/latency_m9_6.json and prints a summary. The steps, for each fault run's detection:

- window: the fault's start to the end of the first incident minute. The live path judges a minute only once it
  ends (plan section 16), so this is the aggregation window's share. It splits into the rest of the minute the
  fault started in, and any whole minutes after that before one was flagged.
- pipeline_wait: that minute's end to its classification (the record's latency.window_end_to_diagnosis_s). A
  minute is classified only once its CloudWatch snapshot is in, which waits 2 minutes for CloudWatch to settle,
  and the window builder runs at about :45 (Docs/PIPELINE.md, "When a minute's window is ready").
- diagnosis: the classification to the first diagnosis stored (latency.diagnosis_to_stored_s), of which
  bedrock is the kept Bedrock call (latency.bedrock_ms).
- end_to_end: the fault's start to the first diagnosis stored. The console then shows it at its next poll,
  within 15 s, which no record measures.

Rules:
- A run's fault start is its first fault's active_from in the manifest. M9-2 starts its clock at the first labeled
  window instead, which for DB throttling is label_from, after the injector has started.
- An incident whose first minute ends at or before the fault's start, and is still open when it starts, covered
  the fault from just before it: the run is "early", detected, but its steps are left out of the timings.
- Otherwise a run's detection is its first incident whose first minute ends after the fault's start.
- An incident that opened and closed before the fault's start is a false incident, as is any on a NORMAL run.
- A fault run with no incident open at or after its start is missed.
- Every timing is runbooks off: all of M9-1's runs predate the runbooks deploy (2026-11-23).
- The test set and the stress slice are summarized apart.
"""
import hashlib
import json
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "genai" / "m9_5" / "data" / "records.json"
MANIFESTS = Path(__file__).parent / "data" / "manifests.json"
RESULTS = Path(__file__).parent / "results" / "latency_m9_6.json"
STEPS = ("window", "window_onset_minute", "window_extra_minutes", "pipeline_wait", "diagnosis", "bedrock",
         "end_to_end")
CHAIN = ("window", "pipeline_wait", "diagnosis")       # the steps end_to_end is the sum of
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


def first_minute_end(incident):
    return parse(incident["prediction"]["started_at"]) + timedelta(minutes=1)


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
        "diagnosis": diagnosis,
        "bedrock": round(lat["bedrock_ms"] / 1000, 3),
        "end_to_end": round(window + pipeline_wait + diagnosis, 3),
    }


def runs(manifests, records):
    """One row per run: what happened to it and, for a detected fault, its chain."""
    by_run = {}
    for r in records:
        by_run.setdefault(r.get("experiment_id"), []).append(r)
    rows = []
    for m in manifests:
        incidents = sorted(by_run.get(m["experiment_id"], []), key=lambda r: r["prediction"]["started_at"])
        row = {"experiment_id": m["experiment_id"], "slot": m["slot"], "phase": m["phase"],
               "labeled_type": m["labeled_type"], "incidents": len(incidents), "false_incidents": 0}
        onset = fault_start(m)
        if onset is None:                                  # a NORMAL run: any incident is a false one
            row.update(outcome="normal", false_incidents=len(incidents))
            rows.append(row)
            continue
        before = [i for i in incidents if first_minute_end(i) <= onset]
        covering = [i for i in before if not i.get("ended_at") or parse(i["ended_at"]) > onset]
        after = [i for i in incidents if first_minute_end(i) > onset]
        row["false_incidents"] = len(before) - len(covering)
        row["fault_start"] = onset.isoformat().replace("+00:00", "Z")
        if covering:
            first = covering[0]
            row.update(outcome="early", incident_id=first["incident_id"],
                       shown_type=first["prediction"]["incident_type"],
                       opened_before_s=round((onset - first_minute_end(first)).total_seconds(), 3))
        elif not after:
            row["outcome"] = "missed"
        else:
            first = after[0]
            row.update(outcome="detected", incident_id=first["incident_id"],
                       shown_type=first["prediction"]["incident_type"], steps=chain(onset, first))
        rows.append(row)
    return rows


def stats(values):
    values = sorted(values)
    if not values:
        return {"n": 0}

    def pct(q):
        return round(values[min(len(values) - 1, int(q * len(values)))], 1)
    return {"n": len(values), "median": round(statistics.median(values), 1), "mean": round(statistics.fmean(values), 1),
            "p90": pct(0.9), "min": round(values[0], 1), "max": round(values[-1], 1)}


def summarize(rows):
    detected = [r for r in rows if r["outcome"] == "detected"]
    steps = {s: stats([r["steps"][s] for r in detected]) for s in STEPS}
    total = sum(steps[s]["mean"] for s in CHAIN) if detected else 0
    return {
        "runs": len(rows),
        "fault_runs": sum(r["outcome"] != "normal" for r in rows),
        "detected": len(detected),
        "early": [{"experiment_id": r["experiment_id"], "slot": r["slot"], "opened_before_s": r["opened_before_s"]}
                  for r in rows if r["outcome"] == "early"],
        "missed": [r["experiment_id"] for r in rows if r["outcome"] == "missed"],
        "false_incidents": {"before_fault_start": sum(r["false_incidents"] for r in rows if r["outcome"] != "normal"),
                            "normal_runs": sum(r["false_incidents"] for r in rows if r["outcome"] == "normal")},
        "steps_s": steps,
        # Shares of the mean end-to-end time: means add up, medians don't.
        "share_of_mean": {s: round(steps[s]["mean"] / total, 3) for s in CHAIN} if total else {},
        "by_type": {t: {s: stats([r["steps"][s] for r in detected if r["labeled_type"] == t])
                        for s in ("window", "pipeline_wait", "diagnosis", "end_to_end")} for t in TYPES},
    }


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build(manifests, records):
    rows = runs(manifests, records)
    return {"summary": {p: summarize([r for r in rows if r["phase"] == p]) for p in PHASES}, "runs": rows}


def main(argv=None):
    manifests = json.loads(MANIFESTS.read_text())
    records = json.loads(RECORDS.read_text())
    result = build(manifests, records)
    result = {"provenance": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "git_commit": git_commit(), "runbooks": RUNBOOKS,
                             "inputs": {str(p.relative_to(ROOT)): sha256(p) for p in (RECORDS, MANIFESTS)},
                             "rules": __doc__.split("Rules:\n", 1)[1].strip()},
              **result}
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(result, indent=1) + "\n")
    test = result["summary"]["test"]
    s = test["steps_s"]
    print(f"test set: {test['fault_runs']} fault runs: {test['detected']} timed, {len(test['early'])} early "
          f"(open just before the fault's start), {len(test['missed'])} with no incident; "
          f"false incidents: {test['false_incidents']}")
    for step in STEPS:
        print(f"  {step:22s} median {s[step]['median']:6.1f} s   p90 {s[step]['p90']:6.1f} s")
    print(f"  shares of the mean: {test['share_of_mean']}")
    print(f"written to {RESULTS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
