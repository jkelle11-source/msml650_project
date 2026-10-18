"""Telemetry-richness spot check for an exported dataset (M4-2).

Samples accepted experiments from datasets/<name>/tier2/experiments.jsonl (a
few of each class), reads their exported tier1/ requests and metrics, and
reports any field that is silently empty: present but null in every record of
a service that should fill it. A field that is null only some of the time is
normal (a 400 rejected before the DB call; a minute with no cold start).

What each service should fill (M2's contract, Docs/TELEMETRY_CONTRACT.md):
    every service    correlation fields, status_code, latency_ms,
                     lambda_duration_ms, cold_start
    product, cart, order   db_latency_ms, db_consumed_capacity
    order            dependency, dependency_latency_ms, dependency_error
                     (on POST /orders, which calls payment)
and leave the rest null.

    python -m simulator.spot_check --dataset v1 --profile <your-profile> [--per-class 2]
"""
import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import boto3

from simulator import dataset

_LAYER = Path(__file__).parent.parent / "layers" / "telemetry" / "python"
SCHEMA_FIELDS = tuple(json.loads((_LAYER / "telemetry_schema.json").read_text())["required"])

SERVICES = ("product-service", "cart-service", "order-service", "payment-service")
ALWAYS = ("timestamp", "request_id", "service", "endpoint", "http_method", "status_code",
          "latency_ms", "lambda_duration_ms", "cold_start")
DB = ("db_latency_ms", "db_consumed_capacity")
DEPENDENCY = ("dependency", "dependency_latency_ms", "dependency_error")
FILLS = {
    "product-service": DB,
    "cart-service": DB,
    "order-service": DB + DEPENDENCY,
    "payment-service": (),
}
# Metrics: every service's Lambda; the DynamoDB table behind the services that have one.
METRIC_BLOCKS = {"product-service": ("lambda", "dynamodb"), "cart-service": ("lambda", "dynamodb"),
                 "order-service": ("lambda", "dynamodb"), "payment-service": ("lambda",)}
# Null in a quiet minute, legitimately (no cold start, so no init duration).
METRIC_MAY_BE_NULL = {"avg_init_duration_ms"}


def check_experiment(requests: list[dict], metrics: list[dict]) -> list[str]:
    """Problems with one experiment's exported telemetry. Empty means rich. Pure."""
    problems = []
    by_service = defaultdict(list)
    for record in requests:
        missing = [f for f in SCHEMA_FIELDS if f not in record]
        if missing:
            problems.append(f"a {record.get('service')} record is missing {', '.join(missing)}")
            break
        by_service[record["service"]].append(record)

    for service in SERVICES:
        records = by_service.get(service, [])
        if not records:
            problems.append(f"{service}: no request records")
            continue
        for field in ALWAYS:
            nulls = sum(r[field] is None for r in records)
            if nulls:
                problems.append(f"{service}: {field} is null in {nulls} of {len(records)} records")
        for field in FILLS[service]:
            if all(r[field] is None for r in records):
                problems.append(f"{service}: {field} is null in every record (silently empty)")
        for field in set(DB + DEPENDENCY) - set(FILLS[service]):
            filled = sum(r[field] is not None for r in records)
            if filled:
                problems.append(f"{service}: {field} is set in {filled} records but should be null")

    snapshots = defaultdict(list)
    for snapshot in metrics:
        snapshots[snapshot.get("service")].append(snapshot.get("metrics") or {})
    for service in SERVICES:
        if not snapshots.get(service):
            problems.append(f"{service}: no metrics snapshots")
            continue
        for block in METRIC_BLOCKS[service]:
            blocks = [m.get(block) for m in snapshots[service]]
            if not any(blocks):
                problems.append(f"{service}: metrics have no {block} block")
                continue
            for field in sorted({k for b in blocks if b for k in b} - METRIC_MAY_BE_NULL):
                if all(b is None or b.get(field) is None for b in blocks):
                    problems.append(f"{service}: metrics {block}.{field} is null in every snapshot")
    return problems


def sample(index: list[dict], per_class: int, seed: int = 0) -> list[dict]:
    """`per_class` accepted experiments of each incident type, chosen with a fixed seed."""
    by_type = defaultdict(list)
    for row in sorted(index, key=lambda r: r["slot"]):
        by_type[row["incident_type"]].append(row)
    rng = random.Random(seed)
    return [row for t in sorted(by_type) for row in rng.sample(by_type[t], min(per_class, len(by_type[t])))]


def _jsonl(s3, bucket, key) -> list[dict]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m simulator.spot_check", description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=dataset.DEFAULT_DATASET)
    p.add_argument("--profile", required=True, help="a profile that can read the telemetry bucket")
    p.add_argument("--region", default="us-east-2")
    p.add_argument("--per-class", type=int, default=2, help="experiments per incident type (default: 2)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    bucket = session.client("ssm").get_parameter(
        Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
    s3 = session.client("s3")
    index = _jsonl(s3, bucket, dataset.index_key(args.dataset))
    chosen = sample(index, args.per_class, args.seed)
    print(f"dataset {args.dataset}: {len(index)} accepted experiments; checking {len(chosen)}")

    failed = 0
    for row in chosen:
        exp = row["experiment_id"]
        requests = _jsonl(s3, bucket, dataset.requests_key(args.dataset, exp))
        metrics = _jsonl(s3, bucket, dataset.metrics_key(args.dataset, exp))
        problems = check_experiment(requests, metrics)
        status = "OK  " if not problems else "FAIL"
        print(f"{status}  slot {row['slot']:>3}  {row['incident_type']:<20} {exp}  "
              f"{len(requests)} requests, {len(metrics)} snapshots")
        for problem in problems:
            print(f"        {problem}")
        failed += bool(problems)
    print(f"{len(chosen) - failed} of {len(chosen)} rich" + ("" if not failed else f"; {failed} with problems"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
