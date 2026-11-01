"""Train the XGBoost incident classifier in a SageMaker training job (M5-3).

Same shape as train.py (M5-1), and it reuses train.py's helpers: the same channels, the
same join to labels, the same refusal of empty cells. What differs:
  - XGBoost trains on class NUMBERS, so labels go through a LabelEncoder fitted on the
    train channel, and model.json's `classes` is list(encoder.classes_), in that order.
    The Lambda reads class i as classes[i] and only checks the count: another order
    mislabels silently.
  - no scaler (trees don't need one), and no `converged` field.
Prints validation:macro_f1=<x> for the tuner. Bad input stops it with exit 1 and a message.
"""
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import accuracy_score, f1_score
from sklearn.preprocessing import LabelEncoder
from xgboost import XGBClassifier

import train
from train import TrainError

ROOT = Path(os.environ.get("ANOMALYPULSE_JOB_ROOT", "/opt/ml"))

# name: (type, default). Each value arrives JSON-encoded (ml/launch.py).
SETTINGS = {
    "n_estimators": (int, 200),
    "max_depth": (int, 6),
    "learning_rate": (float, 0.3),
    "subsample": (float, 1.0),
    "colsample_bytree": (float, 1.0),
}
# Install names. python is added separately; the Lambda refuses a library it doesn't install.
LIBRARIES = ("scikit-learn", "numpy", "scipy", "joblib", "xgboost")


def check_settings(settings):
    if settings["n_estimators"] < 1:
        raise TrainError(f"n_estimators must be at least 1, got {settings['n_estimators']}")
    if settings["max_depth"] < 1:
        raise TrainError(f"max_depth must be at least 1, got {settings['max_depth']}")
    if settings["learning_rate"] <= 0:
        raise TrainError(f"learning_rate must be above 0, got {settings['learning_rate']}")
    for name in ("subsample", "colsample_bytree"):
        if not 0 < settings[name] <= 1:
            raise TrainError(f"{name} must be above 0 and at most 1, got {settings[name]}")


def fit(X, y_encoded, settings):
    model = XGBClassifier(
        objective="multi:softprob",
        n_estimators=settings["n_estimators"],
        max_depth=settings["max_depth"],
        learning_rate=settings["learning_rate"],
        subsample=settings["subsample"],
        colsample_bytree=settings["colsample_bytree"],
        tree_method="hist",
        n_jobs=1,
        random_state=42,
    )
    return model.fit(np.asarray(X, dtype=float), y_encoded)


def scores(model, X, y_encoded):
    predicted = model.predict(np.asarray(X, dtype=float))
    return {"macro_f1": float(f1_score(y_encoded, predicted, average="macro")),
            "accuracy": float(accuracy_score(y_encoded, predicted))}


def package(encoder, columns, settings, counts, metrics):
    """model.json's dict, package format 1. No `converged`: trees have no iterative solver."""
    libraries = {"python": platform.python_version()}
    libraries.update({name: importlib.metadata.version(name) for name in LIBRARIES})
    return {
        "format": train.FORMAT,
        "task": "incident_type",
        "estimator": "xgboost",
        "columns": columns,
        "classes": [str(c) for c in encoder.classes_],
        "settings": settings,
        "rows": counts["rows"],
        "runs": counts["runs"],
        "metrics": metrics,
        "libraries": libraries,
    }


def run(root):
    """Read the channels, fit, score, write the package to <root>/model. Returns model.json's dict."""
    settings = train.read_settings(root / "input" / "config" / "hyperparameters.json", SETTINGS)
    check_settings(settings)
    runs = {name: train.load_runs(root / "input" / "data" / name) for name in train.CHANNELS}
    rows = {name: train.training_rows(runs[name]) for name in train.CHANNELS}
    for name in train.CHANNELS:
        if not rows[name]:
            raise TrainError(f"the {name} channel has no labeled windows")
    # Sorted, never file or dict order: whoever builds a window's vector follows this list.
    columns = sorted(set(rows["train"][0][1]) - {train.KEY, train.LABEL})
    data = {name: train.to_matrix(rows[name], columns) for name in train.CHANNELS}
    unseen = sorted(set(data["validation"][1]) - set(data["train"][1]))
    if unseen:
        raise TrainError(f"validation has classes training lacks: {unseen}")
    encoder = LabelEncoder().fit(data["train"][1])
    if len(encoder.classes_) < 2:
        raise TrainError("training has only one class")
    encoded = {name: encoder.transform(data[name][1]) for name in train.CHANNELS}

    model = fit(data["train"][0], encoded["train"], settings)
    metrics = {name: scores(model, data[name][0], encoded[name]) for name in train.CHANNELS}
    counts = {"rows": {name: len(rows[name]) for name in train.CHANNELS},
              "runs": {name: len(runs[name]) for name in train.CHANNELS}}
    result = package(encoder, columns, settings, counts, metrics)

    out = root / "model"
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out / "model.joblib")
    (out / "model.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    try:
        result = run(ROOT)
    except TrainError as err:
        print(f"TrainError: {err}", file=sys.stderr, flush=True)
        return 1
    # Counts and scores only: the team reads these logs, so no rows and no labels.
    for name in train.CHANNELS:
        print(f"{name}: {result['runs'][name]} runs, {result['rows'][name]} rows", flush=True)
    for name in train.CHANNELS:
        for metric, value in result["metrics"][name].items():
            print(f"{name}:{metric}={value:.6f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
