"""Tests for Model 0 (M5-3). Run from AnomalyPulse/:  python3 -m pytest ml/test_model0.py -q"""
import pytest

from ml import model0


def row(**values):
    return values


def test_a_quiet_window_is_not_flagged():
    quiet = row(order_p95_latency_ms=20.0, order_p95_latency_ms_change=1.0, order_error_rate_5xx=0.0)
    assert not model0.flag(quiet)
    assert not model0.flag_vs_earlier(quiet, [row(order_p95_latency_ms=19.0)])


def test_error_rate_must_be_strictly_above_five_percent():
    assert model0.flag(row(cart_error_rate_5xx=0.06))
    assert not model0.flag(row(cart_error_rate_5xx=0.05))


def test_previous_window_rule_fires_at_double_the_previous_p95():
    assert model0.flag(row(order_p95_latency_ms=100.0, order_p95_latency_ms_change=60.0))
    assert not model0.flag(row(order_p95_latency_ms=50.0, order_p95_latency_ms_change=10.0))


def test_any_service_can_trip_it():
    assert model0.flag(row(payment_error_rate_5xx=0.5))


def test_empty_values_are_skipped_not_fatal():
    assert not model0.flag(row(order_p95_latency_ms=None, order_p95_latency_ms_change=None,
                               order_error_rate_5xx=None))


def test_score_counts_anomalies_as_anything_but_normal():
    labels = ["NORMAL", "NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"]
    result = model0.score(labels, [False, True, True, False])
    assert result["precision"] == 0.5 and result["recall"] == 0.5
    assert result["f1"] == 0.5 and result["fpr"] == 0.5 and result["accuracy"] == 0.5
    assert result["recall_by_class"] == {"CASCADING_FAILURE": 0.0, "TRAFFIC_SPIKE": 1.0}


def test_score_refuses_mismatched_lengths():
    with pytest.raises(ValueError):
        model0.score(["NORMAL"], [True, False])


def p95(value, svc="order"):
    return {f"{svc}_p95_latency_ms": value}


def test_earlier_mean_rule_fires_above_double_the_earlier_mean():
    earlier = [p95(10.0), p95(10.0), p95(10.0)]
    assert model0.flag_vs_earlier(p95(25.0), earlier)
    assert not model0.flag_vs_earlier(p95(15.0), earlier)


def test_earlier_mean_uses_only_the_last_n_rows():
    earlier = [p95(100.0), p95(10.0), p95(10.0), p95(10.0)]
    assert model0.flag_vs_earlier(p95(25.0), earlier, n=3)
    assert not model0.flag_vs_earlier(p95(25.0), earlier, n=4)


def test_the_current_window_is_not_in_its_own_mean():
    # literal "2 x mean(this, prev1, prev2)" would not fire here; the earlier-only mean does
    assert model0.flag_vs_earlier(p95(30.0), [p95(10.0), p95(10.0)], n=3)


def test_earlier_mean_skips_empty_values_and_needs_some_history():
    assert model0.flag_vs_earlier(p95(25.0), [p95(None), p95(10.0)])
    assert not model0.flag_vs_earlier(p95(25.0), [])
    assert not model0.flag_vs_earlier(p95(25.0), [p95(None)])


def test_earlier_mean_rule_still_flags_errors_with_no_history():
    assert model0.flag_vs_earlier(row(cart_error_rate_5xx=0.2), [])
    assert not model0.flag_vs_earlier(row(cart_error_rate_5xx=0.05), [])


def test_earlier_mean_needs_at_least_one_row():
    with pytest.raises(ValueError):
        model0.flag_vs_earlier(row(), [], n=0)
