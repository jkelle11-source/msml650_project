"""Generate the feature-matrix documentation: column -> definition -> tier (M4-5).

The column list comes from what build_matrix really produces, so the doc cannot
drift. A column with no definition here raises, so adding a feature forces a doc
update.   Usage:  python3 document_columns.py   (writes Docs/FEATURE_MATRIX.md)
"""
import re
from pathlib import Path

import build_matrix
from request_features import SERVICES, _prefix
from temporal_features import WINDOW

OUT = Path(__file__).resolve().parents[2] / "Docs" / "FEATURE_MATRIX.md"
S = "|".join(_prefix(s) for s in SERVICES)

# (regex on the column name, definition, source) - the first match wins.
RULES = [
    (rf"^(?P<base>.+)_roll_mean_{WINDOW}$",
     "Mean of `{base}` over this window and up to {prev} earlier labeled windows of the same run; "
     "empty in a run's first labeled window",
     "`{base}`"),
    (rf"^(?P<base>.+)_roll_std_{WINDOW}$",
     "Standard deviation of `{base}` over the same windows; empty in a run's first labeled window",
     "`{base}`"),
    (r"^(?P<base>.+)_change$",
     "`{base}` minus its value in the previous labeled window; empty in a run's first labeled window",
     "`{base}`"),
    (rf"^(?P<s>{S})_request_rate$",
     "Requests per second to {s}-service in the window (count / 60)", "request records"),
    (rf"^(?P<s>{S})_p(?P<q>50|95|99)_latency_ms$",
     "{q}th percentile of end-to-end latency for {s}-service requests; empty when there were none",
     "`latency_ms`"),
    (rf"^(?P<s>{S})_error_rate_5xx$",
     "Share of {s}-service requests with status 500-599; empty when there were none", "`status_code`"),
    (rf"^(?P<s>{S})_error_rate_4xx$",
     "Share of {s}-service requests with status 400-499; empty when there were none", "`status_code`"),
    (rf"^(?P<s>{S})_lambda_duration_ms_mean$",
     "Mean CPU time of the {s}-service handler body; empty when there were no requests",
     "`lambda_duration_ms`"),
    (rf"^(?P<s>{S})_lambda_duration_ms_p95$",
     "95th percentile CPU time of the {s}-service handler body; empty when there were no requests",
     "`lambda_duration_ms`"),
    (rf"^(?P<s>{S})_cold_start_count$",
     "Number of {s}-service requests that paid a cold start", "`cold_start`"),
    (r"^order_dependency_latency_ms_mean$",
     "Mean time Order spent calling Payment; empty when Order made no calls", "`dependency_latency_ms`"),
    (r"^order_dependency_latency_ms_p95$",
     "95th percentile time Order spent calling Payment; empty when Order made no calls",
     "`dependency_latency_ms`"),
    (r"^order_dependency_error_count$",
     "Order's calls to Payment that failed or timed out (telemetry cannot tell a timeout from another failure)",
     "`dependency_error`"),
    (rf"^(?P<s>{S})_db_consumed_read_capacity$",
     "DynamoDB read capacity units consumed by {s}'s table in the minute (CloudWatch)",
     "CloudWatch ConsumedReadCapacityUnits"),
    (rf"^(?P<s>{S})_db_consumed_write_capacity$",
     "DynamoDB write capacity units consumed by {s}'s table in the minute (CloudWatch)",
     "CloudWatch ConsumedWriteCapacityUnits"),
    (rf"^(?P<s>{S})_throttled_requests$",
     "Count of throttled DynamoDB requests on {s}'s table in the minute (CloudWatch count; not the Tier-2 db_throttled flag)",
     "CloudWatch ThrottledRequests"),
    (rf"^(?P<s>{S})_read_throttle_events$",
     "Count of throttled DynamoDB reads on {s}'s table in the minute (CloudWatch)",
     "CloudWatch ReadThrottleEvents"),
    (rf"^(?P<s>{S})_write_throttle_events$",
     "Count of throttled DynamoDB writes on {s}'s table in the minute (CloudWatch)",
     "CloudWatch WriteThrottleEvents"),
    (rf"^(?P<s>{S})_lambda_throttle_count$",
     "Count of {s}-service invocations Lambda throttled in the minute; a throttled request never reaches the handler (CloudWatch)",
     "CloudWatch Lambda Throttles"),
]


