"""Score replay.py's output: the numbers in Docs/INCIDENT_STORE.md ("Checked on validation") and
Docs/DIAGNOSIS_CONTRACT.md ("Decided in M8-3").

    cd AnomalyPulse
    python3 incidents/replay/analyze.py <out.json>
"""
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from genai.contract import SERVICE_METRICS  # noqa: E402
from incidents import builder  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__.split("\n\n")[1])
d = json.loads(Path(sys.argv[1]).read_text())
records, calls, runs = d["records"], d["calls"], d["runs"]
by_id = {r["experiment_id"]: r for r in runs}
by_slot = {r["slot"]: r for r in runs}


def t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def column(service, metric):
    return f"{service.split('-')[0]}_{metric}"


print(f"{'fail-first replay' if d['fail_first'] else 'replay'}: {d['minutes']} minutes, {len(d['errors'])} errors")
for err in d["errors"][:10]:
    print("  error:", err)

# ---- incidents against the runs (ground truth scores, never builds) --------------------------------
fault_runs = [r for r in runs if r["activations"]]
problems = []
for rec in records:
    run = by_id.get(rec.get("experiment_id"))
    if run is None:
        problems.append(f"{rec['sk']}: no reference")
        continue
    if not run["activations"]:
        problems.append(f"{rec['sk']}: opened on NORMAL run {run['experiment_id']}")
        continue
    first = min(t(a["active_from"]) for a in run["activations"])
    last = max(t(a["active_until"]) for a in run["activations"])
    if not first - timedelta(minutes=2) <= t(rec["sk"]) <= last + timedelta(minutes=3):
        problems.append(f"{rec['sk']}: outside its run's fault")
    if rec["prediction"]["incident_type"] != run["incident_type"]:
        problems.append(f"{rec['sk']}: {rec['prediction']['incident_type']}, run is {run['incident_type']}")
    if rec["status"] != "CLOSED":
        problems.append(f"{rec['sk']}: never closed")
per_run = Counter(rec.get("experiment_id") for rec in records)
problems += [f"{r['experiment_id']}: {per_run[r['experiment_id']]} incidents" for r in fault_runs
             if per_run[r["experiment_id"]] != 1]
print(f"incidents: {len(records)} for {len(fault_runs)} fault runs and {len(runs) - len(fault_runs)} NORMAL runs; "
      f"problems: {len(problems)}")
for p in problems[:10]:
    print("  ", p)

# ---- Bedrock -------------------------------------------------------------------------------------
print(f"Bedrock calls: {len(calls)}; per incident {sorted(Counter(r['bedrock_calls'] for r in records).items())}")
print(f"incidents with a diagnosis: {sum(r['diagnosis'] is not None for r in records)} of {len(records)}; "
      f"with an error left: {sum(r['diagnosis_error'] is not None for r in records)}; "
      f"diagnosis not for the final type: {sum(r['diagnosis_for_type'] != r['prediction']['incident_type'] for r in records)}")
print(f"largest item: {d['largest_item'][0]} bytes ({d['largest_item'][1]}); metric lines: {d['metric_lines']}")
sizes = Counter(len(r["prediction"]["affected_services"]) for r in records)
by_type = {}
for r in records:
    by_type.setdefault(r["prediction"]["incident_type"], Counter())[len(r["prediction"]["affected_services"])] += 1
print(f"affected_services stored at close, services per incident: {sorted(sizes.items())}; by type: "
      + "; ".join(f"{k} {sorted(v.items())}" for k, v in sorted(by_type.items())))

# ---- closing: by the incident's own run, or by a later run's windows --------------------------------
closed_by, opened_slot = Counter(), None
for _, slot, changes in d["steps"]:
    for change in changes:
        if change == "closed":
            closed_by["own run" if slot == opened_slot else "a later run's windows"] += 1
        elif change == "opened":
            opened_slot = slot
print(f"closed by: {dict(closed_by)}")

# ---- baselines at opening ------------------------------------------------------------------------
opens = [c for c in calls if c["opened"]]
ages, own = [], 0
for c in opens:
    wins, started = c["baseline_windows"], t(c["evidence"]["started_at"])
    if wins:
        ages.append((started - t(wins[0])).total_seconds() / 60)
        own += all(t(w) >= t(by_slot[c["slot"]]["started_at"]) for w in wins)
ages.sort()
print(f"baselines: windows per opening {sorted(Counter(len(c['baseline_windows']) for c in opens).items())}; "
      f"all from the incident's own run {own} of {len(opens)}; oldest window before started_at: "
      f"median {ages[len(ages) // 2]:.0f} min, max {ages[-1]:.0f} min" if ages else "baselines: none")

# ---- the 10-metric cap ---------------------------------------------------------------------------
at_cap = cut_context = kept_signal = bare = 0
cut_moved = Counter()
for c in calls:
    ev, window, base = c["evidence"], c["window"], c["baseline"] or {}
    sent = {(m["service"], m["metric"]) for m in ev["metrics"]}
    at_cap += len(ev["metrics"]) == builder.MAX_METRICS
    moved = set()
    for service in ev["affected_services"]:
        for metric in SERVICE_METRICS[service]:
            current, b = window.get(column(service, metric)), base.get(column(service, metric))
            if current is not None and builder._moved(metric, b if isinstance(b, (int, float)) else None, current):
                moved.add((service, metric))
        cut_context += sum((service, m) not in sent and (service, m) not in moved for m in builder.STANDARD)
        bare += not any(s == service for s, _ in sent)
    lost = moved - sent
    for _, metric in lost:
        cut_moved[metric] += 1
    signals = builder.TYPE_SIGNALS.get(ev["incident_type"], ())
    kept_signal += bool(lost) and any(m in signals and (s, m) not in moved for s, m in sent)
print(f"metrics: {at_cap} of {len(calls)} inputs at the cap; unmoved standard pairs cut {cut_context}; "
      f"moved metrics cut {dict(cut_moved.most_common())}")
print(f"  inputs that kept an unmoved type signal and cut a moved metric: {kept_signal}; "
      f"affected services sent with no metric: {bare}")
