"""Train the unsupervised anomaly detector, an Isolation Forest, in a SageMaker job (M5-2).

    python -m ml.launch train --script ml/job/train_iforest.py --dry-run
    python -m ml.launch train --script ml/job/train_iforest.py --set n_estimators=300
    python -m ml.launch tune --script ml/job/train_iforest.py --range max_samples=64:512 \
        --range max_features=0.3:1.0 --jobs 8      # objective: validation:roc_auc

Isolation Forest is here to model production, where nobody labels incidents (PROJECT_PLAN §8).
So the fit never sees an incident label:
    fit_on=normal (default)  fit on the training windows of a period known to be healthy, the
                             NORMAL windows, as a team would fit on a quiet baseline week. It uses
                             "this period was quiet", never which incident a window was.
    fit_on=all               fit on every training window, label-blind. Our dataset is mostly
                             incidents by design (over half its windows), which breaks the forest's
                             "anomalies are rare" assumption: an ablation, not production.

The cut-off is the one choice that needs some labels. threshold_rule picks it:
    fpr (default)  flag target_fpr of the validation NORMAL windows (default 5%): needs only
                   known-healthy windows, the same thing the fit used.
    offset         scikit-learn's own cut-off (offset_, contamination="auto"): no labels at all.
    f1             the cut-off with the best validation F1: the optimistic, fully labeled case.
All three are scored and kept in model.json's evaluation.thresholds, so one job gives the
whole picture; `threshold` is the one threshold_rule chose. Choose on validation, never test.

Writes package format 1 as a detector: model.joblib is the fitted IsolationForest; model.json
has "kind": "detector" and "threshold" on score_samples' scale (below it is anomalous),
which is what the Diagnosis Lambda's handler.detect() reads. Isolation Forest splits each
feature at a random point in its range, so scaling changes nothing and there is no scaler.
"""
import importlib.metadata
import json
import platform
import sys

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

import detection
import train
from train import TrainError

SETTINGS = {"n_estimators": (int, 200), "max_samples": (int, 256), "max_features": (float, 1.0),
            "fit_on": (str, "normal"), "threshold_rule": (str, "fpr"),
            "target_fpr": (float, 0.05), "random_state": (int, 0)}
FIT_ON = ("normal", "all")
THRESHOLD_RULES = ("fpr", "offset", "f1")
TASK = "anomaly_iforest"

def check_settings(settings):
    if settings["n_estimators"] < 1:
        raise TrainError(f"n_estimators must be at least 1; got {settings['n_estimators']}")
    if settings["max_samples"] < 2:
        raise TrainError(f"max_samples must be at least 2; got {settings['max_samples']}")
    if not 0 < settings["max_features"] <= 1:
        raise TrainError(f"max_features must be in (0, 1]; got {settings['max_features']}")
    if settings["fit_on"] not in FIT_ON:
        raise TrainError(f"fit_on must be one of {FIT_ON}; got {settings['fit_on']!r}")
    if settings["threshold_rule"] not in THRESHOLD_RULES:
        raise TrainError(f"threshold_rule must be one of {THRESHOLD_RULES}; got {settings['threshold_rule']!r}")
    if not 0 < settings["target_fpr"] < 1:
        raise TrainError(f"target_fpr must be in (0, 1); got {settings['target_fpr']}")

def fit(X, y, settings):
    """The forest, fitted on the rows fit_on picks. y is used only to keep NORMAL rows."""
    X = np.asarray(X, float)
    if settings["fit_on"] == "normal":
        X = X[~np.asarray(detection.is_anomaly(y))]
        if not len(X):
            raise TrainError("fit_on=normal: the train channel has no NORMAL windows")
    model = IsolationForest(n_estimators = settings["n_estimators"],
                            max_samples = min(settings["max_samples"], len(X)),
                            max_features = settings["max_features"],
                            random_state = settings["random_state"])
    return model.fit(X)

def best_f1_threshold(truth, scores):
    """The score_samples cut-off with the best F1 (flags score < cut-off)."""
    truth, scores = np.asarray(truth, bool), np.asarray(scores, float)
    candidates = np.unique(scores)
    # A cut-off just above each distinct score flags it and everything below it.
    cuts = np.append((candidates[1:] + candidates[:-1]) / 2, candidates[-1] + 1e-9)
    best = max(cuts, key = lambda cut: detection.rates(detection.confusion(truth, scores < cut))["f1"])
    return float(best)

