"""Combine the per-window feature sets into one matrix (M4-5).

Usage:  python -m features.build_matrix <requests.jsonl> <metrics.jsonl>
"""
import json
import sys

from features import guard
from features import windows
from features.metrics_features import metrics_features
from features.request_features import DB_SERVICES, SERVICES, request_features
from features.service_features import service_features
from features.temporal_features import temporal_features


def load_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# Excluded windows that still feed later windows' temporal features. The run's startup
# minute matches its baseline on every trended column (median within 5% over the 20
# validation runs, 5xx 0 in both), and without it a run's first labeled window would
# have no history: blank temporal columns would then mark NORMAL in every run.
LOOKBACK_EXCLUSIONS = {"startup"}


def lookback_windows(label_rows):
    """The windows temporal features look back over: those M4-4 labeled (labels.jsonl
    rows with included: true) and the startup minute."""
    return {r["window_start"] for r in label_rows
            if r.get("included") or r.get("excluded_reason") in LOOKBACK_EXCLUSIONS}


def _combine(records, snapshots):
    parts = (request_features(records), service_features(records), metrics_features(snapshots))
    return {w: {k: v for part in parts for k, v in part.get(w, {}).items()}
            for w in sorted(set().union(*parts))}


def _sample_run():
    """One window in which every service has a request and a CloudWatch snapshot."""
    records, snaps = [], []
    for svc in SERVICES:
        is_order = svc == "order-service"
        records.append({
            "timestamp": "2026-01-01T00:00:10.000Z", "service": svc, "status_code": 200,
            "latency_ms": 1.0, "lambda_duration_ms": 1.0, "cold_start": False,
            "db_latency_ms": 1.0 if svc in DB_SERVICES else None,
            "dependency": "payment-service" if is_order else None,
            "dependency_latency_ms": 1.0 if is_order else None,
            "dependency_error": False if is_order else None})
        metrics = {"lambda": {"throttle_count": 0.0}}
        if svc != "payment-service":  # Payment has no table
            metrics["dynamodb"] = {"consumed_read_capacity": 1.0, "consumed_write_capacity": 1.0,
                                   "throttled_requests": 0, "read_throttle_events": 0,
                                   "write_throttle_events": 0}
        snaps.append({"timestamp": "2026-01-01T00:01:00.000Z", "service": svc, "metrics": metrics})
    return records, snaps


def columns():
    """Every feature column, sorted: what the builder makes of a window in which every
    service has a request and a CloudWatch snapshot. Every matrix row has exactly these."""
    rows = _combine(*_sample_run())
    for w, feats in temporal_features(rows).items():
        rows[w].update(feats)
    return sorted({c for row in rows.values() for c in row})


def build_matrix(records, snapshots, bounds=None, lookback=None):
    """{window_start: {column: value}}, every row with every column of columns().
    Raises BoundaryViolation on a forbidden column.

    bounds=(started_at, ended_at) is the run's own start and end. Windows not wholly
    inside it are dropped BEFORE the temporal features, so no rate of change is
    computed against a part-minute window. lookback is lookback_windows() of the
    run's labels, the only windows temporal features look back over. Both are used
    only to pick windows, never as a column.
    """
    rows = _combine(records, snapshots)
    if bounds:
        rows = {w: r for w, r in rows.items() if windows.is_complete(w, *bounds)}
    for w, feats in temporal_features(rows, lookback=lookback).items():
        rows[w].update(feats)
    cols = columns()
    made = {c for row in rows.values() for c in row}
    guard.check_columns(made | set(cols))  # fails the build before anything is written
    if made - set(cols):
        raise ValueError(f"columns _sample_run() doesn't make (add them there): {sorted(made - set(cols))}")
    # A minute with no CloudWatch snapshot keeps its metrics columns, empty.
    return {w: {c: row.get(c) for c in cols} for w, row in rows.items()}


if __name__ == "__main__":
    matrix = build_matrix(load_jsonl(sys.argv[1]), load_jsonl(sys.argv[2]))
    cols = sorted({c for row in matrix.values() for c in row})
    print(f"{len(matrix)} windows, {len(cols)} columns")
    for w, row in matrix.items():
        print(w, "order p95:", row.get("order_p95_latency_ms"),
              "| order lambda mean:", row.get("order_lambda_duration_ms_mean"),
              "| product throttled:", row.get("product_throttled_requests"))