"""Tests for per-window request features (M4-5)."""
import pytest

import guard
import request_features as rf


def rec(ts, service="product-service", status=200, latency=10.0):
    return {"timestamp": ts, "service": service, "status_code": status, "latency_ms": latency}


def test_request_rate_is_count_over_sixty_seconds():
    records = [rec("2026-10-16T03:19:10.000Z") for _ in range(30)]
    row = rf.request_features(records)["2026-10-16T03:19:00.000Z"]
    assert row["product_request_rate"] == 0.5


def test_percentiles_interpolate():
    records = [rec("2026-10-16T03:19:10.000Z", latency=x) for x in (10, 20, 30, 40, 50)]
    row = rf.request_features(records)["2026-10-16T03:19:00.000Z"]
    assert row["product_p50_latency_ms"] == 30
    assert row["product_p95_latency_ms"] == pytest.approx(48)


def test_error_rates_split_4xx_and_5xx():
    records = [rec("2026-10-16T03:19:10.000Z", status=s) for s in (200, 200, 404, 503)]
    row = rf.request_features(records)["2026-10-16T03:19:00.000Z"]
    assert row["product_error_rate_5xx"] == 0.25
    assert row["product_error_rate_4xx"] == 0.25


def test_service_with_no_requests_has_zero_rate_and_empty_stats():
    row = rf.request_features([rec("2026-10-16T03:19:10.000Z")])["2026-10-16T03:19:00.000Z"]
    assert row["payment_request_rate"] == 0.0
    assert row["payment_p50_latency_ms"] is None
    assert row["payment_error_rate_5xx"] is None


def test_boundary_records_land_in_different_windows():
    records = [rec("2026-10-16T03:19:59.999Z"), rec("2026-10-16T03:20:00.000Z")]
    rows = rf.request_features(records)
    assert list(rows) == ["2026-10-16T03:19:00.000Z", "2026-10-16T03:20:00.000Z"]


def test_columns_pass_the_boundary_guard():
    row = rf.request_features([rec("2026-10-16T03:19:10.000Z")])["2026-10-16T03:19:00.000Z"]
    guard.check_columns(row.keys())