def thresholds(model, truth, scores, target_fpr):
    """{rule: cut-off on score_samples} for every rule, from the validation scores."""
    truth, scores = np.asarray(truth, bool), np.asarray(scores, float)
    normal = scores[~truth]
    if not len(normal):
        raise TrainError("the validation channel has no NORMAL windows to set a cut-off on")
    return {"fpr": float(np.quantile(normal, target_fpr)),
            "offset": float(model.offset_),
            "f1": best_f1_threshold(truth, scores)}

def package(model, columns, settings, counts, metrics, threshold, evaluation):
    libraries = {"python": platform.python_version()}
    libraries.update({name: importlib.metadata.version(name) for name in train.LIBRARIES})
    return {
        "format": train.FORMAT,
        "kind": "detector",
        "task": TASK,
        "estimator": "isolation_forest",
        "columns": columns,
        "threshold": threshold,
        "settings": settings,
        "rows": counts["rows"],
        "runs": counts["runs"],
        "metrics": metrics,
        "evaluation": evaluation,
        "libraries": libraries
    }

def run(root):
    """Read the channels, fit, choose the cut-off, score, and write the package to <root>/model.
    Returns model.json's dict. Raises TrainError on bad input, before fitting."""
    settings = train.read_settings(root / "input" / "config" / "hyperparameters.json", SETTINGS)
    check_settings(settings)
    runs = {name: train.load_runs(root / "input" / "data" / name) for name in train.CHANNELS}
    rows = {name: train.training_rows(runs[name]) for name in train.CHANNELS}
    for name in train.CHANNELS:
        if not rows[name]:
            raise TrainError(f"the {name} channel has no labeled windows")
    columns = sorted(set(rows["train"][0][1]) - {train.KEY, train.LABEL})
    data = {name: train.to_matrix(rows[name], columns) for name in train.CHANNELS}

    model = fit(*data["train"], settings)
    scores = {name: model.score_samples(np.asarray(data[name][0], float)) for name in train.CHANNELS}
    truth = detection.is_anomaly(data["validation"][1])
    cuts = thresholds(model, truth, scores["validation"], settings["target_fpr"])
    threshold = cuts[settings["threshold_rule"]]

    metrics, evaluation = {}, {"threshold_rule": settings["threshold_rule"], "thresholds": {}}
    for name in train.CHANNELS:
        flagged = scores[name] < threshold
        metrics[name], evaluation[name] = detection.evaluate(
            runs[name], rows[name], data[name][1], flagged, -scores[name])
    for rule, cut in cuts.items():
        counts = detection.confusion(truth, scores["validation"] < cut)
        evaluation["thresholds"][rule] = {"threshold": cut, **detection.rates(counts)}

    counts = {"rows": {name: len(rows[name]) for name in train.CHANNELS},
              "runs": {name: len(runs[name]) for name in train.CHANNELS}}
    result = package(model, columns, settings, counts, metrics, threshold, evaluation)
    folder = root / "model"
    folder.mkdir(parents = True, exist_ok = True)
    joblib.dump(model, folder / "model.joblib")
    (folder / "model.json").write_text(json.dumps(result, indent = 2) + "\n")
    return result

def report(result, out = sys.stdout):
    """Counts and scores only: the team reads these logs, so no rows and no labels."""
    for name in train.CHANNELS:
        print(f"{name}: {result['runs'][name]} runs, {result['rows'][name]} rows", file = out, flush = True)
    print(f"threshold ({result['settings']['threshold_rule']}): {result['threshold']:.6f}", file = out, flush = True)
    for name in train.CHANNELS:
        for metric, value in result["metrics"][name].items():
            print(f"{name}:{metric}={value:.6f}", file = out, flush = True)
    for rule, entry in result["evaluation"]["thresholds"].items():
        print(f"validation rule {rule}: " + ", ".join(f"{k} {v:.4f}" for k, v in entry.items()),
              file = out, flush = True)

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