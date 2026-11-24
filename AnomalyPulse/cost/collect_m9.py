"""Collect M9-1's measured usage, for M9-4's costs and operational metrics. Read-only.

    python3 -m cost.collect_m9 [--profile dev] [--out cost/inputs/m9]     # run from AnomalyPulse/

Writes into --out:
  batches.json            for each of M9-1's four test batches (v1's test and stress attempts), its window, its
                          attempt counts and the usage inside the window, from CloudWatch's 1-minute sums: API
                          Gateway requests, each anomalypulse Lambda's invocations, GB-seconds and log bytes, and
                          each anomalypulse table's consumed read and write units. A window runs from the first
                          attempt's start to the last one's end plus TAIL, in whole minutes, so it holds the batch's
                          pipeline work too (a minute's diagnosis lands 1-3 minutes after it).
  days.json               the same usage for each UTC day in DAYS (three batch days, three idle ones), to set against
                          the bill's quantities for the same days.
  normal_runs.json        the same usage, and the operational metrics, for each accepted NORMAL test run, over its
                          whole minutes: healthy traffic in the test mix, for the per-request costs.
  ce_daily.json           Cost Explorer, Usage records by day, service and usage type, CE_DAYS (one call, $0.01).
  diagnosis_reports.json  the Diagnosis Lambda's REPORT lines (what each call was billed) totalled per batch window
                          and over IDLE, from Logs Insights: calls, cold starts, billed, handler and init milliseconds.
  incidents.json          the incident records that opened in each batch window, from the Incidents table.
  ops.json                each batch window's operational metrics: API latency and throughput, each Lambda's
                          duration percentiles, errors, throttles and peak concurrency, and the telemetry queue's
                          backlog (CloudWatch percentiles over the whole window, plus per-minute peaks).

CloudWatch keeps 1-minute points for 15 days (the first batch's until about 2026-11-28), then 5-minute points;
these files keep what was read. About 11,000 metrics read with GetMetricData ($0.01 a thousand, ~$0.11), 5 small
Logs Insights queries, one Query of the Incidents table, one Cost Explorer call ($0.01).
"""
import argparse
import json
import statistics as st
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

from cost.collect import metric_series, parse, registry_attempts

U = timezone.utc
TAIL = timedelta(minutes = 5)
CE_DAYS = ("2026-11-13", "2026-11-23")
REPORT_QUERY = ('filter @type = "REPORT" | stats count(*) as calls, sum(ispresent(@initDuration)) as cold_starts, '
                'sum(@billedDuration) as billed_ms, sum(@duration) as duration_ms, sum(@initDuration) as init_ms, '
                'max(@maxMemoryUsed) / 1000000 as max_mb')
REPORT_FIELDS = ("calls", "cold_starts", "billed_ms", "duration_ms", "init_ms", "max_mb")
# Two UTC days with no traffic: the bill shows only the idle floor and no API Gateway charges.
IDLE = (datetime(2026, 11, 20, tzinfo = U), datetime(2026, 11, 22, tzinfo = U))
# UTC days for the bill check: three with batches running, three idle (Nov 17 had check traffic, Nov 19 M9-5's
# offline Bedrock calls).
DAYS = ("2026-11-14", "2026-11-15", "2026-11-16", "2026-11-18", "2026-11-20", "2026-11-21")
QUEUE = "anomalypulse-telemetry"
FAILURE_QUEUES = ("anomalypulse-telemetry-dlq", "anomalypulse-telemetry-undelivered",
                  "anomalypulse-diagnosis-failed", "anomalypulse-incidents-failed")
API = "anomalypulse-api"

def floor_minute(t):
    return t.replace(second = 0, microsecond = 0)

def ceil_minute(t):
    floor = floor_minute(t)
    return floor if floor == t else floor + timedelta(minutes = 1)

