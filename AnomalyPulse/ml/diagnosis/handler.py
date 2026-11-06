import os
import re
import json
import math
import time
import tarfile
import sklearn
import scipy
import joblib
import io
import importlib.metadata

import numpy as np

from datetime import datetime, timedelta, timezone
from pathlib import Path
from sklearn.linear_model import LogisticRegression

# Every model's pickle uses these, so every package must list them.
REQUIRED_LIBRARIES = ["scikit-learn", "numpy", "scipy", "joblib"]
KINDS = ("classifier", "detector")
# The M4-5 guard's list (features.guard.forbidden_names()), copied by write_forbidden_names.py:
# the image is built from ml/ alone and can't read the catalog, the schema or the injectors.
FORBIDDEN_PATH = Path(__file__).resolve().parent / "forbidden_names.json"
# Where the live path's detection-latency metrics go (M8-2, plan §16).
METRIC_NAMESPACE = "AnomalyPulse/Diagnosis"
WINDOW = timedelta(minutes = 1)

class BadWindow(ValueError):
    """A window the model can't take."""

class BadModel(RuntimeError):
    """A model this Lambda refuses to load."""

def forbidden_names(path = FORBIDDEN_PATH):
    names = json.loads(Path(path).read_text())["names"]
    if not names:
        # An empty list would let everything pass, so refuse to start.
        raise BadModel(f"{path} lists no forbidden names")
    return [str(n).lower().split("_") for n in names]

def tier2_columns(columns, forbidden):
    """The columns holding a forbidden name as whole words: features.guard.check_columns's rule
    (split on "_", ignoring case, never a substring), so product_db_throttled fails and
    product_throttled_requests passes."""
    def holds(words, name):
        n = len(name)
        return any(words[i:i + n] == name for i in range(len(words) - n + 1))
    return sorted(str(c) for c in columns if any(holds(str(c).lower().split("_"), f) for f in forbidden))

def build_vector(window, columns):
    """The window's values in `columns` order."""
    acceptable = set(columns + ["window_start"])
    passed = set(window.keys())

    wrong = passed - acceptable
    missing = acceptable - passed

    bad_types = {k: type(v).__name__ for k, v in window.items()
                 if type(v) not in (int, float) and k in columns}

    # Only the names that differ: a renamed feature shows up once in each list.
    problems = []
    if missing:
        problems.append("missing: " + ", ".join(sorted(missing)))
    if wrong:
        problems.append("not in the model: " + ", ".join(sorted(wrong)))
    if problems:
        raise BadWindow("features " + "; ".join(problems))

    if bad_types:
        raise BadWindow("features must be int or float: "
                        + ", ".join(f"{k} is {t}"
                                    for k, t in sorted(bad_types.items())))

    return [window[c] for c in columns]

def fetch_model(s3, uri, version_id, folder):
    """Unpack the model package at `uri` (that S3 version) into `folder`."""
    if not re.compile(r"^s3://[^/]+/.+$").fullmatch(uri):
        raise BadModel(f"uri {uri} does not match required format `s3://bucket/key`")

    bucket, key = uri[len("s3://"):].split("/", 1)
    data = s3.get_object(Bucket = bucket, Key = key, VersionId = version_id)["Body"].read()
    try:
        with tarfile.open(fileobj = io.BytesIO(data), mode = "r:gz") as tar:
            for f in ["model.json", "model.joblib"]:
                tar.extract(f, path = folder, filter = "data")
    except Exception as e:
        raise BadModel(f"error {e} while extracting file")

def installed_version(name):
    """The version of `name` this Lambda installs, or None when it doesn't install it."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None

def load_model(folder):
    """(pipeline, meta) from the folder's model.json and model.joblib."""
    meta = json.loads((Path(folder) / "model.json").read_text())

    if not meta.get("format") == 1:
        raise BadModel(f"format must be 1; got {meta.get("format")}")

    kind = meta.get("kind", "classifier")
    if kind not in KINDS:
        raise BadModel(f"kind must be one of {KINDS}; got {kind}")

    # The live path's Tier-1 guard (M4-5, M8-2): a package whose columns hold a Tier-2 name is
    # never loaded, so no window can carry one into a model.
    leaked = tier2_columns(meta.get("columns", []), forbidden_names())
    if leaked:
        raise BadModel(f"forbidden columns in the model: {', '.join(leaked)}")

    # The pickle loads correctly only under the versions it was saved with, so every library the
    # package lists must be the version installed here.
    recorded = {name: version for name, version in meta.get("libraries", {}).items() if name != "python"}
    for name in sorted(set(REQUIRED_LIBRARIES) | set(recorded)):
        loaded, expected = recorded.get(name), installed_version(name)
        if expected is None:
            raise BadModel(f"the package needs {name}, which this Lambda doesn't install")
        if loaded != expected:
            raise BadModel(f"{name} must be version {expected}; got {name} version {loaded}")

    pipeline = joblib.load(Path(folder) / "model.joblib")

    if kind == "classifier":
        if not hasattr(pipeline, "predict_proba"):
            raise BadModel("a classifier's model must have predict_proba")
        # XGBoost trains on class numbers 0..n-1, which stand for model.json's classes in order.
        p_classes, m_classes = [str(c) for c in pipeline.classes_], meta["classes"]
        if p_classes not in (m_classes, [str(i) for i in range(len(m_classes))]):
            raise BadModel(f"model classes {p_classes} do not match metadata classes {m_classes}")
    else:
        if not hasattr(pipeline, "score_samples"):
            raise BadModel("a detector's model must have score_samples")
        threshold = meta.get("threshold")
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise BadModel(f"a detector's threshold must be a finite number; got {threshold!r}")

    return pipeline, meta

