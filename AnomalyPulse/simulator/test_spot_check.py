import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import spot_check as sc

# A run from 04:15:30 to 04:17:30 overlaps three minutes: snapshots stamped 04:16, 04:17 and 04:18.
RUN = {"started_at": "2026-10-18T04:15:30.000Z", "ended_at": "2026-10-18T04:17:30.000Z"}
STAMPS = ["2026-10-18T04:16:00.000Z", "2026-10-18T04:17:00.000Z", "2026-10-18T04:18:00.000Z"]


def _record(service, **overrides):
    record = {
        "timestamp": "2026-10-18T04:16:10.000Z", "request_id": "r", "service": service,
        "endpoint": "/x", "http_method": "GET", "status_code": 200, "latency_ms": 5.0,
        "lambda_duration_ms": 2.0, "cold_start": False,
        "db_latency_ms": None, "db_consumed_capacity": None,
        "dependency": None, "dependency_latency_ms": None, "dependency_error": None,
    }
    if service != "payment-service":
        record.update(db_latency_ms=3.0, db_consumed_capacity=0.5)
    record.update(overrides)
    return record


def _rich_requests():
    return [
        _record("product-service"), _record("cart-service"),
        _record("order-service"),   # a GET: no dependency
        _record("order-service", http_method="POST", dependency="payment-service",
                dependency_latency_ms=4.0, dependency_error=False),
        _record("payment-service"),
    ]


SENT = 4   # the client's requests above: Payment's record is Order's call


def _snapshot(service, stamp=STAMPS[0], **lam):
    metrics = {"lambda": {"function_name": "f", "avg_duration_ms": 2.0, "throttle_count": 0,
                          "cold_starts": 0, "avg_init_duration_ms": None, **lam}}
    if service != "payment-service":
        metrics["dynamodb"] = {"table_name": "t", "consumed_read_capacity": 10.0,
                               "consumed_write_capacity": 1.0, "throttled_requests": 0,
                               "read_throttle_events": 0, "write_throttle_events": 0}
    return {"timestamp": stamp, "service": service, "metrics": metrics}


def _rich_metrics():
    return [_snapshot(s, t) for t in STAMPS for s in sc.SERVICES]


def _check(requests=None, metrics=None, sent=SENT):
    return sc.check_experiment(_rich_requests() if requests is None else requests,
                               _rich_metrics() if metrics is None else metrics, sent=sent, **RUN)


def test_complete_telemetry_passes():
    assert _check() == []


def test_a_run_expects_a_snapshot_for_every_minute_it_overlaps():
    assert [sc._hhmm(t) for t in sc.snapshot_minutes(**RUN)] == ["04:16", "04:17", "04:18"]
    # On the minute: 04:15:00-04:17:00 overlaps two minutes.
    assert len(sc.snapshot_minutes("2026-10-18T04:15:00.000Z", "2026-10-18T04:17:00.000Z")) == 2
    # The M4 run shape: 600 s from mid-minute overlaps 11 minutes (44 snapshots for four services).
    assert len(sc.snapshot_minutes("2026-10-18T02:24:31.231Z", "2026-10-18T02:34:31.346Z")) == 11


# --- requests ------------------------------------------------------------------

def test_partly_null_db_fields_are_normal():
    # A 400 rejected before the DB call leaves the db fields null on that record only.
    requests = _rich_requests() + [_record("cart-service", status_code=400,
                                           db_latency_ms=None, db_consumed_capacity=None)]
    assert _check(requests, sent=SENT + 1) == []


def test_db_field_null_everywhere_is_silently_empty():
    requests = [r if r["service"] != "product-service" else {**r, "db_consumed_capacity": None}
                for r in _rich_requests()]
    assert _check(requests) == ["product-service: db_consumed_capacity is null in every record (silently empty)"]


def test_order_without_any_dependency_record_is_flagged():
    requests = [r for r in _rich_requests() if r["http_method"] != "POST"]
    assert "order-service: dependency is null in every record (silently empty)" in _check(requests, sent=3)


def test_a_field_that_should_be_null_but_is_set_is_flagged():
    requests = _rich_requests() + [_record("payment-service", db_latency_ms=1.0)]
    assert _check(requests) == ["payment-service: db_latency_ms is set in 1 records but should be null"]


def test_a_missing_service_or_core_null_is_flagged():
    requests = [r for r in _rich_requests() if r["service"] != "cart-service"]
    requests.append(_record("product-service", latency_ms=None))
    problems = _check(requests)
    assert "cart-service: no request records" in problems
    assert "product-service: latency_ms is null in 1 of 2 records" in problems


def test_a_record_missing_a_schema_field_is_flagged():
    broken = _record("product-service")
    del broken["cold_start"]
    assert _check(_rich_requests() + [broken])[0] == "a product-service record is missing cold_start"


