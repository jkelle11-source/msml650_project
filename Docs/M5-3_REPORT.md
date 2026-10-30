# M5-3 report: incident classifier and Model 0 baseline

**Status:** DRAFT. Validation numbers below are measured (slots 561-700, one run per model). Selected model and test-set results are not decided yet.

## What is compared

- **Model 0 (threshold rule).** A window is flagged as an anomaly if any service has p95 latency above 2x a baseline, or a 5xx rate above 5%. Code: `ml/model0.py`. It answers only "anomaly or not", so it is scored in the anomaly view (anything but NORMAL is an anomaly).
  - `rolling_mean`: the ticket's literal rule. The rolling mean includes the current window, so it is a weak floor.
  - `previous_window`: compares with the previous retained window (the run's labeled windows plus its startup minute). It is the stricter floor.
- **Logistic regression.** Trained by M5-1. Validation macro-F1 0.988 at C = 1, and 0.9923 after tuning C to 25.8 (12 of 885 windows wrong at C = 1, 9 of them NORMAL vs TRAFFIC_SPIKE).
- **Random Forest** and **XGBoost.** Scripts: `ml/job/train_incident_rf.py`, `ml/job/train_incident_xgb.py`. Same train/validation split, same features.
- **Data.** v1 slots 1-560 train, 561-700 validation. The test set is not used; final numbers are M9's.
- **Metrics.** Produced by `ml/compare.py` through `ml/evaluate.py`.

## Results (validation, slots 561-700; 885 labeled windows: 375 NORMAL, 510 anomalous)

Scored by `ml/evaluate.py` on the same windows for every model, in the training image's pinned library versions.

### Anomaly view (anything but NORMAL is an anomaly)

| Model | Precision | Recall | F1 | False-alarm rate |
|---|---|---|---|---|
| Model 0 (rolling_mean) | 0.895 | 0.386 | 0.540 | 0.061 |
| Model 0 (previous_window) | 0.897 | 0.392 | 0.546 | 0.061 |
| Logistic regression (tuned, M5-1) | 0.996 | 0.986 | 0.991 | 0.005 |
| XGBoost (defaults) | 0.975 | 0.996 | 0.985 | 0.035 |
| Random Forest (defaults) | 0.973 | 0.996 | 0.984 | 0.037 |

### Six-class view

| Model | Accuracy | Macro-F1 | Train macro-F1 |
|---|---|---|---|
| Logistic regression (tuned, M5-1) | 0.9898 | 0.9923 | TBD |
| XGBoost (defaults) | 0.9831 | 0.9869 | 1.0000 |
| Random Forest (defaults) | 0.9819 | 0.9860 | 1.0000 |

Model 0 has no six-class output, so accuracy and macro-F1 do not apply to it. Per-class tables and confusion matrices are in the evaluation output (`ml/evaluate.py`).

### Cascade margin over Model 0 (53 cascade windows)

Model 0 flags 83.0% of cascade windows under both baselines. All three classifiers get all 53 exactly right, a margin of +0.170. That is a real but modest margin: Model 0 already catches most cascades, and cascades in v1 come from a small set of templates, so 100% here is not evidence of generalization. The held-out bands in M9 are the test of that.

## How to read the two signals

**Margin over Model 0 measures multivariate learning.** Every classifier beats the threshold rule by a wide margin in the anomaly view: it catches about 4 in 10 anomalous windows, they catch 99% or more of them, and raise fewer false alarms. The cascade margin is +0.170.

**The train/validation gap measures overfitting.** The two tree models score macro-F1 1.0000 on train and 0.986 to 0.987 on validation, so they fit the training windows perfectly and lose a little on unseen ones. It is mild. Whether it persists on held-out bands is M9's question. The tuned logistic regression's train score is TBD.

## Where the errors are

- **XGBoost (15 of 885 wrong):** 13 are truly NORMAL windows called an incident (8 as TRAFFIC_SPIKE, 4 as LAMBDA_DEGRADATION, 1 as DEPENDENCY_FAILURE), and every one of those contained 13.7 to 29.4 s of fault, just under the labeler's 30 s rule. The other two are incident windows called NORMAL (34.1 s and 35.5 s of fault).
- **Logistic regression (9 of 885 wrong):** 2 are NORMAL windows called TRAFFIC_SPIKE (22.3 s and 23.8 s of fault). 7 are incident windows called NORMAL, including 5 TRAFFIC_SPIKE windows with 35 to 49.5 s of fault, which are real misses and not label-boundary noise. We have not explained why.
- **Trade-off:** logistic regression has the fewest false alarms (about 2) but misses about 7 real incidents. The tree models miss about 2 and false-alarm on 13 or more. Which is better depends on the relative cost of a missed incident and a false alarm.

## Caveats to state

- **These are validation numbers.** They were used to choose settings, so they are not an unbiased estimate. Test numbers come from M9.
- **Interpolation inside a simulator.** Test bands sit inside the trained range, so a good score supports generalization across held-out regions of the simulator's parameter space. It does not support a claim about production incidents.
- **One-minute windows blur boundaries.** A window with a few seconds of fault can be labeled NORMAL by M4-4's 30-second rule while its features already shift. Boundary errors are expected and are not all model errors.
- **Rolling features cross windows.** Temporal columns look back over a run's retained windows, so a labeled window can carry values from an earlier one.
- **Model 0's two baselines bracket its floor.** The margin is reported against the stricter one. If it is much larger against `rolling_mean`, that is the weak floor, not extra learning.
- **Library versions.** Trained packages must use the pinned versions in `ml/requirements-image.txt`. Numbers from other versions are not comparable.

## Selected model

Chosen on validation macro-F1: TBD. Promoted as `incident_type` with job name: TBD. Checked with a real Diagnosis Lambda call: TBD.