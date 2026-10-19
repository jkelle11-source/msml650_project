"""Tests for the feature/label join (M4-5)."""
import pytest

from features import join_labels as jl


def f(w):
    return {"window_start": w, "x": 1.0}


def lab(w, label="NORMAL", included=True, reason=None):
    return {"window_start": w, "label": label, "included": included, "excluded_reason": reason}


W1, W2, W3 = ("2026-10-16T03:2%d:00.000Z" % i for i in (0, 1, 2))


def test_clean_join_returns_labeled_training_rows():
    rows, report = jl.join([f(W1), f(W2)], [lab(W1), lab(W2, "LAMBDA_DEGRADATION")])
    assert [r["label"] for r in rows] == ["NORMAL", "LAMBDA_DEGRADATION"]
    assert report["training_rows"] == 2


def test_excluded_windows_stay_out_of_training():
    rows, report = jl.join([f(W1), f(W2)], [lab(W1), lab(W2, None, False, "recovery")])
    assert len(rows) == 1
    assert report["excluded_with_features"] == 1


def test_feature_row_without_a_label_is_an_orphan():
    with pytest.raises(ValueError):
        jl.join([f(W1), f(W2)], [lab(W1)])


def test_label_without_features_is_an_orphan():
    with pytest.raises(ValueError):
        jl.join([f(W1)], [lab(W1), lab(W2)])


def test_partial_window_label_without_features_is_not_an_orphan():
    rows, _ = jl.join([f(W2)], [lab(W1, None, False, "partial_window"), lab(W2)])
    assert len(rows) == 1


def test_duplicate_window_is_refused():
    with pytest.raises(ValueError):
        jl.join([f(W1), f(W1)], [lab(W1)])