"""M8-2's live path in the Diagnosis Lambda, on small real models trained here:
window -> detector -> (if anomalous) classifier -> incident_type, confidence; the Tier-1 guard
on the live path; and the detection-latency fields and metric lines.

    cd AnomalyPulse && python -m pytest -q ml/diagnosis
"""
import importlib.metadata
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.ensemble import IsolationForest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

if sys.version_info < (3, 12):
    pytest.skip("handler.py needs Python 3.12+ (the Lambda runs 3.13)", allow_module_level=True)

# Tier-1 names, two of them the throttle counts the guard must let through.
COLUMNS = sorted(["product_request_rate", "cart_p95_latency_ms", "product_throttled_requests",
                  "order_dependency_error_count"])
CLASSES = ["CASCADING_FAILURE", "DATABASE_THROTTLING", "DEPENDENCY_FAILURE", "LAMBDA_DEGRADATION",
           "NORMAL", "TRAFFIC_SPIKE"]
# Each class's mean per column, in COLUMNS order:
# cart_p95_latency_ms, order_dependency_error_count, product_request_rate, product_throttled_requests
CENTRES = {"NORMAL": [100, 0, 10, 0],
           "TRAFFIC_SPIKE": [130, 0, 60, 0],
           "DATABASE_THROTTLING": [400, 0, 10, 30],
           "LAMBDA_DEGRADATION": [900, 0, 10, 0],
           "DEPENDENCY_FAILURE": [110, 40, 10, 0],
           "CASCADING_FAILURE": [400, 40, 10, 30]}
SPREAD = [8, 1, 1, 1]
LIBRARIES = {name: importlib.metadata.version(name) for name in ("scikit-learn", "numpy", "scipy", "joblib")}


def sample(label, rng):
    return [rng.gauss(c, s) for c, s in zip(CENTRES[label], SPREAD)]


def window(values, minute=11):
    return {"window_start": f"2026-11-06T14:{minute:02d}:00.000Z", **dict(zip(COLUMNS, values))}


def write_package(folder, model, meta):
    folder.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, folder / "model.joblib")
    (folder / "model.json").write_text(json.dumps({"format": 1, "columns": COLUMNS, "libraries": LIBRARIES,
                                                   **meta}))
    return folder


@pytest.fixture(scope="module")
def packages(tmp_path_factory):
    rng = random.Random(7)
    X, y = [], []
    for label in CLASSES:
        for _ in range(60):
            X.append(sample(label, rng))
            y.append(label)
    classifier = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)).fit(X, y)
    forest = IsolationForest(n_estimators=200, random_state=0).fit([sample("NORMAL", rng) for _ in range(1000)])
    # Set by hand between the classes' centres: a healthy centre scores about -0.38, and a fault
    # in one of only 4 columns no lower than about -0.51 (a forest picks that column rarely).
    threshold = -0.45
    root = tmp_path_factory.mktemp("packages")
    return {
        "classifier": write_package(root / "classifier", classifier, {
            "task": "incident_type", "estimator": "logistic_regression", "classes": CLASSES}),
        "detector": write_package(root / "detector", forest, {
            "kind": "detector", "task": "anomaly_iforest", "estimator": "isolation_forest",
            "threshold": threshold}),
        "root": root, "classifier_model": classifier, "forest": forest, "threshold": threshold,
    }


def load(monkeypatch, classifier, detector=None):
    """A fresh cold start of handler.py with these packages."""
    monkeypatch.setenv("ANOMALYPULSE_MODEL_DIR", str(classifier))
    if detector:
        monkeypatch.setenv("ANOMALYPULSE_DETECTOR_DIR", str(detector))
    else:
        monkeypatch.delenv("ANOMALYPULSE_DETECTOR_DIR", raising=False)
    monkeypatch.delenv("ANOMALYPULSE_DETECTOR_POINTER", raising=False)
    sys.modules.pop("handler", None)
    import handler
    return handler


@pytest.fixture
def live(monkeypatch, packages):
    return load(monkeypatch, packages["classifier"], packages["detector"])


# ---- The path -------------------------------------------------------------------------------

