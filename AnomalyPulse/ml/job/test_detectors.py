"""M5-2's detectors end to end on a synthetic job root laid out like a SageMaker job's /opt/ml:
both train, write a package the launcher's checks and the Lambda's loader accept, and score."""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

JOB = Path(__file__).resolve().parent
ROOT = JOB.parent.parent
sys.path[:0] = [str(JOB), str(ROOT), str(ROOT / "features")]

import detection
import train
import train_binary
import train_iforest

COLUMNS = ["cart_p95_latency_ms", "cart_error_rate", "order_request_rate", "payment_dependency_latency_ms"]

def write_run(channel, experiment_id, start, incident, rng):
    """One 10-minute run: a healthy NORMAL window; a fault-start one (NORMAL, the fault active its
    last 20 s, so half-shifted); 5 of `incident`; a healthy one. A NORMAL run is 8 healthy windows."""
    features, labels = [], []
    for i in range(8):
        ws = (start + timedelta(minutes = i)).strftime("%Y-%m-%dT%H:%M:00.000Z")
        faulty = incident != "NORMAL" and 2 <= i < 7
        starting = incident != "NORMAL" and i == 1
        shift = 4.0 if faulty else 2.0 if starting else 0.0
        row = {"window_start": ws}
        row.update({c: rng.gauss(10 + (shift if j % 2 == 0 or incident == "LAMBDA_DEGRADATION" else 0), 1)
                    for j, c in enumerate(COLUMNS)})
        features.append(row)
        fault_ms = {"f1": 60000} if faulty else {"f1": 20000} if starting else {}
        labels.append({"window_start": ws, "label": incident if faulty else "NORMAL", "included": True,
                       "excluded_reason": None, "fault_ms": fault_ms})
    for tree, name, data in (("tier1/features", "features.jsonl", features),
                             ("tier2/labels", "labels.jsonl", labels)):
        folder = channel / tree / f"experiment_id={experiment_id}"
        folder.mkdir(parents = True)
        (folder / name).write_text("".join(json.dumps(r) + "\n" for r in data))

@pytest.fixture
def job_root(tmp_path):
    rng = random.Random(1)
    start = datetime(2026, 10, 24, 11, 0, tzinfo = timezone.utc)
    kinds = ["NORMAL", "TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION"]
    for channel, count in (("train", 24), ("validation", 8)):
        for n in range(count):
            write_run(tmp_path / "input" / "data" / channel, f"{channel}-{n:03d}",
                      start + timedelta(minutes = 15 * n), kinds[n % 4], rng)
    (tmp_path / "input" / "config").mkdir(parents = True)
    (tmp_path / "input" / "config" / "hyperparameters.json").write_text("{}")
    return tmp_path

def settings(root, **values):
    (root / "input" / "config" / "hyperparameters.json").write_text(
        json.dumps({k: json.dumps(v) for k, v in values.items()}))

def launcher_checks(model):
    pytest.importorskip("botocore")      # ml.launch imports it; installed wherever the launcher runs
    from ml import launch
    ids = {"train": ["x"] * 24, "validation": ["x"] * 8}
    pins = {name: version for name, version in model["libraries"].items() if name != "python"}
    return launch.check_model(model, ids, pins)

def healthy_scores(root):
    """score_samples of validation's healthy windows (NORMAL, no fault time), found here from the
    files rather than through detection.healthy."""
    import joblib
    import numpy as np
    model, meta = joblib.load(root / "model/model.joblib"), json.loads((root / "model/model.json").read_text())
    channel, vectors = root / "input/data/validation", []
    for folder in sorted((channel / "tier2/labels").iterdir()):
        features = {r["window_start"]: r for r in map(json.loads, (channel / "tier1/features" / folder.name
                                                                    / "features.jsonl").read_text().splitlines())}
        for label in map(json.loads, (folder / "labels.jsonl").read_text().splitlines()):
            if label["label"] == "NORMAL" and not label["fault_ms"]:
                vectors.append([features[label["window_start"]][c] for c in meta["columns"]])
    return model.score_samples(np.asarray(vectors, float))

