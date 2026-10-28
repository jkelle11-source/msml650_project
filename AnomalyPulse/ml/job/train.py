"""Train the trivial model in a SageMaker training job (M5-1).

Fits Pipeline(StandardScaler, LogisticRegression) on the `train` channel's labeled
windows, scores it on the `validation` channel's, and writes package format 1 to
<root>/model, which SageMaker uploads as model.tar.gz:
    
    model.joblib    the fitted pipelien: the scaler and the model together
    model.json      what using it takes: the columns in order, the classes, the versions

Each channel mirrors the dataset: tier1/features/experiment_id=<id>/features.jsonl and
tier2/labels/experiment_id=<id>/labels.jsonl for every run in it. The job prints
validation:macro_f1=<x>, which the tuning job reads from its log. Bad input stops it with
the reason on stderr and in <root>/output/failure.

The root is /opt/ml in a job; set ANOMALYPULSE_JOB_ROOT to run it elsewhere. Python 3.10:
the images's (ml/requirements-image.txt).
"""
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    import join_labels
except ImportError:
    from features import join_labels

ROOT = Path(os.environ.get("ANOMALYPULSE_JOB_ROOT", "/opt/ml"))

# name: (type, default). Each value arrives JSON-encoded (ml/launch.py).
SETTINGS = {"C": (float, 1.0), "max_iter": (int, 1000)}

CHANNELS = ("train", "validation")
# A run's two files sit in a channel as in the dataset: <tree>/experiment_id=<id>/<file>.
TREES = {"features": "tier1/features", "labels": "tier2/labels"}
FILES = {"features": "features.jsonl", "labels": "labels.jsonl"}
KEY = join_labels.KEY
LABEL = "label"

FORMAT = 1
# Install names, as in smoke.py.
LIBRARIES = ("scikit-learn", "numpy", "scipy", "joblib")

class TrainError(Exception):
    """Bad input: the job stops with this message."""

def read_settings(path):
    """{name: value}: each value JSON-decoded once, sagemaker_* skipped (the toolkit's),
    a default for any not sent. Raises TrainError on an unknown name or a wrong type."""
    settings = {name: default for name, (kind, default) in SETTINGS.items()}
    for name, text in json.loads(Path(path).read_text()).items():
        if name.startswith("sagemaker_"):
            continue
        if name not in SETTINGS:
            raise TrainError(f"unknown setting: {name!r}; known: {sorted(SETTINGS)}")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise TrainError(f"{name}: {text!r} is not JSON") from None
        kind = SETTINGS[name][0]
        allowed, wanted = ((int, float), "a number") if kind is float else ((int,), "an integer")
        # bool is a subclass of int: true would otherwise pass as 1
        if isinstance(value, bool) or not isinstance(value, allowed):
            raise TrainError(f"{name} must be {wanted}, got {text!r}")
        settings[name] = kind(value)
    if settings["C"] <= 0:
        raise TrainError(f"C must be above 0, got {settings['C']}")
    if settings["max_iter"] < 1:
        raise TrainError(f"max_iter must be at least 1; got {settings['max_iter']}")
    return settings

def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def load_runs(channel):
    """[(experiment_id, feature_rows, label_rows)], sorted by id, for every run in a channel.
    Raises TrainError on a channel with no runs, or a run with only one of its two files."""
    ids = sorted({folder.name.split("=", 1)[1]
                  for tree in TREES.values() for folder in (channel / tree).glob("experiment_id=*")})
    if not ids:
        raise TrainError(f"no runs in the {channel.name} channel")
    runs = []
    for experiment_id in ids:
        paths = {part: channel / TREES[part] / f"experiment_id={experiment_id}" / FILES[part]
                 for part in TREES}
        missing = [path.name for path in paths.values() if not path.is_file()]
        if missing:
            raise TrainError(f"{channel.name}: run {experiment_id} has no {missing[0]}")
        runs.append((experiment_id, read_jsonl(paths["features"]), read_jsonl(paths["labels"])))
    return runs

def training_rows(runs):
    """[(experiment_id, row)]: each run's labeled windows joined to their features by
    join_labels.join, the join the feature build checks (M4-5). A row is its features
    plus "label"."""
    rows = []
    for experiment_id, feature_rows, label_rows in runs:
        try:
            joined, _ = join_labels.join(feature_rows, label_rows)
        except join_labels.JoinError as err:
            raise TrainError(f"run {experiment_id}: {err}") from None
        rows += [(experiment_id, row) for row in joined]
    return rows

