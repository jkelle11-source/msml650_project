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
    """One 10-minute run: 2 NORMAL windows, then 5 of `incident` (or NORMAL), then 1."""
    features, labels = [], []
    for i in range(8):
        ws = (start + timedelta(minutes = i)).strftime("%Y-%m-%dT%H:%M:00.000Z")
        faulty = incident != "NORMAL" and 2 <= i < 7
        shift = 4.0 if faulty else 0.0
        row = {"window_start": ws}
        row.update({c: rng.gauss(10 + (shift if j % 2 == 0 or incident == "LAMBDA_DEGRADATION" else 0), 1)
                    for j, c in enumerate(COLUMNS)})
        features.append(row)
        fault_ms = {"f1": 60000 if i > 2 else 20000} if faulty else {}
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

def test_iforest_package(job_root):
    result = train_iforest.run(job_root)
    assert result["kind"] == "detector" and result["task"] == "anomaly_iforest"
    assert result["columns"] == sorted(COLUMNS)
    assert result["threshold"] == result["evaluation"]["thresholds"]["fpr"]["threshold"]
    assert set(result["evaluation"]["thresholds"]) == {"fpr", "offset", "f1"}
    v = result["metrics"]["validation"]
    assert v["roc_auc"] > 0.9 and v["recall"] > 0.8
    assert all(passed for _, passed, _ in launcher_checks(result))
    assert all(isinstance(x, float) for x in v.values())

def test_binary_package(job_root):
    result = train_binary.run(job_root)
    assert result["task"] == "anomaly_binary" and result["classes"] == ["ANOMALY", "NORMAL"]
    assert result["metrics"]["validation"]["f1"] > 0.9
    assert result["evaluation"]["top_features"]
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