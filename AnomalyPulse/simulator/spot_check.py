"""Telemetry check for an exported dataset (M4-2): is every run's telemetry all there?

Checks every accepted experiment in a batch's queue (by default the newest
batch's; --batch all for the whole dataset). For each it reads the exported
tier1/ requests and metrics, and from tier2/ the run's start and end
(experiments.jsonl) and what the client sent (the registry). A run fails when
its telemetry is silently incomplete:

    requests  every schema field on every record. Per service, a field M2's
              contract fills (Docs/TELEMETRY_CONTRACT.md) is not null in every
              record, and one it leaves null is null:
                  every service   correlation fields, status_code, latency_ms,
                                  lambda_duration_ms, cold_start
                  product, cart, order   db_latency_ms, db_consumed_capacity
                  order           dependency, dependency_latency_ms,
                                  dependency_error (on POST /orders)
              Product, Cart and Order hold one record per request the client
              sent, within the batch's 0.1% allowance for requests with no
              response (a lost stretch of records shows here).
    metrics   one snapshot per service for every minute the run overlaps, each
              with every field the metrics extractor writes, none of them null
              in every snapshot (avg_init_duration_ms aside: a minute with no
              cold start has none).

A field null only some of the time is normal (a 400 rejected before the DB call;
a minute with no cold start).

    python -m simulator.spot_check --dataset v1 --profile <your-profile> [--batch <id>|all] [--per-class N]
"""
import argparse
import json
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config

from simulator import batch, dataset, manifest

_LAYER = Path(__file__).parent.parent / "layers" / "telemetry" / "python"
SCHEMA_FIELDS = tuple(json.loads((_LAYER / "telemetry_schema.json").read_text())["required"])

SERVICES = ("product-service", "cart-service", "order-service", "payment-service")
# The services the client calls; Payment is called by Order.
ENTRY_SERVICES = ("product-service", "cart-service", "order-service")
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
# What the metrics extractor writes in each block (infra/shared/metrics_extractor/handler.py;
# check_metrics.py holds the same sets, and a test keeps them equal). The feature matrix
# reads these with .get(), so a field the extractor stopped writing would be an empty column.
METRIC_FIELDS = {
    "dynamodb": ("table_name", "consumed_read_capacity", "consumed_write_capacity",
                 "throttled_requests", "read_throttle_events", "write_throttle_events"),
    "lambda": ("function_name", "avg_duration_ms", "throttle_count", "cold_starts", "avg_init_duration_ms"),
}
# Every service's Lambda; the DynamoDB table behind the services that have one.
METRIC_BLOCKS = {"product-service": ("lambda", "dynamodb"), "cart-service": ("lambda", "dynamodb"),
                 "order-service": ("lambda", "dynamodb"), "payment-service": ("lambda",)}
# Null in a quiet minute, legitimately (no cold start, so no init duration).
METRIC_MAY_BE_NULL = {"avg_init_duration_ms"}
MINUTE_MS = 60_000
WORKERS = 8


