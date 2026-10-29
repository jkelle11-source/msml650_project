"""Tests for the evaluation script (M5-3), on a synthetic job folder. No AWS.

Run from AnomalyPulse/:  python3 -m pytest ml/test_evaluate.py -q
"""
import json

import pytest

from ml import evaluate
from ml.test_train_incident_rf import make_root
import train_incident_rf as tr


def trained_root(tmp_path):
    root = make_root(tmp_path)
    tr.run(root)
    return root


def test_a_trained_package_is_scored_against_model0(tmp_path):
    root = trained_root(tmp_path)
    rows = evaluate.load_validation(root)
    report, classes = evaluate.build_report(rows, {"random_forest": root / "model"})
    assert report["classifiers"]["random_forest"]["accuracy"] > 0.9
    assert set(report["model0"]) == {"rolling_mean", "previous_window"}
    assert "CASCADING_FAILURE" in classes


def test_the_cascade_margin_is_reported(tmp_path):
    root = trained_root(tmp_path)
    report, _ = evaluate.build_report(evaluate.load_validation(root),
                                      {"random_forest": root / "model"})
    assert report["cascade"]["windows"] == 2
    assert "random_forest" in report["cascade"]["margin"]


def test_xgboost_numbers_are_mapped_back_to_class_names(tmp_path):
    pytest.importorskip("xgboost")
    import train_incident_xgb as tx
    root = make_root(tmp_path)
    tx.run(root)
    report, _ = evaluate.build_report(evaluate.load_validation(root), {"xgboost": root / "model"})
    assert report["classifiers"]["xgboost"]["accuracy"] > 0.9


def test_a_package_whose_columns_differ_is_refused(tmp_path):
    root = trained_root(tmp_path)
    model, meta = evaluate.load_package(root / "model")
    meta["columns"] = ["x1", "zzz"]
    with pytest.raises(evaluate.EvalError, match="columns differ"):
        evaluate.predict(model, meta, evaluate.load_validation(root))


def test_a_detector_package_is_refused(tmp_path):
    root = trained_root(tmp_path)
    model, meta = evaluate.load_package(root / "model")
    meta["kind"] = "detector"
    with pytest.raises(evaluate.EvalError, match="detector"):
        evaluate.predict(model, meta, evaluate.load_validation(root))


def test_model0_reads_sixteen_columns():
    columns = evaluate.model0_columns()
    assert len(columns) == len(set(columns)) == 16
    assert "order_p95_latency_ms_change" in columns and "payment_error_rate_5xx" in columns


def test_main_refuses_features_that_lack_model0_columns(tmp_path, capsys):
    root = trained_root(tmp_path)
    code = evaluate.main(["--root", str(root), "--package", f"rf={root / 'model'}"])
    assert code == 1
    assert "Model 0 reads columns the features lack" in capsys.readouterr().err


def test_a_malformed_package_argument_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        evaluate.main(["--root", str(tmp_path), "--package", "no-equals-sign"])