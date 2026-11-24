"""What M9-1's test set measured on AWS (M9-4): cost per experiment, per incident and per inference, what the system
costs idle, monthly costs at several request rates, SageMaker Serverless Inference set against the Lambda on paper,
and the operational metrics. Pure: reads what cost/collect_m9.py wrote, prints the results and writes them to
cost/results/ (measured.json, and projections.json for M9-6's cost-curve figure).

    python3 -m cost.measured [--inputs cost/inputs/m9] [--out cost/results]      # run from AnomalyPulse/

The method (Docs/COST_AND_PERFORMANCE.md):
  1. Usage is CloudWatch's, summed over each batch's window, each UTC day and each healthy (NORMAL) run; the
     Diagnosis Lambda's from its REPORT lines (what each call was billed). bill_check sets each day's usage against
     the bill's quantities for the same day, and the bill's unit prices against the ones here.
  2. Priced at list price. The always-free allowances (Lambda, logs, SQS, CloudWatch requests, DynamoDB capacity)
     are every account's, so the projections show them as one line of their own; the per-run costs don't take them.
  3. What CloudWatch doesn't attribute (S3, SQS and EventBridge requests, CloudWatch API use, the runner) comes from
     the bill: the batch days' quantities above the idle days', per API request (the runner per batch minute).
  4. Projections: the store's own cost and AnomalyPulse's, each from its measured unit costs: per request (the
     Lambdas and tables from the healthy runs, the bill lines in 3 from the batch days), per minute (the
     once-a-minute functions, from the batches), per incident (Bedrock and the explanation Lambda) and per month
     (the idle days' fixed costs). request_basis measures what the bill lines' batch-day basis does to S3 PUTs.
"""
import argparse
import json
import statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cost.estimate import P_API, P_CWREQ, P_EB, P_GBS, P_LOG_GB, P_LREQ, P_RCU_H, P_S3_T1, P_S3_T2, P_SQS, P_WRU

U = timezone.utc

# ---- prices: list prices, us-east-2. bill_check compares each with the bill's own unit price ----
P_RRU = 0.125e-6                              # DynamoDB on-demand read request unit
P_WCU_H = 0.00065                             # DynamoDB provisioned write capacity unit, per hour
P_BEDROCK_IN, P_BEDROCK_OUT = 3e-6, 15e-6     # Claude Sonnet 4.6 on Bedrock, per input and output token
P_CW_GMD = 1e-5                               # CloudWatch GetMetricData, per metric requested
P_CW_ALARM = 0.10                             # CloudWatch alarm, standard resolution, per alarm-month
P_LOG_STORE_GB, P_DDB_STORE_GB = 0.03, 0.25   # CloudWatch Logs and DynamoDB storage, per GB-month
P_EC2_H, P_IPV4_H = 0.0723, 0.005             # the runner: c7g.large, and its public IPv4 address, per hour
# SageMaker, from the AWS Price List API (2026-11-23): Serverless Inference at 1 GB, per second of inference; its
# provisioned concurrency, per second provisioned and per second of inference on it; endpoint data, per GB in and
# per GB out; real-time endpoint instances, per hour.
P_SMSI_1GB_S, P_SMSI_PC_1GB_S, P_SMSI_PC_USE_1GB_S = 0.00002, 0.0000117, 0.000005
P_SM_DATA_GB = 0.016
P_SM_HOST_H = {"ml.t2.medium": 0.056, "ml.c6g.large": 0.0816}

GIB = 2 ** 30              # the bill's GB for logs (log bytes / billed GB is 2^30 exactly) and data
HOURS_MONTH = 730          # AWS's month
MONTH_S, MINUTES_MONTH = HOURS_MONTH * 3600, HOURS_MONTH * 60
# Always free, every account, monthly. DynamoDB's capacity is 18,600 unit-hours a month (the price list's free tier,
# 25 units for a 744-hour month). Logs' 5 GB is set against ingestion only, so logs storage gets none of it here.
FREE_MONTH = {"lambda_gbs": 400_000, "lambda_req": 1_000_000, "logs_gib": 5, "sqs": 1_000_000, "cw_req": 1_000_000,
              "rcu_h": 18_600, "wcu_h": 18_600, "cw_alarms": 10, "ddb_gb": 25}
FREE_PRICE = {"lambda_gbs": P_GBS, "lambda_req": P_LREQ, "logs_gib": P_LOG_GB, "sqs": P_SQS, "cw_req": P_CWREQ,
              "rcu_h": P_RCU_H, "wcu_h": P_WCU_H, "cw_alarms": P_CW_ALARM, "ddb_gb": P_DDB_STORE_GB}

# ---- the measurement ----
BATCH_DAYS = ("2026-11-14", "2026-11-15", "2026-11-16")     # UTC days with batches running
IDLE_DAYS = ("2026-11-18", "2026-11-20", "2026-11-21")      # UTC days with no traffic at all
RATES = (1, 2, 5, 10, 20, 50, 100)                          # requests a second, for the projections
HEADLINE_RATES = (1, 20, 100)
INCIDENTS_PER_MONTH = 30                                    # projections' assumption: one a day
WINDOW_BYTES = 4662        # a live window's file, mean of 200 (pipeline/windows/dt=2026-11-15/): SageMaker's input
REPLY_BYTES = 1000         # the Diagnosis Lambda's reply, from its documented shape (Docs/LIVE_DIAGNOSIS.md)
CALLS_PER_MONTH = {"idle": 0, "one a minute": MINUTES_MONTH, "one a second": MONTH_S}
PRODUCTS_RCU, PRODUCTS_WCU = 200, 5                         # anomalypulse-products-v2, provisioned, as deployed
# M9-1 ran with runbooks off. M9-5's with-runbooks comparison was the only Bedrock use on Nov 19: 38 calls
# (Docs/GENAI_EVALUATION.md), so that day's bill gives a call's cost with runbooks on, as they are live since #113.
RUNBOOK_DAY, RUNBOOK_CALLS = "2026-11-19", 38

