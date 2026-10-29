"""Smoke test for a SageMaker training job (M5-1).

Runs inside a training job and reports what the image gives us: Python and
library versions, the SM_/SAGEMAKER_ environment, the hyperparameters and
command line the job started with, the files each channel copied in, the code
the toolkit unpacked, and whether the network is reachable. It only reports;
the launcher judges the report, so one surprise never hides the other checks.

Writes <root>/model/smoke.json (SageMaker uploads it in model.tar.gz) and
prints the same JSON, which lands in the job's CloudWatch log. The root is
/opt/ml in a job; set ANOMALYPULSE_SMOKE_ROOT to run it locally.

Standard library only, and Python 3.9: the images' Python is older than ours.
"""
import importlib.metadata
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ANOMALYPULSE_SMOKE_ROOT", "/opt/ml"))

# Install names, not import names (scikit-learn is imported as sklearn).
LIBRARIES = ("scikit-learn", "xgboost", "numpy", "pandas", "scipy", "joblib")

NETWORK_HOST = "s3.us-east-2.amazonaws.com"
NETWORK_TIMEOUT_S = 5


def library_versions():
    """{install name: version}, None for a library the image doesn't have."""
    versions = {}
    for name in LIBRARIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions

def sagemaker_env():
    """The SM_ and SAGEMAKER_ variables only."""
    return {name: value for name, value in os.environ.items()
            if name.startswith(("SM_", "SAGEMAKER_"))}
    
def hyperparameters():
    """hyperparameters.json as SageMaker wrote it, values unconverted."""
    return json.loads((ROOT / "input" / "config" / "hyperparameters.json").read_text())

def describe(path, folder):
    """{path relative to folder, bytes, lines}; lines only for .jsonl."""
    lines = None
    if path.suffix == ".jsonl":
        with open(path, "rb") as f:
            lines = sum(1 for _ in f)
    return {"path": path.relative_to(folder).as_posix(),
            "bytes": path.stat().st_size, "lines": lines}
    
def channels():
    """{channel: its files, sorted by path} for every folder under input/data."""
    data = ROOT / "input" / "data"
    return {folder.name: sorted((describe(p, folder) for p in folder.rglob("*") if p.is_file()),
                                key = lambda f: f["path"])
            for folder in data.iterdir() if folder.is_dir()}
    
def code_files():
    """What the toolkit unpacked from sourcedir.tar.gz into code/."""
    code = ROOT / "code"
    return sorted(p.relative_to(code).as_posix() for p in code.rglob("*") if p.is_file())

def network():
    """'open', or 'blocked: <error>'. OSError covers a failed DNS lookup, a refused connection,
    and a timeout."""
    try: 
        connection = socket.create_connection((NETWORK_HOST, 443), timeout = NETWORK_TIMEOUT_S)
    except OSError as e:
        return f"blocked: {type(e).__name__}"
    connection.close()
    return "open"


CHECKS = (
    ("python", lambda: sys.version),
    ("libraries", library_versions),
    ("env", sagemaker_env),
    ("hyperparameters", hyperparameters),
    ("argv", lambda: sys.argv[1:]),
    ("channels", channels),
    ("code", code_files),
    ("network", network)
)


def main():
    # A check that fails is recorded under errors and its key set to None,
    # never raised: the report is written whatever the image turns out to be.
    report, errors = {}, {}
    for key, check in CHECKS:
        try:
            report[key] = check()
        except Exception as e:
            report[key] = None
            errors[key] = f"{type(e).__name__}: {e}"
    report["errors"] = errors
    
    text = json.dumps(report, indent = 2, sort_keys = True)
    print(text, flush = True)
    model = ROOT / "model"
    model.mkdir(parents = True, exist_ok = True)
    (model / "smoke.json").write_text(text + "\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
    

