"""Tests for the M9-2 diagnostics, on small hand-checkable windows and stub models. No AWS.

Run from AnomalyPulse/:  python3 -m pytest ml/test_m9_2_diagnostics.py -q
"""
import numpy as np
import pytest

pytest.importorskip("sklearn")
from ml import m9_2_diagnostics as d  # noqa: E402

COLUMNS = ["product_request_rate", "order_db_consumed_write_capacity", d.ORDER_P50]
DEP, CASCADE = d.DEPENDENCY, d.CASCADE


def windows(rows):
    """rows: (rate, capacity, p50, phase, label, healthy, kind, date, flagged, score, pred)"""
    parts = list(zip(*rows))
    return d.Windows(X=np.asarray(list(zip(parts[0], parts[1], parts[2])), float), columns=COLUMNS,
                     phase=np.asarray(parts[3]), label=np.asarray(parts[4]), healthy=np.asarray(parts[5], bool),
                     kind=np.asarray(parts[6]), date=np.asarray(parts[7]), flagged=np.asarray(parts[8], bool),
                     score=np.asarray(parts[9], float), pred=np.asarray(parts[10]))


def forest_world():
    """Validation and stress sit at rate 10, capacity 10 (score 0). The test set sits at 20, 20 (score -20)."""
    ok = lambda phase, date: (10, 10, 5, phase, "NORMAL", True, "normal", date, False, 0.0, "NORMAL")  # noqa: E731
    bad = lambda date: (20, 20, 12, "test", "NORMAL", True, "normal", date, True, -20.0, "NORMAL")  # noqa: E731
    return windows([ok("val", "2026-10-24")] * 4 + [bad("2026-11-15")] * 2 + [bad("2026-11-16")] * 3 + [ok("stress", "2026-11-16")] * 2)


def forest_score(X):
    return -np.abs(X[:, 0] - 10) - np.abs(X[:, 1] - 10)


def dependency_world():
    """Two slow and two fast single-fault test dependency windows, two validation ones, one confounder
    window (never counted) and two real cascades, which the stub calls cascades because of their rate."""
    row = lambda rate, p50, phase, label, kind, pred: (rate, 1, p50, phase, label, False, kind, "2026-11-15", True, -1.0, pred)  # noqa: E731
    return windows([row(1, 5, "val", DEP, "validation", DEP)] * 2
                   + [row(1, 2000, "test", DEP, "single", CASCADE)] * 2 + [row(1, 8, "test", DEP, "single", DEP)] * 2
                   + [row(1, 2000, "test", DEP, "confounder", CASCADE)]
                   + [row(60, 5, "test", CASCADE, "cascade", CASCADE)] * 2)


def dependency_predict(X):
    return np.where((X[:, 2] > d.SLOW_MS) | (X[:, 0] > 50), CASCADE, DEP)


def test_post_orders_share_of_order_traffic():
    assert d.post_share(d.MIXES["default"]) == 5 / 15
    assert d.post_share(d.MIXES["shifted"]) == 10 / 20


def test_the_mixes_match_the_simulator():
    workload = pytest.importorskip("simulator.workload")
    integrity = pytest.importorskip("simulator.integrity")
    assert tuple(workload.DEFAULT_MIX[r] for r in workload.ROUTES) == d.MIXES["default"]
    assert tuple(integrity.TEST_MIX) == d.MIXES["shifted"]


def test_rate_columns_are_the_traffic_volume_columns():
    columns = ["cart_request_rate_change", "order_db_consumed_read_capacity", "order_p50_latency_ms", "cart_error_rate_5xx"]
    assert d.rate_columns(columns) == [0, 1]


def test_swap_changes_only_the_named_columns():
    target, source = np.ones((5, 3)), np.full((2, 3), 7.0)
    out = d.swap_columns(target, source, [1], np.random.default_rng(0))
    assert out[:, 1].tolist() == [7.0] * 5 and out[:, [0, 2]].tolist() == [[1.0, 1.0]] * 5
    assert target[:, 1].tolist() == [1.0] * 5       # the input is not modified


def test_class_names_reads_numbered_and_named_predictions():
    assert d.class_names(np.array([1, 0]), ["A", "B"]).tolist() == ["B", "A"]
    assert d.class_names(np.array(["B"]), ["A", "B"]).tolist() == ["B"]


def test_forest_section_counts_and_the_same_day_comparison():
    out = d.forest_section(forest_world(), forest_score, -5.0)
    assert out["healthy_by_phase"]["val"]["flagged"] == 0 and out["healthy_by_phase"]["test"]["flagged"] == 5
    assert out["same_day"] == {"date": "2026-11-16", "shifted_mix_test": {"windows": 3, "flagged": 3},
                               "default_mix_stress": {"windows": 2, "flagged": 0}}
    assert out["healthy_baseline_means"]["product_request_rate"] == {"validation": 10.0, "test": 20.0}


def test_swapping_the_traffic_columns_clears_a_traffic_driven_alarm():
    swap = d.forest_section(forest_world(), forest_score, -5.0)["swap_traffic_columns"]
    assert swap["columns"] == 2 and swap["windows"] == 5
    assert (swap["flagged_share_before"], swap["flagged_share_after"]) == (1.0, 0.0)


def test_dependency_section_splits_by_order_p50():
    out = d.dependency_section(dependency_world(), dependency_predict)
    assert out["by_group"]["test"] == {"windows": 4, "recall": 0.5, "share_order_p50_slow": 0.5}
    assert out["by_group"]["validation"]["recall"] == 1.0
    assert out["test_windows_by_order_p50"] == {"order_p50_slow": {"windows": 2, "called_cascade": 2},
                                                "order_p50_fast": {"windows": 2, "called_cascade": 0}}


def test_the_order_p50_swap_and_its_control():
    swap = d.dependency_section(dependency_world(), dependency_predict)["swap_order_p50"]
    assert (swap["dependency_recall_before"], swap["dependency_recall_after"]) == (0.5, 1.0)
    assert swap["control_cascade_windows"] == 2
    assert (swap["control_cascade_recall_before"], swap["control_cascade_recall_after"]) == (1.0, 1.0)


def test_the_markdown_says_these_are_not_results():
    report = {"provenance": {"git": {"commit": "abc", "dirty": False}, "seed": 1, "generated_at": "t"},
              "forest": d.forest_section(forest_world(), forest_score, -5.0),
              "dependency": d.dependency_section(dependency_world(), dependency_predict)}
    text = d.render_markdown(report)
    assert "They are not results themselves" in text and "not a controlled live experiment" in text
    assert "33.3% of Order's traffic in the default mix" in text and "50.0% of Order's traffic in the shifted mix" in text
