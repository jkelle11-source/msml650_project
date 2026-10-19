"""Tests for temporal features (M4-5)."""
import pytest

import guard
import temporal_features as tf

COL = "x"


def w(i):
    return f"2026-10-16T03:{19 + i:02d}:00.000Z"


def rows(values):
    return {w(i): {COL: v} for i, v in enumerate(values)}


def run(values):
    return tf.temporal_features(rows(values), columns=[COL])


def test_first_window_has_no_history():
    f = run([10, 20, 30])[w(0)]
    assert f["x_roll_mean_3"] is None
    assert f["x_roll_std_3"] is None
    assert f["x_change"] is None


def test_rolling_mean_uses_the_last_three_windows():
    f = run([10, 20, 30, 40])
    assert f[w(1)]["x_roll_mean_3"] == 15
    assert f[w(2)]["x_roll_mean_3"] == 20
    assert f[w(3)]["x_roll_mean_3"] == 30


def test_rolling_std():
    assert run([10, 20])[w(1)]["x_roll_std_3"] == 5


def test_change_is_this_window_minus_the_previous_one():
    f = run([10, 25, 20])
    assert f[w(1)]["x_change"] == 15
    assert f[w(2)]["x_change"] == -5


def test_missing_values_are_skipped_not_zeroed():
    f = run([10, None, 30])
    assert f[w(1)]["x_roll_mean_3"] is None
    assert f[w(1)]["x_change"] is None
    assert f[w(2)]["x_roll_mean_3"] == 20
    assert f[w(2)]["x_change"] is None


def test_a_window_never_sees_the_future():
    before = run([10, 20, 30])
    after = run([10, 20, 30, 999])
    for i in range(3):
        assert before[w(i)] == after[w(i)]


def test_an_excluded_window_never_feeds_a_later_one():
    # Windows 1 and 2 are excluded (M4-4's db_throttling_onset: the surge). Window 3 looks
    # back over window 0, as if they weren't there.
    f = tf.temporal_features(rows([10, 500, 400, 30, 40]), columns=[COL], lookback={w(0), w(3), w(4)})
    assert f[w(3)]["x_change"] == 20
    assert f[w(3)]["x_roll_mean_3"] == 20
    assert f[w(4)]["x_roll_mean_3"] == pytest.approx(80 / 3)     # 10, 30, 40
    # The excluded windows still get features, from themselves and the labeled windows before.
    assert f[w(1)]["x_change"] == 490
    assert f[w(2)]["x_change"] == 390
    assert f[w(2)]["x_roll_mean_3"] == 205                        # 10, 400


def test_without_labels_every_window_is_looked_back_over():
    assert tf.temporal_features(rows([10, 500, 30]), columns=[COL])[w(2)]["x_change"] == -470


def test_missing_column_gives_empty_values():
    f = tf.temporal_features({w(0): {}, w(1): {}}, columns=[COL])
    assert f[w(1)]["x_roll_mean_3"] is None


def test_output_columns_pass_the_boundary_guard():
    out = tf.temporal_features({w(0): {}, w(1): {}})
    columns = {c for row in out.values() for c in row}
    guard.check_columns(columns)
    assert "order_dependency_latency_ms_mean_roll_mean_3" in columns