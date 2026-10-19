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


def labeled_windows(label_rows):
    """The windows M4-4 labeled (labels.jsonl rows with included: true)."""
    return {r["window_start"] for r in label_rows if r.get("included")}


def build_matrix(records, snapshots, bounds=None, lookback=None):
    """{window_start: {column: value}}. Raises BoundaryViolation on a forbidden column.

    bounds=(started_at, ended_at) is the run's own start and end. Windows not wholly
    inside it are dropped BEFORE the temporal features, so no rate of change is
    computed against a part-minute window. lookback is the run's labeled windows
    (labeled_windows()), the only ones temporal features look back over. Both are
    used only to pick windows, never as a column.
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