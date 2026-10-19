"""Per-window Lambda and dependency features from requests.jsonl records (M4-5).

Same shape as request_features: {window_start: {column: value}}.
Only order-service has a dependency (payment), so only it gets dependency columns.
"""
from collections import defaultdict

from features import windows
from features.request_features import SERVICES, _prefix, percentile


def _mean(values):
    return sum(values) / len(values) if values else None


def service_features(records):
    grouped = defaultdict(lambda: defaultdict(list))
    for r in records:
        grouped[windows.request_window(r["timestamp"])][r["service"]].append(r)

    rows = {}
    for w in sorted(grouped):
        row = {}
        for svc in SERVICES:
            recs = grouped[w].get(svc, [])
            p = _prefix(svc)
            dur = [r["lambda_duration_ms"] for r in recs]
            row[f"{p}_lambda_duration_ms_mean"] = _mean(dur)
            row[f"{p}_lambda_duration_ms_p95"] = percentile(dur, 95)
            row[f"{p}_cold_start_count"] = sum(1 for r in recs if r["cold_start"])
        deps = [r for r in grouped[w].get("order-service", []) if r.get("dependency") is not None]
        dep_lat = [r["dependency_latency_ms"] for r in deps if r["dependency_latency_ms"] is not None]
        row["order_dependency_latency_ms_mean"] = _mean(dep_lat)
        row["order_dependency_latency_ms_p95"] = percentile(dep_lat, 95)
        row["order_dependency_error_count"] = sum(1 for r in deps if r["dependency_error"])
        rows[w] = row
    return rows