S3, SQS = "Amazon Simple Storage Service", "Amazon Simple Queue Service"
EVENTS, CLOUDWATCH, DYNAMODB = "CloudWatch Events", "AmazonCloudWatch", "Amazon DynamoDB"
STORAGE = "USE2-TimedStorage-ByteHrs"                        # every service's storage usage type, in GB-months
BEDROCK_IN = "USE2-MP:USE2_InputTokenCount_Global-Units"     # unit "1M tokens"
BEDROCK_OUT = "USE2-MP:USE2_OutputTokenCount_Global-Units"
# Bill lines CloudWatch doesn't attribute: (service, usage type, list price, group, free allowance).
BILL_LINES = {
    "S3 PUT-class requests": (S3, "USE2-Requests-Tier1", P_S3_T1, "anomalypulse", None),
    "S3 GET-class requests": (S3, "USE2-Requests-Tier2", P_S3_T2, "anomalypulse", None),
    "SQS requests": (SQS, "USE2-Requests-Tier1", P_SQS, "anomalypulse", "sqs"),
    "EventBridge events": (EVENTS, "USE2-Event-64K-Chunks", P_EB, "anomalypulse", None),
    # the metrics extractor's GetMetricStatistics calls, 27 a minute (38,880 a day idle). The bill's GetMetricData
    # metrics are this analysis's own queries, not the system's, so they're left out.
    "CloudWatch API requests": (CLOUDWATCH, "USE2-CW:Requests", P_CWREQ, "anomalypulse", "cw_req"),
}
RUNNER_LINES = {"runner instance": ("Amazon Elastic Compute Cloud - Compute", "USE2-BoxUsage:c7g.large", P_EC2_H),
                "runner public IPv4": ("Amazon Virtual Private Cloud", "USE2-PublicIPv4:InUseAddress", P_IPV4_H)}
# Every bill usage type with a list price here, to price the bill itself at list price (bill_cross_check, idle).
# Other rows keep their billed cost: S3 and ECR storage (billed at list, with no free allowance), and data transfer,
# KMS and Glue requests (each under $0.001 a day).
LIST_PRICES = {("USE2-ApiGatewayRequest", None): P_API, ("USE2-Lambda-GB-Second-ARM", None): P_GBS,
               ("USE2-Request-ARM", None): P_LREQ, ("USE2-VendedLog-Bytes", None): P_LOG_GB,
               ("USE2-WriteRequestUnits", None): P_WRU, ("USE2-ReadRequestUnits", None): P_RRU,
               ("USE2-ReadCapacityUnit-Hrs", None): P_RCU_H, ("USE2-WriteCapacityUnit-Hrs", None): P_WCU_H,
               ("USE2-CW:AlarmMonitorUsage", None): P_CW_ALARM, (STORAGE, CLOUDWATCH): P_LOG_STORE_GB,
               (STORAGE, DYNAMODB): P_DDB_STORE_GB, (BEDROCK_IN, None): P_BEDROCK_IN * 1e6,
               (BEDROCK_OUT, None): P_BEDROCK_OUT * 1e6, ("USE2-Event-64K-Chunks", None): P_EB,
               ("USE2-Requests-Tier1", S3): P_S3_T1, ("USE2-Requests-Tier2", S3): P_S3_T2,
               ("USE2-Requests-Tier1", SQS): P_SQS, ("USE2-CW:Requests", None): P_CWREQ,
               ("USE2-CW:GMD-Metrics", None): P_CW_GMD, ("USE2-BoxUsage:c7g.large", None): P_EC2_H,
               ("USE2-PublicIPv4:InUseAddress", None): P_IPV4_H}

# ---- who each function and table belongs to, and what its use scales with ----
FN = "anomalypulse-"
FUNCTIONS = {   # short name: (group, basis)
    "product": ("app", "request"), "cart": ("app", "request"), "order": ("app", "request"),
    "payment": ("app", "request"),
    "telemetry-shipper": ("anomalypulse", "request"), "pipeline-forwarder": ("anomalypulse", "request"),
    "pipeline-aggregator": ("anomalypulse", "request"),
    "pipeline-window-builder": ("anomalypulse", "minute"), "metrics-extractor": ("anomalypulse", "minute"),
    "diagnosis": ("anomalypulse", "minute"), "incidents": ("anomalypulse", "minute"),
    "explain": ("anomalypulse", "incident"),
    "dashboard": ("anomalypulse", None),      # per console viewer: in the runs' cost, not projected
    "stub-501": ("tooling", None),
}
TABLES = {"carts": ("app", "request"), "orders": ("app", "request"), "incidents": ("anomalypulse", "minute"),
          "experiments": ("tooling", None), "fault-config": ("tooling", None),      # the simulator's
          "products-v2": ("app", None)}       # provisioned: a fixed monthly cost, not per request


def log_group(short):
    """Whose cost a function's log ingestion is. The four services' logs are mostly their per-request telemetry
    records, which exist for AnomalyPulse, so their ingestion is AnomalyPulse's."""
    group = FUNCTIONS[short][0]
    return "anomalypulse" if group == "app" else group


class Inputs:
    def __init__(self, d):
        d = Path(d)
        load = lambda name: json.load(open(d / name))
        b = load("batches.json")
        self.memory, self.batches = b["memory_mb"], b["batches"]
        self.days, self.runs, self.ops = load("days.json"), load("normal_runs.json"), load("ops.json")
        self.ce, self.reports, self.incidents = load("ce_daily.json"), load("diagnosis_reports.json"), load("incidents.json")

    def ce_qty(self, days, usage_type, service=None):
        return sum(r["qty"] for r in self.ce if r["day"] in days and r["usage_type"] == usage_type
                   and (service is None or r["service"] == service))

    def ce_cost(self, days, usage_type=None, service=None):
        return sum(r["cost"] for r in self.ce if r["day"] in days and (usage_type is None or r["usage_type"] == usage_type)
                   and (service is None or r["service"] == service))


def short(name):
    return name[len(FN):]


def fn_cost(gb_s, invocations):
    return gb_s * P_GBS + invocations * P_LREQ


def table_cost(t):
    return t["read_units"] * P_RRU + t["write_units"] * P_WRU


def gbs_factor(inp):
    """Billed GB-seconds per CloudWatch GB-second on the batch days, about 1.006. CloudWatch's Duration leaves out two
    things the bill counts, about equally on the batch days: each call rounded up to the next millisecond, and each
    cold start's start-up (init) time."""
    cw = sum(f["gb_s"] for d in BATCH_DAYS for f in inp.days[d]["functions"].values())
    return inp.ce_qty(BATCH_DAYS, "USE2-Lambda-GB-Second-ARM") / cw


def diagnosis_gbs(inp, report):
    """The Diagnosis Lambda's billed GB-seconds, from its REPORT lines."""
    return report["billed_ms"] / 1000 * inp.memory[FN + "diagnosis"] / 1024


