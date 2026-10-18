import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import spot_check as sc


def _record(service, **overrides):
    record = {
        "timestamp": "2026-10-18T04:15:00.000Z", "request_id": "r", "service": service,
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


def _snapshot(service, **lam):
    metrics = {"lambda": {"function_name": "f", "avg_duration_ms": 2.0, "throttle_count": 0,
                          "cold_starts": 0, "avg_init_duration_ms": None, **lam}}
    if service != "payment-service":
        metrics["dynamodb"] = {"table_name": "t", "consumed_read_capacity": 10.0,
                               "consumed_write_capacity": 1.0, "throttled_requests": 0,
                               "read_throttle_events": 0, "write_throttle_events": 0}
    return {"timestamp": "2026-10-18T04:16:00.000Z", "service": service, "metrics": metrics}


def _rich_metrics():
    return [_snapshot(s) for s in sc.SERVICES]


def test_rich_telemetry_passes():
    assert sc.check_experiment(_rich_requests(), _rich_metrics()) == []


def test_partly_null_db_fields_are_normal():
    # A 400 rejected before the DB call leaves the db fields null on that record only.
    requests = _rich_requests() + [_record("cart-service", status_code=400,
                                           db_latency_ms=None, db_consumed_capacity=None)]
    assert sc.check_experiment(requests, _rich_metrics()) == []


def test_db_field_null_everywhere_is_silently_empty():
    requests = [r if r["service"] != "product-service" else {**r, "db_consumed_capacity": None}
                for r in _rich_requests()]
    assert sc.check_experiment(requests, _rich_metrics()) == [
        "product-service: db_consumed_capacity is null in every record (silently empty)"]


def test_order_without_any_dependency_record_is_flagged():
    requests = [r for r in _rich_requests() if r["http_method"] != "POST"]
    problems = sc.check_experiment(requests, _rich_metrics())
    assert "order-service: dependency is null in every record (silently empty)" in problems


def test_a_field_that_should_be_null_but_is_set_is_flagged():
    requests = _rich_requests() + [_record("payment-service", db_latency_ms=1.0)]
    assert sc.check_experiment(requests, _rich_metrics()) == [
        "payment-service: db_latency_ms is set in 1 records but should be null"]


def test_a_missing_service_or_core_null_is_flagged():
    requests = [r for r in _rich_requests() if r["service"] != "cart-service"]
    requests.append(_record("product-service", latency_ms=None))
    problems = sc.check_experiment(requests, _rich_metrics())
    assert "cart-service: no request records" in problems
    assert "product-service: latency_ms is null in 1 of 2 records" in problems


def test_a_record_missing_a_schema_field_is_flagged():
    broken = _record("product-service")
    del broken["cold_start"]
    problems = sc.check_experiment(_rich_requests() + [broken], _rich_metrics())
    assert problems[0] == "a product-service record is missing cold_start"


def test_metrics_gaps_are_flagged_but_quiet_init_duration_is_not():
    metrics = [s for s in _rich_metrics() if s["service"] != "order-service"]
    metrics = [{**s, "metrics": {"lambda": s["metrics"]["lambda"]}} if s["service"] == "cart-service"
               else s for s in metrics]
    problems = sc.check_experiment(_rich_requests(), metrics)
    assert "order-service: no metrics snapshots" in problems
    assert "cart-service: metrics have no dynamodb block" in problems
    assert not any("avg_init_duration_ms" in p for p in problems)


def test_sample_takes_a_fixed_number_per_class():
    index = [{"slot": i, "incident_type": t, "experiment_id": f"e{i}"}
             for i, t in enumerate(["NORMAL"] * 5 + ["TRAFFIC_SPIKE"] * 5 + ["CASCADING_FAILURE"])]
    chosen = sc.sample(index, per_class=2)
    assert [r["incident_type"] for r in chosen].count("NORMAL") == 2
    assert [r["incident_type"] for r in chosen].count("CASCADING_FAILURE") == 1
    assert sc.sample(index, per_class=2) == chosen
