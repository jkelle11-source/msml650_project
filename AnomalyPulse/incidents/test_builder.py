"""Tests for the Bedrock input builder (M8-3). No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_builder.py -q
"""
import copy

import pytest

from genai import contract
from incidents import builder

STARTED = "2026-10-30T14:32:00.000Z"
LATER = "2026-10-30T14:33:00.000Z"


def state(kind="DEPENDENCY_FAILURE", confidence=0.93):
    return {"status": "OPEN", "started_at": STARTED, "incident_type": kind, "confidence": confidence}


BASE = {"product_request_rate": 15.0, "product_p95_latency_ms": 20.0, "product_error_rate_5xx": 0.0,
        "cart_request_rate": 2.0, "cart_p95_latency_ms": 20.0, "cart_error_rate_5xx": 0.0,
        "order_request_rate": 3.0, "order_p95_latency_ms": 100.0, "order_error_rate_5xx": 0.0,
        "payment_request_rate": 1.0, "payment_p95_latency_ms": 110.0, "payment_error_rate_5xx": 0.0}


def window(**values):
    return {"window_start": LATER, **values}


DEPENDENCY_WINDOW = window(order_error_rate_5xx=0.167, order_p95_latency_ms=2950.0, order_request_rate=3.0,
                           payment_error_rate_5xx=0.5, payment_p95_latency_ms=2900.0, payment_request_rate=1.0,
                           product_error_rate_5xx=0.0, product_p95_latency_ms=20.0, product_request_rate=15.0)


def refs(result):
    return [(m["service"], m["metric"]) for m in result["metrics"]]


def test_incident_id_matches_the_contracts_example():
    assert builder.incident_id(STARTED) == "inc-20261030T143200Z"


def test_a_dependency_failure_input_is_accepted_by_the_contract():
    result = builder.build_input(state(), BASE, DEPENDENCY_WINDOW)
    assert contract.validate_input(result) is result
    assert result["affected_services"] == ["order-service", "payment-service"]
    assert result["incident_type"] == "DEPENDENCY_FAILURE" and result["confidence"] == 0.93
    assert result["contract_version"] == 1 and result["duration_minutes"] == 2


def test_duration_counts_the_first_minute():
    def duration(start):
        return builder.build_input(state(), BASE, {**DEPENDENCY_WINDOW, "window_start": start})["duration_minutes"]
    assert duration(STARTED) == 1
    assert duration(LATER) == 2
    assert duration("2026-10-30T14:37:00.000Z") == 6


def test_moved_metrics_come_first_then_context():
    result = builder.build_input(state(), BASE, DEPENDENCY_WINDOW)
    assert refs(result) == [
        ("order-service", "error_rate_5xx"), ("payment-service", "error_rate_5xx"),
        ("order-service", "p95_latency_ms"), ("payment-service", "p95_latency_ms"),
        ("order-service", "request_rate"), ("payment-service", "request_rate")]


def test_the_types_own_signals_are_sent_even_if_they_did_not_move():
    base = {**BASE, "order_dependency_latency_ms_mean": 5.0, "order_dependency_error_count": 0.0}
    win = {**DEPENDENCY_WINDOW, "order_dependency_latency_ms_mean": 5.0, "order_dependency_error_count": 0.0}
    result = builder.build_input(state(), base, win)
    assert ("order-service", "dependency_latency_ms_mean") in refs(result)
    assert ("order-service", "dependency_error_count") in refs(result)
    contract.validate_input(result)


def test_a_moved_metric_outside_the_standard_set_is_sent():
    base = {**BASE, "order_lambda_duration_ms_mean": 10.0}
    win = window(order_error_rate_5xx=0.2, order_request_rate=3.0, order_p95_latency_ms=100.0,
                 order_lambda_duration_ms_mean=30.0)
    result = builder.build_input(state("TRAFFIC_SPIKE"), base, win)
    assert ("order-service", "lambda_duration_ms_mean") in refs(result)
    contract.validate_input(result)


def test_a_count_above_zero_counts_as_moved():
    base = {**BASE, "product_throttled_requests": 0.0}
    win = window(product_throttled_requests=4.0, product_p95_latency_ms=20.0,
                 product_request_rate=15.0, product_error_rate_5xx=0.0)
    result = builder.build_input(state("DATABASE_THROTTLING"), base, win)
    assert result["affected_services"] == ["product-service"]
    assert ("product-service", "throttled_requests") in refs(result)
    contract.validate_input(result)


def test_the_list_is_capped_at_ten_and_in_a_fixed_order():
    values = {}
    for svc in ("product", "cart", "order", "payment"):
        values[f"{svc}_error_rate_5xx"] = 0.2
        values[f"{svc}_p95_latency_ms"] = BASE[f"{svc}_p95_latency_ms"] * 5
        values[f"{svc}_request_rate"] = BASE[f"{svc}_request_rate"] * 3
    result = builder.build_input(state(), BASE, window(**values))
    assert len(result["metrics"]) == 10
    assert refs(result) == (
        [(s, "error_rate_5xx") for s in contract.SERVICES]
        + [(s, "p95_latency_ms") for s in contract.SERVICES]
        + [("product-service", "request_rate"), ("cart-service", "request_rate")])
    contract.validate_input(result)


def test_payment_gets_no_table_or_dependency_metrics():
    win = {**DEPENDENCY_WINDOW, "payment_throttled_requests": 9.0, "payment_db_latency_ms_p95": 500.0}
    result = builder.build_input(state(), BASE, win)
    assert ("payment-service", "throttled_requests") not in refs(result)
    assert ("payment-service", "db_latency_ms_p95") not in refs(result)
    contract.validate_input(result)


def test_a_missing_baseline_becomes_null():
    win = window(order_error_rate_5xx=0.167, order_p95_latency_ms=2950.0, order_request_rate=3.0)
    result = builder.build_input(state(), {}, win)
    assert {m["baseline"] for m in result["metrics"]} == {None}
    contract.validate_input(result)


def test_an_empty_current_value_is_skipped():
    win = {**DEPENDENCY_WINDOW, "order_p95_latency_ms": None}
    result = builder.build_input(state(), BASE, win)
    assert ("order-service", "p95_latency_ms") not in refs(result)
    assert ("payment-service", "p95_latency_ms") in refs(result)
    contract.validate_input(result)


def test_numbers_are_rounded_to_four_decimals():
    base = {**BASE, "order_p95_latency_ms": 24.776999999999997}
    win = {**DEPENDENCY_WINDOW, "order_p95_latency_ms": 99.123456789}
    result = builder.build_input(state(), base, win)
    entry = next(m for m in result["metrics"] if m["service"] == "order-service" and m["metric"] == "p95_latency_ms")
    assert entry["baseline"] == 24.777 and entry["current"] == 99.1235


def test_the_inputs_are_not_changed():
    s, b, w = state(), dict(BASE), dict(DEPENDENCY_WINDOW)
    before = copy.deepcopy((s, b, w))
    builder.build_input(s, b, w)
    assert (s, b, w) == before


def test_no_ground_truth_key_reaches_the_input():
    result = builder.build_input(state(), BASE, DEPENDENCY_WINDOW)
    assert contract.FORBIDDEN_KEYS.isdisjoint(set(result))
    assert all(set(m) == {"service", "metric", "baseline", "current", "unit"} for m in result["metrics"])


def test_a_type_the_contract_does_not_know_is_refused_by_the_contract_not_by_a_crash():
    result = builder.build_input(state("NORMAL"), BASE, DEPENDENCY_WINDOW)
    with pytest.raises(contract.BadInput):
        contract.validate_input(result)