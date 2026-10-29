# M5-3 report: incident classifier and Model 0 baseline

**Status:** DRAFT. Every `TBD` is a number that needs v1's validation windows. No result below has been measured yet.

## What is compared

- **Model 0 (threshold rule).** A window is flagged as an anomaly if any service has p95 latency above 2x a baseline, or a 5xx rate above 5%. Code: `ml/model0.py`. It answers only "anomaly or not", so it is scored in the anomaly view (anything but NORMAL is an anomaly).
  - `rolling_mean`: the ticket's literal rule. The rolling mean includes the current window, so it is a weak floor.
  - `previous_window`: compares with the previous retained window (the run's labeled windows plus its startup minute). It is the stricter floor.
- **Logistic regression.** Trained by M5-1. Validation macro-F1 0.988 at C = 1, and 0.9923 after tuning C to 25.8 (12 of 885 windows wrong at C = 1, 9 of them NORMAL vs TRAFFIC_SPIKE).
- **Random Forest** and **XGBoost.** Scripts: `ml/job/train_incident_rf.py`, `ml/job/train_incident_xgb.py`. Same train/validation split, same features.
- **Data.** v1 slots 1-560 train, 561-700 validation. The test set is not used; final numbers are M9's.
- **Metrics.** Produced by `ml/compare.py` through `ml/evaluate.py`.

## Results (validation, slots 561-700)

| Model | Accuracy | Macro-F1 | Anomaly precision | Anomaly recall | Anomaly FPR |
|---|---|---|---|---|---|
| Model 0 (rolling_mean) | n/a | n/a | TBD | TBD | TBD |
| Model 0 (previous_window) | n/a | n/a | TBD | TBD | TBD |
| Logistic regression (tuned) | TBD | 0.9923 | TBD | TBD | TBD |
| Random Forest | TBD | TBD | TBD | TBD | TBD |
| XGBoost | TBD | TBD | TBD | TBD | TBD |

Model 0 has no six-class output, so accuracy and macro-F1 do not apply to it.

## How to read the two signals

**Margin over Model 0 measures multivariate learning.** A classifier that beats the threshold rule is using combinations of signals the rule cannot see. The margin is most informative on cascades, where no single metric may cross a threshold. Cascade windows in validation: TBD. Model 0's best recall on them: TBD. Each classifier's exact-class recall on them: TBD. Margin: TBD.

**The train/validation gap measures overfitting to simulator artifacts.** It is a different question from the margin. A small margin over Model 0 is not evidence of overfitting, and a large margin does not rule it out. Train macro-F1 against validation macro-F1: TBD. The held-out-band comparison belongs to M9, not here.

## Caveats to state

- **These are validation numbers.** They were used to choose settings, so they are not an unbiased estimate. Test numbers come from M9.
- **Interpolation inside a simulator.** Test bands sit inside the trained range, so a good score supports generalization across held-out regions of the simulator's parameter space. It does not support a claim about production incidents.
- **One-minute windows blur boundaries.** A window with a few seconds of fault can be labeled NORMAL by M4-4's 30-second rule while its features already shift. Boundary errors are expected and are not all model errors.
- **Rolling features cross windows.** Temporal columns look back over a run's retained windows, so a labeled window can carry values from an earlier one.
- **Model 0's two baselines bracket its floor.** The margin is reported against the stricter one. If it is much larger against `rolling_mean`, that is the weak floor, not extra learning.
- **Library versions.** Trained packages must use the pinned versions in `ml/requirements-image.txt`. Numbers from other versions are not comparable.

## Selected model

Chosen on validation macro-F1: TBD. Promoted as `incident_type` with job name: TBD. Checked with a real Diagnosis Lambda call: TBD.