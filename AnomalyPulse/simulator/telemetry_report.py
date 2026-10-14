"""Telemetry report for one experiment (M3-2): did the fault show up in landed telemetry?

Reads the experiment's manifest, pulls every telemetry record in its time range
from s3://<bucket>/raw/, and splits them into phases at every point a fault
starts or stops (baseline, each fault or overlap of faults, recovery). Prints
request rate, latency, and 4xx and 5xx share per phase and per service, plus
per-minute request counts so a NORMAL run's stability can be seen.

Run from AnomalyPulse/ with a profile that can read raw/ (your own, not `sim`):

    python -m simulator.telemetry_report <experiment_id> --profile <your-profile>

Wait about two minutes after the run ends: the shipper lands records in batches.
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import boto3

from simulator import manifest as mf

# Payment is invoked by Order, not by the workload, so its records are not
# extra client traffic. It is still reported, just not counted in the total.
CLIENT_FACING = ("product-service", "cart-service", "order-service")
SERVICES = (*CLIENT_FACING, "payment-service")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def phases(manifest: dict) -> list[tuple[str, datetime, datetime]]:
    """Contiguous (name, start, end) spans covering the run, cut wherever a fault starts or stops.

    Before the first fault is "baseline" and after the last is "recovery". A
    span with faults active is named after all of them in the order they
    started, so where a cascade's faults overlap the overlap is its own span
    ("fault: traffic-spike + db-throttling"); a gap between faults is
    "no fault". A NORMAL run is one span, "normal".
    """
    start = _parse(manifest["started_at"])
    end = _parse(manifest["ended_at"])
    windows = [(_parse(a["active_from"]), _parse(a["active_until"]), a["injector"])
               for a in manifest["activations"]]
    if not windows:
        return [("normal", start, end)]
    first, last = min(w[0] for w in windows), max(w[1] for w in windows)
    cuts = sorted({start, end, *(t for a, b, _ in windows for t in (a, b) if start < t < end)})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        # Every window boundary is a cut, so what is active at `a` is active until `b`.
        active = [name for f, u, name in sorted(windows) if f <= a < u]
        if active:
            name = "fault: " + " + ".join(active)
        elif b <= first:
            name = "baseline"
        elif a >= last:
            name = "recovery"
        else:
            name = "no fault"
        out.append((name, a, b))
    return out


def _share(recs, low, high):
    if not recs:
        return None
    return round(sum(low <= r["status_code"] <= high for r in recs) / len(recs), 4)


def summarize(manifest: dict, records: list[dict]) -> dict:
    """Per-phase, per-service stats, and per-minute client-facing request counts. Pure."""
    spans = phases(manifest)
    start, end = _parse(manifest["started_at"]), _parse(manifest["ended_at"])
    grouped = defaultdict(list)          # (phase index, service) -> records
    per_minute = Counter()               # minute index -> client-facing records
    for record in records:
        ts = _parse(record["timestamp"])
        if not start <= ts < end:
            continue
        per_minute[int((ts - start).total_seconds() // 60)] += record["service"] in CLIENT_FACING
        for index, (_, a, b) in enumerate(spans):
            if a <= ts < b:
                grouped[(index, record["service"])].append(record)
                break

    report = {"phases": [], "per_minute": [per_minute[m] for m in
                                           range(int((end - start).total_seconds() // 60))]}
    for index, (name, a, b) in enumerate(spans):
        seconds = (b - a).total_seconds()
        rows = {}
        for service in SERVICES:
            recs = grouped.get((index, service), [])
            latencies = [r["latency_ms"] for r in recs]
            rows[service] = {
                "records": len(recs),
                "rps": round(len(recs) / seconds, 2) if seconds > 0 else None,
                "p50_ms": _percentile(latencies, 0.50),
                "p95_ms": _percentile(latencies, 0.95),
                "4xx_share": _share(recs, 400, 499),
                "5xx_share": _share(recs, 500, 599),
            }
        total = sum(rows[s]["records"] for s in CLIENT_FACING)
        report["phases"].append({
            "phase": name, "from": a.isoformat(), "seconds": seconds,
            "client_rps": round(total / seconds, 2) if seconds > 0 else None,
            "services": rows,
        })
    full = report["per_minute"][1:-1] or report["per_minute"]  # edge minutes are partial
    if len(full) >= 2 and statistics.mean(full) > 0:
        report["per_minute_cv"] = round(statistics.pstdev(full) / statistics.mean(full), 3)
    return report


def _load_records(s3, bucket, start: datetime, end: datetime) -> list[dict]:
    records = []
    days = {(start + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range((end.date() - start.date()).days + 1)}
    # Objects land a little after their records; allow for the shipper's delay.
    landed_by = end + timedelta(minutes=10)
    for service in SERVICES:
        for day in sorted(days):
            prefix = f"raw/service={service}/dt={day}/"
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if not start <= obj["LastModified"] <= landed_by:
                        continue
                    body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
                    for line in body.splitlines():
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "timestamp" in record and "service" in record:
                            records.append(record)
    return records


def _print(manifest, report):
    print(f"{manifest['experiment_id']}  {manifest['incident_type']}  status={manifest['status']}")
    for a in manifest["activations"]:
        print(f"  {a['injector']}: params={a['params']} observed={a['observed']}")
    print()
    # Shares to one decimal: a handful of errors in thousands of requests must not round to 0%.
    width = max(26, *(len(p["phase"]) + 2 for p in report["phases"]))
    print(f"{'phase':<{width}}{'client rps':>11}   " + "".join(f"{s.split('-')[0]:>34}" for s in SERVICES))
    print(f"{'':<{width}}{'':>11}   " + "".join(f"{'rps  p50  p95    4xx    5xx':>34}" for _ in SERVICES))
    for phase in report["phases"]:
        cells = []
        for service in SERVICES:
            row = phase["services"][service]
            if not row["records"]:
                cells.append(f"{'-':>34}")
                continue
            cells.append(f"{row['rps']:>10} {row['p50_ms']:>4.0f} {row['p95_ms']:>4.0f} "
                         f"{row['4xx_share']:>6.1%} {row['5xx_share']:>6.1%}")
        print(f"{phase['phase']:<{width}}{phase['client_rps']:>11}   " + "".join(cells))
    print()
    print("client-facing records per minute:", report["per_minute"])
    if "per_minute_cv" in report:
        print(f"per-minute variation (CV, full minutes): {report['per_minute_cv']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("experiment_id")
    p.add_argument("--profile", required=True, help="a profile that can read the telemetry bucket")
    p.add_argument("--region", default="us-east-2")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    args = p.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    bucket = session.client("ssm").get_parameter(
        Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
    s3 = session.client("s3")
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=mf.manifest_key(args.experiment_id))
                          ["Body"].read())
    if manifest["ended_at"] is None:
        # Still RUNNING: the run died without recording an end, so there is no
        # time range to report on.
        print(f"error: {args.experiment_id} is still {manifest['status']} with no end time: "
              f"the run died, and its telemetry isn't usable", file=sys.stderr)
        return 1
    if manifest["status"] != "COMPLETED":
        print(f"warning: this run is {manifest['status']}, not COMPLETED; M4 won't use it")
    records = _load_records(s3, bucket, _parse(manifest["started_at"]), _parse(manifest["ended_at"]))
    report = summarize(manifest, records)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(manifest, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
