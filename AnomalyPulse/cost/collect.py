"""Collect the cost estimate's inputs from AWS (M9-4). Read-only.

    python3 -m cost.collect [--profile dev] [--out cost/inputs]     # run from AnomalyPulse/

Writes into --out:
  attempts.jsonl      every registry attempt (all datasets) with its spec, requests sent and run window, and the
                      usage inside that window, from CloudWatch's 5-minute metrics: API Gateway requests, each
                      service Lambda's GB-seconds and invocations, each log group's ingested bytes.
  kinds.json          the mean of that usage per run kind and band, over v1's accepted attempts.
  ce.json             Cost Explorer, Usage records by service and usage type: October, November so far, Nov 11
                      (the M8-5 live batch) and Nov 12 (idle).
  record_types.json   Cost Explorer, November so far by record type (Usage, Tax, Credit).
  lambda.json         every anomalypulse Lambda's memory, and its invocations and GB-seconds on Nov 11 and on
                      Nov 12 00:00-18:00 UTC.

CloudWatch keeps 5-minute points for 63 days, so the October attribution can be collected only until about
2026-12-17; attempts.jsonl keeps it after that. Cost Explorer charges $0.01 per request (about 6 here).
"""
import argparse
import concurrent.futures as cf
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

U = timezone.utc
SERVICE_FNS = ("product", "order", "payment", "cart", "telemetry-shipper")
SERVICE_MB = 1769                                   # every service function's memory (infra/shared/template.yaml)
OCT_WINDOW = (datetime(2026, 10, 15, tzinfo=U), datetime(2026, 10, 26, tzinfo=U))   # every October batch
LAMBDA_WINDOWS = {"nov11": (datetime(2026, 11, 11, tzinfo=U), datetime(2026, 11, 12, tzinfo=U)),
                  "nov12_idle18h": (datetime(2026, 11, 12, tzinfo=U), datetime(2026, 11, 12, 18, tzinfo=U))}
CE_PERIODS = {"oct": ("2026-10-01", "2026-11-01"), "nov": ("2026-11-01", "2026-11-13"),
              "nov11": ("2026-11-11", "2026-11-12"), "nov12": ("2026-11-12", "2026-11-13")}


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def registry_attempts(s3, bucket):
    """Every attempt in every dataset's registry slots."""
    datasets = [p["Prefix"].split("/")[1] for p in
                s3.list_objects_v2(Bucket=bucket, Prefix="datasets/", Delimiter="/").get("CommonPrefixes", [])]
    keys = [k["Key"] for d in datasets
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"datasets/{d}/tier2/registry/slots/")
            for k in page.get("Contents", [])]

    def get(key):
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    rows = []
    with cf.ThreadPoolExecutor(32) as ex:
        for slot in ex.map(get, keys):
            for a in slot["attempts"]:
                rows.append({"dataset": slot["dataset"], "slot": slot["slot"], "phase": slot.get("phase"),
                             "label": slot.get("incident_type"), "spec": slot.get("spec"),
                             "experiment_id": a["experiment_id"], "verdict": a.get("verdict"),
                             "started_at": a.get("started_at"), "ended_at": a.get("ended_at"),
                             "sent": (a.get("client") or {}).get("sent")})
    return rows


def metric_series(cw, queries, start, end):
    """{query id: {timestamp (UTC): value}} for 5-minute sums."""
    series, token = {}, None
    while True:
        kw = dict(MetricDataQueries=queries, StartTime=start, EndTime=end)
        if token:
            kw["NextToken"] = token
        r = cw.get_metric_data(**kw)
        for res in r["MetricDataResults"]:
            # boto3 returns these in local time; normalise to UTC.
            series.setdefault(res["Id"], {}).update({t.astimezone(U): v for t, v in zip(res["Timestamps"], res["Values"])})
        token = r.get("NextToken")
        if not token:
            return series