def snapshot_minutes(started_at: str, ended_at: str) -> list[int]:
    """The stamps (epoch ms) of the snapshots a run should have: every minute it overlaps.

    A snapshot stamped T covers [T - 1 min, T), and the export takes those with
    started_at < T and T - 1 min < ended_at (export.Exporter.metrics).
    """
    start, end = manifest.to_epoch_ms(started_at), manifest.to_epoch_ms(ended_at)
    first = (start // MINUTE_MS + 1) * MINUTE_MS
    return list(range(first, end + MINUTE_MS, MINUTE_MS))


def _hhmm(epoch_ms: int) -> str:
    return manifest.to_iso(epoch_ms)[11:16]


def check_requests(requests: list[dict], sent: int) -> list[str]:
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

    received = sum(len(by_service.get(s, [])) for s in ENTRY_SERVICES)
    if abs(received - sent) > batch.MAX_ERROR_SHARE * sent:
        problems.append(f"{received} Product, Cart and Order records for {sent} requests sent "
                        f"({received - sent:+d}): records were lost or duplicated")
    return problems


def check_metrics(metrics: list[dict], started_at: str, ended_at: str) -> list[str]:
    problems = []
    expected = snapshot_minutes(started_at, ended_at)
    by_service = defaultdict(dict)
    for snapshot in metrics:
        stamp = manifest.to_epoch_ms(snapshot["timestamp"])
        by_service[snapshot.get("service")][stamp] = snapshot.get("metrics") or {}

    for service in SERVICES:
        snapshots = by_service.get(service, {})
        missing = [t for t in expected if t not in snapshots]
        if len(missing) == len(expected):
            problems.append(f"{service}: no metrics snapshots")
            continue
        if missing:
            problems.append(f"{service}: no metrics snapshot for {len(missing)} of {len(expected)} minutes "
                            f"(stamped {', '.join(map(_hhmm, missing))})")
        present = [snapshots[t] for t in expected if t in snapshots]
        for block in METRIC_BLOCKS[service]:
            blocks = [m.get(block) for m in present]
            without = sum(not b for b in blocks)
            if without:
                problems.append(f"{service}: {without} of {len(blocks)} metrics snapshots have no {block} block")
            blocks = [b for b in blocks if b]
            if not blocks:
                continue
            for field in METRIC_FIELDS[block]:
                absent = sum(field not in b for b in blocks)
                if absent:
                    problems.append(f"{service}: metrics {block}.{field} is missing from {absent} of "
                                    f"{len(blocks)} snapshots")
                elif field not in METRIC_MAY_BE_NULL and all(b[field] is None for b in blocks):
                    problems.append(f"{service}: metrics {block}.{field} is null in every snapshot")
    return problems


def check_experiment(requests: list[dict], metrics: list[dict], *, started_at: str, ended_at: str,
                     sent: int) -> list[str]:
    """Problems with one experiment's exported telemetry. Empty means complete. Pure.

    `started_at` and `ended_at` are the run's (experiments.jsonl's generated_at
    and ended_at); `sent` is how many requests its client sent (the registry).
    """
    return check_requests(requests, sent) + check_metrics(metrics, started_at, ended_at)


def select(index: list[dict], batches: list[dict], which: str | None) -> tuple[str, list[dict]]:
    """(what was chosen, its rows): the accepted experiments in a batch's queue, or every one ("all").

    A batch's queue, not the runs that batch accepted: a queue resumed in a
    second batch is checked whole. `which` None is the newest batch.
    """
    if which == "all":
        return "every batch", sorted(index, key=lambda r: r["slot"])
    if not batches:
        raise ValueError("the dataset has no batches")
    if which is None:
        chosen = batches[-1]
    else:
        chosen = next((b for b in batches if b["batch_id"] == which), None)
        if chosen is None:
            raise ValueError(f"no batch {which}; the dataset's are {', '.join(b['batch_id'] for b in batches)}")
    slots = set(chosen["queue"]["slots"])
    return (f"the queue of {chosen['batch_id']} ({len(slots)} slots)",
            sorted((r for r in index if r["slot"] in slots), key=lambda r: r["slot"]))


def sample(rows: list[dict], per_class: int, seed: int = 0) -> list[dict]:
    """`per_class` of the rows of each incident type, chosen with a fixed seed."""
    by_type = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["slot"]):
        by_type[row["incident_type"]].append(row)
    rng = random.Random(seed)
    return [row for t in sorted(by_type) for row in rng.sample(by_type[t], min(per_class, len(by_type[t])))]


def _jsonl(s3, bucket, key) -> list[dict]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _sent(record: dict, experiment_id: str) -> int:
    attempt = next(a for a in record["attempts"] if a["experiment_id"] == experiment_id)
    return attempt["client"]["sent"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m simulator.spot_check", description=__doc__.splitlines()[0])
    p.add_argument("--dataset", default=dataset.DEFAULT_DATASET)
    p.add_argument("--profile", required=True, help="a profile that can read the telemetry bucket")
    p.add_argument("--region", default="us-east-2")
    p.add_argument("--batch", help="a batch id, or 'all' (default: the newest batch); its whole queue is checked")
    p.add_argument("--per-class", type=int, help="check only this many experiments per incident type")
    p.add_argument("--seed", type=int, default=0, help="for --per-class's sample")
    args = p.parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    bucket = session.client("ssm").get_parameter(
        Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]
    s3 = session.client("s3", config=Config(max_pool_connections=WORKERS * 3))
    registry = dataset.Registry(s3, bucket, args.dataset)
    index = _jsonl(s3, bucket, dataset.index_key(args.dataset))
    try:
        what, rows = select(index, registry.batches(), args.batch)
    except ValueError as e:
        print(f"spot_check: {e}", file=sys.stderr)
        return 2
    chosen = rows if args.per_class is None else sample(rows, args.per_class, args.seed)
    print(f"dataset {args.dataset}: {len(index)} accepted experiments; {len(rows)} in {what}; "
          f"checking {len(chosen)}")
    if not chosen:
        print("nothing to check")
        return 1

    def check(row):
        exp = row["experiment_id"]
        requests = _jsonl(s3, bucket, dataset.requests_key(args.dataset, exp))
        metrics = _jsonl(s3, bucket, dataset.metrics_key(args.dataset, exp))
        sent = _sent(registry.slot(row["slot"]), exp)
        problems = check_experiment(requests, metrics, started_at=row["generated_at"],
                                    ended_at=row["ended_at"], sent=sent)
        return f"{len(requests)} records for {sent} requests sent, {len(metrics)} snapshots", problems

    failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for row, (counts, problems) in zip(chosen, pool.map(check, chosen)):
            print(f"{'OK  ' if not problems else 'FAIL'}  slot {row['slot']:>3}  {row['incident_type']:<20} "
                  f"{row['experiment_id']}  {counts}")
            for problem in problems:
                print(f"        {problem}")
            failed += bool(problems)
    print(f"{len(chosen) - failed} of {len(chosen)} complete" + (f"; {failed} with problems" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