def test_iforest_package(job_root):
    import numpy as np
    result = train_iforest.run(job_root)
    assert result["kind"] == "detector" and result["task"] == "anomaly_iforest"
    assert result["columns"] == sorted(COLUMNS)
    assert result["threshold"] == result["evaluation"]["thresholds"]["fpr"]["threshold"]
    assert set(result["evaluation"]["thresholds"]) == {"fpr", "offset", "f1"}
    # the default cut-off flags 1% of the healthy windows, not of every NORMAL window
    assert result["threshold"] == pytest.approx(np.quantile(healthy_scores(job_root), 0.01), rel = 1e-12)
    assert result["evaluation"]["validation"]["normal_windows"] == {"healthy": 28, "fault_start": 6}
    v = result["metrics"]["validation"]
    assert v["roc_auc"] > 0.9 and v["recall"] > 0.8
    assert v["fpr_healthy"] == pytest.approx(1 / 28)
    assert all(passed for _, passed, _ in launcher_checks(result))
    assert all(isinstance(x, float) for x in v.values())

def test_binary_package(job_root):
    result = train_binary.run(job_root)
    assert result["task"] == "anomaly_binary" and result["classes"] == ["ANOMALY", "NORMAL"]
    assert result["metrics"]["validation"]["f1"] > 0.9
    # Both columns rise in every incident, so both push a window toward ANOMALY.
    weights = {entry["feature"]: entry["weight"] for entry in result["evaluation"]["top_features"]}
    assert weights["cart_p95_latency_ms"] > 0 and weights["order_request_rate"] > 0
    assert all(passed for _, passed, _ in launcher_checks(result))

def test_threshold_rules(job_root):
    for rule in train_iforest.THRESHOLD_RULES:
        settings(job_root, threshold_rule = rule, fit_on = "all")
        result = train_iforest.run(job_root)
        assert result["threshold"] == result["evaluation"]["thresholds"][rule]["threshold"]

def test_bad_settings(job_root):
    settings(job_root, fit_on = "labels")
    with pytest.raises(train.TrainError, match = "fit_on"):
        train_iforest.run(job_root)
    settings(job_root, estimator = "svm")
    with pytest.raises(train.TrainError, match = "estimator"):
        train_binary.run(job_root)

# The job scripts run on the training image's Python 3.10; handler.py on the Lambda's 3.13.
@pytest.mark.skipif(sys.version_info < (3, 12), reason = "handler.py needs Python 3.12+ (the Lambda runs 3.13)")
def test_lambda_loads_both(job_root, monkeypatch, tmp_path_factory):
    """The Diagnosis Lambda's loader takes both; detect() agrees with the package's threshold."""
    import shutil
    folders = {}
    for name, module in (("iforest", train_iforest), ("binary", train_binary)):
        module.run(job_root)
        folders[name] = tmp_path_factory.mktemp(name)
        shutil.copytree(job_root / "model", folders[name], dirs_exist_ok = True)
    monkeypatch.setenv("ANOMALYPULSE_MODEL_DIR", str(folders["binary"]))
    sys.path.insert(0, str(JOB.parent / "diagnosis"))
    sys.modules.pop("handler", None)
    import handler
    window = json.loads((job_root / "input/data/validation/tier1/features/experiment_id=validation-001"
                         / "features.jsonl").read_text().splitlines()[3])
    reply = handler.handler({"windows": [window]}, None)
    assert reply["model"]["task"] == "anomaly_binary" and reply["predictions"][0]["label"] == "ANOMALY"
    pipeline, meta = handler.load_model(folders["iforest"])
    [(score, anomalous)] = handler.detect(pipeline, meta, [handler.build_vector(window, meta["columns"])])
    assert anomalous == (score < meta["threshold"])