def detect(pipeline, meta, vectors):
    """[(score, anomalous)] from a detector: a window is anomalous when its score_samples score is
    below the package's threshold. For M8-2, which runs the detector before the classifier."""
    return [(float(score), bool(score < meta["threshold"])) for score in pipeline.score_samples(vectors)]

def read_event(event):
    """(windows, announced_at, key) from a direct call, {"windows": [...]}, or from the pipeline's
    WindowReady event as an EventBridge rule delivers it, with the window under `detail`
    (Docs/PIPELINE.md). The event's `time` is when the window builder announced the window."""
    if not isinstance(event, dict):
        raise BadWindow("the event is not an object")
    if event.get("detail-type") == "WindowReady" and isinstance(event.get("detail"), dict):
        return event["detail"].get("windows"), event.get("time"), event["detail"].get("key")
    return event.get("windows"), None, event.get("key")

def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

def iso(when):
    return when.astimezone(timezone.utc).isoformat(timespec = "milliseconds").replace("+00:00", "Z")

def latency(window_start, announced_at, diagnosed_at):
    """When the window's minute ended, was announced and was diagnosed, and the seconds between.
    since_window_end_s is the live path's part of detection latency (plan §16): about 2¾ min
    of waiting for the minute's CloudWatch snapshot (Docs/PIPELINE.md), plus this call. The
    fault's start, the rest of it, is ground truth, so M9 joins it offline, never here."""
    end = parse_time(window_start) + WINDOW
    announced = parse_time(announced_at) if announced_at else None
    return {"window_end": iso(end),
            "announced_at": iso(announced) if announced else None,
            "diagnosed_at": iso(diagnosed_at),
            "since_window_end_s": round((diagnosed_at - end).total_seconds(), 3),
            "since_announced_s": round((diagnosed_at - announced).total_seconds(), 3) if announced else None}

