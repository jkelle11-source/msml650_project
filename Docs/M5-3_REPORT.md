# M5-3 report: incident classifier and Model 0 baseline

**Status:** validation numbers are measured (slots 561-700). Test-set results are M9's and are not in this document.

## What is compared

- **Model 0 (threshold rule).** A window is flagged as an anomaly if any service has p95 latency above 2x a baseline, or a 5xx rate above 5%. Code: `ml/model0.py`. It answers only "anomaly or not", so it is scored in the anomaly view (anything but NORMAL is an anomaly). Two baselines for the latency rule:
  - `previous_window`: the previous retained window's p95.
  - `earlier_mean_3`: the mean of the 3 earlier lookback windows of the same run (the labeled windows plus the startup minute, as the feature matrix builds them), **excluding the current window**. This is the stronger floor, and the margin is reported against it. An earlier version used a mean that included the current window, which works out to p95 > 4x the mean of the two earlier windows, so it was replaced.
- **Logistic regression** (tuned, M5-1): job `anomalypulse-hpo-261028173159-003-28af89d1`, C = 25.8, tuned on validation.
- **XGBoost:** defaults, job `anomalypulse-train-incident-xgb-20261030T033735Z`; tuned, best of 6 in tuning job `anomalypulse-hpo-261031214659` (job `anomalypulse-hpo-261031214659-004-76a607cd`: learning rate 0.057, max depth 7, 321 trees). `subsample` and `colsample_bytree` were left at 1.0 and not searched.
- **Random Forest** (defaults, not tuned): job `anomalypulse-train-incident-rf-20261030T034713Z`.
- **Data.** v1 slots 1-560 train, 561-700 validation (885 labeled windows: 375 NORMAL, 510 anomalous). The test set is not used.
- **Metrics.** Produced by `ml/evaluate.py` and `ml/compare.py`, in the training image's pinned library versions (Python 3.10, scikit-learn 1.5.2, numpy 2.1.0, scipy 1.15.0, joblib 1.6.0, xgboost 3.0.5).

## Results (validation)

### Anomaly view (anything but NORMAL is an anomaly)

| Model | Precision | Recall | F1 | False-alarm rate |
|---|---|---|---|---|
| Model 0 (previous_window) | 0.897 | 0.392 | 0.546 | 0.061 |
| Model 0 (earlier_mean_3) | 0.915 | 0.488 | 0.637 | 0.061 |
| Logistic regression (tuned) | 0.996 | 0.986 | 0.991 | 0.005 |
| XGBoost (tuned) | 0.975 | 0.996 | 0.985 | 0.035 |
| XGBoost (defaults) | 0.975 | 0.996 | 0.985 | 0.035 |
| Random Forest (defaults) | 0.973 | 0.996 | 0.984 | 0.037 |

### Six-class view

| Model | Accuracy | Macro-F1 | Train macro-F1 | Windows wrong (of 885) |
|---|---|---|---|---|
| Logistic regression (tuned) | 0.9898 | 0.9923 | 0.9952 | 9 |
| XGBoost (tuned) | 0.9831 | 0.9871 | 1.0000 | 15 |
| XGBoost (defaults) | 0.9831 | 0.9869 | 1.0000 | 15 |
| Random Forest (defaults) | 0.9819 | 0.9860 | 1.0000 | 16 |

Model 0 has no six-class output, so accuracy and macro-F1 do not apply to it. Per-class precision and recall and confusion matrices for each model come from `python -m ml.evaluate`.

### Model 0: share of each anomaly class flagged

| Class | previous_window | earlier_mean_3 |
|---|---|---|
| CASCADING_FAILURE | 0.830 | 0.906 |
| DATABASE_THROTTLING | 0.290 | 0.540 |
| DEPENDENCY_FAILURE | 1.000 | 1.000 |
| LAMBDA_DEGRADATION | 0.111 | 0.306 |
| TRAFFIC_SPIKE | 0.069 | 0.062 |

### Cascade margin over Model 0 (53 cascade windows)

Model 0 flags 90.6% of cascade windows under its stronger baseline (83.0% under the other). All four classifiers get all 53 exactly right, a margin of **+0.094** over the stronger baseline. That margin is real but small: Model 0 already catches most cascades, and v1's cascades come from a small set of templates, so 100% here is not evidence of generalization. Held-out bands (M9) are the test of that.

### Time to recognize a cascade

Cascade performance is reported as the time to recognize the multi-fault state, not as per-window accuracy. Start: the moment two faults are first active together (the run's first window with multi-fault time, at `window_start + 60 s - multi_fault_ms`). Recognized: the end of the first **labeled** window, at or after the start, that the model calls CASCADING_FAILURE. Excluded windows are never scored, so a DB-throttling onset minute (which holds the injector's burst-capacity surge) is never credited to a model, even when the cascade starts inside it. Model 0 cannot name a cascade, so it has no time to recognize.

