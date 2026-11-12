"""What the rest of the project plan will cost on AWS (M9-4): November 2026 and December. Pure: reads the inputs
cost/collect.py wrote, computes everything here, and prints the breakdown.

    python3 -m cost.estimate [--inputs cost/inputs]                  # run from AnomalyPulse/

The method (cost/README.md):
  1. Measured usage per run, by kind and band: October's runs, with CloudWatch's 5-minute usage attributed to each
     run's window (inputs/kinds.json). It reproduces October's bill to within about 1%.
  2. The test set built run by run from simulator/integrity.py: its bands (each fault type's usage fitted on its two
     training bands, then evaluated at the test and stress bands' midpoints, since intensity is drawn uniformly
     inside a band), its endpoint mix, its 2- and 12-minute faults, confounders and cascades.
  3. Priced at the account's own bill lines, with this month's free allowances subtracted once, net of what
     November has already used. The pipeline's and Bedrock's per-run costs come from Nov 11's live batch.
  4. Low / expected / high from the uncertain inputs (SCENARIOS).
"""
import argparse
import itertools
import json
from dataclasses import dataclass
from pathlib import Path

# ---- prices (each matches the account's bill lines; check_prices compares them) ----
P_API = 3.5e-6            # API Gateway REST, per request
P_GBS = 1.33334e-5        # Lambda arm64, per GB-second
P_LREQ = 2e-7             # Lambda, per request
P_LOG_GB = 0.50           # CloudWatch vended logs, per GB
P_S3_T1, P_S3_T2 = 5e-6, 4e-7          # S3 PUT/LIST-class and GET-class requests
P_WRU = 0.625e-6          # DynamoDB on-demand write request unit
P_SQS = 0.40e-6           # SQS standard request
P_EB = 1e-6               # EventBridge custom event, per 64 KB chunk
P_CWREQ = 1e-5            # CloudWatch API request
P_RUNNER_H = 0.0723 + 0.005            # c7g.large + its public IPv4 address, per hour
P_RCU_H = 0.00013         # provisioned read capacity unit, per hour
P_GP3_GB_MONTH = 0.0796   # gp3 storage
FREE = {"gbs": 400_000, "lreq": 1_000_000, "log_gb": 5.0, "sqs": 1_000_000, "cwreq": 1_000_000}   # always free, monthly
S3, SQS = "Amazon Simple Storage Service", "Amazon Simple Queue Service"                          # Cost Explorer service names

# ---- the plan (Milestones/M09, M10; simulator/integrity.py) ----
BASELINE_S, FAULT_S, RECOVERY_S = 180, 300, 120
TRAIN_MIX, TEST_MIX = (60, 15, 10, 10, 5), (40, 25, 15, 10, 10)   # GET /products, /products/{id}, POST /cart, GET /orders/{id}, POST /orders
TEST_BANDS = {"spike": [(4, 6), (8, 10)], "db": [(2.5, 3.5), (4.5, 5.5)], "lam": [(500, 800), (1100, 1400)], "pay": [(1500, 2500), (3500, 4500)]}
TRAIN_BANDS = {"spike": [(2, 4), (6, 8)], "db": [(1.5, 2.5), (3.5, 4.5)], "lam": [(200, 500), (800, 1100)], "pay": [(500, 1500), (2500, 3500)]}
STRESS_BANDS = {"spike": (10, 12), "lam": (1500, 2000), "db": (6, 8), "pay": (5000, 5500)}
TARGETS = ("product-service", "cart-service", "order-service", "payment-service")
TARGET_FN = {"product-service": "product", "cart-service": "cart", "order-service": "order", "payment-service": "payment"}
PAIRS = (("spike", "lam"), ("spike", "pay"), ("db", "lam"), ("lam", "pay"))       # CONFOUNDER_PAIRS
INJECTOR = {"spike": "traffic-spike", "db": "db-throttling", "lam": "lambda-degradation", "pay": "payment-failure"}
SERVICE_FNS = ("product", "order", "payment", "cart", "telemetry-shipper")
PIPELINE_FNS = ("forwarder", "aggregator", "diagnosis", "incidents", "explain")
NOV11_RUNS = 7            # exp-20261111T025839Z-3f9e and the six m85 runs (Docs/DASHBOARD_CONTRACT.md, "The live run")
NOV11_BEDROCK_CALLS = 10  # their incident records' bedrock_calls: 1 + 2 + 1 + 1 + 2 + 3
DEC1 = 30 * 24            # hours from Nov 1 00:00 to Dec 1 00:00 UTC

