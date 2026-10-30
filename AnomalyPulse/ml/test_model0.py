"""Tests for Model 0 (M5-3). Run from AnomalyPulse/:  python3 -m pytest ml/test_model0.py -q"""
import pytest

from ml import model0


def row(**values):
    return values


def test_a_quiet_window_is_not_flagged():
    quiet = row(order_p95_latency_ms=20.0, order_p95_latency_ms_change=1.0,
                order_p95_latency_ms_roll_mean_3=19.0, order_error_rate_5xx=0.0)
    assert not model0.flag(quiet, "previous_window")
    assert not model0.flag(quiet, "rolling_mean")


def test_error_rate_must_be_strictly_above_five_percent():
    assert model0.flag(row(cart_error_rate_5xx=0.06))
    assert not model0.flag(row(cart_error_rate_5xx=0.05))


def test_previous_window_rule_fires_at_double_the_previous_p95():
    assert model0.flag(row(order_p95_latency_ms=100.0, order_p95_latency_ms_change=60.0), "previous_window")
    assert not model0.flag(row(order_p95_latency_ms=50.0, order_p95_latency_ms_change=10.0), "previous_window")


def test_rolling_mean_rule_is_the_literal_ticket_rule():
    assert model0.flag(row(order_p95_latency_ms=100.0, order_p95_latency_ms_roll_mean_3=40.0), "rolling_mean")
    assert not model0.flag(row(order_p95_latency_ms=100.0, order_p95_latency_ms_roll_mean_3=60.0), "rolling_mean")


def test_any_service_can_trip_it():
    assert model0.flag(row(payment_error_rate_5xx=0.5))


def test_empty_values_are_skipped_not_fatal():
    assert not model0.flag(row(order_p95_latency_ms=None, order_p95_latency_ms_change=None,
                               order_error_rate_5xx=None))


def test_unknown_baseline_is_refused():
    with pytest.raises(ValueError):
        model0.flag(row(), "median")


def test_score_counts_anomalies_as_anything_but_normal():
    labels = ["NORMAL", "NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"]
    result = model0.score(labels, [False, True, True, False])
    assert result["precision"] == 0.5 and result["recall"] == 0.5
    assert result["f1"] == 0.5 and result["fpr"] == 0.5 and result["accuracy"] == 0.5
    assert result["recall_by_class"] == {"CASCADING_FAILURE": 0.0, "TRAFFIC_SPIKE": 1.0}


def test_score_refuses_mismatched_lengths():
    with pytest.raises(ValueError):
        model0.score(["NORMAL"], [True, False])