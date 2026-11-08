"""Build Bedrock's input for one incident (M8-3). Pure: no AWS, no clock.

    build_input(state, base, window) -> the dict genai.contract.validate_input accepts

state:  incident.py's state for the open incident.
base:   the baseline fixed when it opened: column -> mean (genai.affected.baseline over up to
        5 NORMAL, non-anomalous windows before started_at). May be empty.
window: the latest feature window: window_start plus the 106 columns, as plain numbers
        (convert DynamoDB's Decimal before calling).

No observed_sequence in this version: it is optional, and without it Bedrock makes no
ordering claim.
"""
from datetime import datetime

from genai import affected
from genai.contract import (ALERT_COUNTS, CONTRACT_VERSION, MEDIUM_5XX, MEDIUM_P95_RATIO,
                            METRICS, SERVICE_METRICS, SERVICES)

MAX_METRICS = 10
DECIMALS = 4
# Sent for every affected service, so Bedrock can also say what did not move.
STANDARD = ("request_rate", "p95_latency_ms", "error_rate_5xx")
# The incident type's own signals (DIAGNOSIS_CONTRACT.md, open question 2), for each affected
# service that has them: sent even when they did not move.
TYPE_SIGNALS = {
    "TRAFFIC_SPIKE": (),
    "DATABASE_THROTTLING": ("throttled_requests", "read_throttle_events", "write_throttle_events",
                            "db_latency_ms_p95", "db_consumed_read_capacity"),
    "LAMBDA_DEGRADATION": ("lambda_duration_ms_mean",),
    "DEPENDENCY_FAILURE": ("dependency_latency_ms_mean", "dependency_latency_ms_p95",
                           "dependency_error_count"),
    "CASCADING_FAILURE": (),
}
# The fixed order among metrics of the same tier, so the list is the same every time.
PRIORITY = ("error_rate_5xx", "p95_latency_ms", "request_rate", *ALERT_COUNTS,
            "dependency_latency_ms_p95", "dependency_latency_ms_mean",
            "lambda_duration_ms_mean", "db_latency_ms_p95", "db_consumed_read_capacity")


def _time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def incident_id(started_at):
    """inc-20261030T143200Z, the same for the whole incident."""
    return "inc-" + _time(started_at).strftime("%Y%m%dT%H%M%SZ")


def _column(service, metric):
    return f"{service.split('-')[0]}_{metric}"


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _rank(metric):
    return PRIORITY.index(metric) if metric in PRIORITY else len(PRIORITY)


def _moved(metric, base, current):
    """Rises only, by the lines genai.affected uses. A baseline of None or 0 moves nothing,
    except a count above zero and a 5xx share, which need no baseline."""
    if metric in ALERT_COUNTS:
        return current > 0
    if metric == "error_rate_5xx":
        return current >= MEDIUM_5XX
    if base is None or base <= 0:
        return False
    ratio = current / base
    if metric == "request_rate":
        return ratio >= affected.TRAFFIC_RATIO
    return ratio >= MEDIUM_P95_RATIO


def _round(value):
    return None if value is None else round(value, DECIMALS)


def build_input(state, base, window):
    services = affected.affected_services(base, window)
    signals = TYPE_SIGNALS.get(state["incident_type"], ())
    picked = []
    for service in services:
        for metric in SERVICE_METRICS[service]:
            current = window.get(_column(service, metric))
            if not _number(current):
                continue
            baseline = base.get(_column(service, metric))
            baseline = baseline if _number(baseline) else None
            moved = _moved(metric, baseline, current)
            if not (moved or metric in STANDARD or metric in signals):
                continue
            tier = 0 if moved or metric in signals else 1
            picked.append((tier, _rank(metric), metric, SERVICES.index(service), service, baseline, current))
    picked.sort(key=lambda p: p[:4])
    metrics = [{"service": service, "metric": metric, "baseline": _round(baseline),
                "current": _round(current), "unit": METRICS[metric][0]}
               for _, _, metric, _, service, baseline, current in picked[:MAX_METRICS]]
    minutes = int((_time(window["window_start"]) - _time(state["started_at"])).total_seconds() // 60)
    return {
        "contract_version": CONTRACT_VERSION,
        "incident_id": incident_id(state["started_at"]),
        "incident_type": state["incident_type"],
        "confidence": state["confidence"],
        "started_at": state["started_at"],
        "window_start": window["window_start"],
        "duration_minutes": minutes + 1,
        "affected_services": services,
        "metrics": metrics,
    }