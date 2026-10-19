"""Per-window CloudWatch metric features from a run's metrics.jsonl (M4-5).

Same shape as the request features: {window_start: {column: value}}.
Throttle counts are Tier-1 observable counts (CloudWatch), unlike the Tier-2
db_throttled flag. Payment has no table, so it gets no DynamoDB columns.
"""
from features import windows
from features.request_features import DB_SERVICES, _prefix

_DB_COLUMNS = {
    "db_consumed_read_capacity": "consumed_read_capacity",
    "db_consumed_write_capacity": "consumed_write_capacity",
    "throttled_requests": "throttled_requests",
    "read_throttle_events": "read_throttle_events",
    "write_throttle_events": "write_throttle_events",
}


def metrics_features(snapshots):
    rows = {}
    for snap in snapshots:
        w = windows.metrics_window(snap["timestamp"])
        row = rows.setdefault(w, {})
        svc = snap["service"]
        p = _prefix(svc)
        m = snap.get("metrics") or {}
        if svc in DB_SERVICES:
            db = m.get("dynamodb") or {}
            for column, key in _DB_COLUMNS.items():
                row[f"{p}_{column}"] = db.get(key)
        lam = m.get("lambda") or {}
        row[f"{p}_lambda_throttle_count"] = lam.get("throttle_count")
    return dict(sorted(rows.items()))