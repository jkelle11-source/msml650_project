"""Tests for the payment-failure and lambda-degradation injectors (M3-4)."""
import random

import pytest

from simulator.injectors import get


def _plan(name, intensity, band, target=None, seed=7):
    cls = get(name)
    return cls.plan(
        "exp-test", 0,
        active_from_ms=1_000_000, active_until_ms=1_300_000,
        rng=random.Random(seed), intensity=intensity, band=band, target=target,
    )


PAYMENT_BAND = {"parameter": "timeout_ms", "low": 2500, "high": 3500}
LAMBDA_BAND = {"parameter": "added_latency_ms", "low": 200, "high": 500}


def test_payment_failure_attributes():
    cls = get("payment-failure")
    assert cls.incident_type == "DEPENDENCY_FAILURE"
    assert cls.parameter == "timeout_ms"
    assert cls.targets == ("payment-service",)


def test_payment_failure_maps_intensity_into_band():
    assert _plan("payment-failure", 0.0, PAYMENT_BAND).params == {"timeout_ms": 2500}
    assert _plan("payment-failure", 0.8, PAYMENT_BAND).params == {"timeout_ms": 3300}
    assert _plan("payment-failure", 1.0, PAYMENT_BAND).params == {"timeout_ms": 3500}


def test_payment_failure_picks_its_only_target():
    assert _plan("payment-failure", 0.5, PAYMENT_BAND).target == "payment-service"


def test_payment_failure_is_reproducible():
    a = _plan("payment-failure", 0.37, PAYMENT_BAND, seed=1).params
    b = _plan("payment-failure", 0.37, PAYMENT_BAND, seed=1).params
    assert a == b


def test_payment_failure_rejects_wrong_band_parameter():
    with pytest.raises(ValueError):
        _plan("payment-failure", 0.5, LAMBDA_BAND)


def test_lambda_degradation_attributes():
    cls = get("lambda-degradation")
    assert cls.incident_type == "LAMBDA_DEGRADATION"
    assert cls.parameter == "added_latency_ms"
    assert set(cls.targets) == {"product-service", "cart-service", "order-service", "payment-service"}


def test_lambda_degradation_maps_intensity_into_band():
    assert _plan("lambda-degradation", 0.0, LAMBDA_BAND, "cart-service").params == {"added_latency_ms": 200}
    assert _plan("lambda-degradation", 0.5, LAMBDA_BAND, "cart-service").params == {"added_latency_ms": 350}
    assert _plan("lambda-degradation", 1.0, LAMBDA_BAND, "cart-service").params == {"added_latency_ms": 500}


def test_lambda_degradation_requires_a_target():
    with pytest.raises(ValueError):
        _plan("lambda-degradation", 0.5, LAMBDA_BAND)


def test_lambda_degradation_rejects_unknown_target():
    with pytest.raises(ValueError):
        _plan("lambda-degradation", 0.5, LAMBDA_BAND, "billing-service")


def test_lambda_degradation_rejects_wrong_band_parameter():
    with pytest.raises(ValueError):
        _plan("lambda-degradation", 0.5, PAYMENT_BAND, "cart-service")