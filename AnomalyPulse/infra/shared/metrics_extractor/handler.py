"""CloudWatch metrics extractor (M2-5).

Pulls Tier-1 observable metrics directly from CloudWatch's metrics API --
signals that can't be captured by a service's own print() telemetry, because
they happen outside the Lambda's own code: e.g. DynamoDB throttling a request
before it ever reaches a handler, or a Lambda invocation rejected by a
concurrency limit, which therefore never runs and never logs a line.

Join convention (how a metrics snapshot ties back to a service's log records):
snapshots are partitioned under metrics/service=<svc>/dt=<date>/, and each
embedded block also carries its own `table_name` / `function_name`. Because
every resource is named `anomalypulse-<svc>` (see template.yaml), that resource
name is the join key back to the owning service. The M4 aggregator joins
metrics to the per-request log records by service + one-minute time bucket.
"""
import boto3
from datetime import datetime, timedelta, timezone
import json
import os

# Region-less clients: on Lambda, boto3 resolves the region from the AWS_REGION
# the runtime injects -- i.e. the stack's own region -- so the module isn't
# pinned to one region. This matches the shipper's `boto3.client("s3")`.
cloudwatch = boto3.client("cloudwatch")
s3 = boto3.client("s3")


def _weighted_average(datapoints):
    """Mean of per-minute Averages weighted by each minute's SampleCount.

    A plain mean of the per-minute averages would weight a busy minute (many
    invocations) the same as a quiet one (a single invocation) and misreport the
    true figure. Weighting each minute's Average by its SampleCount recovers the
    real overall average. Returns None when the window had no samples at all.
    """
    total_samples = sum(dp["SampleCount"] for dp in datapoints)
    if not total_samples:
        return None
    weighted_sum = sum(dp["Average"] * dp["SampleCount"] for dp in datapoints)
    return weighted_sum / total_samples


def get_dynamodb_metrics(table_name, window_end=None, minutes=1):
    """Pull Tier-1 DynamoDB signals for the `minutes` ending at `window_end`.

    `window_end` is the (settled) end of the slice to read: the handler passes a
    minute boundary a couple of minutes in the past, so CloudWatch has finished
    publishing that minute before we ask for it. Defaults to now for ad-hoc calls.
    """
    end = window_end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    def _sum(metric_name):
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/DynamoDB",
            MetricName=metric_name,
            Dimensions=[{"Name": "TableName", "Value": table_name}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=["Sum"],
        )
        # sum([]) == 0, so an idle table naturally reports 0 with no special case.
        return sum(dp["Sum"] for dp in response.get("Datapoints", []))

    return {
        "table_name": table_name,
        "consumed_read_capacity": _sum("ConsumedReadCapacityUnits"),
        "consumed_write_capacity": _sum("ConsumedWriteCapacityUnits"),
        # ThrottledRequests is the plan-named signal (README S3), but CloudWatch
        # publishes it with an Operation dimension, so a TableName-only query can
        # come back empty. ReadThrottleEvents / WriteThrottleEvents are documented
        # table-level counters, so they are the reliable throttle signal for the
        # DB-throttling classifier regardless of how ThrottledRequests aggregates.
        # We keep all three and reconcile them against a real provisioned-capacity
        # table in M3, where throttling actually occurs.
        "throttled_requests": _sum("ThrottledRequests"),
        "read_throttle_events": _sum("ReadThrottleEvents"),
        "write_throttle_events": _sum("WriteThrottleEvents"),
    }


