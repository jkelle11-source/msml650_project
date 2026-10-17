"""Tests for per-window Lambda and dependency features (M4-5)."""
import pytest

import guard
import service_features as sf

W = "2026-10-16T03:19:00.000Z"


def rec(service="order-service", duration=10.0, cold=False, dep=None, dep_lat=None, dep_err=None):
    return {"timestamp": "2026-10-16T03:19:10.000Z", "service": service,
            "lambda_duration_ms": duration, "cold_start": cold,
            "dependency": dep, "dependency_latency_ms": dep_lat, "dependency_error": dep_err}


def test_lambda_duration_mean_and_p95():
    records = [rec(duration=x) for x in (10, 20, 30, 40, 50)]
    row = sf.service_features(records)[W]
    assert row["order_lambda_duration_ms_mean"] == 30
    assert row["order_lambda_duration_ms_p95"] == pytest.approx(48)


def test_cold_starts_are_counted():
    records = [rec(cold=True), rec(cold=True), rec(cold=False)]
    assert sf.service_features(records)[W]["order_cold_start_count"] == 2


def test_service_with_no_requests_has_empty_duration_and_zero_count():
    row = sf.service_features([rec()])[W]
    assert row["payment_lambda_duration_ms_mean"] is None
    assert row["payment_cold_start_count"] == 0


def test_order_dependency_features():
    records = [
        rec(dep="payment-service", dep_lat=20.0, dep_err=False),
        rec(dep="payment-service", dep_lat=3000.0, dep_err=True),
        rec(dep="payment-service", dep_lat=40.0, dep_err=False),
    ]
    row = sf.service_features(records)[W]
    assert row["order_dependency_error_count"] == 1
    assert row["order_dependency_latency_ms_mean"] == pytest.approx(1020)


def test_no_dependency_calls_gives_zero_errors_and_empty_latency():
    row = sf.service_features([rec()])[W]
    assert row["order_dependency_error_count"] == 0
    assert row["order_dependency_latency_ms_mean"] is None


def test_columns_pass_the_boundary_guard():
    guard.check_columns(sf.service_features([rec()])[W].keys())