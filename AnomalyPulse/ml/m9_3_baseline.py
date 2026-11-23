"""M9-3: the threshold baseline (Model 0) beside M9-2's test-set results.

Model 0 is scored with M9-2's own code (ml/m9_2_metrics.py), on its runs and windows, and margins use a
paired bootstrap. Nothing here fits or tunes anything.

Frozen before Model 0 touched the test set (M9-2 had already scored it, so the baseline must not be
adjusted after anyone sees its numbers):
- Model 0 is ml/model0.py as reviewed in M5-3: the previous-window rule and the earlier-mean-3 rule,
  thresholds 2x p95 and 5% 5xx, no tuning.
- Everything is reported, whichever way it falls.
- A rule has no class and no score, so accuracy, macro-F1, ROC AUC, average precision and recall at a
  healthy false-alarm rate are n/a for it.
"""
import zlib

import numpy as np

from ml import evaluate, model0
from ml import m9_2_metrics as m

PREVIOUS = "model0_previous_window"
EARLIER = f"model0_{evaluate.EARLIER_BASELINE}"
NAMES = (PREVIOUS, EARLIER)
CLASSIFIER_AS_DETECTOR = "classifier_as_detector"
STATS = ("precision", "recall", "f1", "fpr")


def model0_flags(scored):
    """{name: np.ndarray[bool]} aligned with scored.rows: Model 0's two floors on each labeled window.
    The earlier-mean floor reads each run's lookback windows (its labeled windows plus the startup
    minute) before the window, as ml/evaluate.py does."""
    if not scored.rows:
        raise m.MetricsError("no labeled windows to score")
    missing = [c for c in evaluate.model0_columns() if c not in scored.rows[0][1]]
    if missing:
        raise m.MetricsError(f"Model 0 reads columns the features lack: {missing}")
    lookback = {eid: evaluate.lookback_rows(features, labels) for eid, features, labels in scored.runs}
    previous = [model0.flag(row) for _, row in scored.rows]
    earlier = [model0.flag_vs_earlier(row, [r for r in lookback[eid] if r["window_start"] < row["window_start"]])
               for eid, row in scored.rows]
    return {PREVIOUS: np.asarray(previous, bool), EARLIER: np.asarray(earlier, bool)}


def add_baselines(scored, classifier=None):
    """Put Model 0's flags (and the classifier read as a detector, if `classifier` names one in
    scored.preds) into M9-2's Scored object, so its code scores them. Returns the names whose
    'anomaly score' is only the 0/1 flag: their ranking metrics are not real and must be reported n/a."""
    flag_only = []
    for name, flags in model0_flags(scored).items():
        scored.flags[name] = flags
        scored.anomaly_scores[name] = flags.astype(float)
        flag_only.append(name)
    if classifier is not None:
        flags = np.asarray([p != m.NORMAL for p in scored.preds[classifier]], bool)
        scored.flags[CLASSIFIER_AS_DETECTOR] = flags
        scored.anomaly_scores[CLASSIFIER_AS_DETECTOR] = flags.astype(float)
        flag_only.append(CLASSIFIER_AS_DETECTOR)
    return flag_only


def paired_margin(scored, eids, ml, baseline, key):
    """`ml` minus `baseline` for precision, recall, F1 and false-positive rate over the runs `eids`, with a
    95% interval from resampling whole runs, the SAME runs for both detectors in each resample (a margin's
    uncertainty is smaller than two separate intervals suggest). Lower is better for fpr, so a negative
    fpr difference favors `ml`. Undefined values (a detector that flags nothing has no precision) are None."""
    sub = m.subset(scored, eids)
    per_ml = m.per_run_detection(sub, scored.flags[ml][sub.idx])
    per_base = m.per_run_detection(sub, scored.flags[baseline][sub.idx])
    point_ml = m.det_stats(per_ml.sum(axis=0, keepdims=True))
    point_base = m.det_stats(per_base.sum(axis=0, keepdims=True))
    difference = {s: float(point_ml[s][0] - point_base[s][0]) for s in STATS}
    ci95 = {s: None for s in STATS}
    runs = per_ml.shape[0]
    if runs >= 2:
        rng = np.random.default_rng([m.CONFIG["seed"], zlib.crc32(key.encode())])
        idx = rng.integers(0, runs, size=(m.CONFIG["resamples"], runs))
        draws_ml, draws_base = m.det_stats(per_ml[idx].sum(axis=1)), m.det_stats(per_base[idx].sum(axis=1))
        for s in STATS:
            values = np.asarray(draws_ml[s] - draws_base[s], float)
            values = values[np.isfinite(values)]
            if len(values):
                ci95[s] = [float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))]
    return m._clean({"runs": runs, "difference": difference, "ci95": ci95})