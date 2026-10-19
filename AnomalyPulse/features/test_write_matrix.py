"""Tests for writing the feature matrix with the guard and join check in front (M4-5)."""
import json

import pytest

from features import build_matrix as bm
from features import guard
from features import write_matrix as wm
from features.join_labels import JoinError
from features.test_build_matrix import REC, SNAP

W = "2026-10-16T03:19:00.000Z"
BOUNDS = ("2026-10-16T03:19:00.000Z", "2026-10-16T03:20:00.000Z")   # 03:19 is the run's one whole window
LABELS = [{"window_start": W, "label": "NORMAL", "included": True, "excluded_reason": None}]


def write(put, labels=LABELS):
    return wm.write_features("v1", "exp-1", [REC], [SNAP], labels, BOUNDS, put)


def test_key_is_under_tier1_features_and_keyed_by_experiment():
    key = wm.feature_key("v1", "exp-1")
    assert key == "datasets/v1/tier1/features/experiment_id=exp-1/features.jsonl"
    assert "tier2" not in key


def test_lines_carry_window_start_but_not_experiment_id_or_label():
    calls = []
    write(lambda k, b: calls.append((k, b)))
    line = json.loads(calls[0][1].splitlines()[0])
    assert line["window_start"] == W
    assert "experiment_id" not in line and "label" not in line
    assert len(line) == 1 + len(bm.columns())


def test_a_clean_matrix_is_written_once():
    calls = []
    key, matrix = write(lambda k, b: calls.append(k))
    assert calls == [key]
    assert list(matrix) == [W]


def test_tier2_column_in_the_matrix_is_refused():
    with pytest.raises(guard.BoundaryViolation):
        wm.to_jsonl({W: {"request_rate": 1.0, "severity": "HIGH"}})


def test_leaky_builder_never_reaches_put(monkeypatch):
    real = bm.request_features

    def leaky(records):
        rows = real(records)
        for row in rows.values():
            row["incident_type"] = "TRAFFIC_SPIKE"
        return rows

    monkeypatch.setattr(bm, "request_features", leaky)
    calls = []
    with pytest.raises(guard.BoundaryViolation):
        write(lambda k, b: calls.append(k))
    assert calls == []  # nothing was written


def test_a_run_that_does_not_join_its_labels_is_never_written():
    calls = []
    with pytest.raises(JoinError):
        write(lambda k, b: calls.append(k), labels=[])   # a feature row with no label row
    assert calls == []
