import os
import re
import json
import time
import tarfile
import sklearn
import scipy
import joblib
import io
import importlib.metadata

import numpy as np

from pathlib import Path
from sklearn.linear_model import LogisticRegression

LIBRARIES = ["scikit-learn", "numpy", "scipy", "joblib"]

class BadWindow(ValueError):
    """A window the model can't take."""

class BadModel(RuntimeError):
    """A model this Lambda refuses to load."""

def build_vector(window, columns):
    """The window's values in `columns` order."""
    acceptable = set(columns + ["window_start"])
    passed = set(window.keys())
    
    wrong = passed - acceptable
    missing = acceptable - passed
    
    bad_types = {k: type(v).__name__ for k, v in window.items() 
                 if type(v) not in (int, float) and k in columns}
    
    if wrong:
        raise BadWindow(f"features {wrong} not in model; model accepts {acceptable}")
    
    if missing:
        raise BadWindow(f"features {missing} not passed; model requires {acceptable}")
    
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

def load_model(folder):
    """(pipeline, meta) from the folder's model.json and model.joblib."""
    meta = json.loads((Path(folder) / "model.json").read_text())
    
    if not meta.get("format") == 1:
        raise BadModel(f"format must be 1; got {meta.get("format")}")
    
    for name in LIBRARIES:
        loaded, expected = meta.get("libraries", {}).get(name), importlib.metadata.version(name)
        if loaded != expected:
            raise BadModel(f"{name} must be version {expected}; got {name} version {loaded}")
        
    pipeline = joblib.load(Path(folder) / "model.joblib")
    p_classes, m_classes = [str(c) for c in pipeline.classes_], meta["classes"]
    
    if p_classes != m_classes:
        raise BadModel(f"model classes {p_classes} do not match metadata classes {m_classes}")
    
    return pipeline, meta

def handler(event, context):
    """{"windows": [window, ...]} -> the model's prediction for each."""
    windows = event.get("windows") if isinstance(event, dict) else None
    
    if not windows:
        raise BadWindow("the event has no windows")
    
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
    return {"model": {"task": MODEL["task"], 
                      "estimator": MODEL["estimator"],
                      "job": (POINTER or {}).get("job"),
                      "version_id": (POINTER or {}).get("version_id")},
            "predictions": predictions,
            "inference_ms": round(inference_ms, 3)}
    
def model_source():
    """(folder, pointer) for this cold start: a local folder, or the promoted model fetched into /tmp."""
    folder = os.environ.get("ANOMALYPULSE_MODEL_DIR")
    name = os.environ.get("ANOMALYPULSE_MODEL_POINTER")
    if folder:
        return (folder, None)
    else:
        import boto3
        s3, ssm = boto3.client("s3"), boto3.client("ssm")
        pointer = json.loads(ssm.get_parameter(Name = name)["Parameter"]["Value"])
        fetch_model(s3, pointer["uri"], pointer["version_id"], "/tmp/model")
        return ("/tmp/model", pointer)

# Once per cold start: every call after it reuses the loaded model.
FOLDER, POINTER = model_source()
PIPELINE, MODEL = load_model(FOLDER)
    