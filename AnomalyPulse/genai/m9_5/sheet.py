"""The blind actionability sheet (M9-5): each sampled incident's recommended actions with and without
runbooks, as lists A and B in a shuffled order, for people to score; then the scored sheets unblinded.

    cd AnomalyPulse
    python -m genai.m9_5.sheet build              # results/blind/sheet.md for the raters, key.json beside it
    python -m genai.m9_5.sheet score <sheet.md>...   # each rater's filled copy -> results/blind/scores.json

Each rater fills their own copy without seeing key.json or the runbooks: for each incident the better list
(A, B or tie), and each list 0 or 1 on Correct and Safe (M6-2's criteria; Specific and Ordered left out, as
both lists scored full marks on them in M6-2). Telling someone to check or raise capacity on the Carts or
Orders table, or on a payment-service table, is Correct 0 (M8-4).
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

from genai.m9_5 import compare, score

BLIND = score.RESULTS / "blind"
SEED = 20261120
SIDES = ("with", "without")

HEADER = """# Blind scoring: recommended actions (M9-5)

For each incident there are two lists of recommended actions, **A** and **B**, for the same incident and the same
metrics. Don't try to guess where the lists came from; judge only what they say. Fill in your own copy:

- **Better:** write A, B or tie after "Better:". Pick the list you'd rather follow as the on-call engineer.
- **Correct** (0 or 1): 1 if the list contains no false fact about this stack (see the facts below): for example a
  table on a service that has none, checking or raising capacity on an on-demand table, a setting that doesn't
  exist.
- **Safe** (0 or 1): 1 if nothing destructive or irreversible comes before something has shown it's needed (a
  rollback, a restart, a capacity change on a guess).

## Stack facts

- Four services (product, cart, order, payment), each a Lambda (`anomalypulse-<service>`): Python 3.13, arm64,
  1,769 MB (one vCPU), 10 s timeout. The account allows 1,000 concurrent Lambda executions, shared by all functions.
- Products table: provisioned at 200 RCU / 5 WCU per second; `GET /products` scans it. Carts and Orders tables:
  on-demand (no capacity to raise). **Payment has no table.**
- Product, Cart and Order retry DynamoDB calls in the SDK (7 attempts in all); retries are inside `db_latency_ms`.
- Order calls Payment synchronously on every `POST /orders` (2 s connect timeout, 6 s read timeout, no retries);
  failures count in `dependency_error_count`.
- `lambda_duration_ms` is handler CPU time, not wall-clock time. API Gateway (REST, stage `dev`) fronts all four
  services.
"""


def _number(value):
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def incidents():
    """[(stored record, with-runbooks result)] for the comparison's sample, in incident order."""
    by_id = {r["incident_id"]: r for r in score.load()}
    out = []
    for rec in compare.sample(list(by_id.values())):
        path = compare.OUT / f"{rec['incident_id']}.json"
        if not path.exists():
            raise SystemExit(f"{path} is missing: run genai.m9_5.compare first")
        out.append((rec, json.loads(path.read_text())))
    return out


def build():
    rng, parts, key = random.Random(SEED), [HEADER], []
    for n, (rec, on) in enumerate(incidents(), 1):
        inp = rec["diagnosis_input"]
        lists = {"with": on["diagnosis"]["recommended_actions"], "without": rec["diagnosis"]["recommended_actions"]}
        a = rng.choice(SIDES)
        order = (a, "without" if a == "with" else "with")
        key.append({"n": n, "incident_id": rec["incident_id"], "A": order[0], "B": order[1]})
        metrics = "; ".join(f"{m['service']} {m['metric']} {_number(m['baseline'])} → {_number(m['current'])} "
                            f"{m['unit']}" for m in inp["metrics"])
        parts.append(f"## Incident {n}: {inp['incident_type']}\n\n"
                     f"Affected: {', '.join(inp['affected_services'])}. Metrics (baseline → current): {metrics}.\n")
        for label, side in zip("AB", order):
            parts.append(f"**{label}**\n\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(lists[side], 1)) + "\n")
        parts.append("| | Correct | Safe |\n|---|---|---|\n| A | | |\n| B | | |\n\n**Better:** \n")
    BLIND.mkdir(parents=True, exist_ok=True)
    (BLIND / "sheet.md").write_text("\n".join(parts))
    (BLIND / "key.json").write_text(json.dumps(key, indent=1) + "\n")
    print(f"{len(key)} incidents: {BLIND / 'sheet.md'} for the raters, {BLIND / 'key.json'} kept from them")


_INCIDENT = re.compile(r"^## Incident (\d+):", re.MULTILINE)
_ROW = re.compile(r"^\|\s*([AB])\s*\|\s*([01])\s*\|\s*([01])\s*\|", re.MULTILINE)
_BETTER = re.compile(r"\*\*Better:\s*(?:\*\*)?\s*(A|B|tie)\b", re.IGNORECASE)   # "**Better:** B" or "**Better: B**"


def parse(text):
    """{n: {"A": (correct, safe), "B": (correct, safe), "better": "A"|"B"|"tie"}} from a filled sheet.
    Raises ValueError naming the first incident left blank."""
    starts = list(_INCIDENT.finditer(text))
    out = {}
    for i, m in enumerate(starts):
        block = text[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        rows = {side: (int(c), int(s)) for side, c, s in _ROW.findall(block)}
        better = _BETTER.search(block)
        if set(rows) != {"A", "B"} or not better:
            raise ValueError(f"incident {m.group(1)} isn't filled in")
        b = better.group(1)
        out[int(m.group(1))] = {**rows, "better": "tie" if b.lower() == "tie" else b.upper()}
    return out


def unblind(scored, key):
    """Per incident, the scores by side ("with"/"without") and which side was picked."""
    out = []
    for k in key:
        s = scored[k["n"]]
        side = {"A": k["A"], "B": k["B"]}
        out.append({"incident_id": k["incident_id"], "better": side.get(s["better"], "tie"),
                    **{side[l]: {"correct": s[l][0], "safe": s[l][1]} for l in "AB"}})
    return out


def tally(rows):
    picks = {p: sum(r["better"] == p for r in rows) for p in (*SIDES, "tie")}
    return {"incidents": len(rows), "better": picks,
            **{side: {c: sum(r[side][c] for r in rows) for c in ("correct", "safe")} for side in SIDES}}


def score_sheets(paths):
    key = json.loads((BLIND / "key.json").read_text())
    raters = {Path(p).stem: unblind(parse(Path(p).read_text()), key) for p in paths}
    result = {"raters": {name: tally(rows) for name, rows in raters.items()}}
    if len(raters) > 1:
        names = list(raters)
        rows = list(zip(*raters.values()))
        result["agreement"] = {
            "better": sum(len({r["better"] for r in group}) == 1 for group in rows),
            **{f"{side}_{c}": sum(len({r[side][c] for r in group}) == 1 for group in rows)
               for side in SIDES for c in ("correct", "safe")},
            "of": len(rows), "raters": names}
    result["incidents"] = {name: rows for name, rows in raters.items()}
    (BLIND / "scores.json").write_text(json.dumps(result, indent=1) + "\n")
    for name, t in result["raters"].items():
        print(f"{name}: better {t['better']}; with runbooks correct {t['with']['correct']}, safe {t['with']['safe']}; "
              f"without correct {t['without']['correct']}, safe {t['without']['safe']} (of {t['incidents']})")
    if "agreement" in result:
        print(f"agreement: {result['agreement']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    s = sub.add_parser("score")
    s.add_argument("sheets", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "build":
        build()
    else:
        score_sheets(args.sheets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
