"""Tests for the evaluation script (M5-3), on a synthetic job folder. No AWS.

Run from AnomalyPulse/:  python3 -m pytest ml/test_evaluate.py -q
"""
import json

import pytest

from ml import evaluate
from ml.test_train_incident_rf import CLASSES, SMALL, make_root
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

def write_full_run(root, channel, eid):
    """Like the job-script tests' runs, plus the 16 columns Model 0 reads.

    Non-NORMAL windows get a 50% 5xx rate, so Model 0 flags exactly those."""
    features, labels = [], []
    n = 0
    for label in CLASSES:
        index = CLASSES.index(label)
        for k in range(2):
            w = f"2026-10-16T03:{n:02d}:00.000Z"
            n += 1
            row = {"window_start": w, "x1": float(index * 10 + k), "x2": float(-3 * index)}
            row.update({column: 0.0 for column in evaluate.model0_columns()})
            row["order_error_rate_5xx"] = 0.0 if label == "NORMAL" else 0.5
            features.append(row)
            labels.append({"window_start": w, "label": label, "included": True,
                           "excluded_reason": None})
    base = root / "input" / "data" / channel
    for tree, file, rows in (("tier1/features", "features.jsonl", features),
                             ("tier2/labels", "labels.jsonl", labels)):
        folder = base / tree / f"experiment_id={eid}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_full_root(tmp_path):
    for channel, eid in (("train", "a"), ("train", "b"), ("validation", "c")):
        write_full_run(tmp_path, channel, eid)
    config = tmp_path / "input" / "config"
    config.mkdir(parents=True)
    (config / "hyperparameters.json").write_text(json.dumps(SMALL))
    return tmp_path


def test_main_scores_and_writes_the_report_end_to_end(tmp_path, capsys):
    root = make_full_root(tmp_path)
    tr.run(root)
    out = tmp_path / "report.md"
    code = evaluate.main(["--root", str(root), "--package", f"random_forest={root / 'model'}",
                          "--out", str(out)])
    assert code == 0
    text = out.read_text()
    assert capsys.readouterr().out.strip() == text.strip()
    for needle in ("Model 0 (rolling_mean)", "Model 0 (previous_window)", "random_forest",
                   "Confusion matrix", "Cascade margin over Model 0"):
        assert needle in text


def test_model0_alone_can_be_scored_with_no_package(tmp_path, capsys):
    root = make_full_root(tmp_path)
    assert evaluate.main(["--root", str(root)]) == 0
    assert "Model 0 (previous_window)" in capsys.readouterr().out


def test_model0_flags_exactly_the_anomalous_windows_in_the_synthetic_data(tmp_path):
    root = make_full_root(tmp_path)
    report, _ = evaluate.build_report(evaluate.load_validation(root), {})
    for variant in ("rolling_mean", "previous_window"):
        assert report["model0"][variant]["recall"] == 1.0
        assert report["model0"][variant]["fpr"] == 0.0


def test_main_reports_a_missing_validation_channel(tmp_path, capsys):
    assert evaluate.main(["--root", str(tmp_path)]) == 1
    assert "no runs" in capsys.readouterr().err