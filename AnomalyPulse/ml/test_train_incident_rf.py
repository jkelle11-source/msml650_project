"""Tests for the Random Forest job script (M5-3), on a tiny synthetic job folder. No AWS.

Run from AnomalyPulse/:  python -m pytest ml/test_train_incident_rf.py -q
"""
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pytest

pytest.importorskip("sklearn")
sys.path.insert(0, str(Path(__file__).parent / "job"))

import train  # noqa: E402
import train_incident_rf as tr  # noqa: E402

CLASSES = sorted(["NORMAL", "TRAFFIC_SPIKE", "DATABASE_THROTTLING",
                  "LAMBDA_DEGRADATION", "DEPENDENCY_FAILURE", "CASCADING_FAILURE"])
SMALL = {"n_estimators": "20", "max_depth": "4"}


def write_run(root, channel, eid, classes=CLASSES, hole=False):
    """Two windows per class; x1 and x2 separate the classes."""
    features, labels = [], []
    n = 0
    for label in classes:
        for k in range(2):
            w = f"2026-10-16T03:{n:02d}:00.000Z"
            n += 1
            features.append({"window_start": w, "x1": float(CLASSES.index(label) * 10 + k),
                             "x2": float(-3 * CLASSES.index(label))})
            labels.append({"window_start": w, "label": label, "included": True, "excluded_reason": None})
    if hole:
        features[0]["x1"] = None
    base = root / "input" / "data" / channel
    for tree, file, rows in (("tier1/features", "features.jsonl", features),
                             ("tier2/labels", "labels.jsonl", labels)):
        folder = base / tree / f"experiment_id={eid}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_root(tmp_path, settings=SMALL, **val):
    write_run(tmp_path, "train", "a")
    write_run(tmp_path, "train", "b")
    write_run(tmp_path, "validation", "c", **val)
    config = tmp_path / "input" / "config"
    config.mkdir(parents=True)
    (config / "hyperparameters.json").write_text(json.dumps(settings))
    return tmp_path


def test_writes_a_format_1_package_without_xgboost(tmp_path):
    result = tr.run(make_root(tmp_path))
    meta = json.loads((tmp_path / "model" / "model.json").read_text())
    assert meta == json.loads(json.dumps(result))
    assert meta["format"] == 1 and meta["task"] == "incident_type" and meta["estimator"] == "random_forest"
    assert meta["columns"] == ["x1", "x2"]
    assert meta["runs"] == {"train": 2, "validation": 1}
    assert "converged" not in meta and "kind" not in meta
    assert set(meta["libraries"]) == {"python", "scikit-learn", "numpy", "scipy", "joblib"}


def test_classes_match_the_models_class_order(tmp_path):
    result = tr.run(make_root(tmp_path))
    model = joblib.load(tmp_path / "model" / "model.joblib")
    assert result["classes"] == list(model.classes_) == CLASSES
    for k, label in enumerate(CLASSES):
        assert model.predict(np.array([[k * 10.0, -3.0 * k]]))[0] == label


def test_scores_are_in_range(tmp_path):
    result = tr.run(make_root(tmp_path))
    for channel in ("train", "validation"):
        assert 0.0 <= result["metrics"][channel]["macro_f1"] <= 1.0
        assert 0.0 <= result["metrics"][channel]["accuracy"] <= 1.0


def test_empty_cell_is_refused_not_filled(tmp_path):
    with pytest.raises(train.TrainError, match="empty cells"):
        tr.run(make_root(tmp_path, hole=True))


def test_unknown_setting_is_refused(tmp_path):
    with pytest.raises(train.TrainError, match="unknown setting"):
        tr.run(make_root(tmp_path, settings={"bogus": "1"}))


def test_the_tuning_setting_sagemaker_adds_is_skipped(tmp_path):
    tr.run(make_root(tmp_path, settings={**SMALL, "_tuning_objective_metric": "validation:macro_f1"}))


@pytest.mark.parametrize("settings", [{"max_depth": "0"}, {"n_estimators": "0"},
                                      {"min_samples_leaf": "0"}])
def test_bad_setting_values_are_refused(tmp_path, settings):
    with pytest.raises(train.TrainError):
        tr.run(make_root(tmp_path, settings=settings))


def test_validation_class_missing_from_training_is_refused(tmp_path):
    write_run(tmp_path, "train", "a", classes=CLASSES[:5])
    write_run(tmp_path, "validation", "c")
    config = tmp_path / "input" / "config"
    config.mkdir(parents=True)
    (config / "hyperparameters.json").write_text(json.dumps(SMALL))
    with pytest.raises(train.TrainError, match="training lacks"):
        tr.run(tmp_path)