def test_latency_from_onset():
    """Fault active 20 s of window 2, caught in window 3: onset 11:02:40, detected at 11:04:00."""
    start = datetime(2026, 10, 24, 11, 0, tzinfo = timezone.utc)
    ws = [(start + timedelta(minutes = i)).strftime("%Y-%m-%dT%H:%M:00.000Z") for i in range(5)]
    labels = [{"window_start": w, "fault_ms": ({"f": 20000} if i == 2 else {"f": 60000} if i > 2 else {})}
              for i, w in enumerate(ws)]
    rows = [("r", {"window_start": w, "label": "NORMAL" if i < 2 else "TRAFFIC_SPIKE"}) for i, w in enumerate(ws)]
    summary, per_run = detection.latency([("r", [], labels)], rows, [False, False, False, True, True])
    assert per_run[0]["latency_s"] == 80.0 and per_run[0]["windows_late"] == 1
    assert summary == {"incident_runs": 1, "detected_runs": 1, "missed_runs": 0, "median_s": 80.0,
                       "mean_s": 80.0, "p90_s": 80.0, "max_s": 80.0, "first_window_share": 0.0}
    summary, _ = detection.latency([("r", [], labels)], rows, [True, False, False, False, False])
    assert summary["missed_runs"] == 1

def test_rates_by_hand():
    """tp 3, fp 1, fn 1, tn 5: precision 3/4, recall 3/4, F1 3/4, FPR 1/6."""
    truth = [True] * 4 + [False] * 6
    flagged = [True, True, True, False, True] + [False] * 5
    counts = detection.confusion(truth, flagged)
    assert counts == {"tp": 3, "fp": 1, "fn": 1, "tn": 5}
    assert detection.rates(counts) == pytest.approx({"precision": 0.75, "recall": 0.75, "f1": 0.75, "fpr": 1 / 6})

def test_healthy_and_false_alarms_by_hand():
    """Run r: healthy, fault-start, incident, healthy, fault-start. Run s: a healthy window at the
    same minute as r's fault-start one, so healthy() must tell runs apart."""
    ws = [f"2026-10-24T11:0{i}:00.000Z" for i in range(5)]
    faults = [{}, {"f": 20000}, {"f": 60000}, {}, {"f": 5000}]
    labels = ["NORMAL", "NORMAL", "TRAFFIC_SPIKE", "NORMAL", "NORMAL"]
    runs = [("r", [], [{"window_start": w, "fault_ms": f} for w, f in zip(ws, faults)]),
            ("s", [], [{"window_start": ws[1], "fault_ms": {}}])]
    rows = [("r", {"window_start": w, "label": l}) for w, l in zip(ws, labels)] + \
           [("s", {"window_start": ws[1], "label": "NORMAL"})]
    calm = detection.healthy(runs, rows)
    assert calm == [True, False, False, True, False, True]
    truth = detection.is_anomaly([row["label"] for _, row in rows])
    flagged = [True, True, True, False, False, True]
    # healthy flagged: 2 of 3 (r 11:00, s 11:01); fault-start flagged: 1 of 2 (r 11:01)
    assert detection.false_alarms(truth, calm, flagged) == pytest.approx(
        {"fpr_healthy": 2 / 3, "fault_start_flagged": 1 / 2})
    assert detection.recall_by_label(rows, flagged) == pytest.approx({"NORMAL": 3 / 5, "TRAFFIC_SPIKE": 1.0})

def test_recall_at_fpr_by_hand():
    """100 healthy windows scored 0..99, and a fault-start window scored 1000 that must not move
    the cut. 1% of the healthy windows flagged means a cut at 98.01 (5% would be 94.05), between
    the anomalies' 96.0 and 98.5, so one of the two is caught."""
    truth = [True, True, False] + [False] * 100
    calm = [False, False, False] + [True] * 100
    assert detection.recall_at_fpr(truth, calm, [98.5, 96.0, 1000.0] + list(range(100))) == 0.5