def batch_windows(attempts):
    """{batch_id: {"start", "end", "attempts", "accepted"}} for v1's test and stress attempts."""
    grouped = defaultdict(list)

    for attempt in attempts:
        if not (attempt["dataset"] == "v1" and attempt["phase"] in ["test", "stress"]):
            continue
        grouped[attempt["batch_id"]].append(attempt)

    windows = {}

    for bid, atts in grouped.items():

        windows[bid] = {
            "start": floor_minute(min(parse(a["started_at"]) for a in atts)),
            "end": ceil_minute(max(parse(a["ended_at"]) for a in atts) + TAIL),
            "attempts": len(atts),
            "accepted": sum(a["verdict"] == "accepted" for a in atts)
        }

    return windows

def normal_runs(attempts):
    """v1's accepted NORMAL test runs, each over its whole minutes: [{"experiment_id", "slot", "start", "end"}].
    Whole minutes only, because runs follow one another and a 1-minute point straddling two runs belongs to both."""
    runs = []
    for a in attempts:
        if a["dataset"] == "v1" and a["phase"] == "test" and a["label"] == "NORMAL" and a["verdict"] == "accepted":
            runs.append({"experiment_id": a["experiment_id"], "slot": a["slot"],
                         "start": ceil_minute(parse(a["started_at"])), "end": floor_minute(parse(a["ended_at"]))})
    return sorted(runs, key = lambda r: r["start"])

def q(qid, namespace, metric, dim, value):
    return {"Id": qid, "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": metric,
            "Dimensions": [{"Name": dim, "Value": value}]}, "Period": 60, "Stat": "Sum"}}

def batch_usage(cw, memory, start, end):
    """Usage in [start, end) from 1-minute sums:
    {"api": requests, "functions": {name: {"invocations", "gb_s", "log_bytes"}}}"""

    queries = [q("api", "AWS/ApiGateway", "Count", "ApiName", API)]
    ids = {}                                    # query Id -> (function name, field); Ids can't hold hyphens
    for i, name in enumerate(sorted(memory)):
        queries += [q(f"inv_{i}", "AWS/Lambda", "Invocations", "FunctionName", name),
                    q(f"dur_{i}", "AWS/Lambda", "Duration", "FunctionName", name),
                    q(f"log_{i}", "AWS/Logs", "IncomingBytes", "LogGroupName", f"/aws/lambda/{name}")]
        ids[f"inv_{i}"] = (name, "invocations")
        ids[f"dur_{i}"] = (name, "duration")
        ids[f"log_{i}"] = (name, "log_bytes")

    min_by_min = metric_series(cw, queries, start, end)

    def total(qid):                             # a query with no points (a function never called) is 0
        return sum(min_by_min.get(qid, {}).values())

    usage = {"api": round(total("api")), "functions": {}}
    for qid, (name, field) in ids.items():
        f = usage["functions"].setdefault(name, {"invocations": 0, "gb_s": 0.0, "log_bytes": 0})
        if field == "duration":                 # Duration is milliseconds; Lambda bills memory x time
            f["gb_s"] = round(total(qid) / 1000 * memory[name] / 1024, 2)
        else:
            f[field] = round(total(qid))
    return usage

def table_usage(cw, tables, start, end):
    """Each table's consumed read and write units in [start, end): {table: {"read_units", "write_units"}}.
    On an on-demand table these are the request units it is billed for."""
    queries, ids = [], {}
    for i, name in enumerate(sorted(tables)):
        queries += [q(f"r_{i}", "AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", name),
                    q(f"w_{i}", "AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", name)]
        ids[f"r_{i}"], ids[f"w_{i}"] = (name, "read_units"), (name, "write_units")
    series = metric_series(cw, queries, start, end)
    out = {name: {"read_units": 0.0, "write_units": 0.0} for name in tables}
    for qid, (name, field) in ids.items():
        out[name][field] = round(sum(series.get(qid, {}).values()), 1)
    return out

def window_usage(cw, memory, tables, start, end):
    """batch_usage plus the tables' consumed units."""
    return {**batch_usage(cw, memory, start, end), "tables": table_usage(cw, tables, start, end)}