SCENARIOS = {
    # f_prod: the test mix's effect on Product's cost, between all of it in GET /products scans (40/60) and the same
    #   cost per invocation (65/75). usage_scale: the extrapolation to bands no run has used yet. The Nov 11 backtest
    #   (training bands) matched within 4% on requests and 1% on GB-s, but it can't check the test bands.
    # calls: Bedrock calls per incident (single fault, cascade, confounder); live so far 1.43 and 2.5.
    # runbook_calls: M9-5's with/without-runbooks comparison, from a sample of 30 to every incident.
    "low": dict(f_prod=40 / 60, usage_scale=0.85, validation=8, reruns=1, calls=(1.1, 2.0, 1.5), call_cost=0.025,
                runbook_calls=30, m10_sessions=3, idle_tail_h=1.0, batches=5, analysis=0.2),
    "expected": dict(f_prod=(40 / 60 + 65 / 75) / 2, usage_scale=1.0, validation=16, reruns=4, calls=(1.45, 2.5, 2.0),
                     call_cost=None, runbook_calls=50, m10_sessions=4, idle_tail_h=3.5, batches=6, analysis=0.5),
    "high": dict(f_prod=65 / 75, usage_scale=1.15, validation=30, reruns=15, calls=(2.0, 4.0, 3.0), call_cost=0.035,
                 runbook_calls=315, m10_sessions=6, idle_tail_h=7.0, batches=8, analysis=2.0),
}


@dataclass
class Inputs:
    kinds: list
    ce: dict
    record_types: dict
    lambdas: dict
    oct_attempts: int

    @classmethod
    def load(cls, folder):
        folder = Path(folder)
        attempts = [json.loads(line) for line in open(folder / "attempts.jsonl")]
        return cls(kinds=json.load(open(folder / "kinds.json")), ce=json.load(open(folder / "ce.json")),
                   record_types=json.load(open(folder / "record_types.json")), lambdas=json.load(open(folder / "lambda.json")),
                   oct_attempts=sum(1 for r in attempts if "usage" in r))

    def kind(self, kind, band=None):
        for r in self.kinds:
            if r["kind"] == kind and (band is None or (r["band"] == band if isinstance(band, str) else tuple(r["band"] or ()) == band)):
                return r
        raise KeyError((kind, band))

    def qty(self, period, usage_type, service=None):
        """Usage quantity; `service` (a prefix) is needed where usage types repeat across services (S3 and SQS
        both have USE2-Requests-Tier1)."""
        return sum(r["qty"] for r in self.ce[period]
                   if r["usage_type"] == usage_type and (service is None or r["service"].startswith(service)))

    def cost(self, period, usage_type=None, service=None):
        return sum(r["cost"] for r in self.ce[period] if (usage_type is None or r["usage_type"] == usage_type)
                   and (service is None or r["service"].startswith(service)))


def mid(band):
    return (band[0] + band[1]) / 2


def line(x1, y1, x2, y2):
    slope = (y2 - y1) / (x2 - x1)
    return lambda x: y1 + slope * (x - x1)


