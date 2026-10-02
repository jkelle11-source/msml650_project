"""CloudWatch metrics extractor (M2-5).

Pulls Tier-1 observable metrics directly from CloudWatch's metrics API --
signals that can't be captured by a service's own print() telemetry, because
they happen outside the Lambda's own code (e.g. DynamoDB throttling a request
before it ever reaches a handler).
"""
import boto3
from datetime import datetime, timedelta, timezone
import json

cloudwatch = boto3.client("cloudwatch", region_name="us-east-2")


def get_dynamodb_metrics(table_name, minutes=5):
    """Pull Tier-1 DynamoDB signals for one table over the last `minutes`."""
    end = datetime.now(timezone.utc)
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
        datapoints = response.get("Datapoints", [])
        return sum(dp["Sum"] for dp in datapoints) if datapoints else 0

    return {
        "table_name": table_name,
        "consumed_read_capacity": _sum("ConsumedReadCapacityUnits"),
        "consumed_write_capacity": _sum("ConsumedWriteCapacityUnits"),
        "throttled_requests": _sum("ThrottledRequests"),
    }

def get_lambda_metrics(function_name, minutes=5):
    """Pull Tier-1 Lambda signals for one function over the last `minutes`."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    def _stat(metric_name, statistic):
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName=metric_name,
            Dimensions=[{"Name": "FunctionName", "Value": function_name}],
            StartTime=start,
            EndTime=end,
            Period=60,
            Statistics=[statistic],
        )
        datapoints = response.get("Datapoints", [])
        values = [dp[statistic] for dp in datapoints]
        return values

    duration_values = _stat("Duration", "Average")
    throttle_values = _stat("Throttles", "Sum")
    init_duration_values = _stat("InitDuration", "Average")

    return {
        "function_name": function_name,
        "avg_duration_ms": sum(duration_values) / len(duration_values) if duration_values else None,
        "throttle_count": sum(throttle_values) if throttle_values else 0,
        "cold_starts": len(init_duration_values),
        "avg_init_duration_ms": sum(init_duration_values) / len(init_duration_values) if init_duration_values else None,
    }

s3 = boto3.client("s3", region_name="us-east-2")


def write_metrics_to_s3(bucket_name, service_name, metrics, now=None):
    """Write a metrics snapshot to S3, alongside the regular log records."""
    now = now or datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y%m%dT%H%M%S%fZ")

    record = {
        "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "service": service_name,
        "metrics": metrics,
    }

    key = f"raw/service={service_name}/dt={date_str}/metrics-{timestamp_str}.json"
    s3.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
    )
    return key