def window_sum(points, start, end):
    """The sum of 5-minute points over [start, end), a partial point counted by its overlap."""
    total = 0.0
    for t, v in points.items():
        lo, hi = max(start, t), min(end, t + timedelta(minutes=5))
        if hi > lo:
            total += v * (hi - lo).total_seconds() / 300
    return total


def attribute(cw, attempts):
    """Each attempt's usage inside its run window, from 5-minute metrics over October's batches."""
    def q(qid, namespace, metric, dim, value):
        return {"Id": qid, "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": metric,
                "Dimensions": [{"Name": dim, "Value": value}]}, "Period": 300, "Stat": "Sum"}}
    queries = [q("api", "AWS/ApiGateway", "Count", "ApiName", "anomalypulse-api")]
    for f in SERVICE_FNS:
        k = f.replace("-", "_")
        queries += [q(f"dur_{k}", "AWS/Lambda", "Duration", "FunctionName", f"anomalypulse-{f}"),
                    q(f"inv_{k}", "AWS/Lambda", "Invocations", "FunctionName", f"anomalypulse-{f}"),
                    q(f"log_{k}", "AWS/Logs", "IncomingBytes", "LogGroupName", f"/aws/lambda/anomalypulse-{f}")]
    series = metric_series(cw, queries, *OCT_WINDOW)
    for r in attempts:
        if not (r["started_at"] and r["ended_at"]):
            continue
        a, b = parse(r["started_at"]), parse(r["ended_at"])
        u = {"minutes": round((b - a).total_seconds() / 60, 3), "api": round(window_sum(series["api"], a, b), 1)}
        for f in SERVICE_FNS:
            k = f.replace("-", "_")
            u[f"gbs_{f}"] = round(window_sum(series[f"dur_{k}"], a, b) / 1000 * SERVICE_MB / 1024, 2)
            u[f"inv_{f}"] = round(window_sum(series[f"inv_{k}"], a, b), 1)
            u[f"log_{f}"] = round(window_sum(series[f"log_{k}"], a, b))
        r["usage"] = u
    totals = {"api": sum(series["api"].values()),
              "gbs": sum(v / 1000 * SERVICE_MB / 1024 for f in SERVICE_FNS for v in series[f"dur_{f.replace('-', '_')}"].values())}
    return totals


def kind_of(spec):
    """(kind, band) for a v1 spec: a cascade's kind is its stages, a single fault's its injector (and target)."""
    if "cascade" in spec:
        stages = spec["cascade"]["stages"]
        return "cascade", "+".join(s["injector"] + (f":{s['target']}" if s.get("target") else "") for s in stages)
    i = spec["incident"]
    band = i.get("band")
    return (i["injector"] + (f":{i['target']}" if i.get("target") else ""),
            None if not band else [band["low"], band["high"]])


def kinds(attempts):
    """Mean usage per kind and band over v1's accepted attempts (train endpoint mix, 600 s runs)."""
    groups = defaultdict(list)
    for r in attempts:
        if r["dataset"] == "v1" and r["verdict"] == "accepted" and "usage" in r:
            groups[json.dumps(kind_of(r["spec"]))].append(r)
    out = []
    for key, rs in sorted(groups.items()):
        kind, band = json.loads(key)
        mean = lambda f: st.mean(f(r["usage"]) for r in rs)
        row = {"kind": kind, "band": band, "n": len(rs), "minutes": mean(lambda u: u["minutes"]),
               "api": mean(lambda u: u["api"]), "sent": st.mean(r["sent"] for r in rs),
               "gbs": mean(lambda u: sum(u[f"gbs_{f}"] for f in SERVICE_FNS)),
               "inv": mean(lambda u: sum(u[f"inv_{f}"] for f in SERVICE_FNS)),
               "log_mb": mean(lambda u: sum(u[f"log_{f}"] for f in SERVICE_FNS)) / 1e6}
        for f in SERVICE_FNS:
            row[f"gbs_{f}"] = mean(lambda u, f=f: u[f"gbs_{f}"])
            row[f"inv_{f}"] = mean(lambda u, f=f: u[f"inv_{f}"])
        out.append(row)
    return out