class Model:
    """Per-run usage and cost, built from the measured kinds."""

    def __init__(self, inp: Inputs):
        self.inp = inp
        k = inp.kind
        self.noop = k("noop")
        inc = lambda r, key: r[key] - self.noop[key]
        s1, s2 = k("traffic-spike", (2, 4)), k("traffic-spike", (6, 8))
        self.spike = {key: line(3, inc(s1, key), 7, inc(s2, key)) for key in ("api", "gbs", "log_mb", "inv")}
        self.db_gbs = line(2, inc(k("db-throttling", (1.5, 2.5)), "gbs"), 4, inc(k("db-throttling", (3.5, 4.5)), "gbs"))
        self.pay_gbs = line(1000, inc(k("payment-failure", (500, 1500)), "gbs"), 3000, inc(k("payment-failure", (2500, 3500)), "gbs"))
        self.lam_gbs = {}
        for t in TARGETS:
            y1, y2 = inc(k(f"lambda-degradation:{t}", (200, 500)), "gbs"), inc(k(f"lambda-degradation:{t}", (800, 1100)), "gbs")
            if t == "payment-service":
                # Its two bands measured flat and noisy; use the physical shape (added latency x invocations), through 0.
                slope = (y1 * 350 + y2 * 950) / (350 ** 2 + 950 ** 2)
                self.lam_gbs[t] = lambda x, s=slope: s * x
            else:
                self.lam_gbs[t] = line(350, y1, 950, y2)
        # Pipeline per run, from Nov 11's live batch net of an idle day
        lam = inp.lambdas
        self.pipe_gbs = sum(lam[f"anomalypulse-{f}" if f in ("diagnosis", "incidents", "explain") else f"anomalypulse-pipeline-{f}"]["nov11"]["gb_s"]
                            for f in PIPELINE_FNS) / NOV11_RUNS
        self.pipe_inv = sum(lam[f"anomalypulse-{f}" if f in ("diagnosis", "incidents", "explain") else f"anomalypulse-pipeline-{f}"]["nov11"]["invocations"]
                            for f in PIPELINE_FNS) / NOV11_RUNS
        self.idle = self.idle_day()
        n11 = lambda ut, svc=None: inp.qty("nov11", ut, svc)
        self.s3_t1_run = (n11("USE2-Requests-Tier1", S3) - self.idle["s3_t1"]) / NOV11_RUNS
        self.s3_t2_run = (n11("USE2-Requests-Tier2", S3) - self.idle["s3_t2"]) / NOV11_RUNS
        self.sqs_run = (n11("USE2-Requests-Tier1", SQS) - self.idle["sqs"]) / NOV11_RUNS
        self.eb_run = n11("USE2-Event-64K-Chunks") / NOV11_RUNS
        self.cwreq_run = max(0.0, n11("USE2-CW:Requests") - self.idle["cwreq"]) / NOV11_RUNS
        self.wru_run_train = inp.qty("oct", "USE2-WriteRequestUnits") / inp.oct_attempts
        self.call_cost = inp.cost("nov11", service="Claude") / NOV11_BEDROCK_CALLS

    def idle_day(self):
        """One idle day, from Nov 12 so far (the hours observed are its provisioned RCU-hours / 200), plus the
        Products table, the runner's disk and S3 storage."""
        inp = self.inp
        hours = inp.qty("nov12", "USE2-ReadCapacityUnit-Hrs") / 200
        per_day = lambda q: q * 24 / hours
        nov_days = 11 + hours / 24
        return {"hours_observed": hours,
                "s3_t1": per_day(inp.qty("nov12", "USE2-Requests-Tier1", S3)),
                "s3_t2": per_day(inp.qty("nov12", "USE2-Requests-Tier2", S3)),
                "sqs": per_day(inp.qty("nov12", "USE2-Requests-Tier1", SQS)),
                "cwreq": per_day(inp.qty("nov12", "USE2-CW:Requests")),
                "gbs": per_day(inp.qty("nov12", "USE2-Lambda-GB-Second-ARM")),
                "lreq": per_day(inp.qty("nov12", "USE2-Request-ARM")),
                "ecr": per_day(inp.cost("nov12", service="Amazon EC2 Container Registry")),
                "rcu": 200 * 24 * P_RCU_H, "ebs": 16 * P_GP3_GB_MONTH / 30,
                "s3_store": inp.cost("nov", "USE2-TimedStorage-ByteHrs", S3) / nov_days}

    def ratio(self, fn, key):
        """Mean test-band prediction over mean train-band prediction."""
        return sum(fn(mid(b)) for b in TEST_BANDS[key]) / sum(fn(mid(b)) for b in TRAIN_BANDS[key])

    def baseline_gbs_factor(self, f_prod):
        """A normal run's GB-seconds in the test mix over the train mix. Invocation shares per function: Product
        75% -> 65% (f_prod, between the two bounds), Cart 10% -> 15%, Order 15% -> 20% with POST /orders (which
        waits for Payment) weighted twice a GET, Payment 5% -> 10% (only POST /orders reaches it)."""
        fac = {"product": f_prod, "order": (10 + 10 * 2) / (10 + 5 * 2), "payment": 2.0, "cart": 1.5, "telemetry-shipper": 1.04}
        return sum(self.noop[f"gbs_{f}"] * fac[f] for f in SERVICE_FNS) / self.noop["gbs"]

    INV_RATIO = {"product": 65 / 75, "cart": 15 / 10, "order": 20 / 15, "payment": 10 / 5}

    def usage(self, run, test_mix, f_prod):
        """One run's usage. run: {"kind": noop|spike|db|lam|pay|cascade|confounder, ...}."""
        n, dur = self.noop, run.get("duration_s", FAULT_S)
        minutes = (BASELINE_S + dur + RECOVERY_S) / 60
        t = minutes / 10
        bf = self.baseline_gbs_factor(f_prod) if test_mix else 1.0
        u = {"api": n["api"] * t, "gbs": n["gbs"] * bf * t, "inv": n["inv"] * t * (1.04 if test_mix else 1.0),
             "log_mb": n["log_mb"] * t, "minutes": minutes}
        fd = dur / FAULT_S

        def single(k, x, target=None):
            if k == "spike":
                u["api"] += self.spike["api"](x) * fd
                u["gbs"] += self.spike["gbs"](x) * bf * fd
                u["log_mb"] += self.spike["log_mb"](x) * fd
                u["inv"] += self.spike["inv"](x) * fd
            elif k == "db":
                u["gbs"] += self.db_gbs(x) * (f_prod if test_mix else 1.0) * fd
            elif k == "lam":
                u["gbs"] += self.lam_gbs[target](x) * (self.INV_RATIO[TARGET_FN[target]] if test_mix else 1.0) * fd
            elif k == "pay":
                u["gbs"] += self.pay_gbs(x) * (self.INV_RATIO["payment"] if test_mix else 1.0) * fd

        kind = run["kind"]
        if kind in ("spike", "db", "pay"):
            single(kind, run["x"])
        elif kind == "lam":
            single("lam", run["x"], run["target"])
        elif kind == "confounder":
            for k, x, target in run["faults"]:
                single(k, x, target)
        elif kind == "cascade":
            # A cascade is measured on training bands. A test-set cascade (test_bands) scales each part by its
            # dominant stage's test/train ratio; one on training bands (a live run, the demo) is used as measured.
            tr = self.inp.kind("cascade", run["template"])
            tpl, scale = run["template"], run.get("test_bands", False)
            r_spike = self.ratio(self.spike["api"], "spike") if scale else 1.0
            r_prod = ((self.ratio(self.lam_gbs["product-service"], "lam") if "lambda-degradation:product-service" in tpl
                       else self.ratio(self.db_gbs, "db") if "db-throttling" in tpl else 1.0) if scale else 1.0)
            r_op = ((self.ratio(self.pay_gbs, "pay") if "payment-failure" in tpl
                     else self.ratio(self.lam_gbs["order-service"], "lam") if "lambda-degradation:order-service" in tpl else 1.0)
                    if scale else 1.0)
            prod_inc = tr["gbs_product"] - n["gbs_product"]
            op_inc = tr["gbs_order"] + tr["gbs_payment"] - n["gbs_order"] - n["gbs_payment"]
            other = sum(tr[f"gbs_{f}"] - n[f"gbs_{f}"] for f in ("cart", "telemetry-shipper"))
            mix_op = (self.INV_RATIO["order"] + self.INV_RATIO["payment"]) / 2 if test_mix else 1.0
            u["gbs"] += prod_inc * r_prod * (f_prod if test_mix else 1.0) + op_inc * r_op * mix_op + other
            s = r_spike if "traffic-spike" in tpl else 1.0
            for key in ("api", "log_mb", "inv"):
                u[key] += (tr[key] - n[key]) * s
        return u

    def cost(self, u, test_mix):
        """One run's cost before free allowances (Lambda, logs, SQS and CloudWatch requests are netted later)."""
        return {"api": u["api"] * P_API,
                "s3_ddb_eb": (self.s3_t1_run * P_S3_T1 + self.s3_t2_run * P_S3_T2
                              + self.wru_run_train * ((15 + 10) / (10 + 5) if test_mix else 1.0) * P_WRU + self.eb_run * P_EB),
                "runner": u["minutes"] / 60 * P_RUNNER_H}


