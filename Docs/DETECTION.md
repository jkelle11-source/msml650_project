# Anomaly detection (M5-2)

**Owner:** Melisa · **Code:** `AnomalyPulse/ml/job/` (`train_iforest.py`, `train_binary.py`, `detection.py`),
`AnomalyPulse/ml/compare_detectors.py` · **Used by:** M8-2 (live path), M9 (test-set metrics)

Two detectors answer NORMAL vs ANOMALY for one window (plan §8); every incident label counts as ANOMALY.
The **Isolation Forest** models production, where nobody labels incidents. The **supervised binary
detector** (logistic regression) shows what labels buy. Both train through the M5-1 launcher and are
packaged for the Diagnosis Lambda ([`ML_INFERENCE.md`](ML_INFERENCE.md)): the forest as a detector
(`task: anomaly_iforest`, `threshold` on `score_samples`), the supervised one as a classifier
(`task: anomaly_binary`, classes `[ANOMALY, NORMAL]`). Neither is promoted; M8-2 promotes what it serves.

```bash
cd AnomalyPulse
python -m ml.launch train --script ml/job/train_iforest.py   # --set fit_on=all, threshold_rule=offset, ...
python -m ml.launch train --script ml/job/train_binary.py    # --set estimator=xgboost
aws s3 cp <S3ModelArtifacts> iforest.tar.gz --profile dev    # each job's package (the launcher prints it)
python -m ml.compare_detectors iforest.tar.gz binary.tar.gz  # the tables below, and the M4-5 guard
```

Neither script can go through `ml.launch tune`: the tuning job maximizes `validation:macro_f1`, which
they don't print.

## Healthy and fault-start windows

The labeling policy gives a window a fault's label only when the fault is active for at least 30 s of it
([`LABELING.md`](LABELING.md)). So NORMAL windows come in two kinds, told apart by the labels' `fault_ms`:

| Kind | What it is | Validation windows |
|---|---|---|
| healthy | NORMAL, no fault active at all | 336 |
| fault-start | NORMAL, a fault active for under 30 s of it. On v1 always the minute a fault starts in, followed by the fault's first labeled window | 39 |

A detector that flags a fault-start window has caught a real fault early, but the labels count it as a
false positive. On validation, every false alarm the supervised detector raised, and every one the forest
raised at its first cut-off, was a fault-start window. So each package reports three numbers where one
would hide this:

- `fpr`: flagged NORMAL windows of either kind, as the labels say;
- `fpr_healthy`: flagged healthy windows, the false alarms an operator would see;
- `fault_start_flagged`: flagged fault-start windows, faults caught early.

DB throttling has no fault-start windows: its first minutes are excluded as onset.

## The Isolation Forest's cut-off

The forest scores each window; the cut-off is the one choice that needs any labels. `threshold_rule`:

- **`fpr`** (packaged): flag `target_fpr` (1%) of validation's healthy windows, the cut a team could set
  from a period it knows was quiet.
- `offset`: scikit-learn's own (−0.5), no labels at all.
- `f1`: the best F1 on validation. Picked on the same labels it's scored on, so the optimistic case.

