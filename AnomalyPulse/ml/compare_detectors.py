"""The cost of going unsupervised (M5-2): the two detectors' validation metrics side by side.

    cd AnomalyPulse
    python -m ml.compare_detectors <iforest model.json or model.tar.gz> <binary model.json or model.tar.gz>

Get a job's package with:  aws s3 cp <S3ModelArtifacts> . --profile <your-profile>
(the launcher prints the address; or `aws sagemaker describe-training-job`).
Prints a markdown table for the PR: each metric for both, and the gap (supervised minus
unsupervised). Also confirms both packages' columns pass the M4-5 Tier-1 guard.
"""
import json
import sys
import tarfile
from pathlib import Path

# fpr counts every flagged NORMAL window; fpr_healthy and fault_start_flagged split it (detection.py).
METRICS = ("precision", "recall", "f1", "fpr", "fpr_healthy", "fault_start_flagged", "roc_auc",
           "average_precision", "recall_at_healthy_fpr_1", "latency_median_s")
RULE_COLUMNS = ("threshold", "precision", "recall", "f1", "fpr", "fpr_healthy", "fault_start_flagged")

def read_package(path):
    path = Path(path)
    if path.suffix == ".json":
        return json.loads(path.read_text())
    with tarfile.open(path, "r:gz") as tar:
        return json.load(tar.extractfile("model.json"))

def table(unsupervised, supervised):
    u, s = unsupervised["metrics"]["validation"], supervised["metrics"]["validation"]
    lines = ["| validation metric | Isolation Forest | supervised | gap (supervised − IF) |",
             "|---|---|---|---|"]
    for metric in METRICS:
        if metric in u and metric in s:
            lines.append(f"| {metric} | {u[metric]:.4f} | {s[metric]:.4f} | {s[metric] - u[metric]:+.4f} |")
    lu, ls = (m["evaluation"]["validation"]["latency"] for m in (unsupervised, supervised))
    lines += ["", "| latency (incident runs) | Isolation Forest | supervised |", "|---|---|---|"]
    for key in ("incident_runs", "detected_runs", "missed_runs", "median_s", "p90_s", "max_s",
                "first_window_share"):
        if key in lu or key in ls:
            fmt = lambda v: "–" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
            lines.append(f"| {key} | {fmt(lu.get(key))} | {fmt(ls.get(key))} |")
    ru, rs = (m["evaluation"]["validation"]["recall_by_label"] for m in (unsupervised, supervised))
    lines += ["", "| share flagged, by label (NORMAL = FPR) | Isolation Forest | supervised |", "|---|---|---|"]
    for label in sorted(set(ru) | set(rs)):
        lines.append(f"| {label} | {ru.get(label, 0):.3f} | {rs.get(label, 0):.3f} |")
    thresholds = unsupervised["evaluation"].get("thresholds", {})
    if thresholds:
        lines += ["", f"Isolation Forest cut-off rules on validation (chosen: "
                  f"{unsupervised['evaluation']['threshold_rule']}):", "",
                  "| rule | " + " | ".join(RULE_COLUMNS) + " |", "|---" * (len(RULE_COLUMNS) + 1) + "|"]
        for rule, e in thresholds.items():
            lines.append(f"| {rule} | " + " | ".join(f"{e[c]:.4f}" for c in RULE_COLUMNS) + " |")
    return "\n".join(lines)

def guard(packages):
    """The M4-5 boundary guard on each package's columns: raises BoundaryViolation on a leak."""
    from features import guard as g
    for package in packages:
        g.check_columns(package["columns"])
    return "M4-5 guard: PASS for both packages' columns (" + \
        ", ".join(f"{p['task']} {len(p['columns'])}" for p in packages) + ")"

def main(argv = None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file = sys.stderr)
        return 2
    unsupervised, supervised = (read_package(p) for p in argv)
    if unsupervised.get("kind") != "detector" or supervised.get("kind", "classifier") != "classifier":
        print("give the Isolation Forest's package first, then the supervised one", file = sys.stderr)
        return 2
    print(table(unsupervised, supervised))
    print()
    print(guard([unsupervised, supervised]))
    return 0

if __name__ == "__main__":
    sys.exit(main())