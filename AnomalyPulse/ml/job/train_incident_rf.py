"""Train the Random Forest incident classifier in a SageMaker training job (M5-3).

Same shape as train.py (M5-1) and train_incident_xgb.py, reusing train.py's helpers: the
same channels, the same join to labels, the same refusal of empty cells. A forest takes
string labels directly, so model.json's `classes` is list(model.classes_), in that order
(the Lambda reads class i as classes[i]). No scaler (trees don't need one), no `converged`.
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
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score

import train
from train import TrainError

ROOT = Path(os.environ.get("ANOMALYPULSE_JOB_ROOT", "/opt/ml"))

# name: (type, default). Each value arrives JSON-encoded (ml/launch.py).
SETTINGS = {
    "n_estimators": (int, 300),
    "max_depth": (int, 12),
    "min_samples_leaf": (int, 1),
}
# Install names. python is added separately.
LIBRARIES = ("scikit-learn", "numpy", "scipy", "joblib")


def check_settings(settings):
    for name in SETTINGS:
        if settings[name] < 1:
            raise TrainError(f"{name} must be at least 1, got {settings[name]}")


def fit(X, y, settings):
    model = RandomForestClassifier(
        n_estimators=settings["n_estimators"],
        max_depth=settings["max_depth"],
        min_samples_leaf=settings["min_samples_leaf"],
        n_jobs=1,
        random_state=42,
    )
    return model.fit(np.asarray(X, dtype=float), y)


def scores(model, X, y):
    predicted = model.predict(np.asarray(X, dtype=float))
    return {"macro_f1": float(f1_score(y, predicted, average="macro")),
            "accuracy": float(accuracy_score(y, predicted))}


def package(model, columns, settings, counts, metrics):
    """model.json's dict, package format 1. No `converged`: a forest has no iterative solver."""
    libraries = {"python": platform.python_version()}
    libraries.update({name: importlib.metadata.version(name) for name in LIBRARIES})
    return {
        "format": train.FORMAT,
        "task": "incident_type",
        "estimator": "random_forest",
        "columns": columns,
        "classes": [str(c) for c in model.classes_],
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
    if len(set(data["train"][1])) < 2:
        raise TrainError("training has only one class")

    model = fit(*data["train"], settings)
    metrics = {name: scores(model, *data[name]) for name in train.CHANNELS}
    counts = {"rows": {name: len(rows[name]) for name in train.CHANNELS},
              "runs": {name: len(runs[name]) for name in train.CHANNELS}}
    result = package(model, columns, settings, counts, metrics)

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