def bill_check(inp):
    """Each batch and idle day's measured usage next to the bill's quantity for the same day, and the bill's unit
    prices (on Nov 16, after the free allowances ran out) next to the ones here."""
    days = {}
    for d in BATCH_DAYS + IDLE_DAYS:
        u = inp.days[d]
        f, t = u["functions"].values(), u["tables"]
        on_demand = [k for k in t if short(k) != "products-v2"]
        days[d] = {
            "api_requests": (u["api"], inp.ce_qty([d], "USE2-ApiGatewayRequest")),
            "lambda_gb_s": (sum(x["gb_s"] for x in f), inp.ce_qty([d], "USE2-Lambda-GB-Second-ARM")),
            "lambda_requests": (sum(x["invocations"] for x in f), inp.ce_qty([d], "USE2-Request-ARM")),
            "log_gib": (sum(x["log_bytes"] for x in f) / GIB, inp.ce_qty([d], "USE2-VendedLog-Bytes")),
            "dynamodb_write_units": (sum(t[k]["write_units"] for k in on_demand), inp.ce_qty([d], "USE2-WriteRequestUnits")),
            "dynamodb_read_units": (sum(t[k]["read_units"] for k in on_demand), inp.ce_qty([d], "USE2-ReadRequestUnits")),
        }
    unit = lambda ut, svc=None, per=1: inp.ce_cost(["2026-11-16"], ut, svc) / inp.ce_qty(["2026-11-16"], ut, svc) / per
    prices = {"API Gateway request": (unit("USE2-ApiGatewayRequest"), P_API),
              "Lambda GB-second": (unit("USE2-Lambda-GB-Second-ARM"), P_GBS),
              "Lambda request": (unit("USE2-Request-ARM"), P_LREQ),
              "log GB": (unit("USE2-VendedLog-Bytes"), P_LOG_GB),
              "DynamoDB write unit": (unit("USE2-WriteRequestUnits"), P_WRU),
              "DynamoDB read unit": (unit("USE2-ReadRequestUnits"), P_RRU),
              "Bedrock input token": (unit(BEDROCK_IN, per=1e6), P_BEDROCK_IN),
              "Bedrock output token": (unit(BEDROCK_OUT, per=1e6), P_BEDROCK_OUT),
              "EventBridge 64 KB chunk": (unit("USE2-Event-64K-Chunks"), P_EB),
              "S3 PUT-class request": (unit("USE2-Requests-Tier1", S3), P_S3_T1),
              "S3 GET-class request": (unit("USE2-Requests-Tier2", S3), P_S3_T2),
              "SQS request": (unit("USE2-Requests-Tier1", SQS), P_SQS),
              "runner hour": (unit("USE2-BoxUsage:c7g.large"), P_EC2_H)}
    # S3 PUT-class requests above the idle days', against the pipeline's own writes: one object per shipper call
    # and per aggregator call. What's left is the simulator's and the post-batch label and feature builds'.
    puts = above_idle(inp, S3, "USE2-Requests-Tier1")
    writes = sum(inp.days[d]["functions"][FN + s]["invocations"] for d in BATCH_DAYS
                 for s in ("telemetry-shipper", "pipeline-aggregator"))
    return {"days": days, "prices": prices, "s3_puts_above_idle": puts, "pipeline_object_writes": writes,
            "s3_puts_explained": writes / puts}


def list_cost(row):
    """A bill row at list price: its quantity times the list price, when there is one; otherwise its billed cost."""
    price = LIST_PRICES.get((row["usage_type"], row["service"]), LIST_PRICES.get((row["usage_type"], None)))
    return row["qty"] * price if price is not None else row["cost"]


def bill_cross_check(inp, model_total):
    """The test set's cost straight from the bill, at list price: the batch days less three idle days, scaled from
    the batch minutes inside those days to all of the test set's. Independent of the CloudWatch usage, which carries
    about 90% of the model's total (API Gateway, Lambda, logs, DynamoDB, Bedrock)."""
    days = sum(list_cost(r) for r in inp.ce if r["day"] in BATCH_DAYS)
    idle_day = sum(list_cost(r) for r in inp.ce if r["day"] in IDLE_DAYS) / len(IDLE_DAYS)
    inside = batch_minutes_in(inp, BATCH_DAYS)
    minutes = sum(b["minutes"] for b in inp.batches.values())
    bill = (days - idle_day * len(BATCH_DAYS)) * minutes / inside
    billed = inp.ce_cost(BATCH_DAYS)
    return {"batch_days_list": days, "batch_days_billed": billed, "free_allowances_used": days - billed,
            "idle_day_list": idle_day, "batch_minutes_inside": inside, "batch_minutes": minutes,
            "test_set_from_bill": bill, "test_set_model": model_total, "model_over_bill": model_total / bill}


def bedrock_per_call(inp):
    """Tokens and cost per Bedrock call: the batch days' billed tokens over the explanation Lambda's calls on them."""
    calls = sum(inp.days[d]["functions"][FN + "explain"]["invocations"] for d in BATCH_DAYS)
    tin = inp.ce_qty(BATCH_DAYS, BEDROCK_IN) * 1e6 / calls
    tout = inp.ce_qty(BATCH_DAYS, BEDROCK_OUT) * 1e6 / calls
    return {"calls": calls, "input_tokens": tin, "output_tokens": tout,
            "cost": tin * P_BEDROCK_IN + tout * P_BEDROCK_OUT}


def bedrock_runbooks_on(inp, off):
    """A Bedrock call's tokens and cost with runbooks on (M9-5's 38 calls on Nov 19), beside `off`, M9-1's."""
    tin = inp.ce_qty([RUNBOOK_DAY], BEDROCK_IN) * 1e6 / RUNBOOK_CALLS
    tout = inp.ce_qty([RUNBOOK_DAY], BEDROCK_OUT) * 1e6 / RUNBOOK_CALLS
    cost = tin * P_BEDROCK_IN + tout * P_BEDROCK_OUT
    return {"calls": RUNBOOK_CALLS, "input_tokens": tin, "output_tokens": tout, "cost": cost,
            "billed": inp.ce_cost([RUNBOOK_DAY], BEDROCK_IN) + inp.ce_cost([RUNBOOK_DAY], BEDROCK_OUT),
            "over_runbooks_off": cost / off["cost"]}


def above_idle(inp, service, usage_type):
    """The batch days' quantity above three idle days' (per day, averaged)."""
    idle = inp.ce_qty(IDLE_DAYS, usage_type, service) / len(IDLE_DAYS)
    return inp.ce_qty(BATCH_DAYS, usage_type, service) - idle * len(BATCH_DAYS)


