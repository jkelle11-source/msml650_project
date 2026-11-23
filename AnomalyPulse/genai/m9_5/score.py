"""Score the stored diagnoses (M9-5): evidence fidelity, hallucination, completeness, the stack-fact slip in
actions, and what the incident store's retries did. No AWS, no Bedrock.

    cd AnomalyPulse
    python -m genai.m9_5.score                  # data/ from fetch.py; writes results/scores.json

Each diagnosis is checked again with today's self-check against the input it was made from
(`diagnosis_input`, as feed.check does), so every record is judged by one set of rules. Only the summary,
root cause and evidence are claims; actions are scored by people (sheet.py).

- A **claim** is the summary, the root cause or one evidence bullet. It is **unsupported** when the
  self-check finds a number, time, service or metric family the input doesn't hold, a number put on a
  metric the bullet doesn't cite, or a bullet citing no input metric (or one the input lacks). Evidence
  fidelity is the share of claims with none of these.
- A **mention** is one service, number, clock time or metric-family word in a claim. It is **hallucinated**
  when it is absent from the input. A real number put on the wrong metric is misattributed, not
  hallucinated: it counts against fidelity only.
- **Salient evidence** is every input metric that moved, by the window builder's own rule
  (incidents.builder._moved), and every affected service. A diagnosis covers a moved metric when an evidence
  bullet cites it, and a service when a claim names it.
- **The capacity slip** (M8-4): an action about a table's capacity that names the Carts or Orders table,
  both on-demand, or payment-service, which has none.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from genai import contract, runbooks, self_check
from incidents import builder

HERE = Path(__file__).parent
DATA, RESULTS = HERE / "data", HERE / "results"
TYPES = ("TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION", "DEPENDENCY_FAILURE", "CASCADING_FAILURE")
CLAIM_FIELDS = ("summary", "root_cause", "evidence")
# Finding kinds that make a claim unsupported, and those that count a mention as absent from the input.
UNSUPPORTED = {"number", "time", "service", "metric", "citation"}
_SERVICE_WORD = re.compile(r"\b(product|cart|order|payment)(?:-service)?\b", re.IGNORECASE)
_MISATTRIBUTED = "is not from the metrics it cites"
# Setting a table's capacity: provisioned capacity or throughput (not Lambda's provisioned concurrency),
# auto-scaling, a capacity mode or limit, or raising capacity. Watching consumed capacity is not one.
_SETTING = re.compile(r"\bprovisioned\b(?! concurrency)|auto-?scal|capacity mode|capacity (limits?|ceilings?)|"
                      r"\b(increase|raise|add)\b[^.;]{0,40}\b(capacity|throughput|RCU|WCU)\b", re.IGNORECASE)
_TABLE = re.compile(r"\bDynamoDB\b|\btables?\b|\bRCU\b|\bWCU\b|\bthroughput\b", re.IGNORECASE)
_ON_DEMAND = re.compile(r"\b(cart|order|payment)(?:-service)?\b|\bCarts\b|\bOrders\b", re.IGNORECASE)


def load(name="records.json"):
    return json.loads((DATA / name).read_text())


def raw(rec):
    """The diagnosis as Bedrock gave it: evidence bullets with the metrics each cites."""
    d = rec["diagnosis"]
    return {**d, "evidence": [{"statement": s, "metrics": list(m)}
                              for s, m in zip(d["evidence"], rec.get("evidence_metrics") or [])]}


def findings(rec):
    """Today's self-check of the stored diagnosis against its stored input (with its runbook, if it had one)."""
    text = runbooks.runbook_for(rec["diagnosis_input"]["incident_type"])[1] if rec.get("runbook") else None
    return contract.check_output_shape(raw(rec)) + self_check.check(rec["diagnosis_input"], raw(rec), runbook=text)


def _field(finding):
    field = self_check._FIELD.match(finding)
    if field:
        return finding[:field.end() - 2]
    bullet = re.match(r"^(evidence\[\d+\]) cites no input metric$", finding)
    return bullet.group(1) if bullet else None


def claims(rec):
    """[(field, text, cited refs or None)] for the summary, root cause and each evidence bullet."""
    d = raw(rec)
    return ([("summary", d["summary"], None), ("root_cause", d["root_cause"], None)]
            + [(f"evidence[{i}]", e["statement"], e["metrics"]) for i, e in enumerate(d["evidence"])])


