"""Train the supervised binary anomaly detector (NORMAL vs ANOMALY) in a SageMaker job (M5-2).

    python -m ml.launch train --script ml/job/train_binary.py --dry-run
    python -m ml.launch train --script ml/job/train_binary.py                  # logistic regression
    python -m ml.launch train --script ml/job/train_binary.py --set estimator=xgboost --set max_depth=4
    python -m ml.launch tune --script ml/job/train_binary.py --range C=0.01:100:log --jobs 8

The labeled-data comparison point for the Isolation Forest (train_iforest.py): we do have
labels here, so this detector measures what going unsupervised costs (PROJECT_PLAN §8). Its
target is derived from the six-class label: anything but NORMAL is ANOMALY.

    estimator=logistic_regression (default)  StandardScaler + LogisticRegression(C): the
                                              interpretable baseline, like train.py's.
    estimator=xgboost                         XGBClassifier(n_estimators, max_depth, learning_rate)

Writes package format 1 as a classifier with its own task, `anomaly_binary`, and classes
[ANOMALY, NORMAL] in classes_ order (LabelEncoder's for xgboost, which trains on 0/1). The
Diagnosis Lambda answers with the likelier class, the 0.5 cut-off these metrics use.
"""
import importlib.metadata
import json
import platform
import sys

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler

import detection
import train
from train import TrainError

SETTINGS = {"estimator": (str, "logistic_regression"), "C": (float, 1.0), "max_iter": (int, 1000),
            "n_estimators": (int, 200), "max_depth": (int, 4), "learning_rate": (float, 0.1),
            "random_state": (int, 0)}
ESTIMATORS = ("logistic_regression", "xgboost")
TASK = "anomaly_binary"

def check_settings(settings):
    if settings["estimator"] not in ESTIMATORS:
        raise TrainError(f"estimator must be one of {ESTIMATORS}; got {settings['estimator']!r}")
    if settings["C"] <= 0:
        raise TrainError(f"C must be above 0, got {settings['C']}")
    for name in ("max_iter", "n_estimators", "max_depth"):
        if settings[name] < 1:
            raise TrainError(f"{name} must be at least 1; got {settings[name]}")
    if not 0 < settings["learning_rate"] <= 1:
        raise TrainError(f"learning_rate must be in (0, 1]; got {settings['learning_rate']}")

def fit(X, y, settings):
    """(pipeline, classes): the fitted pipeline, and the class names in its classes_ order.
    y is NORMAL or ANOMALY."""
    if len(set(y)) < 2:
        raise TrainError(f"the train channel has only {sorted(set(y))} windows")
    if settings["estimator"] == "logistic_regression":
        pipeline = Pipeline([
            ("scale", StandardScaler()),
            ("model", LogisticRegression(C = settings["C"], max_iter = settings["max_iter"]))])
        pipeline.fit(X, y)
        return pipeline, [str(c) for c in pipeline.classes_]
    from xgboost import XGBClassifier      # only the xgboost estimator needs it
    encoder = LabelEncoder().fit(y)
    pipeline = Pipeline([("model", XGBClassifier(
        n_estimators = settings["n_estimators"], max_depth = settings["max_depth"],
        learning_rate = settings["learning_rate"], random_state = settings["random_state"],
        n_jobs = 1))])
    pipeline.fit(np.asarray(X, float), encoder.transform(y))
    # The Lambda reads class i as classes[i]: LabelEncoder's order, never another.
    return pipeline, [str(c) for c in encoder.classes_]

def anomaly_probability(pipeline, classes, X):
    return pipeline.predict_proba(np.asarray(X, float))[:, classes.index(detection.ANOMALY)]

def package(pipeline, classes, columns, settings, counts, metrics, evaluation):
    names = list(train.LIBRARIES) + (["xgboost"] if settings["estimator"] == "xgboost" else [])
    libraries = {"python": platform.python_version()}
    libraries.update({name: importlib.metadata.version(name) for name in names})
    result = {
        "format": train.FORMAT,
        "task": TASK,
        "estimator": settings["estimator"],
        "columns": columns,
        "classes": classes,
        "settings": settings,
        "rows": counts["rows"],
        "runs": counts["runs"],
        "metrics": metrics,
        "evaluation": evaluation,
        "libraries": libraries
    }
    if settings["estimator"] == "logistic_regression":
        result["converged"] = int(pipeline.named_steps["model"].n_iter_.max()) < settings["max_iter"]
    return result

def top_features(pipeline, columns, count = 10):
    """The logistic regression's largest standardized coefficients, toward ANOMALY: which
    features move a window toward an alarm. Empty for xgboost."""
    if "scale" not in pipeline.named_steps:
        return []
    model = pipeline.named_steps["model"]
    # classes_ is [ANOMALY, NORMAL], so coef_ points toward NORMAL; negate it.
    sign = -1 if list(model.classes_).index(detection.ANOMALY) == 0 else 1
    weights = sign * model.coef_[0]
    order = np.argsort(-np.abs(weights))[:count]
    return [{"feature": columns[i], "weight": round(float(weights[i]), 4)} for i in order]

def run(root):
    settings = train.read_settings(root / "input" / "config" / "hyperparameters.json", SETTINGS)
    check_settings(settings)
    runs = {name: train.load_runs(root / "input" / "data" / name) for name in train.CHANNELS}
    rows = {name: train.training_rows(runs[name]) for name in train.CHANNELS}
    for name in train.CHANNELS:
        if not rows[name]:
            raise TrainError(f"the {name} channel has no labeled windows")
    columns = sorted(set(rows["train"][0][1]) - {train.KEY, train.LABEL})
    data = {name: train.to_matrix(rows[name], columns) for name in train.CHANNELS}

    X, y = data["train"]
    pipeline, classes = fit(X, detection.binary_labels(y), settings)
    metrics, evaluation = {}, {"top_features": top_features(pipeline, columns)}
    for name in train.CHANNELS:
        p = anomaly_probability(pipeline, classes, data[name][0])
        # The Lambda answers the likelier class: ANOMALY when P(ANOMALY) > 0.5
        metrics[name], evaluation[name] = detection.evaluate(
            runs[name], rows[name], data[name][1], p > 0.5, p)

    counts = {"rows": {name: len(rows[name]) for name in train.CHANNELS},
              "runs": {name: len(runs[name]) for name in train.CHANNELS}}
    result = package(pipeline, classes, columns, settings, counts, metrics, evaluation)
    folder = root / "model"
    folder.mkdir(parents = True, exist_ok = True)
    joblib.dump(pipeline, folder / "model.joblib")
    (folder / "model.json").write_text(json.dumps(result, indent = 2) + "\n")
    return result

def report(result, out = sys.stdout):
    """Counts and scores only: the team reads these logs, so no rows and no labels."""
    for name in train.CHANNELS:
        print(f"{name}: {result['runs'][name]} runs, {result['rows'][name]} rows", file = out, flush = True)
    if "converged" in result:
        print(f"converged: {result['converged']}", file = out, flush = True)
    for name in train.CHANNELS:
        for metric, value in result["metrics"][name].items():
            print(f"{name}:{metric}={value:.6f}", file = out, flush = True)

def main():
    try:
        result = run(train.ROOT)
    except TrainError as err:
        print(f"TrainError: {err}", file = sys.stderr, flush = True)
        return 1
    report(result)
    return 0

if __name__ == "__main__":
    sys.exit(main())