"""Score Model 0 and trained packages on the validation channel (M5-3).

    python -m ml.evaluate --root ~/anomalypulse-job \
        --package xgboost=~/pkgs/xgboost --package random_forest=~/pkgs/rf --out report.md

--root is laid out like a job's /opt/ml (input/data/validation/...). Each --package is a folder
holding model.joblib and model.json. Prints the comparison report (ml/compare.py), Model 0's
recall by class, and each model's time to recognize a cascade.
Run it in the training image's library versions, so the pickles load as they were written.
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "job"))
import train  # noqa: E402
from train import TrainError  # noqa: E402

from ml import compare, model0  # noqa: E402

CASCADE = "CASCADING_FAILURE"
EARLIER_BASELINE = f"earlier_mean_{model0.EARLIER}"
_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class EvalError(Exception):
    """Bad input: the evaluation stops with this message."""


def load_runs(root):
    """[(experiment_id, feature_rows, label_rows)] for every validation run."""
    return train.load_runs(Path(root) / "input" / "data" / "validation")


def load_validation(root):
    """[(experiment_id, row)]: every labeled validation window, features plus "label"."""
    rows = train.training_rows(load_runs(root))
    if not rows:
        raise EvalError("the validation channel has no labeled windows")
    return rows


def model0_columns():
    """The feature columns Model 0 reads. A missing one is skipped silently, so check them."""
    return [f"{svc}_{metric}" for svc in model0.SERVICES
            for metric in ("p95_latency_ms", "p95_latency_ms_change", "error_rate_5xx")]


def lookback_rows(feature_rows, label_rows):
    """A run's lookback windows in time order: its labeled windows plus its startup minute,
    as the feature matrix builds them. Onset, recovery and partial windows are left out."""
    labels = {r["window_start"]: r for r in label_rows}

    def kept(row):
        label = labels.get(row["window_start"]) or {}
        return bool(label.get("included")) or label.get("excluded_reason") == "startup"

    return sorted((r for r in feature_rows if kept(r)), key=lambda r: r["window_start"])


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


def _seconds(timestamp):
    return datetime.strptime(timestamp, _FMT).replace(tzinfo=timezone.utc).timestamp()


def is_cascade_run(label_rows):
    return any(r.get("included") and r.get("label") == CASCADE for r in label_rows)


def cascade_start(label_rows):
    """Epoch seconds when two faults are first active together: the run's first window with
    multi_fault_ms > 0, at window_start + 60 s - multi_fault_ms. None if there is none."""
    for r in sorted(label_rows, key=lambda r: r["window_start"]):
        if r.get("multi_fault_ms", 0) > 0:
            return _seconds(r["window_start"]) + 60 - r["multi_fault_ms"] / 1000
    return None


def time_to_recognize(label_rows, called):
    """Seconds from the cascade's start to the end of the first LABELED window, at or after
    the start, that the model calls CASCADING_FAILURE. None if it never does.
    `called` maps window_start -> the model's class. Excluded windows are never scored."""
    start = cascade_start(label_rows)
    if start is None:
        raise EvalError("a cascade run has no window with multi-fault time")
    for r in sorted(label_rows, key=lambda r: r["window_start"]):
        if not r.get("included"):
            continue
        end = _seconds(r["window_start"]) + 60
        if end > start and called.get(r["window_start"]) == CASCADE:
            return end - start
    return None


def summarize_ttr(times):
    found = [t for t in times if t is not None]
    return {"runs": len(times), "median": statistics.median(found) if found else None,
            "max": max(found) if found else None, "never": len(times) - len(found)}


def build_report(root, packages):
    """(report, classes): Model 0's two baselines and each package, on the same windows."""
    runs = load_runs(root)
    rows = train.training_rows(runs)
    if not rows:
        raise EvalError("the validation channel has no labeled windows")
    lookback = {eid: lookback_rows(features, labels) for eid, features, labels in runs}
    truth = [row[train.LABEL] for _, row in rows]
    flags = {
        "previous_window": model0.predict([row for _, row in rows], "previous_window"),
        EARLIER_BASELINE: [
            model0.flag_vs_earlier(
                row, [r for r in lookback[eid] if r["window_start"] < row["window_start"]])
            for eid, row in rows],
    }
    predictions = {}
    for name, directory in packages.items():
        model, meta = load_package(directory)
        predictions[name] = predict(model, meta, rows)
    classes = sorted(set(truth).union(*predictions.values()))
    report = compare.compare(truth, predictions, flags, classes)

    cascade_runs = [(eid, labels) for eid, _, labels in runs if is_cascade_run(labels)]
    report["time_to_recognize"] = {}
    for name, guesses in predictions.items():
        called = {}
        for (eid, row), guess in zip(rows, guesses):
            called.setdefault(eid, {})[row["window_start"]] = guess
        report["time_to_recognize"][name] = {
            eid: time_to_recognize(labels, called.get(eid, {})) for eid, labels in cascade_runs}
    return report, classes


def render_model0_by_class(report):
    variants = list(report["model0"])
    classes = sorted({c for s in report["model0"].values() for c in s["recall_by_class"]})
    lines = ["", "## Model 0: share of each anomaly class flagged", "",
             "| Class | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)]
    for c in classes:
        lines.append(f"| {c} | " + " | ".join(
            f"{report['model0'][v]['recall_by_class'].get(c, 0.0):.3f}" for v in variants) + " |")
    return "\n".join(lines) + "\n"


def _fmt(value):
    return "n/a" if value is None else f"{value:.1f}"


def render_time_to_recognize(ttr):
    lines = ["", "## Time to recognize a cascade", "",
             "Seconds from the moment two faults are first active together to the end of the first "
             "labeled window the model calls CASCADING_FAILURE. Only labeled windows are scored, so a "
             "DB-throttling onset minute is never scored, even when the cascade starts inside it. "
             "Model 0 cannot name a cascade, so it has no time to recognize.", ""]
    if not ttr or not any(ttr.values()):
        lines.append("No trained model was scored on a cascade run.")
        return "\n".join(lines) + "\n"
    lines += ["| Model | Cascade runs | Median (s) | Max (s) | Never recognized |", "|---|---|---|---|---|"]
    for name, per_run in ttr.items():
        s = summarize_ttr(list(per_run.values()))
        lines.append(f"| {name} | {s['runs']} | {_fmt(s['median'])} | {_fmt(s['max'])} | {s['never']} |")
    names = list(ttr)
    lines += ["", "Per run (seconds; `never` if not recognized):", "",
              "| Run | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for eid in sorted(next(iter(ttr.values()))):
        cells = ["never" if ttr[n][eid] is None else f"{ttr[n][eid]:.1f}" for n in names]
        lines.append(f"| {eid} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


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
        root = Path(args.root).expanduser()
        rows = load_validation(root)
        missing = [c for c in model0_columns() if c not in rows[0][1]]
        if missing:
            raise EvalError(f"Model 0 reads columns the features lack: {missing}")
        report, classes = build_report(root, packages)
    except (EvalError, TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    text = (compare.render_markdown(report, classes) + render_model0_by_class(report)
            + render_time_to_recognize(report["time_to_recognize"]))
    print(text)
    if args.out:
        Path(args.out).expanduser().write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())