def mentions(text):
    """Counter of the services, numbers, clock times and metric-family words a claim states."""
    found = Counter()
    found["service"] = len(_SERVICE_WORD.findall(self_check._NOT_SERVICES.sub(" ", text)))
    found["time"] = len(self_check._CLOCK.findall(text))
    found["number"] = len(self_check._numbers(text))
    found["metric"] = sum(bool(re.search(p, text, re.IGNORECASE)) for p in self_check.FAMILIES)
    return found


def score_record(rec):
    """One diagnosis's counts."""
    found = findings(rec)
    inputs = {contract.metric_ref(m["service"], m["metric"]) for m in rec["diagnosis_input"]["metrics"]}
    by_field = defaultdict(list)
    for f in found:
        if _field(f):
            by_field[_field(f)].append(f)
    # "evidence cites X, which is not in the input": every bullet citing X traces to nothing real.
    for field, _, cites in claims(rec):
        if cites is not None and set(cites) - inputs:
            by_field[field].append(f"{field}: cites a metric the input lacks")
    out = {"claims": Counter(), "unsupported": Counter(), "mentions": Counter(), "hallucinated": Counter(),
           "misattributed": 0, "unsupported_kinds": Counter()}
    for field, text, _ in claims(rec):
        part = field.split("[")[0]
        out["claims"][part] += 1
        out["mentions"].update(mentions(text))
        kinds = set()
        for f in by_field[field]:
            k = "citation" if "cites" in f else self_check.kind(f)
            if k in UNSUPPORTED:
                kinds.add(k)
                if _MISATTRIBUTED in f:
                    out["misattributed"] += 1
                elif k != "citation":
                    out["hallucinated"][k] += 1
        if kinds:
            out["unsupported"][part] += 1
            out["unsupported_kinds"].update(kinds)
    out["findings"] = found
    out["completeness"] = completeness(rec)
    out["capacity_slips"] = capacity_slips(rec)
    return out


def completeness(rec):
    """(moved metrics, moved metrics cited, affected services, affected services named in a claim)."""
    inp = rec["diagnosis_input"]
    moved = {contract.metric_ref(m["service"], m["metric"]) for m in inp["metrics"]
             if m["current"] is not None and builder._moved(m["metric"], m["baseline"], m["current"])}
    cited = {r for refs in rec.get("evidence_metrics") or [] for r in refs}
    text = " ".join(t for _, t, _ in claims(rec))
    named = {self_check._SERVICE_NAMES[w.lower()]
             for w in _SERVICE_WORD.findall(self_check._NOT_SERVICES.sub(" ", text))}
    services = set(inp["affected_services"])
    return {"moved": len(moved), "moved_cited": len(moved & cited),
            "services": len(services), "services_named": len(services & named)}


def capacity_slips(rec):
    """The recommended actions that check or change capacity on a table that has none to set."""
    return [a for a in rec["diagnosis"]["recommended_actions"]
            if _SETTING.search(a) and _TABLE.search(a) and _ON_DEMAND.search(a)]


def _rate(part, whole):
    return round(part / whole, 4) if whole else None


def summarize(records):
    """The headline numbers over a group of records."""
    scored = [score_record(r) for r in records]
    claims_, unsupported, mentions_, hallucinated = Counter(), Counter(), Counter(), Counter()
    kinds = Counter()
    for s in scored:
        claims_.update(s["claims"])
        unsupported.update(s["unsupported"])
        mentions_.update(s["mentions"])
        hallucinated.update(s["hallucinated"])
        kinds.update(s["unsupported_kinds"])
    n_claims, n_unsup = sum(claims_.values()), sum(unsupported.values())
    n_mentions, n_hall = sum(mentions_.values()), sum(hallucinated.values())
    comp = [s["completeness"] for s in scored]
    moved, cited = sum(c["moved"] for c in comp), sum(c["moved_cited"] for c in comp)
    with_moved = [c for c in comp if c["moved"]]
    return {
        "diagnoses": len(records),
        "fidelity": {
            "claims": n_claims, "unsupported": n_unsup, "unsupported_rate": _rate(n_unsup, n_claims),
            "evidence_fidelity": _rate(n_claims - n_unsup, n_claims),
            "by_field": {f: {"claims": claims_[f], "unsupported": unsupported[f],
                             "rate": _rate(unsupported[f], claims_[f])} for f in CLAIM_FIELDS},
            "by_kind": dict(kinds),
            "diagnoses_with_unsupported": sum(bool(s["unsupported"]) for s in scored),
            "misattributed_numbers": sum(s["misattributed"] for s in scored),
        },
        "hallucination": {
            "mentions": n_mentions, "hallucinated": n_hall, "rate": _rate(n_hall, n_mentions),
            "by_kind": {k: {"mentions": mentions_[k], "hallucinated": hallucinated[k],
                            "rate": _rate(hallucinated[k], mentions_[k])} for k in ("service", "metric", "number", "time")},
            "diagnoses_with_any": sum(bool(s["hallucinated"]) for s in scored),
        },
        "completeness": {
            "moved_metrics": moved, "moved_cited": cited, "rate": _rate(cited, moved),
            "diagnoses_citing_every_moved_metric": sum(c["moved"] == c["moved_cited"] for c in with_moved),
            "diagnoses_with_a_moved_metric": len(with_moved),
            "services": sum(c["services"] for c in comp),
            "services_named": sum(c["services_named"] for c in comp),
            "services_rate": _rate(sum(c["services_named"] for c in comp), sum(c["services"] for c in comp)),
        },
        "capacity_slips": {"diagnoses": sum(bool(s["capacity_slips"]) for s in scored),
                           "actions": sum(len(s["capacity_slips"]) for s in scored)},
        # The stored diagnosis carries the rubric's severity, so only the stored self-check knows Bedrock's own.
        "severity_disagreements": sum(any(self_check.kind(f) == "severity"
                                          for f in (r.get("self_check") or {}).get("findings") or [])
                                      for r in records),
    }


