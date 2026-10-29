"""Score Model 0 and trained packages on the validation channel (M5-3).

    python -m ml.evaluate --root ~/anomalypulse-job \
        --package xgboost=~/pkgs/xgboost --package random_forest=~/pkgs/rf --out report.md

--root is laid out like a job's /opt/ml (input/data/validation/...). Each --package is a folder
holding model.joblib and model.json. Prints the comparison report (ml/compare.py).
Run it in the training image's library versions, so the pickles load as they were written.
"""
import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "job"))
import train  # noqa: E402
from train import TrainError  # noqa: E402

from ml import compare, model0  # noqa: E402


class EvalError(Exception):
    """Bad input: the evaluation stops with this message."""


def load_validation(root):
    """[(experiment_id, row)]: every labeled validation window, features plus "label"."""
    runs = train.load_runs(Path(root) / "input" / "data" / "validation")
    rows = train.training_rows(runs)
    if not rows:
        raise EvalError("the validation channel has no labeled windows")
    return rows


def model0_columns():
    """The feature columns Model 0 reads. A missing one is skipped silently, so check them."""
    return [f"{svc}_{metric}" for svc in model0.SERVICES
            for metric in ("p95_latency_ms", "p95_latency_ms_change",
                           "p95_latency_ms_roll_mean_3", "error_rate_5xx")]


def load_package(directory):
    directory = Path(directory)
    try:
        meta = json.loads((directory / "model.json").read_text())
        model = joblib.load(directory / "model.joblib")
    except FileNotFoundError as err:
        raise EvalError(f"{directory}: {err.filename} not found") from None
    return model, meta


def predict(model, meta, rows):
    """Class names for each (experiment_id, row), in the package's own column order."""
    if meta.get("kind", "classifier") != "classifier":
        raise EvalError(f"a {meta.get('kind')} package is not a classifier")
    columns = meta["columns"]
    expected = set(columns) | {train.KEY, train.LABEL}
    X = []
    for experiment_id, row in rows:
        if set(row) != expected:
            raise EvalError(f"run {experiment_id}, window {row[train.KEY]}: the package's columns "
                            f"differ from the validation features")
        if any(row[c] is None for c in columns):
            raise EvalError(f"run {experiment_id}, window {row[train.KEY]}: empty feature cells")
        X.append([row[c] for c in columns])
    classes = meta["classes"]
    names = []
    for value in model.predict(np.asarray(X, dtype=float)):
        if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
            if not 0 <= int(value) < len(classes):
                raise EvalError(f"the model predicted class number {int(value)}; classes are {classes}")
            names.append(classes[int(value)])
        else:
            names.append(str(value))
    return names


def build_report(rows, packages):
    """(report, classes): Model 0's two baselines and each package, on the same windows."""
    truth = [row[train.LABEL] for _, row in rows]
    flags = {variant: model0.predict([row for _, row in rows], variant)
             for variant in model0.BASELINES}
    predictions = {}
    for name, directory in packages.items():
        model, meta = load_package(directory)
        predictions[name] = predict(model, meta, rows)
    classes = sorted(set(truth).union(*predictions.values()))
    return compare.compare(truth, predictions, flags, classes), classes


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ml.evaluate", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, help="a job-style folder with input/data/validation")
    parser.add_argument("--package", action="append", default=[], metavar="NAME=DIR")
    parser.add_argument("--out", help="write the markdown report here as well")
    args = parser.parse_args(argv)
    packages = {}
    for item in args.package:
        name, sep, directory = item.partition("=")
        if not sep or not name or not directory:
            parser.error(f"--package takes NAME=DIR, got {item!r}")
        packages[name] = Path(directory).expanduser()
    try:
        rows = load_validation(Path(args.root).expanduser())
        missing = [c for c in model0_columns() if c not in rows[0][1]]
        if missing:
            raise EvalError(f"Model 0 reads columns the features lack: {missing}")
        report, classes = build_report(rows, packages)
    except (EvalError, TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    text = compare.render_markdown(report, classes)
    print(text)
    if args.out:
        Path(args.out).expanduser().write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())