def metric_line(prediction, key, inference_ms):
    """One CloudWatch embedded-metric-format log line per window: Lambda's log stream turns the
    numbers into metrics with no API call and no extra permission, and the other fields stay
    queryable in Logs Insights."""
    lag = prediction["latency"]
    values = {"SinceWindowEndSeconds": (lag["since_window_end_s"], "Seconds"),
              "SinceAnnouncedSeconds": (lag["since_announced_s"], "Seconds"),
              "InferenceMs": (inference_ms, "Milliseconds"),
              "Anomalous": (int(prediction["anomalous"]), "Count"),
              "Incident": (int(prediction["incident_type"] is not None), "Count")}
    values = {name: v for name, v in values.items() if v[0] is not None}
    return {"_aws": {"Timestamp": int(time.time() * 1000),
                     "CloudWatchMetrics": [{"Namespace": METRIC_NAMESPACE,
                                            "Dimensions": [["Function"]],
                                            "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in values.items()]}]},
            "Function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "local"),
            **{n: v for n, (v, _) in values.items()},
            "event": "diagnosis", "key": key,
            **{k: prediction[k] for k in ("window_start", "anomalous", "score", "label",
                                          "incident_type", "confidence")},
            **{k: lag[k] for k in ("announced_at", "diagnosed_at")}}

def model_info(meta, pointer):
    return {"task": meta["task"], "estimator": meta["estimator"],
            "job": (pointer or {}).get("job"), "version_id": (pointer or {}).get("version_id")}

def classify_only(windows):
    """The M5-1 reply: the classifier's prediction for every window. Served while no detector is
    configured, and to direct calls such as M9-4's batches."""
    start = time.perf_counter()
    vectors = [build_vector(window, MODEL["columns"]) for window in windows]
    rows = PIPELINE.predict_proba(vectors)
    inference_ms = (time.perf_counter() - start) * 1000

    predictions = []
    for window, row in zip(windows, rows):
        probabilities = {name: float(p) for name, p in zip(MODEL["classes"], row)}
        predictions.append({"window_start": window["window_start"],
                            "label": max(probabilities, key = probabilities.get),
                            "probabilities": probabilities})
    return {"model": model_info(MODEL, POINTER),
            "predictions": predictions,
            "inference_ms": round(inference_ms, 3)}

def detect_then_classify(windows, announced_at, key, clock = None):
    """M8-2's path: window -> detector -> (if anomalous) classifier -> incident_type, confidence.

    A window's incident_type is the classifier's label when the detector flags the window and the
    classifier names an incident; otherwise None. Requiring both drops most of the minutes the
    detector alone keeps flagging after a fault ends, and both of its after-idle flags
    (Docs/PIPELINE.md, "On the minutes only the live path scores"). confidence is that label's
    probability. Every window is answered, so M8-3 sees the quiet minutes that close an incident.
    """
    start = time.perf_counter()
    vectors = [build_vector(window, MODEL["columns"]) for window in windows]
    built = time.perf_counter()
    detections = detect(DETECTOR, DETECTOR_MODEL, vectors)
    detected = time.perf_counter()
    flagged = [i for i, (_, anomalous) in enumerate(detections) if anomalous]
    rows = dict(zip(flagged, PIPELINE.predict_proba([vectors[i] for i in flagged]))) if flagged else {}
    classified = time.perf_counter()
    diagnosed_at = (clock or (lambda: datetime.now(timezone.utc)))()

    predictions = []
    for i, (window, (score, anomalous)) in enumerate(zip(windows, detections)):
        probabilities = ({name: float(p) for name, p in zip(MODEL["classes"], rows[i])}
                         if i in rows else None)
        label = max(probabilities, key = probabilities.get) if probabilities else "NORMAL"
        incident = anomalous and label != "NORMAL"
        predictions.append({"window_start": window["window_start"],
                            "anomalous": anomalous,
                            "score": score,
                            "label": label,
                            "probabilities": probabilities,
                            "incident_type": label if incident else None,
                            "confidence": probabilities[label] if incident else None,
                            "latency": latency(window["window_start"], announced_at, diagnosed_at)})

    inference_ms = (classified - start) * 1000
    for prediction in predictions:
        print(json.dumps(metric_line(prediction, key, round(inference_ms, 3))))
    return {"model": model_info(MODEL, POINTER),
            "detector": {**model_info(DETECTOR_MODEL, DETECTOR_POINTER),
                         "threshold": DETECTOR_MODEL["threshold"]},
            "key": key,
            "predictions": predictions,
            "inference_ms": round(inference_ms, 3),
            "detect_ms": round((detected - built) * 1000, 3),
            "classify_ms": round((classified - detected) * 1000, 3)}

def handler(event, context):
    """{"windows": [window, ...]}, or a WindowReady event -> each window's diagnosis: the detector,
    then the classifier, when a detector is configured; the classifier alone otherwise."""
    windows, announced_at, key = read_event(event)

    if not windows:
        raise BadWindow("the event has no windows")

    # The live path's Tier-1 guard on the windows themselves. build_vector would refuse any name
    # outside the model's columns anyway; this names a Tier-2 leak as one.
    for window in windows:
        leaked = tier2_columns(window.keys(), FORBIDDEN)
        if leaked:
            raise BadWindow(f"forbidden columns in the window: {', '.join(leaked)}")

    if DETECTOR is None:
        return classify_only(windows)
    return detect_then_classify(windows, announced_at, key)

def model_source(folder_variable = "ANOMALYPULSE_MODEL_DIR", pointer_variable = "ANOMALYPULSE_MODEL_POINTER",
                 into = "/tmp/model"):
    """(folder, pointer) for this cold start: a local folder, or the promoted model fetched into
    `into`. (None, None) when neither variable is set."""
    folder = os.environ.get(folder_variable)
    name = os.environ.get(pointer_variable)
    if folder:
        return (folder, None)
    if not name:
        return (None, None)
    import boto3
    s3, ssm = boto3.client("s3"), boto3.client("ssm")
    pointer = json.loads(ssm.get_parameter(Name = name)["Parameter"]["Value"])
    fetch_model(s3, pointer["uri"], pointer["version_id"], into)
    return (into, pointer)

# Once per cold start: every call after it reuses the loaded models.
FORBIDDEN = forbidden_names()
FOLDER, POINTER = model_source()
if FOLDER is None:
    raise BadModel("set ANOMALYPULSE_MODEL_POINTER or ANOMALYPULSE_MODEL_DIR to the classifier")
PIPELINE, MODEL = load_model(FOLDER)
# handler answers with class probabilities, so the model it serves must be a classifier.
if MODEL.get("kind", "classifier") != "classifier":
    raise BadModel(f"this Lambda serves a classifier; {FOLDER} holds a {MODEL['kind']}")

# The detector (M8-2) is optional: without one, the Lambda answers as M5-1 built it.
DETECTOR_FOLDER, DETECTOR_POINTER = model_source("ANOMALYPULSE_DETECTOR_DIR", "ANOMALYPULSE_DETECTOR_POINTER",
                                                 "/tmp/detector")
DETECTOR, DETECTOR_MODEL = load_model(DETECTOR_FOLDER) if DETECTOR_FOLDER else (None, None)
if DETECTOR_MODEL is not None:
    if DETECTOR_MODEL.get("kind") != "detector":
        raise BadModel(f"the detector must be a detector; {DETECTOR_FOLDER} holds a "
                       f"{DETECTOR_MODEL.get('kind', 'classifier')}")
    # One window feeds both, so both must read the same columns in the same order.
    if DETECTOR_MODEL["columns"] != MODEL["columns"]:
        raise BadModel("the detector's columns differ from the classifier's")