Across the 14 validation cascade runs, **every model has the same time in every run**: median 75.1 s, max 108.0 s, none never recognized.

| Run | Time to recognize (s), all four models |
|---|---|
| exp-20261024T112908Z-a9bf | 43.6 |
| exp-20261024T114909Z-5cfb | 53.4 |
| exp-20261024T145915Z-2a18 | 108.0 |
| exp-20261024T171919Z-3111 | 43.8 |
| exp-20261024T175920Z-5fb3 | 89.3 |
| exp-20261024T181921Z-addd | 38.3 |
| exp-20261024T213927Z-1fe1 | 101.6 |
| exp-20261024T232930Z-0017 | 82.6 |
| exp-20261025T013934Z-cd7f | 73.6 |
| exp-20261025T023936Z-9d10 | 74.8 |
| exp-20261025T045940Z-4d92 | 75.4 |
| exp-20261025T051941Z-3e14 | 81.7 |
| exp-20261025T092948Z-b068 | 50.6 |
| exp-20261025T094949Z-62bb | 75.6 |

In every run all models call the cascade at the first window labeled CASCADING_FAILURE, so on validation this number is set by the one-minute windows and the labeler's 30 s rule, not by the models, and it does not separate them. The test set (M9) may. The 9 runs over 60 s are those where the first minute of overlap is an excluded onset minute or holds under 30 s of overlap.

## How to read the two signals

**Margin over Model 0 measures multivariate learning.** Every classifier beats the threshold rule by a wide margin in the anomaly view: Model 0's stronger baseline catches 49% of anomalous windows, the classifiers catch 99% or more, and they raise fewer false alarms in the logistic regression's case (0.005 against 0.061) and slightly fewer for the trees (0.035-0.037). The cascade margin is +0.094.

**The train/validation gap measures overfitting.** The tree models score 1.0000 on train and 0.986-0.987 on validation. Logistic regression scores 0.9952 on train and 0.9923 on validation, a gap of about 0.003. Whether the gap persists on held-out bands is M9's question.

## Where the errors are

All 25 wrong windows across the three first models are the first minute of their run's fault, where the fault starts partway through the minute (Jake's analysis of the 885 windows):

- Logistic regression's 5 missed TRAFFIC_SPIKE windows have 35-49.5 s of fault.
- The tree models' 13-14 false alarms on NORMAL windows have 13.7-29.4 s of fault, just under the labeler's 30 s rule.

So the trade-off between the models is not missed incidents against false alarms in general. It is where each model draws the line in that first minute: logistic regression leans NORMAL (fewest false alarms, about 7 misses), and the trees lean incident (about 2 misses, 13-14 false alarms). Tuning XGBoost did not change this: its 6 tuning jobs scored 0.9828-0.9871 on validation, against 0.9869 for the defaults.

## Feature boundary check

The Feature/Label Boundary guard (`features/guard.py`, M4-5) was run over every downloaded v1 train and validation feature file: 700 files, 106 columns, no forbidden names. Every column the models train on is Tier-1. Labels come from `tier2/labels/` through `join_labels`, after the matrix is written, and never enter it.

## Caveats

- **Validation numbers are optimistic for the tuned models.** Logistic regression's C and XGBoost's depth, learning rate and trees were chosen on validation, so both validation scores are slightly optimistic. Test numbers come from M9.
- **Interpolation inside a simulator.** Test bands sit inside the trained range, so a good score supports generalization across held-out regions of the simulator's parameter space. It does not support a claim about production incidents.
- **One-minute windows blur boundaries.** A window with a few seconds of fault can be labeled NORMAL by the 30 s rule while its features already shift. Every error above is of this kind.
- **Rolling features cross windows.** Temporal columns look back over a run's retained windows, so a labeled window can carry values from an earlier one.
- **Random Forest was not tuned,** and XGBoost's `subsample` and `colsample_bytree` were not searched.
- **Library versions.** Trained packages use the pinned versions in `ml/requirements-image.txt`. Numbers from other versions are not comparable.

## Selected model

**Logistic regression** (tuned, job `anomalypulse-hpo-261028173159-003-28af89d1`, package version `kNUehxgFp8NvWKB4nurs3JgoXXPOT_yY`), chosen on the highest validation macro-F1 (0.9923, against 0.9871 for the tuned XGBoost). It is already promoted as `incident_type`. A real Diagnosis Lambda call on 2026-10-31 returned HTTP 200, and the reply's `model.job` names this job. Nothing was promoted by M5-3.