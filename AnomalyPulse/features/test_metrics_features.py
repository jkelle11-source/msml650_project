"""Tests for per-window CloudWatch metric features (M4-5)."""
from features import guard
from features import metrics_features as mf


def snap(service, ts="2026-10-16T03:20:00.000Z", dynamodb=None, throttle=0.0):
    metrics = {"lambda": {"throttle_count": throttle}}
    if dynamodb is not None:
        metrics["dynamodb"] = dynamodb
    return {"timestamp": ts, "service": service, "metrics": metrics}


DB = {"consumed_read_capacity": 45.0, "consumed_write_capacity": 0.0,
      "throttled_requests": 3, "read_throttle_events": 2, "write_throttle_events": 1}


def test_snapshot_lands_in_the_minute_before_its_timestamp():
    rows = mf.metrics_features([snap("product-service", dynamodb=DB)])
    assert list(rows) == ["2026-10-16T03:19:00.000Z"]


def test_database_columns_for_a_service_with_a_table():
    row = mf.metrics_features([snap("product-service", dynamodb=DB)])["2026-10-16T03:19:00.000Z"]
    assert row["product_db_consumed_read_capacity"] == 45.0
    assert row["product_throttled_requests"] == 3
    assert row["product_read_throttle_events"] == 2
    assert row["product_write_throttle_events"] == 1


def test_payment_has_no_database_columns_but_has_lambda_throttles():
    row = mf.metrics_features([snap("payment-service", throttle=4.0)])["2026-10-16T03:19:00.000Z"]
    assert row["payment_lambda_throttle_count"] == 4.0
    assert not [c for c in row if c.startswith("payment_db_") or "throttle_events" in c]


def test_missing_database_block_on_a_table_service_gives_empty_values():
    row = mf.metrics_features([snap("cart-service")])["2026-10-16T03:19:00.000Z"]
    assert row["cart_throttled_requests"] is None


def test_snapshots_from_different_services_share_a_window():
    rows = mf.metrics_features([snap("product-service", dynamodb=DB), snap("payment-service")])
    assert len(rows) == 1
    assert "product_throttled_requests" in rows["2026-10-16T03:19:00.000Z"]
    assert "payment_lambda_throttle_count" in rows["2026-10-16T03:19:00.000Z"]


def test_columns_pass_the_boundary_guard():
    row = mf.metrics_features([snap("product-service", dynamodb=DB)])["2026-10-16T03:19:00.000Z"]
    guard.check_columns(row.keys())  # throttled_requests is allowed, db_throttled is not