"""Telemetry report for one experiment (M3-2): did the fault show up in landed telemetry?

Reads the experiment's manifest, pulls every telemetry record in its time range
from s3://<bucket>/raw/, and splits them into baseline / fault / recovery by the
manifest's windows. Prints request rate, latency and 5xx share per phase and per
service, plus per-minute request counts so a NORMAL run's stability can be seen.

Run from AnomalyPulse/ with a profile that can read raw/ (your own, not `sim`):

    python -m simulator.telemetry_report <experiment_id> --profile <your-profile>

Wait about two minutes after the run ends: the shipper lands records in batches.
"""
import argparse
import json
import statistics
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
    """(name, start, end) for baseline, each fault window, and recovery."""
    start = _parse(manifest["started_at"])
    end = _parse(manifest["ended_at"])
    windows = sorted((_parse(a["active_from"]), _parse(a["active_until"]), a["injector"])
                     for a in manifest["activations"])
    if not windows:
        return [("normal", start, end)]
    first, last = windows[0][0], max(w[1] for w in windows)
    out = [("baseline", start, first)]
    out += [(f"fault: {name}", a, b) for a, b, name in windows]
    out.append(("recovery", last, end))
    return out


def summarize(manifest: dict, records: list[dict]) -> dict:
    """Per-phase, per-service stats, and per-minute client-facing request counts. Pure."""
    spans = phases(manifest)
    start, end = _parse(manifest["started_at"]), _parse(manifest["ended_at"])
    grouped = defaultdict(list)          # (phase, service) -> records
    per_minute = Counter()               # minute index -> client-facing records
    for record in records:
        ts = _parse(record["timestamp"])
        if not start <= ts < end:
            continue
        per_minute[int((ts - start).total_seconds() // 60)] += record["service"] in CLIENT_FACING
        for name, a, b in spans:
            if a <= ts < b:
                grouped[(name, record["service"])].append(record)
                break

    report = {"phases": [], "per_minute": [per_minute[m] for m in
                                           range(int((end - start).total_seconds() // 60))]}
    for name, a, b in spans:
        seconds = (b - a).total_seconds()
        rows = {}
        for service in SERVICES:
            recs = grouped.get((name, service), [])
            latencies = [r["latency_ms"] for r in recs]
            rows[service] = {
                "records": len(recs),
                "rps": round(len(recs) / seconds, 2) if seconds > 0 else None,
                "p50_ms": _percentile(latencies, 0.50),
                "p95_ms": _percentile(latencies, 0.95),
                "5xx_share": round(sum(r["status_code"] >= 500 for r in recs) / len(recs), 4)
                if recs else None,
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
    print(f"{'phase':<26}{'client rps':>11}   " + "".join(f"{s.split('-')[0]:>22}" for s in SERVICES))
    print(f"{'':<26}{'':>11}   " + "".join(f"{'rps  p50  p95  5xx':>22}" for _ in SERVICES))
    for phase in report["phases"]:
        cells = []
        for service in SERVICES:
            row = phase["services"][service]
            if not row["records"]:
                cells.append(f"{'-':>22}")
                continue
            cells.append(f"{row['rps']:>6} {row['p50_ms']:>5.0f} {row['p95_ms']:>5.0f} "
                         f"{row['5xx_share']:>4.0%}")
        print(f"{phase['phase']:<26}{phase['client_rps']:>11}   " + "".join(cells))
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