def retry_summary(lines):
    """How often a retry was kept, and what the kept and dropped ones still found."""
    kept = [line for line in lines if line.get("kept")]
    return {"retries": len(lines), "kept": len(kept), "errors": sum("error" in line for line in lines),
            "flagged_for": dict(Counter(k for line in lines for k in line["flagged"])),
            "kept_still_found": dict(Counter(k for line in kept for k in line.get("found", []))),
            "dropped_found": dict(Counter(k for line in lines if not line.get("kept")
                                          for k in line.get("found", [])))}


def report(records, lines):
    test = [r for r in records if r["phase"] == "test"]
    stress = [r for r in records if r["phase"] == "stress"]
    ids = {r["incident_id"] for r in test}
    return {
        "test": summarize(test),
        "test_by_classified_type": {t: summarize([r for r in test if r["diagnosis_input"]["incident_type"] == t])
                                    for t in TYPES},
        "test_when_the_type_agrees_with_the_run": {
            name: summarize([r for r in test if (r["diagnosis_input"]["incident_type"] == r["labeled_type"]) == agree])
            for name, agree in (("agrees", True), ("disagrees", False))},
        "stress": summarize(stress),
        "retries_test": retry_summary([line for line in lines if line["incident_id"] in ids]),
        "held_back_now": sum(bool(set(self_check.kind(f) for f in (r["self_check"] or {}).get("findings", []))
                                  & {"wording", "citation"}) for r in test),
    }


def runbook_sample(records, results_dir):
    """The comparison's incidents scored both ways: the stored diagnosis, and compare.py's with runbooks
    (scored against the same input, with the runbook's text, so a runbook number is allowed in actions only)."""
    by_id = {r["incident_id"]: r for r in records}
    pairs = [(by_id[p.stem], json.loads(p.read_text())) for p in sorted(results_dir.glob("*.json"))]
    with_ = [{**rec, "diagnosis": on["diagnosis"], "evidence_metrics": on["evidence_metrics"],
              "self_check": on["self_check"], "runbook": on["runbook"]} for rec, on in pairs]
    return {"without": summarize([rec for rec, _ in pairs]), "with": summarize(with_),
            "bedrock_calls": sum(len(on["calls"]) for _, on in pairs)}


def main(argv=None):
    records = load()
    lines = [json.loads(line) for line in (DATA / "retries.jsonl").read_text().splitlines() if line.strip()]
    result = report(records, lines)
    if (RESULTS / "with_runbooks").is_dir():
        result["runbook_sample"] = runbook_sample(records, RESULTS / "with_runbooks")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "scores.json").write_text(json.dumps(result, indent=2) + "\n")
    t = result["test"]
    print(f"test: {t['diagnoses']} diagnoses")
    print(f"  evidence fidelity {t['fidelity']['evidence_fidelity']:.1%} "
          f"({t['fidelity']['unsupported']} of {t['fidelity']['claims']} claims unsupported)")
    print(f"  hallucination {t['hallucination']['rate']:.2%} "
          f"({t['hallucination']['hallucinated']} of {t['hallucination']['mentions']} mentions)")
    print(f"  completeness {t['completeness']['rate']:.1%} of moved metrics cited, "
          f"{t['completeness']['services_rate']:.1%} of affected services named")
    print(f"  capacity slip in {t['capacity_slips']['diagnoses']} diagnoses")
    print(f"written to {RESULTS / 'scores.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