The first version set `fpr` at 5% of every NORMAL window. Fault-start windows score as anomalous (they
are), so that budget went on 19 of them, no healthy window was flagged, and the cut sat low enough to
miss every DB-throttling window and most Lambda-degradation ones (recall 0.549; PR #83's review).

The forest fits on the NORMAL training windows (`fit_on=normal`: "a quiet baseline week"), fault-start
ones included. Fitting on healthy windows only did worse (recall 0.88 at 1% of healthy windows flagged).
The fully label-free fit (`fit_on=all`) scores the same as the packaged forest at the 1% cut (recall
1.000, 4 healthy windows flagged; ROC AUC 0.960 against 0.955).

## Validation results (2026-10-31)

Jobs `anomalypulse-train-iforest-20261031T224214Z` and `anomalypulse-train-binary-20261031T224214Z`
(119 s and 114 s billed): both Completed, every launcher check PASS. Validation is 140 runs, 885
windows: 510 incident, 336 healthy, 39 fault-start.

| validation | Isolation Forest | supervised |
|---|---|---|
| recall | 1.000 | 0.986 |
| healthy windows flagged (`fpr_healthy`) | 4 of 336 (0.012) | 0 of 336 |
| fault-start windows flagged | 38 of 39 | 4 of 39 |
| FPR, against the labels | 0.112 | 0.011 |
| precision, against the labels | 0.924 | 0.992 |
| F1, against the labels | 0.960 | 0.989 |
| ROC AUC, against the labels | 0.955 | 0.999 |
| recall at 1% of healthy windows flagged | 1.000 | 1.000 |

**The cost of going unsupervised is close to nothing here.** Every incident window scores above every
healthy window, for both detectors: each could catch all 510 before a single healthy alarm. They differ
on the fault-start minute. The forest flags it (38 of 39); the supervised detector learned the labels'
30-second rule and mostly doesn't (4 of 39). That's why the forest trails on every number measured
against the labels, and why those numbers undersell it. The forest's 1.2% of healthy windows flagged is
its target by construction, the cut having been set on those windows; on the train split the same cut
flags 2.5% (34 of 1,344) and still catches every incident window. All of this is validation, drawn
from the training bands. The test set's held-out bands, confounders, 2- and 12-minute faults and
shifted traffic mix (plan §6b) are where a gap can open; M9 reports it.

Share of windows flagged, by label: the forest 1.00 for every incident type; the supervised detector
1.00 cascading and DB throttling, 0.99 dependency failure and Lambda degradation, 0.97 traffic spike.

Isolation Forest cut-off rules on validation:

| rule | threshold | recall | F1 | FPR (labels) | healthy flagged | fault-start flagged |
|---|---|---|---|---|---|---|
| `fpr`, 1% of healthy (packaged) | −0.3695 | 1.000 | 0.960 | 0.112 | 4 of 336 | 38 of 39 |
| `offset` | −0.5000 | 0.678 | 0.786 | 0.064 | 0 | 24 of 39 |
| `f1` | −0.3926 | 1.000 | 0.964 | 0.101 | 0 | 38 of 39 |

**M4-5 guard:** PASS on both packages' 106 columns.

### Detection latency

From the fault's start (the labels' `fault_ms`; DB throttling's `label_from`) to the end of the first
flagged incident window, per run. Only labeled incident windows count, so a flag in the fault-start minute
isn't credited.

| | Isolation Forest | supervised |
|---|---|---|
| runs detected | 112 of 112 | 112 of 112 |
| median | 77.5 s | 81.3 s |
| p90 | 105.0 s | 108.2 s |
| max | 119.3 s | 119.3 s |
| caught in the run's first incident window | 100% | 93.8% |

Latency is set by the one-minute window (plan §16), not the detector. A window is scored when its minute
closes, and inference takes milliseconds. A fault that starts *s* seconds into a minute is first
scorable at that minute's end (60 − *s* s) if it ran at least 30 s of it, or else at the next minute's
end (120 − *s* s). DB throttling's clock starts at `label_from`, and that minute is excluded as onset,
so its runs take 60–120 s (median 97.2 s for both).

## For M9

- Report `fpr` against the labels, with `fpr_healthy` and `fault_start_flagged` beside it.
- Time detection from labeled incident windows only; db_throttling_onset minutes are never scored.
- The forest's cut-off is this package's `threshold`, set on validation, never on the test set.

## On the live path (M8-1)

Live windows are built as training's were, but with no labels their temporal columns look back over every
earlier minute, and the live path scores minutes no label covers. Measured on validation
([`PIPELINE.md`](PIPELINE.md)): the forest's recall stays 510/510, and its healthy false alarms go from 4 to 5
of 336 (the first NORMAL minute after idle time). It also flags every minute whose look-back still holds a fault,
for about 3 minutes after a fault ends, and the first full minute after idle time. None of those minutes is
labeled, so no metric above counts them.