# ---- the plan's runs ----
def cascade_templates(inp):
    return [r["band"] for r in inp.kinds if r["kind"] == "cascade"]


def test_set(inp):
    tpl, runs = cascade_templates(inp), []
    runs += [{"kind": "noop"} for _ in range(60)]
    runs += [{"kind": "spike", "x": mid(TEST_BANDS["spike"][i % 2])} for i in range(60)]
    runs += [{"kind": "db", "x": mid(TEST_BANDS["db"][i % 2])} for i in range(60)]
    runs += [{"kind": "lam", "x": mid(TEST_BANDS["lam"][i % 2]), "target": TARGETS[i % 4]} for i in range(45)]
    runs += [{"kind": "pay", "x": mid(TEST_BANDS["pay"][i % 2])} for i in range(45)]
    runs += [{"kind": "cascade", "template": tpl[i % 4], "test_bands": True} for i in range(30)]
    for i in range(30):
        a, b = PAIRS[i % 4]
        fault = lambda k, j: (k, mid(TEST_BANDS[k][j % 2]), TARGETS[(i + j) % 4] if k == "lam" else None)
        runs.append({"kind": "confounder", "faults": [fault(a, i), fault(b, i + 1)]})
    for i in range(20):    # 2- and 12-minute single faults, 10 of each, across four types
        k = ("spike", "db", "lam", "pay")[i % 4]
        runs.append({"kind": k, "x": mid(TEST_BANDS[k][i % 2]), "target": TARGETS[i % 4], "duration_s": (120, 720)[i // 10]})
    return runs


def stress_set():
    return [{"kind": k, "x": mid(STRESS_BANDS[k]), "target": TARGETS[i % 4], "stress": True}
            for k in ("spike", "lam", "db", "pay") for i in range(5)]


def validation_set(n):
    """M9-1 validates the test-set kinds first in a separate dataset: confounders, 2- and 12-minute faults, stress."""
    base = [{"kind": "confounder", "faults": [(a, mid(TEST_BANDS[a][0]), "order-service" if a == "lam" else None),
                                              (b, mid(TEST_BANDS[b][1]), "order-service" if b == "lam" else None)]} for a, b in PAIRS]
    base += [{"kind": k, "x": mid(TEST_BANDS[k][1]), "target": "order-service", "duration_s": d}
             for k in ("spike", "db", "lam", "pay") for d in (120, 720)]
    base += [{"kind": k, "x": mid(STRESS_BANDS[k]), "target": "order-service", "stress": True} for k in ("spike", "lam", "db", "pay")]
    return list(itertools.islice(itertools.cycle(base), n))


def bedrock_calls(runs, per_single, per_cascade, per_confounder):
    """(incidents, calls): every fault run opens one incident (validation's replay: 112 of 112), a normal run none."""
    incidents = calls = 0
    for r in runs:
        if r["kind"] == "noop":
            continue
        incidents += 1
        calls += per_cascade if r["kind"] == "cascade" else per_confounder if r["kind"] == "confounder" else per_single
    return incidents, calls


def billable(used, free):
    return max(0.0, used - free)


def estimate(inp: Inputs, s: dict):
    m = Model(inp)
    call_cost = s["call_cost"] or m.call_cost
    groups = {"test": (test_set(inp), True), "stress": (stress_set(), False), "validation": (validation_set(s["validation"]), True)}
    cost = {"api": 0.0, "s3_ddb_eb": 0.0, "runner": 0.0}
    use = {"gbs": 0.0, "inv": 0.0, "log_mb": 0.0}
    runs = 0
    for runs_, mix in groups.values():
        for r in runs_:
            test_mix = mix and not r.get("stress")
            u = m.usage(r, test_mix, s["f_prod"])
            for key in ("api", "gbs", "inv", "log_mb"):
                u[key] *= s["usage_scale"]
            for key, v in m.cost(u, test_mix).items():
                cost[key] += v
            use["gbs"] += u["gbs"] + m.pipe_gbs
            use["inv"] += u["inv"] + m.pipe_inv
            use["log_mb"] += u["log_mb"]
            runs += 1
    for key in cost:                                         # reruns: an average run each
        cost[key] *= (runs + s["reruns"]) / runs
    for key in use:
        use[key] *= (runs + s["reruns"]) / runs
    runs += s["reruns"]
    # M10: per session a DB-throttling run, a cascade and ~20 minutes more baseline traffic, then a half-hour tail
    m10, m10_use = 0.0, {"gbs": 0.0, "inv": 0.0, "log_mb": 0.0}
    tpl = cascade_templates(inp)[1]
    for _ in range(s["m10_sessions"]):
        for r in ({"kind": "db", "x": 4.0}, {"kind": "cascade", "template": tpl}, {"kind": "noop", "duration_s": 300}):
            u = m.usage(r, False, s["f_prod"])
            m10 += sum(m.cost(u, False).values())
            m10_use["gbs"] += u["gbs"] + m.pipe_gbs
            m10_use["inv"] += u["inv"] + m.pipe_inv
            m10_use["log_mb"] += u["log_mb"]
    m10 += s["m10_sessions"] * 0.5 * P_RUNNER_H
    m10_runs = 3 * s["m10_sessions"]
    # Bedrock
    all_runs = test_set(inp) + stress_set() + validation_set(s["validation"])
    incidents, calls = bedrock_calls(all_runs, *s["calls"])
    calls += s["reruns"] * s["calls"][0] + s["m10_sessions"] * (s["calls"][0] + s["calls"][1])
    bedrock = calls * call_cost + s["runbook_calls"] * call_cost * 1.2      # runbooks lengthen the input
    # November's free allowances, net of what's used so far and the idle days left
    idle = m.idle
    hours_seen = 11 * 24 + idle["hours_observed"]
    days_left = (DEC1 - hours_seen) / 24
    used = {"gbs": inp.qty("nov", "USE2-Lambda-GB-Second-ARM") + idle["gbs"] * days_left + use["gbs"] + m10_use["gbs"],
            "lreq": inp.qty("nov", "USE2-Request-ARM") + idle["lreq"] * days_left + use["inv"] + m10_use["inv"],
            "log_gb": inp.qty("nov", "USE2-VendedLog-Bytes") + (use["log_mb"] + m10_use["log_mb"]) / 1000,
            "sqs": inp.qty("nov", "USE2-Requests-Tier1", SQS) + idle["sqs"] * days_left + m.sqs_run * (runs + m10_runs),
            "cwreq": inp.qty("nov", "USE2-CW:Requests") + idle["cwreq"] * days_left + m.cwreq_run * (runs + m10_runs)}
    bill = {k: billable(v, FREE[k]) for k, v in used.items()}
    fixed_day = idle["rcu"] + idle["ebs"] + idle["s3_t1"] * P_S3_T1 + idle["s3_t2"] * P_S3_T2 + idle["s3_store"] + idle["ecr"]
    lines = {
        "M9-1 API Gateway requests": cost["api"],
        "Lambda (November, after the free allowance)": bill["gbs"] * P_GBS + bill["lreq"] * P_LREQ,
        "Bedrock: test-set and M10 diagnoses, M9-5 runbook comparison": bedrock,
        "Fixed to Nov 30: Products table 200 RCU, runner disk, S3, ECR": fixed_day * days_left,
        "M9-1 S3, DynamoDB writes, EventBridge (incl. the pipeline)": cost["s3_ddb_eb"],
        "M9-1 runner time (runs, batch overheads, idle tails)": cost["runner"] + (s["batches"] * 0.25 + s["idle_tail_h"]) * P_RUNNER_H,
        "CloudWatch logs and API requests, SQS (after free allowances)": bill["log_gb"] * P_LOG_GB + bill["cwreq"] * P_CWREQ + bill["sqs"] * P_SQS,
        "M10 dry runs and demo (API, S3, runner)": m10,
        "Log queries and Cost Explorer calls": s["analysis"],
    }
    return {"lines": lines, "rest": sum(lines.values()), "runs": runs, "incidents": incidents, "calls": calls + s["runbook_calls"],
            "used": used, "days_left": days_left, "fixed_day": fixed_day, "call_cost": call_cost}


def so_far(inp):
    """November so far: Usage and Tax from Cost Explorer, and the tax rate they imply."""
    t = inp.record_types["totals"]
    usage, tax = t.get("Usage", 0.0), t.get("Tax", 0.0)
    return usage, tax, (tax / usage if usage else 0.0)


def december(inp):
    """December, before tax, with no planned work after the Nov 29 demo."""
    idle = Model(inp).idle
    keep = (idle["s3_t1"] * P_S3_T1 + idle["s3_t2"] * P_S3_T2 + idle["s3_store"] + idle["ecr"]) * 31
    table = (idle["rcu"] * 31 - 25 * 730 * P_RCU_H) + idle["ebs"] * 31
    return {"everything deleted by Nov 30": 0.0,
            "runner and Products table deleted after M10 (the plan); pipeline, services and data kept": keep,
            "nothing deleted": keep + table}


def backtest(inp):
    """Nov 11's seven live runs (360 s baseline, 300 s fault, 300 s recovery; the cascade 20 minutes) predicted
    with the model, against Nov 11's measured API requests and service GB-seconds."""
    m = Model(inp)
    runs = [({"kind": "noop"}, 16), ({"kind": "spike", "x": 7.4}, 16), ({"kind": "db", "x": 4.2}, 16),
            ({"kind": "lam", "x": 1010, "target": "order-service"}, 16), ({"kind": "pay", "x": 1200}, 16),
            ({"kind": "pay", "x": 1200}, 16),
            ({"kind": "cascade", "template": "db-throttling+traffic-spike+lambda-degradation:order-service"}, 20)]
    pred = {"api": 0.0, "gbs": 0.0}
    for run, minutes in runs:
        u = m.usage(run, False, 1.0)
        extra = (minutes - u["minutes"]) / 10
        pred["api"] += u["api"] + m.noop["api"] * extra
        pred["gbs"] += u["gbs"] + m.noop["gbs"] * extra
    lam = inp.lambdas
    actual = {"api": inp.qty("nov11", "USE2-ApiGatewayRequest"),
              "gbs": sum(lam[f"anomalypulse-{f}"]["nov11"]["gb_s"] for f in SERVICE_FNS)}
    return pred, actual


def check_prices(inp):
    """The bill's implied unit prices (cost / quantity) next to the model's."""
    implied = lambda p, ut, svc=None: inp.cost(p, ut, svc) / inp.qty(p, ut, svc)
    return {"API Gateway request": (implied("oct", "USE2-ApiGatewayRequest"), P_API),
            "Lambda GB-s (after 400k free)": (inp.cost("oct", "USE2-Lambda-GB-Second-ARM") / (inp.qty("oct", "USE2-Lambda-GB-Second-ARM") - FREE["gbs"]), P_GBS),
            "runner hour": (implied("oct", "USE2-BoxUsage:c7g.large"), P_RUNNER_H - 0.005),
            "RCU-hour (Nov 11)": (implied("nov11", "USE2-ReadCapacityUnit-Hrs"), P_RCU_H),
            "S3 PUT-class request": (implied("oct", "USE2-Requests-Tier1", S3), P_S3_T1)}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inputs", default=str(Path(__file__).parent / "inputs"))
    inp = Inputs.load(p.parse_args().inputs)
    res = {k: estimate(inp, v) for k, v in SCENARIOS.items()}
    usage, tax, rate = so_far(inp)
    money = lambda x: f"${x:8.2f}"
    print(f"{'November 2026, before tax':64s} {'low':>9s} {'expected':>9s} {'high':>9s}")
    print(f"  {'so far (Cost Explorer, Nov 1 to Nov 12 morning UTC)':62s} " + " ".join(money(usage) for _ in res))
    for name in res["expected"]["lines"]:
        print(f"  {name:62s} " + " ".join(money(res[s]["lines"][name]) for s in res))
    totals = {s: usage + r["rest"] for s, r in res.items()}
    print(f"  {'November total':62s} " + " ".join(money(totals[s]) for s in res))
    print(f"  {'November total with tax (at November-so-far %.1f%%)' % (rate * 100):62s} " + " ".join(money(totals[s] * (1 + rate)) for s in res))
    e = res["expected"]
    print(f"\nexpected: {e['runs']} runs, {e['incidents']} incidents, {e['calls']:.0f} Bedrock calls at ${e['call_cost']:.4f}; "
          f"{e['days_left']:.2f} days left in November at ${e['fixed_day']:.3f}/day fixed")
    print("expected November use vs free allowances: " + ", ".join(f"{k} {v:,.0f} / {FREE[k]:,}" for k, v in e["used"].items()))
    print("\nDecember, before tax (nothing planned after the Nov 29 demo):")
    for name, v in december(inp).items():
        print(f"  {name:88s} {money(v)}")
    pred, actual = backtest(inp)
    print("\nbacktest, Nov 11's live runs: " + "; ".join(f"{k} predicted {pred[k]:,.0f}, actual {actual[k]:,.0f} ({pred[k] / actual[k]:.2f}x)" for k in pred))
    print("prices, bill vs model: " + "; ".join(f"{k} {b:.3g} vs {mdl:.3g}" for k, (b, mdl) in check_prices(inp).items()))


if __name__ == "__main__":
    main()
