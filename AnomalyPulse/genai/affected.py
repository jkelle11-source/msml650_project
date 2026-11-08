"""Which services an incident affects (M6-1), for M8-3 to put in the incident record.

    affected_services(baseline(lookback_windows), latest_window) -> ["order-service", "payment-service"]

The list is both the diagnosis input's `affected_services` and the dashboard's
`prediction.affected_services` (a service in it shows DEGRADED). Windows are feature
rows keyed by column (`order_p95_latency_ms`, ...; Docs/FEATURE_MATRIX.md), so only
Tier-1 values decide it, never the manifest: a prediction never borrows from ground
truth.

A service is affected when, in the latest window, one of its own signals crosses the
severity rubric's MEDIUM line (5xx share at least 0.01, p95 at least 2x its baseline,
any throttle or dependency-error count above zero), or its request rate is at least
1.75x its baseline. If none does, the service whose request rate or p95 rose most
against its baseline, so the list is never empty. Calibrated on v1's training and
validation runs (Docs/DIAGNOSIS_CONTRACT.md, "affected_services").
"""
from statistics import mean

from genai.contract import ALERT_COUNTS, MEDIUM_5XX, MEDIUM_P95_RATIO, SERVICE_METRICS, SERVICES

# Lower than p95's 2x: a spike's first minute is only partly spiked. At 2x, 37 of v1's 718
# traffic-spike windows didn't list Product (12 at 1.75x); at 1.5x, 8 of its 980 NORMAL windows
# listed a service (none at 1.75x).
TRAFFIC_RATIO = 1.75


def _column(service: str, metric: str) -> str:
    return f"{service.split('-')[0]}_{metric}"


def baseline(windows: list[dict]) -> dict:
    """The mean of each column over the windows before the incident began, skipping empty cells
    (a column empty in every window stays None)."""
    columns = {c for w in windows for c in w if c != "window_start"}
    out = {}
    for c in columns:
        values = [w[c] for w in windows if w.get(c) is not None]
        out[c] = mean(values) if values else None
    return out


def _ratio(base: dict, current: dict, service: str, metric: str):
    b, c = base.get(_column(service, metric)), current.get(_column(service, metric))
    return c / b if b and c is not None else None


def _crosses(base: dict, current: dict, service: str) -> bool:
    share = current.get(_column(service, "error_rate_5xx"))
    if share is not None and share >= MEDIUM_5XX:
        return True
    p95 = _ratio(base, current, service, "p95_latency_ms")
    if p95 is not None and p95 >= MEDIUM_P95_RATIO:
        return True
    rate = _ratio(base, current, service, "request_rate")
    if rate is not None and rate >= TRAFFIC_RATIO:
        return True
    return any((current.get(_column(service, m)) or 0) > 0
               for m in ALERT_COUNTS if m in SERVICE_METRICS[service])


def _rise(base: dict, current: dict, service: str) -> float:
    ratios = [_ratio(base, current, service, m) for m in ("request_rate", "p95_latency_ms")]
    return max((r for r in ratios if r is not None), default=0)


def crossing(base: dict, current: dict) -> list[str]:
    """The services whose own signals cross the line in `current`, in SERVICES order; may be empty.
    M8-3 adds these to an open incident's list on each counted minute (no fallback there)."""
    return [s for s in SERVICES if _crosses(base, current, s)]


def affected_services(base: dict, current: dict) -> list[str]:
    """The affected services, in SERVICES order; never empty."""
    return crossing(base, current) or [max(SERVICES, key=lambda s: _rise(base, current, s))]
