"""Tests for writing the feature matrix with the guard in front (M4-5)."""
import json

import pytest

import build_matrix as bm
import guard
import write_matrix as wm
from test_build_matrix import REC, SNAP


def test_key_is_under_tier1_features_and_keyed_by_experiment():
    key = wm.feature_key("v1", "exp-1")
    assert key == "datasets/v1/tier1/features/experiment_id=exp-1/features.jsonl"
    assert "tier2" not in key


def test_lines_carry_window_start_but_not_experiment_id():
    calls = []
    wm.write_features("v1", "exp-1", [REC], [SNAP], lambda k, b: calls.append((k, b)))
    line = json.loads(calls[0][1].decode().splitlines()[0])
    assert line["window_start"] == "2026-10-16T03:19:00.000Z"
    assert "experiment_id" not in line


def test_a_clean_matrix_is_written_once():
    calls = []
    key = wm.write_features("v1", "exp-1", [REC], [SNAP], lambda k, b: calls.append(k))
    assert calls == [key]


def test_tier2_column_in_the_matrix_is_refused():
    with pytest.raises(guard.BoundaryViolation):
        wm.to_jsonl({"2026-10-16T03:19:00.000Z": {"request_rate": 1.0, "severity": "HIGH"}})


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
        wm.write_features("v1", "exp-1", [REC], [SNAP], lambda k, b: calls.append(k))
    assert calls == []  # nothing was written

def test_s3_put_refuses_keys_outside_tier1_features(monkeypatch):
    import sys
    from unittest import mock
    fake = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "boto3", mock.MagicMock(Session=lambda **k: mock.MagicMock(client=lambda n: fake)))
    put = wm.s3_put("bucket")
    with pytest.raises(ValueError):
        put("datasets/v1/tier2/labels/x.json", b"{}")
    fake.put_object.assert_not_called()


def test_s3_put_never_overwrites(monkeypatch):
    import sys
    from unittest import mock
    fake = mock.MagicMock()  # head_object succeeds, meaning the key exists
    monkeypatch.setitem(sys.modules, "boto3", mock.MagicMock(Session=lambda **k: mock.MagicMock(client=lambda n: fake)))
    put = wm.s3_put("bucket")
    with pytest.raises(FileExistsError):
        put(wm.feature_key("v1", "exp-1"), b"{}")
    fake.put_object.assert_not_called()