def batch_minutes_in(inp, days):
    """Minutes of batch windows inside the given UTC days (contiguous)."""
    lo = datetime.fromisoformat(days[0]).replace(tzinfo=U)
    hi = datetime.fromisoformat(days[-1]).replace(tzinfo=U) + timedelta(days=1)
    total = 0.0
    for b in inp.batches.values():
        a, e = max(lo, datetime.fromisoformat(b["start"])), min(hi, datetime.fromisoformat(b["end"]))
        total += max(0.0, (e - a).total_seconds() / 60)
    return total


def bill_rates(inp):
    """The bill lines CloudWatch doesn't attribute, as quantities per API request (above the idle days') and per
    month (the idle days'), and the runner's hours per batch minute."""
    api = sum(inp.days[d]["api"] for d in BATCH_DAYS)
    rates = {}
    for name, (svc, ut, price, group, free) in BILL_LINES.items():
        idle_day = inp.ce_qty(IDLE_DAYS, ut, svc) / len(IDLE_DAYS)
        rates[name] = {"price": price, "group": group, "free": free,
                       "per_request": above_idle(inp, svc, ut) / api, "per_month": idle_day * HOURS_MONTH / 24}
    minutes = batch_minutes_in(inp, BATCH_DAYS)
    runner = {name: {"price": price, "per_batch_minute": above_idle(inp, svc, ut) / minutes}
              for name, (svc, ut, price) in RUNNER_LINES.items()}
    return rates, runner


def batch_cost(inp, bid, factor, bedrock, rates, runner):
    """One batch's cost at list price, line by line: [{"line", "group", "cost"}]."""
    b = inp.batches[bid]
    u = b["usage"]
    lines = [{"line": "API Gateway requests", "group": "app", "cost": u["api"] * P_API}]
    for name, f in sorted(u["functions"].items()):
        s = short(name)
        gbs = diagnosis_gbs(inp, inp.reports["batches"][bid]) if s == "diagnosis" else f["gb_s"] * factor
        lines.append({"line": f"Lambda {s}", "group": FUNCTIONS[s][0], "cost": fn_cost(gbs, f["invocations"])})
        lines.append({"line": f"logs {s}", "group": log_group(s), "cost": f["log_bytes"] / GIB * P_LOG_GB})
    for name, t in sorted(u["tables"].items()):
        s = short(name)
        if s != "products-v2":
            lines.append({"line": f"DynamoDB {s}", "group": TABLES[s][0], "cost": table_cost(t)})
    lines.append({"line": "Bedrock", "group": "anomalypulse",
                  "cost": u["functions"][FN + "explain"]["invocations"] * bedrock["cost"]})
    for name, r in rates.items():
        lines.append({"line": name, "group": r["group"], "cost": r["per_request"] * u["api"] * r["price"]})
    for name, r in runner.items():
        lines.append({"line": name, "group": "tooling", "cost": r["per_batch_minute"] * b["minutes"] * r["price"]})
    return lines


def totals(lines):
    out = {"app": 0.0, "anomalypulse": 0.0, "tooling": 0.0}
    for x in lines:
        out[x["group"]] += x["cost"]
    out["total"] = sum(out.values())
    return out


def line_kind(line):
    """A cost line's kind for the by-line totals: every function's Lambda line is "Lambda", its logs "logs", every
    table "DynamoDB"; the other lines are their own kind."""
    return line.split(" ")[0] if line.startswith(("Lambda ", "logs ", "DynamoDB ")) else line


def per_experiment(inp, factor, bedrock, rates, runner):
    batches, all_lines = {}, []
    for bid, b in inp.batches.items():
        lines = batch_cost(inp, bid, factor, bedrock, rates, runner)
        all_lines += lines
        t = totals(lines)
        batches[bid] = {"attempts": b["attempts"], "accepted": b["accepted"], "minutes": b["minutes"], **t,
                        "per_attempt": t["total"] / b["attempts"]}
    attempts = sum(b["attempts"] for b in inp.batches.values())
    t = totals(all_lines)
    by_line = {}
    for x in all_lines:
        by_line[line_kind(x["line"])] = by_line.get(line_kind(x["line"]), 0.0) + x["cost"]
    return {"batches": batches, "test_set": {**t, "attempts": attempts,
            "accepted": sum(b["accepted"] for b in inp.batches.values()),
            "per_attempt": t["total"] / attempts,
            "per_attempt_by_group": {g: t[g] / attempts for g in ("app", "anomalypulse", "tooling")},
            "by_line": dict(sorted(by_line.items(), key=lambda kv: -kv[1]))}}


def waiting_gbs(inp):
    """The incident store's GB-seconds spent waiting on the explanation Lambda. It calls that Lambda and waits for
    its answer, and both have the same memory, so the wait is the explanation Lambda's own GB-seconds (6,592 of the
    store's 6,773; the other 180 are its once-a-minute work, about 96 ms a call)."""
    assert inp.memory[FN + "incidents"] == inp.memory[FN + "explain"]
    return sum(b["usage"]["functions"][FN + "explain"]["gb_s"] for b in inp.batches.values())


def per_incident(inp, factor, bedrock):
    """What one incident adds: its Bedrock calls, the explanation Lambda's work with its logs, and the incident
    store's wait for that Lambda."""
    n = sum(i["incidents"] for i in inp.incidents.values())
    calls = sum(i["bedrock_calls"] for i in inp.incidents.values())
    ex = [b["usage"]["functions"][FN + "explain"] for b in inp.batches.values()]
    lam = sum(fn_cost(f["gb_s"] * factor, f["invocations"]) + f["log_bytes"] / GIB * P_LOG_GB for f in ex)
    wait = waiting_gbs(inp) * factor * P_GBS
    explain_calls = sum(f["invocations"] for f in ex)
    return {"incidents": n, "bedrock_calls": calls, "explain_invocations": explain_calls,
            "calls_per_incident": calls / n, "bedrock": calls * bedrock["cost"] / n, "explain_lambda": lam / n,
            "store_waiting": wait / n, "cost": (calls * bedrock["cost"] + lam + wait) / n}