def test_without_a_detector_the_reply_is_m5_1s(monkeypatch, packages):
    handler = load(monkeypatch, packages["classifier"])
    reply = handler.handler({"windows": [window(CENTRES["TRAFFIC_SPIKE"])]}, None)
    assert set(reply) == {"model", "predictions", "inference_ms"}
    assert set(reply["predictions"][0]) == {"window_start", "label", "probabilities"}
    assert reply["predictions"][0]["label"] == "TRAFFIC_SPIKE"


def test_a_traffic_spike_is_detected_then_classified(live, packages):
    reply = live.handler({"windows": [window(CENTRES["TRAFFIC_SPIKE"])]}, None)
    [p] = reply["predictions"]
    assert p["anomalous"] is True and p["score"] < packages["threshold"]
    assert p["incident_type"] == "TRAFFIC_SPIKE"
    assert p["confidence"] == p["probabilities"]["TRAFFIC_SPIKE"] == max(p["probabilities"].values())
    assert reply["detector"]["task"] == "anomaly_iforest"
    assert reply["detector"]["threshold"] == packages["threshold"]
    assert reply["model"]["task"] == "incident_type"


@pytest.mark.parametrize("label", [c for c in CLASSES if c != "NORMAL"])
def test_every_incident_type_comes_through(live, label):
    [p] = live.handler({"windows": [window(CENTRES[label])]}, None)["predictions"]
    assert (p["anomalous"], p["incident_type"]) == (True, label)


def test_a_healthy_window_never_reaches_the_classifier(live, monkeypatch):
    seen = []
    real = live.PIPELINE.predict_proba
    monkeypatch.setattr(live.PIPELINE, "predict_proba", lambda rows: seen.append(len(rows)) or real(rows))
    reply = live.handler({"windows": [window(CENTRES["NORMAL"], 10), window(CENTRES["LAMBDA_DEGRADATION"], 11),
                                      window(CENTRES["NORMAL"], 12)]}, None)
    assert seen == [1]                       # one call, for the one flagged window
    healthy = reply["predictions"][0]
    assert healthy == {**healthy, "anomalous": False, "label": "NORMAL", "probabilities": None,
                       "incident_type": None, "confidence": None}
    assert [p["window_start"] for p in reply["predictions"]] == [
        "2026-11-06T14:10:00.000Z", "2026-11-06T14:11:00.000Z", "2026-11-06T14:12:00.000Z"]


def test_flagged_but_classified_normal_is_no_incident(live, monkeypatch):
    # The after-idle minute and the recovery tail: the detector flags, the classifier says NORMAL.
    monkeypatch.setattr(live, "detect", lambda pipeline, meta, vectors: [(-1.0, True)] * len(vectors))
    [p] = live.handler({"windows": [window(CENTRES["NORMAL"])]}, None)["predictions"]
    assert p["anomalous"] is True and p["label"] == "NORMAL"
    assert p["incident_type"] is None and p["confidence"] is None and p["probabilities"] is not None


# ---- The pipeline's event, and latency ------------------------------------------------------

def window_ready(windows, at="2026-11-06T14:15:46Z"):
    """A WindowReady event as the rule delivers it (Docs/PIPELINE.md)."""
    return {"version": "0", "id": "x", "source": "anomalypulse.pipeline", "detail-type": "WindowReady",
            "account": "1", "time": at, "region": "us-east-2", "resources": [],
            "detail": {"windows": windows, "key": "pipeline/windows/dt=2026-11-06/minute=14-11.json"}}


def test_a_window_ready_event_is_read_from_its_detail(live):
    reply = live.handler(window_ready([window(CENTRES["TRAFFIC_SPIKE"])]), None)
    assert reply["key"] == "pipeline/windows/dt=2026-11-06/minute=14-11.json"
    assert reply["predictions"][0]["incident_type"] == "TRAFFIC_SPIKE"


def test_latency_is_timed_from_the_windows_end_and_its_announcement(live):
    now = datetime(2026, 11, 6, 14, 15, 47, 250000, tzinfo=timezone.utc)
    windows, announced, key = live.read_event(window_ready([window(CENTRES["TRAFFIC_SPIKE"])]))
    reply = live.detect_then_classify(windows, announced, key, clock=lambda: now)
    assert reply["predictions"][0]["latency"] == {
        "window_end": "2026-11-06T14:12:00.000Z", "announced_at": "2026-11-06T14:15:46.000Z",
        "diagnosed_at": "2026-11-06T14:15:47.250Z",
        "since_window_end_s": 227.25, "since_announced_s": 1.25}
    assert reply["detect_ms"] >= 0 and reply["classify_ms"] >= 0 and reply["inference_ms"] >= 0


