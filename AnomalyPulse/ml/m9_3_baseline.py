"""M9-3: the threshold baseline (Model 0) beside M9-2's test-set results.

Model 0 is scored with M9-2's own code (ml/m9_2_metrics.py), on its runs and windows, and margins use a
paired bootstrap. Nothing here fits or tunes anything.

Model 0 was fixed in M5-3, before the test set existed, and is not tuned here. M9-2 had already scored and
seen the test set, so every result is reported whichever way it falls:
- Model 0 is ml/model0.py as reviewed in M5-3: the previous-window rule and the earlier-mean-3 rule,
  thresholds 2x p95 and 5% 5xx, no tuning.
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
CLASSIFIER_AS_DETECTOR = "classifier_as_detector"     # the live classifier, read as a detector
AS_DETECTOR = "_as_detector"                          # suffix for every other classifier
STATS = ("precision", "recall", "f1", "fpr")
HEALTHY = "healthy"                                   # share_margins' key for the fault-free NORMAL windows
LOW, HIGH = 0.025, 0.975                              # a 95% percentile interval


def as_detector(name, live):
    """The flag name a classifier gets when read as a detector (anything but NORMAL is an anomaly)."""
    return CLASSIFIER_AS_DETECTOR if name == live else f"{name}{AS_DETECTOR}"


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


def add_baselines(scored, classifier=None, others=()):
    """Put Model 0's flags, and each classifier read as a detector, into M9-2's Scored object, so its
    code scores them. `classifier` is the live one (CLASSIFIER_AS_DETECTOR); `others` are the other
    classifiers in scored.preds (<name>_as_detector). Returns the names whose 'anomaly score' is only
    the 0/1 flag: their ranking metrics are not real and must be reported n/a."""
    flag_only = []
    for name, flags in model0_flags(scored).items():
        scored.flags[name] = flags
        scored.anomaly_scores[name] = flags.astype(float)
        flag_only.append(name)
    for name in ([classifier] if classifier is not None else []) + list(others):
        flags = np.asarray([p != m.NORMAL for p in scored.preds[name]], bool)
        key = as_detector(name, classifier)
        scored.flags[key] = flags
        scored.anomaly_scores[key] = flags.astype(float)
        flag_only.append(key)
    return flag_only


def add_deployed(scored, forest, classifier):
    """The live rule (the forest flags the window AND the live classifier names a fault), when
    score_runs did not add it (it only does with exactly one classifier)."""
    if forest is not None and m.DEPLOYED not in scored.flags:
        scored.flags[m.DEPLOYED] = m.deployed_flags(scored.flags[forest], scored.preds[classifier])


def _resample(key, runs):
    """The run indices of every resample: one draw per (seed, key), used for BOTH detectors."""
    rng = np.random.default_rng([m.CONFIG["seed"], zlib.crc32(key.encode())])
    return rng.integers(0, runs, size=(m.CONFIG["resamples"], runs))


def _interval(values):
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    return [float(np.quantile(values, LOW)), float(np.quantile(values, HIGH))] if len(values) else None


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
        idx = _resample(key, runs)
        draws_ml, draws_base = m.det_stats(per_ml[idx].sum(axis=1)), m.det_stats(per_base[idx].sum(axis=1))
        for s in STATS:
            ci95[s] = _interval(draws_ml[s] - draws_base[s])
    return m._clean({"runs": runs, "difference": difference, "ci95": ci95})


def _paired_share(sub, hit_ml, hit_base, mask, key):
    """Share of the `mask` windows each detector flags, and their difference (ml minus baseline), with a
    paired run-bootstrap interval for the difference."""
    per_ml, per_base = m.per_run_ratio(sub, hit_ml, mask), m.per_run_ratio(sub, hit_base, mask)
    windows = per_ml[:, 1].sum()
    out = {"windows": int(windows), "ml": None, "baseline": None, "difference": None, "ci95": None}
    if not windows:
        return out
    out["ml"], out["baseline"] = float(per_ml[:, 0].sum() / windows), float(per_base[:, 0].sum() / windows)
    out["difference"] = out["ml"] - out["baseline"]
    if sub.R >= 2:
        idx = _resample(key, sub.R)
        draws = (m.ratio_stats(per_ml[idx].sum(axis=1))["ratio"] - m.ratio_stats(per_base[idx].sum(axis=1))["ratio"])
        out["ci95"] = _interval(draws)
    return out


def paired_share_margins(scored, eids, ml, baseline, key):
    """{label: share margin} for every label among the runs' windows, and HEALTHY: the NORMAL windows with
    no fault active, so its difference is the false alarms an operator would see (lower is better). For
    a fault label it is the paired version of 'share of the class flagged', the cascade margin included."""
    sub = m.subset(scored, eids)
    hit_ml, hit_base = scored.flags[ml][sub.idx], scored.flags[baseline][sub.idx]
    labels = np.asarray(sub.labels)
    out = {label: _paired_share(sub, hit_ml, hit_base, labels == label, f"{key}/share/{label}")
           for label in sorted(set(sub.labels))}
    calm = np.asarray(m.detection.healthy(sub.runs, sub.rows), bool)
    out[HEALTHY] = _paired_share(sub, hit_ml, hit_base, calm, f"{key}/share/{HEALTHY}")
    return m._clean(out)