def to_matrix(rows, columns):
    """X, y), X's values in `columns` order. Raises TrainError on a row whose columns
    differ from `columns`, or with an empty cell: a missing value is refused, never filled."""
    expected = set(columns) | {KEY, LABEL}
    X, y = [], []
    for experiment_id, row in rows:
        where = f"run {experiment_id}, window {row[KEY]}"
        if set(row) != expected:
            raise TrainError(f"{where}: columns differ from training's "
                             f"extra {sorted(set(row) - expected)}, missing {sorted(expected - set(row))}")
        empty = [c for c in columns + [LABEL] if row[c] is None]
        if empty:
            raise TrainError(f"{where}: {len(empty)} empty cells, e.g. {empty[:3]}")
        X.append([row[c] for c in columns])
        y.append(row[LABEL])
    return X, y

def fit(X, y, settings):
    """The scaler learns each column's mean and spread from these rows only."""
    pipeline = Pipeline([
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C = settings["C"], max_iter = settings["max_iter"]))
    ])
    return pipeline.fit(X, y)

def scores(pipeline, X, y):
    predicted = pipeline.predict(X)
    return {"macro_f1": float(f1_score(y, predicted, average = "macro")),
            "accuracy": float(accuracy_score(y, predicted))}

def package(pipeline, columns, settings, count, metrics):
    """model.json's dict, package format 1. numpy values become plain ones: json.dumps
    can't write a numpy int."""
    libraries = {"python": platform.python_version()}
    libraries.update({name: importlib.metadata.version(name) for name in LIBRARIES})
    return {
        "format": FORMAT,
        "task": "incident_type",
        "estimator": "logistic_regression",
        "columns": columns,
        "classes": [str(c) for c in pipeline.classes_],
        "settings": settings,
        "converged": int(pipeline.named_steps["model"].n_iter_.max()) < settings["max_iter"],
        "rows": count["rows"],
        "runs": count["runs"],
        "metrics": metrics,
        "libraries": libraries
    }

def train(root):
    """Read the channels, fit, score, and write the package to <root>/model.
    Returns model.json's dict. Raises TrainError on bad input, before fitting."""
    settings = read_settings(root / "input" / "config" / "hyperparameters.json")
    runs = {name: load_runs(root / "input" / "data" / name) for name in CHANNELS}
    rows = {name: training_rows(runs[name]) for name in CHANNELS}
    for name in CHANNELS:
        if not rows[name]:
            raise TrainError(f"the {name} channel has no labeled windows")
    # Sorted, never file or dict order: whoever builds a window's vector follows this list.
    columns = sorted(set(rows["train"][0][1]) - {KEY, LABEL})
    data = {name: to_matrix(rows[name], columns) for name in CHANNELS}
    unseen = sorted(set(data["validation"][1]) - set(data["train"][1]))
    if unseen:
        raise TrainError(f"validation has classes training lacks: {unseen}")
    pipeline = fit(*data["train"], settings)
    metrics = {name: scores(pipeline, *data[name]) for name in CHANNELS}
    counts = {"rows": {name: len(rows[name]) for name in CHANNELS},
              "runs": {name: len(runs[name]) for name in CHANNELS}}
    result = package(pipeline, columns, settings, counts, metrics)
    
    model = root / "model"
    model.mkdir(parents = True, exist_ok = True)
    joblib.dump(pipeline, model / "model.joblib")
    (model / "model.json").write_text(json.dumps(result, indent = 2) + "\n")
    return result

def main():
    try:
        result = train(ROOT)
    except TrainError as err:
        print(f"TrainError: {err}", file = sys.stderr, flush = True)
        output = ROOT / "output"
        output.mkdir(parents = True, exist_ok = True)
        (output / "failure").write_text(f"TrainError: {err}\n")
        return 1
    # Counts and scores only: the team reads these logs, so no rows and no labels.
    for name in CHANNELS:
        print(f"{name}: {result['runs'][name]} runs, {result['rows'][name]} rows", flush = True)
    print(f"converged: {result['converged']}", flush = True)
    for name in CHANNELS:
        for metric, value in result["metrics"][name].items():
            print(f"{name}:{metric}={value:.6f}", flush = True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
    