def per_inference(inp, factor):
    """The Diagnosis Lambda's measured cost per call (one minute scored by the detector and the classifier), from
    its REPORT lines, and what scoring one minute costs along the whole once-a-minute path."""
    r = inp.reports["batches"].values()
    calls, billed = sum(x["calls"] for x in r), sum(x["billed_ms"] for x in r)
    init, handler, cold = sum(x["init_ms"] for x in r), sum(x["duration_ms"] for x in r), sum(x["cold_starts"] for x in r)
    gb = inp.memory[FN + "diagnosis"] / 1024
    cost = (billed / 1000 * gb * P_GBS + calls * P_LREQ) / calls
    warm = ((billed - init) / calls / 1000 * gb * P_GBS + P_LREQ)      # a call with no cold start
    minutes = sum(b["minutes"] for b in inp.batches.values())
    path = 0.0
    for bid, b in inp.batches.items():
        for s in ("pipeline-window-builder", "diagnosis", "incidents"):
            f = b["usage"]["functions"][FN + s]
            gbs = diagnosis_gbs(inp, inp.reports["batches"][bid]) if s == "diagnosis" else f["gb_s"] * factor
            path += fn_cost(gbs, f["invocations"]) + f["log_bytes"] / GIB * P_LOG_GB
        path += table_cost(b["usage"]["tables"][FN + "incidents"])
    path -= waiting_gbs(inp) * factor * P_GBS                          # the store's wait is per incident
    return {"calls": calls, "cold_starts": cold, "billed_ms_per_call": billed / calls,
            "handler_ms_per_call": handler / calls, "cold_start_share_of_billed": init / billed,
            "cost": cost, "warm_cost": warm, "per_million": cost * 1e6, "per_month_one_a_minute": cost * MINUTES_MONTH,
            "scored_minute_path": path / minutes,
            "idle_calls": inp.reports["idle"]["calls"], "idle_window": [inp.reports["idle"]["start"], inp.reports["idle"]["end"]]}


def idle_group(service, usage_type):
    """Whose an idle bill row is: the store's Products table's capacity and the tables' storage (the orders table
    holds 99% of it), the stopped runner's disk, and AnomalyPulse's everything else."""
    if usage_type in ("USE2-ReadCapacityUnit-Hrs", "USE2-WriteCapacityUnit-Hrs") or (service, usage_type) == (DYNAMODB, STORAGE):
        return "app"
    if service in ("EC2 - Other", "Amazon Elastic Compute Cloud - Compute", "Amazon Virtual Private Cloud",
                   "Amazon Elastic Load Balancing"):
        return "tooling"
    return "anomalypulse"


def idle(inp):
    """What a day with no traffic costs, from the bill's three idle days (mean): at list price like every other cost
    here, by group and by line, and what the bill charged. The bill is lower because some always-free allowances
    weren't used up: it shows the metrics extractor's CloudWatch requests and the Products table's write capacity
    as $0."""
    by, billed = {}, 0.0
    for r in inp.ce:
        if r["day"] in IDLE_DAYS and r["usage_type"] != "USE2-CW:GMD-Metrics":     # this analysis's own reads
            key = (r["service"], r["usage_type"], idle_group(r["service"], r["usage_type"]))
            by[key] = by.get(key, 0.0) + list_cost(r) / len(IDLE_DAYS)
            billed += r["cost"] / len(IDLE_DAYS)
    groups = {"app": 0.0, "anomalypulse": 0.0, "tooling": 0.0}
    for (_, _, g), c in by.items():
        groups[g] += c
    running = {short(n): sum(inp.days[d]["functions"][n]["invocations"] for d in IDLE_DAYS) / len(IDLE_DAYS)
               for n in inp.memory if any(inp.days[d]["functions"][n]["invocations"] for d in IDLE_DAYS)}
    day = sum(groups.values())
    return {"day": day, "month": day * HOURS_MONTH / 24, "by_group_day": groups,
            "billed_day": billed, "billed_month": billed * HOURS_MONTH / 24, "free_allowances_day": billed - day,
            "by_line_day": {f"{s} / {ut}": round(c, 5) for (s, ut, _), c in sorted(by.items(), key=lambda kv: -kv[1]) if c >= 0.0005},
            "functions_running_per_day": running,
            "diagnosis_calls": inp.reports["idle"]["calls"]}


def request_basis(inp, rates):
    """The bill lines' per-request rates come from the batch days, the Lambdas' from the healthy runs. The batches'
    faults change how many pipeline calls a request makes, so S3 PUT-class requests, nearly all the pipeline's object
    writes (one per shipper and aggregator call, bill_check), are also given on the healthy runs' basis: scaled by
    the healthy runs' writes per request over the batch days'."""
    def per_request(usages, names):
        usages = list(usages)
        return sum(u["functions"][FN + s]["invocations"] for u in usages for s in names) / sum(u["api"] for u in usages)
    pipeline = ("telemetry-shipper", "pipeline-aggregator")
    healthy = per_request((r["usage"] for r in inp.runs), pipeline)
    batch = per_request((inp.days[d] for d in BATCH_DAYS), pipeline)
    return {"pipeline_writes_per_request": {"healthy_runs": healthy, "batch_days": batch},
            "shipper_calls_per_request": {"healthy_runs": per_request((r["usage"] for r in inp.runs), pipeline[:1]),
                                          "batch_days": per_request((inp.days[d] for d in BATCH_DAYS), pipeline[:1])},
            "healthy_over_batch": healthy / batch,
            "s3_put_per_request_healthy": rates["S3 PUT-class requests"]["per_request"] * healthy / batch}


def healthy_s3_lines(lines, basis):
    """The projection lines with S3 PUT-class requests per request on the healthy runs' basis."""
    return [dict(x, per={**x["per"], "request": basis["s3_put_per_request_healthy"]}) if x["line"] == "S3 PUT-class requests"
            else x for x in lines]