def test_a_direct_call_has_no_announcement(live):
    [p] = live.handler({"windows": [window(CENTRES["NORMAL"])]}, None)["predictions"]
    assert p["latency"]["announced_at"] is None and p["latency"]["since_announced_s"] is None


def test_each_window_writes_one_metric_line(live, capsys):
    live.handler(window_ready([window(CENTRES["TRAFFIC_SPIKE"], 11), window(CENTRES["NORMAL"], 12)]), None)
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert len(lines) == 2
    spike = lines[0]
    [directive] = spike["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "AnomalyPulse/Diagnosis" and directive["Dimensions"] == [["Function"]]
    names = {m["Name"] for m in directive["Metrics"]}
    assert names == {"SinceWindowEndSeconds", "SinceAnnouncedSeconds", "InferenceMs", "Anomalous", "Incident"}
    assert all(isinstance(spike[n], (int, float)) for n in names)     # EMF: every metric has its value
    assert (spike["incident_type"], spike["Incident"], lines[1]["Incident"]) == ("TRAFFIC_SPIKE", 1, 0)


# ---- The Tier-1 guard on the live path ------------------------------------------------------

@pytest.mark.parametrize("leak", ["product_db_throttled", "order_error_type_count", "label", "experiment_id"])
def test_a_window_carrying_a_tier2_name_is_refused(live, leak):
    with pytest.raises(live.BadWindow, match=f"forbidden columns in the window: {leak}"):
        live.handler({"windows": [{**window(CENTRES["NORMAL"]), leak: 1.0}]}, None)


def test_the_throttle_counts_are_tier1(live):
    assert live.tier2_columns(["product_throttled_requests", "cart_read_throttle_events"], live.FORBIDDEN) == []


def test_a_model_with_a_tier2_column_is_never_loaded(live, packages, tmp_path):
    folder = tmp_path / "leaky"
    folder.mkdir()
    (folder / "model.json").write_text(json.dumps({
        "format": 1, "task": "incident_type", "estimator": "x", "classes": CLASSES, "libraries": LIBRARIES,
        "columns": COLUMNS + ["cart_db_throttled"]}))
    with pytest.raises(live.BadModel, match="forbidden columns in the model: cart_db_throttled"):
        live.load_model(folder)


def test_the_lambdas_list_is_the_guards():
    pytest.importorskip("boto3")             # features.guard imports the simulator, which needs it
    sys.path.insert(0, str(ROOT))
    from features import guard
    names = json.loads((HERE / "forbidden_names.json").read_text())["names"]
    assert names == sorted(guard.forbidden_names()), "run: python ml/diagnosis/write_forbidden_names.py"


def test_every_real_feature_column_passes_the_lambdas_guard(live):
    pytest.importorskip("boto3")
    sys.path.insert(0, str(ROOT))
    from features.build_matrix import columns
    assert len(columns()) == 106 and live.tier2_columns(columns(), live.FORBIDDEN) == []


# ---- Startup refusals -----------------------------------------------------------------------

def test_a_classifier_where_the_detector_goes_is_refused(monkeypatch, packages):
    with pytest.raises(Exception, match="the detector must be a detector"):
        load(monkeypatch, packages["classifier"], packages["classifier"])


def test_a_detector_where_the_classifier_goes_is_refused(monkeypatch, packages):
    with pytest.raises(Exception, match="this Lambda serves a classifier"):
        load(monkeypatch, packages["detector"], packages["detector"])


def test_a_detector_reading_other_columns_is_refused(monkeypatch, packages):
    folder = packages["root"] / "other-columns"
    forest = IsolationForest(random_state=0).fit(np.random.default_rng(0).normal(size=(50, 3)))
    write_package(folder, forest, {"kind": "detector", "task": "anomaly_iforest", "estimator": "isolation_forest",
                                   "threshold": -0.5})
    meta = json.loads((folder / "model.json").read_text())
    (folder / "model.json").write_text(json.dumps({**meta, "columns": COLUMNS[:3]}))
    with pytest.raises(Exception, match="the detector's columns differ from the classifier's"):
        load(monkeypatch, packages["classifier"], folder)