def get_lambda_metrics(function_name, window_end=None, minutes=1):
    """Pull Tier-1 Lambda signals for the `minutes` ending at `window_end`.

    `window_end` works exactly as in get_dynamodb_metrics: the settled end of
    the slice to read. Defaults to now for ad-hoc calls.
    """
    end = window_end or datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    def _datapoints(metric_name, statistics):
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName=metric_name,
            Dimensions=[{"Name": "FunctionName", "Value": function_name}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=statistics,
        )
        return response.get("Datapoints", [])

    duration_dps = _datapoints("Duration", ["Average", "SampleCount"])
    throttle_dps = _datapoints("Throttles", ["Sum"])
    # InitDuration is emitted once per cold start. Summing its SampleCount is the
    # true cold-start COUNT; len(datapoints) would instead count *minutes that had
    # at least one cold start*, undercounting any minute with more than one.
    init_dps = _datapoints("InitDuration", ["Average", "SampleCount"])

    return {
        "function_name": function_name,
        "avg_duration_ms": _weighted_average(duration_dps),
        "throttle_count": sum(dp["Sum"] for dp in throttle_dps),
        "cold_starts": int(sum(dp["SampleCount"] for dp in init_dps)),
        "avg_init_duration_ms": _weighted_average(init_dps),
    }


def write_metrics_to_s3(bucket_name, service_name, metrics, now=None):
    """Write a metrics snapshot to S3, in its own prefix beside the log records.

    Snapshots live under metrics/ (not raw/) so the M4 aggregator can scan the
    per-request log records in raw/ without tripping over snapshots -- whose shape
    is entirely different and which are not validated against the telemetry
    schema. Partitioning still mirrors raw/ (service + date) so the two remain
    joinable.
    """
    now = now or datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y%m%dT%H%M%S%fZ")

    record = {
        "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "service": service_name,
        "metrics": metrics,
    }

    key = f"metrics/service={service_name}/dt={date_str}/metrics-{timestamp_str}.json"
    s3.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
    )
    return key


# ---------------------------------------------------------------------------
# Scheduled entry point. EventBridge invokes handler() once a minute (wired in
# template.yaml); each run reads one *settled* minute of metrics for every
# monitored service and writes one snapshot per service to S3.
# ---------------------------------------------------------------------------

def _parse_resource_map(raw):
    """Parse a 'service=resource,service=resource' env string into pairs.

    Returns [(service, resource), ...]. Blank entries are skipped, so a trailing
    comma or an unset variable is harmless.
    """
    pairs = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        service, _, resource = item.partition("=")
        pairs.append((service.strip(), resource.strip()))
    return pairs


def _settled_window_end(now, lag_minutes):
    """End of the most recent minute that is `lag_minutes` old.

    Floor `now` to the minute boundary, then step back `lag_minutes` so
    CloudWatch has finished publishing that minute before we read it. Paired
    with get_*_metrics(..., minutes=1) this reads exactly one settled minute.
    """
    current_minute = now.replace(second=0, microsecond=0)
    return current_minute - timedelta(minutes=lag_minutes)


def handler(event, context):
    bucket = os.environ["BUCKET_NAME"]
    lag_minutes = int(os.environ.get("METRICS_LAG_MINUTES", "2"))

    # Fold the two env lists into service -> {"table": name, "function": name}.
    resources = {}
    for service, table_name in _parse_resource_map(os.environ.get("MONITORED_TABLES", "")):
        resources.setdefault(service, {})["table"] = table_name
    for service, function_name in _parse_resource_map(os.environ.get("MONITORED_FUNCTIONS", "")):
        resources.setdefault(service, {})["function"] = function_name

    window_end = _settled_window_end(datetime.now(timezone.utc), lag_minutes)

    written, failed = [], []
    for service, refs in resources.items():
        try:
            metrics = {}
            if "table" in refs:
                metrics["dynamodb"] = get_dynamodb_metrics(refs["table"], window_end=window_end, minutes=1)
            if "function" in refs:
                metrics["lambda"] = get_lambda_metrics(refs["function"], window_end=window_end, minutes=1)
            # Stamp the snapshot with the minute it represents (window_end), not
            # wall-clock now, so M4 joins it to the matching one-minute window.
            written.append(write_metrics_to_s3(bucket, service, metrics, now=window_end))
        except Exception as exc:
            # Orchestration-layer guard (deferred from review): one service's
            # failure must not sink the others, and we must NOT persist a
            # snapshot of false zeros -- a gap M4 can see beats a fabricated 0.
            print(json.dumps({"metrics_error": str(exc), "service": service}))
            failed.append(service)

    return {"window_end": window_end.isoformat(), "written": written, "failed": failed}