def unit_usage(inp, factor, bedrock, rates):
    """The projections' building blocks: each line's usage per request, per minute, per incident and per month,
    with its price, group and free allowance."""
    runs, batches = inp.runs, inp.batches.values()
    requests = sum(r["usage"]["api"] for r in runs)
    minutes = sum(b["minutes"] for b in batches)
    incidents = sum(i["incidents"] for i in inp.incidents.values())
    lines = [{"line": "API Gateway requests", "group": "app", "price": P_API, "free": None, "per": {"request": 1.0}}]

    def per_basis(name, basis):
        """A function's total GB-s, invocations and log bytes over its basis' measurement, and that basis' count."""
        if basis == "request":
            fs = [r["usage"]["functions"][name] for r in runs]
            n = requests
        else:
            fs = [b["usage"]["functions"][name] for b in batches]
            n = minutes if basis == "minute" else incidents
        if short(name) == "diagnosis":
            gbs = sum(diagnosis_gbs(inp, x) for x in inp.reports["batches"].values())
        elif short(name) == "incidents":                # its wait on the explanation is per incident, below
            gbs = (sum(f["gb_s"] for f in fs) - waiting_gbs(inp)) * factor
        else:
            gbs = sum(f["gb_s"] for f in fs) * factor
        return gbs, sum(f["invocations"] for f in fs), sum(f["log_bytes"] for f in fs), n

    lines.append({"line": "Lambda incidents GB-s, waiting on the explanation", "group": "anomalypulse",
                  "price": P_GBS, "free": "lambda_gbs", "per": {"incident": waiting_gbs(inp) * factor / incidents}})
    for name in sorted(inp.memory):
        group, basis = FUNCTIONS[short(name)]
        if basis is None:
            continue
        gbs, inv, logb, n = per_basis(name, basis)
        s = short(name)
        lines += [{"line": f"Lambda {s} GB-s", "group": group, "price": P_GBS, "free": "lambda_gbs", "per": {basis: gbs / n}},
                  {"line": f"Lambda {s} requests", "group": group, "price": P_LREQ, "free": "lambda_req", "per": {basis: inv / n}},
                  {"line": f"logs {s}", "group": log_group(s), "price": P_LOG_GB, "free": "logs_gib", "per": {basis: logb / GIB / n}}]
    for s, (group, basis) in TABLES.items():
        if basis is None:
            continue
        src = [r["usage"]["tables"][FN + s] for r in runs] if basis == "request" else [b["usage"]["tables"][FN + s] for b in batches]
        n = requests if basis == "request" else minutes
        lines += [{"line": f"DynamoDB {s} writes", "group": group, "price": P_WRU, "free": None,
                   "per": {basis: sum(t["write_units"] for t in src) / n}},
                  {"line": f"DynamoDB {s} reads", "group": group, "price": P_RRU, "free": None,
                   "per": {basis: sum(t["read_units"] for t in src) / n}}]
    lines += [{"line": "Bedrock input tokens", "group": "anomalypulse", "price": P_BEDROCK_IN, "free": None,
               "per": {"incident": bedrock["input_tokens"] * per_incident_calls(inp)}},
              {"line": "Bedrock output tokens", "group": "anomalypulse", "price": P_BEDROCK_OUT, "free": None,
               "per": {"incident": bedrock["output_tokens"] * per_incident_calls(inp)}}]
    for name, r in rates.items():
        lines.append({"line": name, "group": r["group"], "price": r["price"], "free": r["free"],
                      "per": {"request": r["per_request"], "month": r["per_month"]}})
    # fixed: the Products table's provisioned capacity (as deployed), and the idle days' storage and alarms: S3, the
    # explanation image in ECR, the log groups, the tables (the orders table's, nearly all) and the pipeline's alarms
    lines += [{"line": "DynamoDB products-v2 provisioned reads", "group": "app", "price": P_RCU_H, "free": "rcu_h",
               "per": {"month": PRODUCTS_RCU * HOURS_MONTH}},
              {"line": "DynamoDB products-v2 provisioned writes", "group": "app", "price": P_WCU_H, "free": "wcu_h",
               "per": {"month": PRODUCTS_WCU * HOURS_MONTH}}]
    for name, svc, ut, group, price, free in (
            ("S3 storage", S3, STORAGE, "anomalypulse", None, None),
            ("ECR image storage", "Amazon EC2 Container Registry (ECR)", STORAGE, "anomalypulse", None, None),
            ("CloudWatch Logs storage", CLOUDWATCH, STORAGE, "anomalypulse", P_LOG_STORE_GB, None),
            ("DynamoDB storage", DYNAMODB, STORAGE, "app", P_DDB_STORE_GB, "ddb_gb"),
            ("CloudWatch alarms", CLOUDWATCH, "USE2-CW:AlarmMonitorUsage", "anomalypulse", P_CW_ALARM, "cw_alarms")):
        q = inp.ce_qty(IDLE_DAYS, ut, svc)
        if price is None:                               # billed at list: the bill's own unit price
            price = inp.ce_cost(IDLE_DAYS, ut, svc) / q if q else 0.0
        lines.append({"line": name, "group": group, "price": price, "free": free,
                      "per": {"month": q / len(IDLE_DAYS) * HOURS_MONTH / 24}})
    return lines


def per_incident_calls(inp):
    return sum(i["bedrock_calls"] for i in inp.incidents.values()) / sum(i["incidents"] for i in inp.incidents.values())


def project(lines, rps, incidents=INCIDENTS_PER_MONTH):
    """One month at `rps` requests a second, with traffic every minute: each group's cost at list price, and the
    always-free allowances as one line."""
    n = {"request": rps * MONTH_S, "minute": MINUTES_MONTH, "incident": incidents, "month": 1}
    groups, free_qty, detail = {"app": 0.0, "anomalypulse": 0.0}, {}, {}
    for x in lines:
        qty = sum(v * n[basis] for basis, v in x["per"].items())
        cost = qty * x["price"]
        groups[x["group"]] += cost
        detail[x["line"]] = cost
        if x["free"]:
            free_qty[x["free"]] = free_qty.get(x["free"], 0.0) + qty
    free = sum(min(q, FREE_MONTH[k]) * FREE_PRICE[k] for k, q in free_qty.items())
    total = groups["app"] + groups["anomalypulse"]
    return {"rps": rps, "requests": n["request"], "incidents": incidents, "app": groups["app"],
            "anomalypulse": groups["anomalypulse"], "total": total, "anomalypulse_share": groups["anomalypulse"] / total,
            "always_free": -free, "total_after_free": total - free,
            "lines": {k: round(v, 4) for k, v in sorted(detail.items(), key=lambda kv: -kv[1])}}