def lambda_memory(lam):
    """{function name: memory in MB} for every anomalypulse Lambda."""
    return {f["FunctionName"]: f["MemorySize"] for page in lam.get_paginator("list_functions").paginate()
            for f in page["Functions"] if f["FunctionName"].startswith("anomalypulse")}

def anomalypulse_tables(ddb):
    return sorted(t for page in ddb.get_paginator("list_tables").paginate() for t in page["TableNames"]
                  if t.startswith("anomalypulse"))

def ce_daily(ce, start, end):
    """Cost Explorer Usage records, one row per day, service, and usage type:
    [{"day", "service", "usage_type", "cost", "qty", "unit"}, ...]"""
    out, token = [], None
    while True:
        kw = dict(TimePeriod={"Start": start, "End": end}, Granularity="DAILY", Metrics=["UnblendedCost", "UsageQuantity"],
                  Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
                  GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
        if token:
            kw["NextPageToken"] = token
        r = ce.get_cost_and_usage(**kw)
        for d in r["ResultsByTime"]:
            day = d["TimePeriod"]["Start"]
            for g in d["Groups"]:
                out.append({"service": g["Keys"][0], "usage_type": g["Keys"][1],
                            "cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                            "qty": float(g["Metrics"]["UsageQuantity"]["Amount"]),
                            "unit": g["Metrics"]["UsageQuantity"]["Unit"],
                            "day": day})
        token = r.get("NextPageToken")
        if not token:
            return out

def diagnosis_reports(logs, start, end):
    """The Diagnosis Lambda's REPORT totals in [start, end):
    {"calls", "cold_starts", "billed_ms", "duration_ms", "init_ms", "max_mb"}, all 0 when nothing ran."""
    qid = logs.start_query(logGroupName = "/aws/lambda/anomalypulse-diagnosis",
                           startTime = int(start.timestamp()),
                           endTime = int(end.timestamp()) - 1,     # endTime is inclusive
                           queryString = REPORT_QUERY)["queryId"]
    while True:                                 # Logs Insights runs asynchronously: ask until it's done
        r = logs.get_query_results(queryId = qid)
        if r["status"] == "Complete":
            break
        if r["status"] in ("Failed", "Cancelled", "Timeout"):
            raise RuntimeError(f"Logs Insights query {qid} ended {r['status']}")
        time.sleep(1)

    totals = dict.fromkeys(REPORT_FIELDS, 0.0)
    if r["results"]:                            # no rows when there were no calls at all
        totals.update({f["field"]: float(f["value"]) for f in r["results"][0]})
    return totals

def sk_time(t):
    """A datetime written the way the Incidents table writes sk (started_at): 2026-11-14T03:12:00.000Z.
    sk is a string, so a range on it compares letter by letter and must use the same format."""
    return t.astimezone(U).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

def batch_incidents(table, start, end):
    """Incidents that started in [start, end): {"incidents", "bedrock_calls", "types": {type: count}}."""
    kw = {"KeyConditionExpression": Key("pk").eq("INCIDENT") & Key("sk").between(sk_time(start), sk_time(end))}
    items = []
    while True:                                 # each item holds a whole diagnosis: a page (1 MB) won't hold them all
        r = table.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    items = [i for i in items if i["sk"] < sk_time(end)]        # between() includes its end; the window doesn't
    return {"incidents": len(items),
            "bedrock_calls": sum(int(i.get("bedrock_calls", 0)) for i in items),      # Decimal -> int
            "types": dict(sorted(Counter(i["prediction"]["incident_type"] for i in items).items()))}

def stat_query(qid, namespace, metric, dims, stat, period):
    return {"Id": qid, "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": metric,
            "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()]}, "Period": period, "Stat": stat}}

def ops_stats(cw, memory, start, end):
    """Operational metrics over [start, end).
    Whole-window statistics (one period covering the window): API latency percentiles and error counts; each
    Lambda's duration percentiles, errors, throttles and peak concurrency; the telemetry queue's messages and
    backlog; whether any failure queue held a message. CloudWatch's percentiles over a period match exact ones from
    the REPORT lines to about 1% at p50/p95 and 3% at p99 (checked on the stress batch).
    Per-minute peaks: API requests a minute (mean and busiest), and the queue's backlog (most messages waiting,
    oldest message's age)."""
    period = int((end - start).total_seconds())
    whole, ids = [], {}

    def add(key, namespace, metric, dims, stat):
        qid = f"m{len(whole)}"
        whole.append(stat_query(qid, namespace, metric, dims, stat, period))
        ids[qid] = key

    api = {"ApiName": API}
    for stat in ("p50", "p95", "p99", "Maximum"):
        add(("api", "latency_" + stat.lower()), "AWS/ApiGateway", "Latency", api, stat)
    for metric in ("Count", "4XXError", "5XXError"):
        add(("api", metric.lower()), "AWS/ApiGateway", metric, api, "Sum")
    for name in sorted(memory):
        fn = {"FunctionName": name}
        for stat in ("p50", "p95", "p99", "Maximum"):
            add(("functions", name, "duration_" + stat.lower()), "AWS/Lambda", "Duration", fn, stat)
        for metric in ("Errors", "Throttles"):
            add(("functions", name, metric.lower()), "AWS/Lambda", metric, fn, "Sum")
        add(("functions", name, "concurrency_max"), "AWS/Lambda", "ConcurrentExecutions", fn, "Maximum")
    queue = {"QueueName": QUEUE}
    for metric, stat in (("NumberOfMessagesSent", "Sum"), ("NumberOfMessagesDeleted", "Sum"),
                         ("ApproximateNumberOfMessagesVisible", "Maximum"), ("ApproximateAgeOfOldestMessage", "Maximum")):
        add(("queue", metric), "AWS/SQS", metric, queue, stat)
    for name in FAILURE_QUEUES:
        add(("failure_queues", name), "AWS/SQS", "ApproximateNumberOfMessagesVisible", {"QueueName": name}, "Maximum")

    values = {}
    for i in range(0, len(whole), 500):         # GetMetricData takes at most 500 queries
        r = cw.get_metric_data(MetricDataQueries = whole[i:i + 500], StartTime = start, EndTime = end)
        for x in r["MetricDataResults"]:
            if len(x["Values"]) > 1:            # one period covers the window: more points would each be a part of it
                raise RuntimeError(f"{ids[x['Id']]}: {len(x['Values'])} points for one whole-window period")
            values[x["Id"]] = x["Values"][0] if x["Values"] else None

    out = {"period_s": period, "api": {}, "functions": {}, "queue": {}, "failure_queues": {}}
    for qid, key in ids.items():
        v = values.get(qid)
        v = None if v is None else round(v, 3)
        if key[0] == "functions":
            out["functions"].setdefault(key[1], {})[key[2]] = v
        else:
            out[key[0]][key[1]] = v

    per_minute = metric_series(cw, [
        stat_query("rpm", "AWS/ApiGateway", "Count", api, "Sum", 60),
        stat_query("vis", "AWS/SQS", "ApproximateNumberOfMessagesVisible", queue, "Maximum", 60),
        stat_query("age", "AWS/SQS", "ApproximateAgeOfOldestMessage", queue, "Maximum", 60)], start, end)
    rpm = list(per_minute.get("rpm", {}).values())
    out["api"]["requests_per_minute_mean"] = round(sum(rpm) / (period / 60), 1)
    out["api"]["requests_per_minute_max"] = max(rpm) if rpm else 0
    for qid, name in (("vis", "visible"), ("age", "age_s")):
        pts = sorted(per_minute.get(qid, {}).values())
        out["queue"][f"{name}_minute_median"] = st.median(pts) if pts else None
        out["queue"][f"{name}_minute_max"] = pts[-1] if pts else None
    return out

def write(out, name, obj):
    with open(out / name, "w") as f:
        json.dump(obj, f, indent = 1)
    print(f"wrote {out / name}")

def main():
    p = argparse.ArgumentParser(description = __doc__.split("\n\n")[0])
    p.add_argument("--profile", default = "dev")
    p.add_argument("--region", default = "us-east-2")
    p.add_argument("--out", default = str(Path(__file__).parent / "inputs" / "m9"))
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents = True, exist_ok = True)
    s = boto3.Session(profile_name = args.profile, region_name = args.region)
    ssm = s.client("ssm")
    bucket = ssm.get_parameter(Name = "/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]

    attempts = registry_attempts(s.client("s3"), bucket)
    windows = dict(sorted(batch_windows(attempts).items(), key = lambda kv: kv[1]["start"]))
    memory = lambda_memory(s.client("lambda"))
    tables = anomalypulse_tables(s.client("dynamodb"))
    cw = s.client("cloudwatch")

    batches = {}
    for bid, w in windows.items():
        usage = window_usage(cw, memory, tables, w["start"], w["end"])
        batches[bid] = {"start": w["start"].isoformat(), "end": w["end"].isoformat(),
                        "minutes": round((w["end"] - w["start"]).total_seconds() / 60),
                        "attempts": w["attempts"], "accepted": w["accepted"], "usage": usage}
        fns = usage["functions"]
        print(f"{bid}  {batches[bid]['minutes']} min  {w['attempts']} attempts ({w['accepted']} accepted)  "
              f"api {usage['api']:,}  GB-s {sum(f['gb_s'] for f in fns.values()):,.0f}  "
              f"window builder {fns['anomalypulse-pipeline-window-builder']['invocations']}  "
              f"diagnosis {fns['anomalypulse-diagnosis']['invocations']}")
    write(out, "batches.json", {"memory_mb": memory, "tables": tables, "batches": batches})

    days = {}
    for d in DAYS:
        a = datetime.fromisoformat(d).replace(tzinfo = U)
        days[d] = window_usage(cw, memory, tables, a, a + timedelta(days = 1))
    write(out, "days.json", days)

    runs = normal_runs(attempts)
    for r in runs:
        r["minutes"] = round((r["end"] - r["start"]).total_seconds() / 60)
        r["usage"] = window_usage(cw, memory, tables, r["start"], r["end"])
        r["ops"] = ops_stats(cw, memory, r["start"], r["end"])
        r["start"], r["end"] = r["start"].isoformat(), r["end"].isoformat()
    print(f"{len(runs)} NORMAL test runs, {sum(r['usage']['api'] for r in runs):,} API requests")
    write(out, "normal_runs.json", runs)

    write(out, "ops.json", {bid: ops_stats(cw, memory, w["start"], w["end"]) for bid, w in windows.items()})

    ce = s.client("ce", region_name = "us-east-1")
    write(out, "ce_daily.json", ce_daily(ce, *CE_DAYS))

    logs = s.client("logs")
    reports = {"batches": {bid: diagnosis_reports(logs, w["start"], w["end"]) for bid, w in windows.items()},
               "idle": {"start": IDLE[0].isoformat(), "end": IDLE[1].isoformat(), **diagnosis_reports(logs, *IDLE)}}
    for bid, r in reports["batches"].items():
        print(f"{bid}  diagnosis calls {r['calls']:.0f}  cold starts {r['cold_starts']:.0f}  billed {r['billed_ms']:,.0f} ms")
    print(f"idle {IDLE[0]:%b %d} to {IDLE[1] - timedelta(days = 1):%b %d} UTC  "
          f"diagnosis calls {reports['idle']['calls']:.0f}")
    write(out, "diagnosis_reports.json", reports)

    name = ssm.get_parameter(Name = "/anomalypulse/tables/incidents/name")["Parameter"]["Value"]
    table = s.resource("dynamodb").Table(name)
    incidents = {bid: batch_incidents(table, w["start"], w["end"]) for bid, w in windows.items()}
    for bid, i in incidents.items():
        print(f"{bid}  incidents {i['incidents']}  bedrock calls {i['bedrock_calls']}")
    write(out, "incidents.json", incidents)

if __name__ == "__main__":
    main()
