"""Local unit tests for the CloudWatch metrics extractor. No live AWS needed --
boto3 clients are mocked so these tests run offline, fast, and without credentials."""
from unittest import mock

import metrics_extractor


def test_get_dynamodb_metrics_sums_datapoints():
    fake_response = {"Datapoints": [{"Sum": 2.0}, {"Sum": 3.0}]}
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value=fake_response):
        result = metrics_extractor.get_dynamodb_metrics("fake-table", minutes=5)

    assert result["table_name"] == "fake-table"
    assert result["consumed_read_capacity"] == 5.0
    assert result["consumed_write_capacity"] == 5.0
    assert result["throttled_requests"] == 5.0


def test_get_dynamodb_metrics_handles_no_datapoints():
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value={"Datapoints": []}):
        result = metrics_extractor.get_dynamodb_metrics("quiet-table", minutes=5)

    assert result["consumed_read_capacity"] == 0
    assert result["throttled_requests"] == 0


def test_get_lambda_metrics_computes_averages_and_cold_starts():
    def fake_stats(**kwargs):
        if kwargs["MetricName"] == "Duration":
            return {"Datapoints": [{"Average": 100.0}, {"Average": 200.0}]}
        if kwargs["MetricName"] == "Throttles":
            return {"Datapoints": [{"Sum": 1.0}]}
        if kwargs["MetricName"] == "InitDuration":
            return {"Datapoints": [{"Average": 300.0}]}
        return {"Datapoints": []}

    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", side_effect=fake_stats):
        result = metrics_extractor.get_lambda_metrics("fake-function", minutes=5)

    assert result["function_name"] == "fake-function"
    assert result["avg_duration_ms"] == 150.0
    assert result["throttle_count"] == 1.0
    assert result["cold_starts"] == 1
    assert result["avg_init_duration_ms"] == 300.0


def test_get_lambda_metrics_handles_no_invocations():
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value={"Datapoints": []}):
        result = metrics_extractor.get_lambda_metrics("idle-function", minutes=5)

    assert result["avg_duration_ms"] is None
    assert result["cold_starts"] == 0


def test_write_metrics_to_s3_builds_correct_key_and_body():
    fixed_time = __import__("datetime").datetime(2026, 1, 15, 10, 30, 0, tzinfo=__import__("datetime").timezone.utc)
    captured = {}

    def fake_put_object(**kwargs):
        captured.update(kwargs)

    with mock.patch.object(metrics_extractor.s3, "put_object", side_effect=fake_put_object):
        key = metrics_extractor.write_metrics_to_s3(
            "fake-bucket", "payment-service", {"throttled_requests": 0}, now=fixed_time
        )

    assert key == captured["Key"]
    assert key.startswith("raw/service=payment-service/dt=2026-01-15/metrics-")
    assert captured["Bucket"] == "fake-bucket"

    import json
    body = json.loads(captured["Body"])
    assert body["service"] == "payment-service"
    assert body["metrics"] == {"throttled_requests": 0}