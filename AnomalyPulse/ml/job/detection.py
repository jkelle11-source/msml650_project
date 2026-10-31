"""Detection metrics shared by the two M5-2 detectors (train_iforest.py, train_binary.py).

Both answer NORMAL vs ANOMALY for one window. Every incident label (anything but NORMAL)
counts as ANOMALY. A detector's anomaly score is higher for a more anomalous window:
Isolation Forest's is -score_samples, the supervised detector's is P(ANOMALY).

Metrics are flat numbers per channel, so model.json's `metrics` stays small enough for the
promotion pointer (an SSM String). Anything longer (latency per run, recall per incident
type, the threshold table) goes in model.json's `evaluation`.

Detection latency (§16) is measured per incident run, from the fault's onset to the END of
the first window flagged ANOMALY: a window can only be scored once its minute has closed, so
it can never be under the time left in the onset's minute. The onset comes from the label
rows' evidence (`fault_ms`, M4-4): the first window with any counted fault active, where the
fault has been active for its last fault_ms. DB throttling's onset is its `label_from`, the
moment throttling is first observed (LABELING.md). Inference itself takes milliseconds
(ML_INFERENCE.md), so latency is window-dominated by construction.
"""
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

NORMAL, ANOMALY = "NORMAL", "ANOMALY"
WINDOW_S = 60
# The operating point both detectors are compared at, threshold-free: recall with 5% of
# NORMAL windows flagged.
COMPARE_FPR = 0.05

def is_anomaly(labels):
    """[bool] from six-class labels: anything but NORMAL is an anomaly."""
    return [label != NORMAL for label in labels]

def binary_labels(labels):
    """[NORMAL or ANOMALY] from six-class labels."""
    return [ANOMALY if anomalous else NORMAL for anomalous in is_anomaly(labels)]

def confusion(truth, flagged):
    """{tp, fp, fn, tn} from two equal-length lists of bools (True = anomaly)."""
    truth, flagged = np.asarray(truth, bool), np.asarray(flagged, bool)
    return {"tp": int(np.sum(truth & flagged)), "fp": int(np.sum(~truth & flagged)),
            "fn": int(np.sum(truth & ~flagged)), "tn": int(np.sum(~truth & ~flagged))}

def rates(counts):
    """precision, recall, f1 and fpr from confusion counts; 0.0 where undefined."""
    tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "fpr": fpr}

def recall_at_fpr(truth, anomaly_scores, fpr = COMPARE_FPR):
    """Recall when the cut-off flags `fpr` of the NORMAL windows: a threshold-free way to
    compare two detectors at the same false-alarm rate."""
    truth, scores = np.asarray(truth, bool), np.asarray(anomaly_scores, float)
    if truth.all() or not truth.any():
        return 0.0
    cut = np.quantile(scores[~truth], 1 - fpr)
    return float(np.mean(scores[truth] > cut))

def ranking(truth, anomaly_scores):
    """ROC AUC and average precision: how well the scores rank anomalies above NORMAL
    windows, before any cut-off is chosen."""
    truth = np.asarray(truth, bool)
    if truth.all() or not truth.any():
        return {"roc_auc": 0.0, "average_precision": 0.0}
    return {"roc_auc": float(roc_auc_score(truth, anomaly_scores)),
            "average_precision": float(average_precision_score(truth, anomaly_scores))}

def parse_time(text):
    """A window_start (YYYY-MM-DDTHH:MM:SS.mmmZ) as an aware datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo = timezone.utc)

def onset(label_rows):
    """When the run's first counted fault became active, from its labels' fault_ms; None
    for a run with no fault (NORMAL)."""
    for row in sorted(label_rows, key = lambda r: r["window_start"]):
        active = [ms for ms in (row.get("fault_ms") or {}).values() if ms]
        if active:
            end = parse_time(row["window_start"]) + timedelta(seconds = WINDOW_S)
            return end - timedelta(milliseconds = max(active))
    return None

def latency(runs, rows, flagged):
    """Detection latency per incident run.

    `runs` is load_runs' [(experiment_id, feature_rows, label_rows)], `rows` training_rows'
    [(experiment_id, row)] for the same channel, and `flagged` one bool per row. A run counts
    when it has a labeled anomalous window. Its latency is from onset() to the end of the
    first anomalous window flagged; a run with none flagged is missed. Returns (summary,
    per_run): summary's numbers are over detected runs."""
    onsets = {experiment_id: onset(label_rows) for experiment_id, _, label_rows in runs}
    windows = {}
    for (experiment_id, row), hit in zip(rows, flagged):
        windows.setdefault(experiment_id, []).append((row["window_start"], row["label"], bool(hit)))
    per_run = []
    for experiment_id, entries in sorted(windows.items()):
        anomalous = sorted((start, label, hit) for start, label, hit in entries if label != NORMAL)
        if not anomalous:
            continue
        labels = sorted({label for _, label, _ in anomalous})
        start = onsets.get(experiment_id) or parse_time(anomalous[0][0])
        hits = [s for s, _, hit in anomalous if hit]
        entry = {"experiment_id": experiment_id, "labels": labels, "onset": start.isoformat(),
                 "anomalous_windows": len(anomalous), "detected": bool(hits)}
        if hits:
            closed = parse_time(hits[0]) + timedelta(seconds = WINDOW_S)
            entry["latency_s"] = round((closed - start).total_seconds(), 3)
            # 0 when the first labeled anomalous window is the one that fired
            entry["windows_late"] = [s for s, _, _ in anomalous].index(hits[0])
        per_run.append(entry)

    detected = [e["latency_s"] for e in per_run if e["detected"]]
    summary = {"incident_runs": len(per_run), "detected_runs": len(detected),
               "missed_runs": len(per_run) - len(detected)}
    if detected:
        values = np.asarray(detected)
        summary.update({
            "median_s": float(np.median(values)), "mean_s": float(np.mean(values)),
            "p90_s": float(np.quantile(values, 0.9)), "max_s": float(np.max(values)),
            # share of detected runs caught in the first labeled anomalous window
            "first_window_share": float(np.mean([e["windows_late"] == 0 for e in per_run if e["detected"]]))})
    return summary, per_run

def recall_by_label(rows, flagged):
    """{label: share flagged} per six-class label; for NORMAL that share is the FPR."""
    totals, hits = {}, {}
    for (_, row), hit in zip(rows, flagged):
        totals[row["label"]] = totals.get(row["label"], 0) + 1
        hits[row["label"]] = hits.get(row["label"], 0) + int(bool(hit))
    return {label: hits[label] / totals[label] for label in sorted(totals)}

def evaluate(runs, rows, labels, flagged, anomaly_scores):
    """(metrics, evaluation) for one channel. metrics: flat numbers for model.json's
    `metrics` and the log; evaluation: the confusion counts, recall per label and latency."""
    truth = is_anomaly(labels)
    counts = confusion(truth, flagged)
    summary, per_run = latency(runs, rows, flagged)
    metrics = {**rates(counts), **ranking(truth, anomaly_scores),
               "recall_at_fpr_5": recall_at_fpr(truth, anomaly_scores)}
    if "median_s" in summary:
        metrics["latency_median_s"] = summary["median_s"]
    evaluation = {"confusion": counts, "recall_by_label": recall_by_label(rows, flagged),
                  "latency": {**summary, "runs": per_run}}
    return metrics, evaluation