"""Per-window request features from a run's requests.jsonl records (M4-5).

One row per one-minute window, with columns per service, e.g.
order_p95_latency_ms. Records must already be deduplicated (the export does it).
"""
from collections import defaultdict

from features import windows

SERVICES = ("product-service", "cart-service", "order-service", "payment-service")


def _prefix(service):
    return service.replace("-service", "").replace("-", "_")


def percentile(values, q):
    """q-th percentile by linear interpolation; None for no values."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _share(recs, low, high):
    if not recs:
        return None
    return sum(1 for r in recs if low <= r["status_code"] <= high) / len(recs)


def request_features(records):
    """Return {window_start: {column: value}}, windows in time order."""
    grouped = defaultdict(lambda: defaultdict(list))
    for r in records:
        grouped[windows.request_window(r["timestamp"])][r["service"]].append(r)

    rows = {}
    for w in sorted(grouped):
        row = {}
        for svc in SERVICES:
            recs = grouped[w].get(svc, [])
            p = _prefix(svc)
            lat = [r["latency_ms"] for r in recs]
            row[f"{p}_request_rate"] = len(recs) / 60.0
            row[f"{p}_p50_latency_ms"] = percentile(lat, 50)
            row[f"{p}_p95_latency_ms"] = percentile(lat, 95)
            row[f"{p}_p99_latency_ms"] = percentile(lat, 99)
            row[f"{p}_error_rate_5xx"] = _share(recs, 500, 599)
            row[f"{p}_error_rate_4xx"] = _share(recs, 400, 499)
        rows[w] = row
    return rows