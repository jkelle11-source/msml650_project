"""Local unit tests for the CloudWatch metrics extractor. No live AWS needed --
boto3 clients are mocked so these tests run offline, fast, and without credentials."""
from unittest import mock

import handler as metrics_extractor


def test_get_dynamodb_metrics_sums_datapoints():
    fake_response = {"Datapoints": [{"Sum": 2.0}, {"Sum": 3.0}]}
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value=fake_response):
        result = metrics_extractor.get_dynamodb_metrics("fake-table", minutes=5)

    assert result["table_name"] == "fake-table"
    assert result["consumed_read_capacity"] == 5.0
    assert result["consumed_write_capacity"] == 5.0
    assert result["throttled_requests"] == 5.0
    # Table-level throttle counters are summed the same way.
    assert result["read_throttle_events"] == 5.0
    assert result["write_throttle_events"] == 5.0


def test_get_dynamodb_metrics_handles_no_datapoints():
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value={"Datapoints": []}):
        result = metrics_extractor.get_dynamodb_metrics("quiet-table", minutes=5)

    assert result["consumed_read_capacity"] == 0
    assert result["throttled_requests"] == 0
    assert result["read_throttle_events"] == 0
    assert result["write_throttle_events"] == 0


def test_getter_reads_the_single_minute_ending_at_window_end():
    import datetime as _dt
    window_end = _dt.datetime(2026, 1, 15, 10, 3, 0, tzinfo=_dt.timezone.utc)
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return {"Datapoints": []}

    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", side_effect=capture):
        metrics_extractor.get_dynamodb_metrics("tbl", window_end=window_end, minutes=1)

    assert captured["EndTime"] == window_end
    assert captured["StartTime"] == window_end - _dt.timedelta(minutes=1)
    assert captured["Period"] == 60


def test_get_lambda_metrics_weights_averages_and_counts_cold_starts():
    # Two Duration minutes: one slow invocation (100ms x1) and three fast-ish
    # (200ms x3). A naive mean-of-means is 150; the SampleCount-weighted mean is
    # (100*1 + 200*3) / 4 = 175. We assert 175 to lock in the weighting.
    def fake_stats(**kwargs):
        if kwargs["MetricName"] == "Duration":
            return {"Datapoints": [{"Average": 100.0, "SampleCount": 1.0},
                                   {"Average": 200.0, "SampleCount": 3.0}]}
        if kwargs["MetricName"] == "Throttles":
            return {"Datapoints": [{"Sum": 1.0}]}
        if kwargs["MetricName"] == "InitDuration":
            # One minute, but TWO cold starts within it -- cold_starts must read
            # the SampleCount (2), not the number of datapoints (1).
            return {"Datapoints": [{"Average": 300.0, "SampleCount": 2.0}]}
        return {"Datapoints": []}

    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", side_effect=fake_stats):
        result = metrics_extractor.get_lambda_metrics("fake-function", minutes=5)

    assert result["function_name"] == "fake-function"
    assert result["avg_duration_ms"] == 175.0
    assert result["throttle_count"] == 1.0
    assert result["cold_starts"] == 2
    assert result["avg_init_duration_ms"] == 300.0


def test_get_lambda_metrics_cold_starts_sum_across_minutes():
    # Regression guard for the original bug: cold starts spread across minutes
    # must sum (2 + 1 = 3), never be counted as "2 minutes had a cold start".
    def fake_stats(**kwargs):
        if kwargs["MetricName"] == "InitDuration":
            return {"Datapoints": [{"Average": 250.0, "SampleCount": 2.0},
                                   {"Average": 250.0, "SampleCount": 1.0}]}
        return {"Datapoints": []}

    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", side_effect=fake_stats):
        result = metrics_extractor.get_lambda_metrics("bursty-function", minutes=5)

    assert result["cold_starts"] == 3