def describe(column):
    """(definition, source) for a column; raises ValueError if it has none."""
    for pattern, definition, source in RULES:
        m = re.match(pattern, column)
        if m:
            fields = dict(m.groupdict(), prev=WINDOW - 1)
            return definition.format(**fields), source.format(**fields)
    raise ValueError(f"no definition for column {column!r}: add a rule in document_columns.py")


def columns():
    """Every column build_matrix produces, from synthetic records for all four services."""
    records, snaps = [], []
    for svc in SERVICES:
        is_order = svc == "order-service"
        records.append({
            "timestamp": "2026-01-01T00:00:10.000Z", "service": svc, "status_code": 200,
            "latency_ms": 1.0, "lambda_duration_ms": 1.0, "cold_start": False,
            "dependency": "payment-service" if is_order else None,
            "dependency_latency_ms": 1.0 if is_order else None,
            "dependency_error": False if is_order else None})
        metrics = {"lambda": {"throttle_count": 0.0}}
        if svc != "payment-service":  # Payment has no table
            metrics["dynamodb"] = {"consumed_read_capacity": 1.0, "consumed_write_capacity": 1.0,
                                   "throttled_requests": 0, "read_throttle_events": 0,
                                   "write_throttle_events": 0}
        snaps.append({"timestamp": "2026-01-01T00:01:00.000Z", "service": svc, "metrics": metrics})
    rows = build_matrix.build_matrix(records, snaps)
    return sorted({c for row in rows.values() for c in row})


def render():
    cols = columns()
    lines = [
        "# Feature matrix",
        "",
        "Generated by `AnomalyPulse/features/document_columns.py`; do not edit by hand. "
        "Re-run it when a feature changes.",
        "",
        "- **One row per one-minute window** of one experiment, named by the window's start time. "
        "The join key to labels is `(experiment_id, window start)`; neither is a column.",
        "- **Every column is Tier-1**: derived from observable telemetry or CloudWatch counts. "
        "No Tier-2 name, join key, label or fault parameter is a column. "
        "`features/guard.py` fails the build if one appears.",
        "- **Empty means unknown, not zero.** A service with no requests in a window has an empty "
        "latency or error rate. Counts and request rates are `0`.",
        "- **Temporal columns use only the current and earlier windows** of the same run.",
        "- **Temporal columns look back over labeled windows only** (M4-4's `labels.jsonl`, `included: true`; "
        "`write_matrix.py --labels`). An excluded window gets temporal columns from itself and the earlier "
        "labeled windows, but never feeds a later one. So the DB-throttling surge, which M4-4 excludes as "
        "onset, never reaches the first labeled throttling window: it is how the injector spends DynamoDB's "
        "burst capacity, and real throttling has nothing like it. Nor do startup's cold starts reach the "
        "baseline. Blanking the columns that drew on an excluded window instead would leave blanks in "
        "training only where DB throttling starts, a fingerprint of its own (decided 2026-10-19). "
        "Checked on `m4-3-validation`'s six throttling cascades (2026-10-19): the minute holding "
        "`label_from` had 0.8–3.9% of Product requests failing, as the surge ran out the burst bank, against "
        "0–0.5% in the next minute (1.8% in one). Looking back over every window put that burst in the first "
        "labeled minute's `product_error_rate_5xx_roll_mean_3` (0.3–1.4%); over labeled windows only it is "
        "0–0.9%, that minute's own errors. That minute's `product_p95_latency_ms_change` is now the jump from "
        "baseline (~3,190 ms), as every other incident's first fault minute is, where before it was about 0 "
        "because the jump fell in the excluded minute. A traffic-spike run's fault minutes are unchanged.",
        "- A run's first and last windows can be partial. Which windows to drop is decided with M4-4.",
        "",
        f"{len(cols)} columns.",
        "",
        "| Column | Definition | Source | Tier |",
        "|---|---|---|---|",
    ]
    for col in cols:
        definition, source = describe(col)
        lines.append(f"| `{col}` | {definition} | {source} | Tier-1 |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT} ({len(columns())} columns)")