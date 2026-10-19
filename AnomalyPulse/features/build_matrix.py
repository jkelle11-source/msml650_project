"""Combine the per-window feature sets into one matrix (M4-5).

Usage:  python -m features.build_matrix <requests.jsonl> <metrics.jsonl>
"""
import json
import sys

from features import guard
from features import windows
from features.metrics_features import metrics_features
from features.request_features import request_features
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


def build_matrix(records, snapshots, bounds=None, lookback=None):
    """{window_start: {column: value}}. Raises BoundaryViolation on a forbidden column.

    bounds=(started_at, ended_at) is the run's own start and end. Windows not wholly
    inside it are dropped BEFORE the temporal features, so no rate of change is
    computed against a part-minute window. lookback is lookback_windows() of the
    run's labels, the only windows temporal features look back over. Both are used
    only to pick windows, never as a column.
    """
    parts = (request_features(records), service_features(records), metrics_features(snapshots))
    rows = {}
    for w in sorted(set().union(*parts)):
        row = {}
        for part in parts:
            row.update(part.get(w, {}))
        rows[w] = row
    if bounds:
        rows = {w: r for w, r in rows.items() if windows.is_complete(w, *bounds)}
    for w, feats in temporal_features(rows, lookback=lookback).items():
        rows[w].update(feats)
    columns = set()
    for row in rows.values():
        columns |= set(row)
    guard.check_columns(columns)  # fails the build before anything is written
    return rows


if __name__ == "__main__":
    matrix = build_matrix(load_jsonl(sys.argv[1]), load_jsonl(sys.argv[2]))
    cols = sorted({c for row in matrix.values() for c in row})
    print(f"{len(matrix)} windows, {len(cols)} columns")
    for w, row in matrix.items():
        print(w, "order p95:", row.get("order_p95_latency_ms"),
              "| order lambda mean:", row.get("order_lambda_duration_ms_mean"),
              "| product throttled:", row.get("product_throttled_requests"))