def serverless_comparison(inference):
    """The Diagnosis model's monthly cost on the Lambda (measured) and on SageMaker (on paper, at its list prices)
    at no calls, one a minute and one a second. Serverless Inference is billed for the inference code's run time
    only, so it is given the Lambda's measured handler time and no cold-start time: the comparison favours it."""
    handler_s = inference["handler_ms_per_call"] / 1000
    data = (WINDOW_BYTES + REPLY_BYTES) / GIB * P_SM_DATA_GB
    per_call = {"Lambda (measured)": inference["cost"],
                "SageMaker Serverless Inference, 1 GB": handler_s * P_SMSI_1GB_S + data,
                "Serverless Inference + 1 provisioned concurrency": handler_s * P_SMSI_PC_USE_1GB_S + data}
    fixed = {"Lambda (measured)": 0.0, "SageMaker Serverless Inference, 1 GB": 0.0,
             "Serverless Inference + 1 provisioned concurrency": P_SMSI_PC_1GB_S * MONTH_S}
    for inst, p in P_SM_HOST_H.items():
        per_call[f"real-time endpoint, {inst}"] = data
        fixed[f"real-time endpoint, {inst}"] = p * HOURS_MONTH
    month = {opt: {k: fixed[opt] + per_call[opt] * c for k, c in CALLS_PER_MONTH.items()} for opt in per_call}
    # At one call a minute the Lambda's measured cost includes its cold starts (33 in 3,752 calls, 58% of billed
    # time); at one a second it stays warm, so that row uses the cost of a call without one.
    month["Lambda (measured)"]["one a second"] = inference["warm_cost"] * CALLS_PER_MONTH["one a second"]
    return {"per_call": per_call, "fixed_month": fixed, "month": month,
            "assumptions": {"handler_ms": inference["handler_ms_per_call"], "input_bytes": WINDOW_BYTES,
                            "output_bytes": REPLY_BYTES, "lambda_warm_cost_per_call": inference["warm_cost"]}}


def operations(inp):
    """The live pipeline's operational metrics: throughput, API latency (healthy runs and the whole test set), each
    Lambda's duration, errors and concurrency, and the telemetry queue's backlog."""
    ops, runs = inp.ops, inp.runs
    requests = sum(b["usage"]["api"] for b in inp.batches.values())
    minutes = sum(b["minutes"] for b in inp.batches.values())
    healthy = {k: {"median": st.median(r["ops"]["api"][k] for r in runs), "max": max(r["ops"]["api"][k] for r in runs)}
               for k in ("latency_p50", "latency_p95", "latency_p99", "latency_maximum")}
    api = {bid: {k: o["api"][k] for k in ("latency_p50", "latency_p95", "latency_p99", "latency_maximum",
                                          "requests_per_minute_mean", "requests_per_minute_max", "4xxerror", "5xxerror")}
           for bid, o in ops.items()}
    fns = {}
    for name in sorted(inp.memory):
        per = [o["functions"][name] for o in ops.values() if o["functions"][name]["duration_p50"] is not None]
        if not per:
            continue
        healthy_p95 = [r["ops"]["functions"][name]["duration_p95"] for r in runs
                       if r["ops"]["functions"][name]["duration_p95"] is not None]
        fns[short(name)] = {"group": FUNCTIONS[short(name)][0],
                            "p50_median_of_batches": st.median(x["duration_p50"] for x in per),
                            "p95_worst_batch": max(x["duration_p95"] for x in per),
                            "p99_worst_batch": max(x["duration_p99"] for x in per),
                            "max": max(x["duration_maximum"] for x in per),
                            "p95_healthy_median": st.median(healthy_p95) if healthy_p95 else None,
                            "errors": sum(x["errors"] or 0 for x in per), "throttles": sum(x["throttles"] or 0 for x in per),
                            "concurrency_max": max((x["concurrency_max"] or 0) for x in per)}
    q = [o["queue"] for o in ops.values()]
    queue = {"messages_sent": sum(x["NumberOfMessagesSent"] for x in q),
             "messages_deleted": sum(x["NumberOfMessagesDeleted"] for x in q),
             "most_visible": max(x["visible_minute_max"] for x in q),
             "oldest_message_s_max": max(x["age_s_minute_max"] for x in q),
             "oldest_message_s_minute_median": st.median(x["age_s_minute_median"] for x in q)}
    failures = {name: [o["failure_queues"][name] for o in ops.values()] for name in next(iter(ops.values()))["failure_queues"]}
    return {"throughput": {"requests": requests, "minutes": minutes, "mean_rps": requests / minutes / 60,
                           "busiest_minute_rps": max(o["api"]["requests_per_minute_max"] for o in ops.values()) / 60,
                           "healthy_rps_median": st.median(r["usage"]["api"] / r["minutes"] / 60 for r in runs)},
            "api_latency_healthy_ms": healthy, "api_by_batch": api, "functions": fns, "queue": queue,
            "failure_queues_most_visible": failures}


def report(inp):
    factor = gbs_factor(inp)
    bedrock = bedrock_per_call(inp)
    runbooks = bedrock_runbooks_on(inp, bedrock)
    rates, runner = bill_rates(inp)
    inference = per_inference(inp, factor)
    lines = unit_usage(inp, factor, bedrock, rates)
    experiments = per_experiment(inp, factor, bedrock, rates, runner)
    incident = per_incident(inp, factor, bedrock)
    # the same incident with runbooks on: each Bedrock call at runbooks-on cost, the Lambdas' share unchanged
    incident["cost_runbooks_on"] = incident["cost"] + incident["calls_per_incident"] * (runbooks["cost"] - bedrock["cost"])
    basis = request_basis(inp, rates)
    basis["total_healthy_s3"] = {r: project(healthy_s3_lines(lines, basis), r)["total"] for r in HEADLINE_RATES}
    return {"gbs_factor": factor, "bill_check": bill_check(inp), "bedrock_per_call": bedrock,
            "bedrock_per_call_runbooks_on": runbooks,
            "bill_cross_check": bill_cross_check(inp, experiments["test_set"]["total"]),
            "experiments": experiments,
            "per_incident": incident, "per_inference": inference, "idle": idle(inp),
            "serverless_comparison": serverless_comparison(inference), "operations": operations(inp),
            "unit_usage": lines, "projections": [project(lines, r) for r in RATES], "request_basis": basis}