def ce_usage(ce, start, end):
    out, token = [], None
    while True:
        kw = dict(TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY", Metrics=["UnblendedCost", "UsageQuantity"],
                  Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
                  GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
        if token:
            kw["NextPageToken"] = token
        r = ce.get_cost_and_usage(**kw)
        for m in r["ResultsByTime"]:
            for g in m["Groups"]:
                out.append({"service": g["Keys"][0], "usage_type": g["Keys"][1],
                            "cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                            "qty": float(g["Metrics"]["UsageQuantity"]["Amount"]), "unit": g["Metrics"]["UsageQuantity"]["Unit"]})
        token = r.get("NextPageToken")
        if not token:
            return out


def ce_record_types(ce, start, end):
    r = ce.get_cost_and_usage(TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY", Metrics=["UnblendedCost"],
                              GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}])
    totals = defaultdict(float)
    for m in r["ResultsByTime"]:
        for g in m["Groups"]:
            totals[g["Keys"][0]] += float(g["Metrics"]["UnblendedCost"]["Amount"])
    return dict(totals)


def lambda_daily(lam, cw):
    fns = [f for page in lam.get_paginator("list_functions").paginate() for f in page["Functions"]
           if f["FunctionName"].startswith("anomalypulse")]
    out = {}
    for f in sorted(fns, key=lambda f: f["FunctionName"]):
        name, mb = f["FunctionName"], f["MemorySize"]
        row = {"memory_mb": mb, "arch": f["Architectures"][0]}
        for key, (a, b) in LAMBDA_WINDOWS.items():
            r = cw.get_metric_data(StartTime=a, EndTime=b, MetricDataQueries=[
                {"Id": i, "MetricStat": {"Metric": {"Namespace": "AWS/Lambda", "MetricName": m,
                 "Dimensions": [{"Name": "FunctionName", "Value": name}]}, "Period": 86400, "Stat": "Sum"}}
                for i, m in (("inv", "Invocations"), ("dur", "Duration"))])
            v = {x["Id"]: sum(x["Values"]) for x in r["MetricDataResults"]}
            row[key] = {"invocations": v["inv"], "gb_s": round(v["dur"] / 1000 * mb / 1024, 2)}
        out[name] = row
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--profile", default="dev")
    p.add_argument("--region", default="us-east-2")
    p.add_argument("--out", default=str(Path(__file__).parent / "inputs"))
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    s = boto3.Session(profile_name=args.profile, region_name=args.region)
    bucket = s.client("ssm").get_parameter(Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
    cw = s.client("cloudwatch")

    attempts = registry_attempts(s.client("s3"), bucket)
    totals = attribute(cw, attempts)
    with open(out / "attempts.jsonl", "w") as f:
        for r in sorted(attempts, key=lambda r: (r["dataset"], r["slot"], r["started_at"] or "")):
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    in_runs = sum(r["usage"]["api"] for r in attempts if "usage" in r)
    sent = sum(r["sent"] or 0 for r in attempts if "usage" in r)
    print(f"{len(attempts)} attempts; Oct 15-26 API requests {totals['api']:,.0f}, {in_runs / totals['api']:.1%} inside "
          f"attempt windows; inside / workload sent = {in_runs / sent:.3f}")
    json.dump(kinds(attempts), open(out / "kinds.json", "w"), indent=1)

    ce = s.client("ce", region_name="us-east-1")
    json.dump({k: ce_usage(ce, *v) for k, v in CE_PERIODS.items()}, open(out / "ce.json", "w"), indent=1)
    json.dump({"period": CE_PERIODS["nov"], "totals": ce_record_types(ce, *CE_PERIODS["nov"])},
              open(out / "record_types.json", "w"), indent=1)
    json.dump(lambda_daily(s.client("lambda"), cw), open(out / "lambda.json", "w"), indent=1)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