def test_get_lambda_metrics_handles_no_invocations():
    with mock.patch.object(metrics_extractor.cloudwatch, "get_metric_statistics", return_value={"Datapoints": []}):
        result = metrics_extractor.get_lambda_metrics("idle-function", minutes=5)

    assert result["avg_duration_ms"] is None
    assert result["cold_starts"] == 0
    assert result["avg_init_duration_ms"] is None


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
    # Snapshots land under metrics/ (not raw/) so the aggregator's raw/ scan
    # never sees them, while keeping the service+date partitioning for joins.
    assert key.startswith("metrics/service=payment-service/dt=2026-01-15/metrics-")
    assert captured["Bucket"] == "fake-bucket"

    import json
    body = json.loads(captured["Body"])
    assert body["service"] == "payment-service"
    assert body["metrics"] == {"throttled_requests": 0}


# --- Scheduled handler -----------------------------------------------------

def test_parse_resource_map_handles_blanks_and_spaces():
    pairs = metrics_extractor._parse_resource_map(
        "product-service=anomalypulse-products, order-service=anomalypulse-orders,"
    )
    assert pairs == [("product-service", "anomalypulse-products"),
                     ("order-service", "anomalypulse-orders")]


def test_settled_window_end_floors_then_lags():
    import datetime as _dt
    now = _dt.datetime(2026, 1, 15, 10, 5, 30, 123000, tzinfo=_dt.timezone.utc)
    end = metrics_extractor._settled_window_end(now, lag_minutes=2)
    # Floored to 10:05:00, stepped back 2 minutes -> read the 10:02-10:03 bucket.
    assert end == _dt.datetime(2026, 1, 15, 10, 3, 0, tzinfo=_dt.timezone.utc)


def test_handler_writes_one_snapshot_per_service(monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("MONITORED_TABLES",
                       "product-service=anomalypulse-products,order-service=anomalypulse-orders")
    monkeypatch.setenv("MONITORED_FUNCTIONS",
                       "product-service=anomalypulse-product,payment-service=anomalypulse-payment")

    monkeypatch.setattr(metrics_extractor, "get_dynamodb_metrics",
                        lambda name, window_end, minutes: {"table_name": name})
    monkeypatch.setattr(metrics_extractor, "get_lambda_metrics",
                        lambda name, window_end, minutes: {"function_name": name})
    writes = []
    monkeypatch.setattr(metrics_extractor, "write_metrics_to_s3",
                        lambda bucket, service, metrics, now: writes.append((service, metrics)) or f"key/{service}")

    result = metrics_extractor.handler({}, None)

    by_service = dict(writes)
    assert set(by_service) == {"product-service", "order-service", "payment-service"}
    # product-service has both a table and a function; order only a table;
    # payment only a function (no DynamoDB table exists for it).
    assert set(by_service["product-service"]) == {"dynamodb", "lambda"}
    assert set(by_service["order-service"]) == {"dynamodb"}
    assert set(by_service["payment-service"]) == {"lambda"}
    assert result["failed"] == []
    assert len(result["written"]) == 3


def test_handler_isolates_a_failing_service(monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "b")
    monkeypatch.setenv("MONITORED_TABLES", "")
    monkeypatch.setenv("MONITORED_FUNCTIONS", "good-service=fn-good,bad-service=fn-bad")

    def flaky(name, window_end, minutes):
        if name == "fn-bad":
            raise RuntimeError("cloudwatch blew up")
        return {"function_name": name}

    monkeypatch.setattr(metrics_extractor, "get_lambda_metrics", flaky)
    writes = []
    monkeypatch.setattr(metrics_extractor, "write_metrics_to_s3",
                        lambda bucket, service, metrics, now: writes.append(service) or "k")

    result = metrics_extractor.handler({}, None)

    assert writes == ["good-service"]           # the healthy service still landed
    assert result["failed"] == ["bad-service"]  # the bad one is reported, not fatal
    assert result["written"] == ["k"]
