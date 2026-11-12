"""Tests for cost/estimate.py, on the committed inputs (no AWS). Expected values are worked out here from the
measured kinds or the plan, not by calling the code under test.

    python3 -m pytest cost -q        # from AnomalyPulse/
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from cost import estimate as e

INPUTS = Path(__file__).parent / "inputs"
INP = e.Inputs.load(INPUTS)
M = e.Model(INP)
KINDS = json.load(open(INPUTS / "kinds.json"))


def kind(name, band=None):
    return next(r for r in KINDS if r["kind"] == name and (band is None or r["band"] == band))


NOOP = kind("noop")


def approx(x, rel=1e-9):
    return pytest.approx(x, rel=rel)


# ---- the fits pass through the measured bands ----

def test_line_extrapolates_a_straight_line():
    f = e.line(3, 10_000, 7, 30_000)
    assert (f(5), f(9), f(11)) == (20_000, 40_000, 50_000)


@pytest.mark.parametrize("x, band", [(3, [2, 4]), (7, [6, 8])])
def test_a_spike_at_a_training_band_midpoint_is_that_band_s_measured_run(x, band):
    u = M.usage({"kind": "spike", "x": x}, False, 1.0)
    assert u["api"] == approx(kind("traffic-spike", band)["api"])
    assert u["gbs"] == approx(kind("traffic-spike", band)["gbs"])


@pytest.mark.parametrize("name, key, x, band", [
    ("db-throttling", "db", 2, [1.5, 2.5]), ("db-throttling", "db", 4, [3.5, 4.5]),
    ("payment-failure", "pay", 1000, [500, 1500]), ("payment-failure", "pay", 3000, [2500, 3500])])
def test_db_and_payment_runs_at_training_midpoints_are_the_measured_runs(name, key, x, band):
    assert M.usage({"kind": key, "x": x}, False, 1.0)["gbs"] == approx(kind(name, band)["gbs"])


@pytest.mark.parametrize("target", ["product-service", "cart-service", "order-service"])
def test_lambda_degradation_fits_each_target_s_two_bands(target):
    for x, band in ((350, [200, 500]), (950, [800, 1100])):
        u = M.usage({"kind": "lam", "x": x, "target": target}, False, 1.0)
        assert u["gbs"] == approx(kind(f"lambda-degradation:{target}", band)["gbs"])


def test_a_normal_run_in_the_training_mix_is_the_measured_normal_run():
    u = M.usage({"kind": "noop"}, False, 1.0)
    assert (u["api"], u["gbs"], u["minutes"]) == (approx(NOOP["api"]), approx(NOOP["gbs"]), 10)


# ---- the test set's differences ----

def test_the_test_mix_reweights_each_function_by_its_share():
    f = 0.75
    by_hand = (NOOP["gbs_product"] * f + NOOP["gbs_order"] * 1.5 + NOOP["gbs_payment"] * 2 + NOOP["gbs_cart"] * 1.5
               + NOOP["gbs_telemetry-shipper"] * 1.04) / NOOP["gbs"]
    assert M.baseline_gbs_factor(f) == approx(by_hand)


def test_a_12_minute_spike_has_17_minutes_of_baseline_and_2_4_faults_worth_of_spike():
    u = M.usage({"kind": "spike", "x": 7, "duration_s": 720}, False, 1.0)
    by_hand = NOOP["api"] * 17 / 10 + (kind("traffic-spike", [6, 8])["api"] - NOOP["api"]) * 720 / 300
    assert u["minutes"] == 17 and u["api"] == approx(by_hand)


def test_payment_failure_doubles_in_the_test_mix_because_post_orders_doubles():
    train = M.usage({"kind": "pay", "x": 3000}, False, 0.75)["gbs"] - NOOP["gbs"]
    test = M.usage({"kind": "pay", "x": 3000}, True, 0.75)["gbs"] - NOOP["gbs"] * M.baseline_gbs_factor(0.75)
    assert test == approx(2 * train)


def test_a_cascade_on_training_bands_is_used_as_measured():
    t = "traffic-spike+payment-failure+retry-amplification"
    u = M.usage({"kind": "cascade", "template": t}, False, 1.0)
    assert u["api"] == approx(kind("cascade", t)["api"]) and u["gbs"] == approx(kind("cascade", t)["gbs"])


def test_a_test_band_cascade_scales_the_spike_s_requests_by_its_band_ratio():
    # The spike increment is linear through (3, A1) and (7, A2); at the test midpoints 5 and 9 it is (A1+A2)/2 and
    # (3*A2 - A1)/2, so the ratio of the means is 2*A2 / (A1 + A2).
    a1 = kind("traffic-spike", [2, 4])["api"] - NOOP["api"]
    a2 = kind("traffic-spike", [6, 8])["api"] - NOOP["api"]
    t = "traffic-spike+payment-failure+retry-amplification"
    u = M.usage({"kind": "cascade", "template": t, "test_bands": True}, False, 1.0)
    assert u["api"] == approx(NOOP["api"] + (kind("cascade", t)["api"] - NOOP["api"]) * 2 * a2 / (a1 + a2))


# ---- the plan ----

def test_the_test_set_is_the_plan_s_350_runs():
    runs = e.test_set(INP)
    counts = Counter(r["kind"] for r in runs)
    assert len(runs) == 350
    assert counts == {"noop": 60, "spike": 65, "db": 65, "lam": 50, "pay": 50, "cascade": 30, "confounder": 30}
    assert Counter(r.get("duration_s", 300) for r in runs) == {300: 330, 120: 10, 720: 10}
    assert sorted(tuple(f[0] for f in r["faults"]) for r in runs if r["kind"] == "confounder") == sorted(e.PAIRS * 7 + e.PAIRS[:2])


def test_every_fault_run_opens_one_incident_and_normal_runs_none():
    assert e.bedrock_calls(e.test_set(INP), 1, 1, 1) == (290, 290)
    assert e.bedrock_calls(e.stress_set(), 2, 0, 0) == (20, 40)


def test_free_allowances_are_subtracted_once_and_never_go_negative():
    assert (e.billable(450_000, 400_000), e.billable(300_000, 400_000)) == (50_000, 0.0)


def test_scenarios_are_ordered():
    totals = [e.estimate(INP, e.SCENARIOS[s])["rest"] for s in ("low", "expected", "high")]
    assert totals[0] < totals[1] < totals[2]


def test_december_with_the_table_and_disk_kept_costs_their_month_more():
    d = e.december(INP)
    keep, plan = d["nothing deleted"], d["runner and Products table deleted after M10 (the plan); pipeline, services and data kept"]
    assert keep - plan == approx(200 * 24 * 0.00013 * 31 - 25 * 730 * 0.00013 + 16 * 0.0796 / 30 * 31)


# ---- against the bill ----

def test_the_model_s_prices_are_the_bill_s():
    for name, (bill, model) in e.check_prices(INP).items():
        assert bill == pytest.approx(model, rel=0.01), name


def test_the_model_predicts_nov_11s_live_batch():
    pred, actual = e.backtest(INP)
    assert 0.9 < pred["api"] / actual["api"] < 1.1
    assert 0.85 < pred["gbs"] / actual["gbs"] < 1.15