def show(res):
    money = lambda x: f"${x:,.2f}" if abs(x) >= 0.01 else f"${x:.2e}"
    print("bill check, measured vs billed, per UTC day:")
    for d, row in res["bill_check"]["days"].items():
        print(f"  {d}  " + "  ".join(f"{k} {m:,.0f}/{b:,.0f}" if b > 100 else f"{k} {m:.3f}/{b:.3f}" for k, (m, b) in row.items()))
    print("  prices, bill (Nov 16) vs here: " + "; ".join(f"{k} {b:.3g} vs {p:.3g}" for k, (b, p) in res["bill_check"]["prices"].items()))
    print(f"  S3 PUT-class requests above idle {res['bill_check']['s3_puts_above_idle']:,.0f}, the pipeline's object writes "
          f"{res['bill_check']['pipeline_object_writes']:,.0f} ({res['bill_check']['s3_puts_explained']:.0%})")
    c = res["bill_cross_check"]
    print(f"  the test set from the bill at list price {money(c['test_set_from_bill'])} vs the model's {money(c['test_set_model'])} "
          f"({c['model_over_bill']:.3f}x); the batch days' free allowances {money(c['free_allowances_used'])}")
    e = res["experiments"]["test_set"]
    print(f"\ntest set: {e['attempts']} runs ({e['accepted']} accepted), {money(e['total'])} at list price: app {money(e['app'])}, "
          f"AnomalyPulse {money(e['anomalypulse'])}, tooling {money(e['tooling'])}")
    print(f"  per experiment {money(e['per_attempt'])} (app {money(e['per_attempt_by_group']['app'])}, AnomalyPulse "
          f"{money(e['per_attempt_by_group']['anomalypulse'])}, runner and simulator {money(e['per_attempt_by_group']['tooling'])})")
    print("  biggest lines: " + "; ".join(f"{k} {money(v)}" for k, v in list(e["by_line"].items())[:8]))
    i = res["per_incident"]
    print(f"per incident: {money(i['cost'])} (Bedrock {money(i['bedrock'])}, explanation Lambda {money(i['explain_lambda'])}, "
          f"the store's wait {money(i['store_waiting'])}; {i['calls_per_incident']:.2f} Bedrock calls at {money(res['bedrock_per_call']['cost'])}: "
          f"{res['bedrock_per_call']['input_tokens']:,.0f} in, {res['bedrock_per_call']['output_tokens']:,.0f} out), "
          f"{i['incidents']} incidents")
    rb = res["bedrock_per_call_runbooks_on"]
    print(f"  with runbooks on (Nov 19, {rb['calls']} calls, billed {money(rb['billed'])}): {money(rb['cost'])} a call, "
          f"{rb['input_tokens']:,.0f} in, {rb['output_tokens']:,.0f} out, {rb['over_runbooks_off']:.2f}x; "
          f"{money(i['cost_runbooks_on'])} an incident; 30 incidents {money(30 * i['cost'])} ({money(30 * i['cost_runbooks_on'])} with runbooks)")
    n = res["per_inference"]
    print(f"per inference: {money(n['cost'])} ({money(n['per_million'])} a million; {money(n['per_month_one_a_minute'])} a month at one a "
          f"minute); {n['billed_ms_per_call']:.1f} ms billed a call, handler {n['handler_ms_per_call']:.1f} ms, cold starts "
          f"{n['cold_starts']:.0f} of {n['calls']:.0f} calls but {n['cold_start_share_of_billed']:.0%} of billed time; "
          f"the once-a-minute path {money(n['scored_minute_path'])} a minute; idle calls {n['idle_calls']:.0f}")
    d = res["idle"]
    print(f"idle, list price: {money(d['day'])} a day, {money(d['month'])} a month: " + ", ".join(f"{g} {money(v)}" for g, v in d["by_group_day"].items())
          + f"; billed {money(d['billed_day'])} a day ({money(d['billed_month'])} a month); running: {d['functions_running_per_day']}")
    print("  biggest idle lines a day: " + "; ".join(f"{k} {money(v)}" for k, v in list(d["by_line_day"].items())[:6]))
    print("\nmonthly, list price (always-free allowances shown apart):")
    for p in res["projections"]:
        print(f"  {p['rps']:>4} rps  app {money(p['app']):>10}  AnomalyPulse {money(p['anomalypulse']):>9} ({p['anomalypulse_share']:.0%})"
              f"  total {money(p['total']):>10}  after always-free {money(p['total_after_free']):>10}")
    b = res["request_basis"]
    print(f"  S3 PUTs on the healthy runs' basis: the pipeline's writes per 1,000 requests {1000 * b['pipeline_writes_per_request']['healthy_runs']:.1f} "
          f"healthy vs {1000 * b['pipeline_writes_per_request']['batch_days']:.1f} on the batch days (shipper {1000 * b['shipper_calls_per_request']['healthy_runs']:.1f} "
          f"vs {1000 * b['shipper_calls_per_request']['batch_days']:.1f}), {b['healthy_over_batch']:.3f}x; totals "
          + ", ".join(f"{r} rps {money(t)}" for r, t in b["total_healthy_s3"].items()))
    print("\nthe Diagnosis model, a month:")
    for opt, m in res["serverless_comparison"]["month"].items():
        print(f"  {opt:52s} " + "  ".join(f"{k} {money(v)}" for k, v in m.items()))
    o = res["operations"]
    print(f"\nthroughput: mean {o['throughput']['mean_rps']:.1f} rps, busiest minute {o['throughput']['busiest_minute_rps']:.0f} rps, "
          f"healthy runs {o['throughput']['healthy_rps_median']:.1f} rps")
    print("API latency, healthy runs (median of 60): " + ", ".join(f"{k[8:]} {v['median']:.1f} ms" for k, v in o["api_latency_healthy_ms"].items()))
    print(f"queue: {o['queue']}")
    for s, f in o["functions"].items():
        print(f"  {s:24s} p50 {f['p50_median_of_batches']:8.1f}  p95 {f['p95_worst_batch']:8.1f}  p99 {f['p99_worst_batch']:8.1f}  "
              f"max {f['max']:8.1f} ms  errors {f['errors']:.0f}  throttles {f['throttles']:.0f}  concurrency {f['concurrency_max']:.0f}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--inputs", default=str(Path(__file__).parent / "inputs" / "m9"))
    p.add_argument("--out", default=str(Path(__file__).parent / "results"))
    args = p.parse_args()
    res = report(Inputs(args.inputs))
    show(res)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "measured.json", "w") as f:
        json.dump(res, f, indent=1)
    curve = {"note": "Monthly AWS cost at list price by request rate, from M9-1's measured unit costs "
                     "(cost/measured.py, Docs/COST_AND_PERFORMANCE.md). 'app' is the monitored store, 'anomalypulse' "
                     "what monitoring it adds; always_free is every account's always-free allowance, one line.",
             "incidents_per_month": INCIDENTS_PER_MONTH, "hours_per_month": HOURS_MONTH,
             "rows": [{k: round(v, 4) if isinstance(v, float) else v for k, v in r.items() if k != "lines"} for r in res["projections"]]}
    with open(out / "projections.json", "w") as f:
        json.dump(curve, f, indent=1)
    print(f"\nwrote {out / 'measured.json'} and {out / 'projections.json'}")


if __name__ == "__main__":
    main()