def test_fewer_records_than_requests_sent_is_flagged():
    # A lost stretch of records: every field is filled in the records that are there.
    assert _check(sent=SENT + 1) == [
        "4 Product, Cart and Order records for 5 requests sent (-1): records were lost or duplicated"]
    assert "(+1)" in _check(sent=SENT - 1)[0]


def test_records_within_the_no_response_allowance_pass():
    # 0.1% of requests may get no response (batch.MAX_ERROR_SHARE): 1 of 1,000.
    requests = _rich_requests() + [_record("product-service")] * 996
    assert _check(requests, sent=1_001) == []
    assert _check(requests, sent=1_002) != []


# --- metrics -------------------------------------------------------------------

def test_a_missing_minute_of_snapshots_is_flagged():
    metrics = [s for s in _rich_metrics() if not (s["service"] == "order-service" and s["timestamp"] == STAMPS[1])]
    assert _check(metrics=metrics) == [
        "order-service: no metrics snapshot for 1 of 3 minutes (stamped 04:17)"]


def test_a_service_without_snapshots_is_flagged():
    metrics = [s for s in _rich_metrics() if s["service"] != "order-service"]
    assert _check(metrics=metrics) == ["order-service: no metrics snapshots"]


def test_snapshots_outside_the_run_do_not_count():
    metrics = [s for s in _rich_metrics() if s["timestamp"] != STAMPS[2]]
    metrics += [_snapshot(s, "2026-10-18T04:20:00.000Z") for s in sc.SERVICES]
    assert all("1 of 3 minutes (stamped 04:18)" in p for p in _check(metrics=metrics))


def test_a_metric_field_the_extractor_stopped_writing_is_flagged():
    metrics = _rich_metrics()
    for s in metrics:
        if s["service"] == "product-service":
            del s["metrics"]["dynamodb"]["throttled_requests"]
    del metrics[-1]["metrics"]["lambda"]["throttle_count"]
    assert _check(metrics=metrics) == [
        "product-service: metrics dynamodb.throttled_requests is missing from 3 of 3 snapshots",
        f"{metrics[-1]['service']}: metrics lambda.throttle_count is missing from 1 of 3 snapshots"]


def test_a_missing_block_or_an_always_null_field_is_flagged_but_quiet_init_duration_is_not():
    metrics = _rich_metrics()
    for s in metrics:
        if s["service"] == "cart-service":
            del s["metrics"]["dynamodb"]
        if s["service"] == "order-service":
            s["metrics"]["lambda"]["avg_duration_ms"] = None
    assert _check(metrics=metrics) == [
        "cart-service: 3 of 3 metrics snapshots have no dynamodb block",
        "order-service: metrics lambda.avg_duration_ms is null in every snapshot"]


def test_metric_fields_match_the_extractors_post_deploy_check():
    path = Path(__file__).parent.parent / "infra" / "shared" / "metrics_extractor" / "check_metrics.py"
    spec = importlib.util.spec_from_file_location("check_metrics", path)
    check_metrics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check_metrics)
    assert set(sc.METRIC_FIELDS["dynamodb"]) == check_metrics.DYNAMODB_KEYS
    assert set(sc.METRIC_FIELDS["lambda"]) == check_metrics.LAMBDA_KEYS


# --- choosing what to check ----------------------------------------------------

INDEX = [{"slot": i, "incident_type": t, "experiment_id": f"e{i}"}
         for i, t in enumerate(["NORMAL"] * 5 + ["TRAFFIC_SPIKE"] * 5 + ["CASCADING_FAILURE"], start=1)]
BATCHES = [{"batch_id": "batch-1", "queue": {"slots": [1, 2, 3, 4, 5, 6]}},
           {"batch_id": "batch-2", "queue": {"slots": [7, 8, 9, 10, 11]}}]


def test_the_newest_batchs_whole_queue_is_checked_by_default():
    what, rows = sc.select(INDEX, BATCHES, None)
    assert [r["slot"] for r in rows] == [7, 8, 9, 10, 11]
    assert "batch-2" in what


def test_a_named_batch_or_all():
    assert [r["slot"] for r in sc.select(INDEX, BATCHES, "batch-1")[1]] == [1, 2, 3, 4, 5, 6]
    assert len(sc.select(INDEX, BATCHES, "all")[1]) == 11
    with pytest.raises(ValueError, match="no batch batch-3"):
        sc.select(INDEX, BATCHES, "batch-3")


def test_sample_takes_a_fixed_number_per_class():
    chosen = sc.sample(INDEX, per_class=2)
    assert [r["incident_type"] for r in chosen].count("NORMAL") == 2
    assert [r["incident_type"] for r in chosen].count("CASCADING_FAILURE") == 1
    assert sc.sample(INDEX, per_class=2) == chosen
