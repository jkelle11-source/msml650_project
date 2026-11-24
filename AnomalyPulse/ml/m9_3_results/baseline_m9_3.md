# M9-3 The threshold baseline beside the test-set results

## Read this first

- **Model 0 is a rule.** It says anomaly or not, with no class and no score, so accuracy, macro-F1, ROC AUC, average precision and recall at a fixed false-alarm rate are n/a for it. It is compared on the anomaly view (anything but NORMAL is an anomaly). Each classifier is read the same way (`read as a detector`) for a like-for-like margin.
- **Fixed before the test set existed.** Model 0 is `ml/model0.py` as reviewed in M5-3: the rule trips on p95 above 2.0x its baseline or a 5xx share above 5%, against the previous window or the mean of the last 3 earlier windows. Not tuned. M9-2 had already scored and seen the test set, so every result is reported whichever way it falls.
- **Every classifier M5-3 trained is scored.** The live one (`incident_type`) first, then `xgboost`, `xgboost_tuned`, `random_forest`. The live one was chosen on validation (M5-3); that choice is not revisited on these numbers.
- **M9-2's detectors are scored again here** (`iforest`, `binary`, and the deployed rule), so their margins over Model 0 are paired. Section 10 checks them against M9-2's published numbers.
- **Margins are paired:** both detectors are resampled on the same runs, so a margin's interval is narrower than two separate intervals suggest.
- **The test set changes the workload on purpose,** so a change from validation to test mixes the held-out bands with the shifted traffic mix. M9-2's diagnostics attribute dependency failure's drop to the mix (`Docs/M9-2_REPORT.md`). Nothing here says which cause moved Model 0.
- **No claim about real production incidents:** these results support generalization across held-out regions of the simulator's parameter space only.

## Scope

- **Headline:** 320 test runs (2092 windows), the test set without its confounders. Stress slice: 20 runs, reported apart. Validation: 140 runs (885 windows).
- **Intervals:** 95% percentile bootstrap, 1000 resamples of whole runs, seed 20261118.
- **Live classifier:** logistic_regression (incident_type), job `anomalypulse-hpo-261028173159-003-28af89d1`.
- **`xgboost`:** xgboost, job `anomalypulse-train-incident-xgb-20261030T033735Z`.
- **`xgboost_tuned`:** xgboost, job `anomalypulse-hpo-261031214659-004-76a607cd`.
- **`random_forest`:** random_forest, job `anomalypulse-train-incident-rf-20261030T034713Z`.
- **`iforest`:** isolation_forest, job `anomalypulse-train-iforest-20261031T224214Z`.
- **`binary`:** logistic_regression, job `anomalypulse-train-binary-20261031T224214Z`.
- **Code:** commit `a78f86e80c1128f1c6268580d95e8cca4709654c` (dirty: False); generated 2026-11-24T05:14:11+00:00.

## 1. Detection beside the baseline (headline)

| Metric | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector |
|---|---|---|---|
| Precision | 0.896 [0.868, 0.918] | 0.916 [0.897, 0.935] | 0.956 [0.944, 0.965] |
| Recall | 0.373 [0.320, 0.424] | 0.472 [0.419, 0.521] | 0.999 [0.997, 1.000] |
| F1 | 0.527 [0.468, 0.577] | 0.623 [0.574, 0.665] | 0.977 [0.971, 0.982] |
| False-positive rate, against the labels | 0.064 [0.047, 0.084] | 0.064 [0.047, 0.082] | 0.069 [0.052, 0.089] |
| False-positive rate, healthy windows only | 0.000 | 0.000 | 0.024 |
| Fault-start windows flagged | 0.514 | 0.514 | 0.381 |
| Median detection latency, s [95% CI] | 73.2 [69.1, 77.3] | 74.2 [70.7, 77.3] | 70.6 [67.1, 73.8] |
| Incident runs never flagged | 88 of 260 | 64 of 260 | 0 of 260 |

The latency median covers only the incident runs a detector flagged at all; the last row counts the runs it never flagged, which a median would hide.

Classification (the live classifier alone; Model 0 cannot name a class): accuracy 0.910 [0.892, 0.927], macro-F1 0.869 [0.845, 0.890]; Model 0: n/a.

### The margin

| Classifier minus Model 0 | against the earlier-mean floor | against the previous-window floor |
|---|---|---|
| Precision | +0.040 [0.018, 0.060] | +0.060 [0.035, 0.087] |
| Recall | +0.528 [0.479, 0.576] | +0.626 [0.576, 0.679] |
| F1 | +0.354 [0.310, 0.400] | +0.450 [0.400, 0.507] |
| False-positive rate, against the labels (lower is better) | +0.005 [-0.017, 0.029] | +0.005 [-0.019, 0.029] |
| Healthy windows flagged (lower is better) | +0.024 [0.014, 0.036] | +0.024 [0.015, 0.036] |

A positive recall difference means the classifier finds anomalous windows Model 0 misses. A positive false-positive-rate or healthy-window difference means the classifier raises more false alarms. Healthy windows are the NORMAL windows with no fault active: the false alarms an operator would see.

## 2. Share of each class flagged (headline)

| Label | Windows | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | paired margin over the earlier-mean floor | classifier recall (names it) | classifier precision |
|---|---|---|---|---|---|---|---|
| CASCADING_FAILURE | 118 | 0.839 [0.733, 0.926] | 0.907 [0.846, 0.962] | 1.000 [1.000, 1.000] | +0.093 [0.035, 0.156] | 1.000 | 0.474 |
| DATABASE_THROTTLING | 264 | 0.246 [0.223, 0.269] | 0.485 [0.439, 0.532] | 1.000 [1.000, 1.000] | +0.515 [0.472, 0.558] | 1.000 | 1.000 |
| DEPENDENCY_FAILURE | 265 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | +0.000 [0.000, 0.000] | 0.540 | 0.986 |
| LAMBDA_DEGRADATION | 262 | 0.118 [0.086, 0.153] | 0.317 [0.274, 0.363] | 0.996 [0.987, 1.000] | +0.679 [0.636, 0.721] | 0.989 | 0.992 |
| NORMAL (= false-alarm rate) | 845 | 0.064 [0.047, 0.083] | 0.064 [0.047, 0.084] | 0.069 [0.051, 0.086] | +0.005 [-0.018, 0.029] | 0.931 | 0.999 |
| TRAFFIC_SPIKE | 338 | 0.015 [0.003, 0.030] | 0.015 [0.003, 0.029] | 1.000 [1.000, 1.000] | +0.985 [0.971, 0.997] | 0.982 | 0.862 |
| healthy windows (NORMAL, no fault active) | 740 | 0.000 | 0.000 | 0.024 | +0.024 [0.014, 0.036] |  |  |

The margin column is the classifier's share minus the earlier-mean floor's, both resampled on the same runs. For `CASCADING_FAILURE` it is the cascade margin. The classifier's precision for that class is low because dependency failures are called cascades (M9-2).

## 3. Every ML model beside Model 0 (headline)

| Model | Precision | Recall | False-positive rate | Healthy windows flagged | Recall margin over the earlier-mean floor | FPR margin | Healthy-window margin |
|---|---|---|---|---|---|---|---|
| classifier, read as a detector | 0.956 [0.944, 0.965] | 0.999 [0.997, 1.000] | 0.069 [0.052, 0.089] | 0.024 | +0.528 [0.479, 0.576] | +0.005 [-0.017, 0.029] | +0.024 [0.014, 0.036] |
| xgboost (xgboost), read as a detector | 0.949 [0.937, 0.959] | 1.000 [1.000, 1.000] | 0.079 [0.062, 0.100] | 0.022 | +0.528 [0.479, 0.581] | +0.015 [-0.006, 0.040] | +0.022 [0.012, 0.033] |
| xgboost_tuned (xgboost), read as a detector | 0.906 [0.889, 0.921] | 1.000 [1.000, 1.000] | 0.153 [0.124, 0.184] | 0.088 | +0.528 [0.478, 0.578] | +0.089 [0.059, 0.120] | +0.088 [0.065, 0.113] |
| random_forest (random_forest), read as a detector | 0.967 [0.959, 0.976] | 0.997 [0.993, 0.999] | 0.050 [0.035, 0.067] | 0.000 | +0.525 [0.472, 0.571] | -0.014 [-0.038, 0.008] | +0.000 [0.000, 0.000] |
| iforest (isolation_forest) (flags every healthy window) | 0.596 [0.555, 0.634] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 | +0.528 [0.479, 0.578] | +0.936 [0.917, 0.952] | +1.000 [1.000, 1.000] |
| binary (logistic_regression) | 0.967 [0.957, 0.976] | 0.999 [0.998, 1.000] | 0.051 [0.036, 0.068] | 0.000 | +0.528 [0.476, 0.576] | -0.013 [-0.035, 0.007] | +0.000 [0.000, 0.000] |
| deployed rule (forest AND live classifier) | 0.956 [0.945, 0.966] | 0.999 [0.997, 1.000] | 0.069 [0.051, 0.088] | 0.024 | +0.528 [0.475, 0.581] | +0.005 [-0.018, 0.027] | +0.024 [0.015, 0.036] |
| Model 0 (earlier mean, the stronger floor) | 0.916 [0.897, 0.935] | 0.472 [0.419, 0.521] | 0.064 [0.047, 0.082] | 0.000 | (the floor) | (the floor) | (the floor) |

Margins are the model minus the earlier-mean floor, paired. A detector that flags every healthy window has recall and latency that follow from flagging everything, not from detecting faults. The deployed rule equals the live classifier read as a detector wherever the forest flags every window.

## 4. Every classifier, six classes (headline)

| Classifier | Accuracy, validation | Accuracy, test | Macro-F1, validation | Macro-F1, test | Dependency-failure recall | Cascade precision | Time to recognize a cascade | Confounders: either type / labeled type / cascade |
|---|---|---|---|---|---|---|---|---|
| incident_type (logistic_regression), live | 0.990 | 0.910 [0.892, 0.927] | 0.992 | 0.869 [0.845, 0.890] | 0.540 | 0.474 | 58.5 s, max 113.5 s, never 0 of 30 | 0.469 / 0.343 / 0.531 |
| xgboost (xgboost) | 0.983 | 0.740 [0.705, 0.773] | 0.987 | 0.610 [0.590, 0.628] | 0.000 | 0.195 | 56.3 s, max 113.5 s, never 0 of 30 | 0.280 / 0.280 / 0.720 |
| xgboost_tuned (xgboost) | 0.983 | 0.755 [0.720, 0.790] | 0.987 | 0.650 [0.625, 0.672] | 0.000 | 0.225 | 56.3 s, max 113.5 s, never 0 of 30 | 0.308 / 0.308 / 0.692 |
| random_forest (random_forest) | 0.982 | 0.826 [0.792, 0.860] | 0.986 | 0.704 [0.686, 0.723] | 0.000 | 0.269 | 59.1 s, max 113.5 s, never 0 of 30 | 0.294 / 0.294 / 0.706 |
| Model 0 (either floor) | n/a | n/a | n/a | n/a | n/a | n/a | n/a: it cannot name a cascade | n/a: it names no class |

Time to recognize: seconds from the moment two faults are first active together to the end of the first labeled window called `CASCADING_FAILURE` (ml/evaluate.py), over the headline's cascade runs. Confounders: over the 143 windows of the 30 confounder runs with both faults active (M9-2's definition).

## 5. Validation to test: the shift in each detector

| Detector | Recall, validation | Recall, test | FPR, validation | FPR, test |
|---|---|---|---|---|
| Model 0 (previous window) | 0.392 [0.309, 0.467] | 0.373 [0.320, 0.424] | 0.061 [0.039, 0.092] | 0.064 [0.047, 0.084] |
| Model 0 (earlier mean, the stronger floor) | 0.488 [0.421, 0.563] | 0.472 [0.419, 0.521] | 0.061 [0.039, 0.091] | 0.064 [0.047, 0.082] |
| classifier, read as a detector | 0.986 [0.976, 0.995] | 0.999 [0.997, 1.000] | 0.005 [0.000, 0.014] | 0.069 [0.052, 0.089] |
| xgboost (xgboost), read as a detector | 0.996 [0.990, 1.000] | 1.000 [1.000, 1.000] | 0.035 [0.017, 0.057] | 0.079 [0.062, 0.100] |
| xgboost_tuned (xgboost), read as a detector | 0.996 [0.990, 1.000] | 1.000 [1.000, 1.000] | 0.035 [0.016, 0.055] | 0.153 [0.124, 0.184] |
| random_forest (random_forest), read as a detector | 0.996 [0.990, 1.000] | 0.997 [0.993, 0.999] | 0.037 [0.019, 0.060] | 0.050 [0.035, 0.067] |
| iforest (isolation_forest) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.112 [0.080, 0.153] | 1.000 [1.000, 1.000] |
| binary (logistic_regression) | 0.986 [0.976, 0.995] | 0.999 [0.998, 1.000] | 0.011 [0.002, 0.022] | 0.051 [0.036, 0.068] |
| deployed rule (forest AND live classifier) | 0.986 [0.975, 0.996] | 0.999 [0.997, 1.000] | 0.005 [0.000, 0.015] | 0.069 [0.051, 0.088] |

Live classifier, six-class: accuracy 0.990 [0.983, 0.996] on validation and 0.910 [0.892, 0.927] on test; macro-F1 0.992 [0.987, 0.997] and 0.869 [0.845, 0.890]. Validation tuned the packages, so it is the optimistic side of any gap.

## 6. Per held-out band

| Fault | Band | Role | Model | Recall | Gap to the other test band | Gap to validation |
|---|---|---|---|---|---|---|
| db-throttling | 2.5-3.5 | interpolation | classifier (names the class) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| db-throttling | 4.5-5.5 | mild extrapolation | classifier (names the class) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| lambda-degradation | 500-800 | interpolation | classifier (names the class) | 1.000 [1.000, 1.000] | -0.009 (ok) | -0.010 (ok) |
| lambda-degradation | 1100-1400 | mild extrapolation | classifier (names the class) | 0.991 [0.973, 1.000] | 0.009 (ok) | -0.000 (ok) |
| payment-failure | 1500-2500 | interpolation | classifier (names the class) | 0.555 [0.491, 0.627] | 0.011 (ok) | 0.436 (degraded) |
| payment-failure | 3500-4500 | mild extrapolation | classifier (names the class) | 0.565 [0.470, 0.652] | -0.011 (ok) | 0.425 (degraded) |
| traffic-spike | 4-6 | interpolation | classifier (names the class) | 1.000 [1.000, 1.000] | -0.013 (ok) | -0.029 (ok) |
| traffic-spike | 8-10 | mild extrapolation | classifier (names the class) | 0.987 [0.967, 1.000] | 0.013 (ok) | -0.015 (ok) |
| db-throttling | 2.5-3.5 | interpolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| db-throttling | 4.5-5.5 | mild extrapolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| lambda-degradation | 500-800 | interpolation | classifier, read as a detector | 1.000 [1.000, 1.000] | -0.009 (ok) | -0.010 (ok) |
| lambda-degradation | 1100-1400 | mild extrapolation | classifier, read as a detector | 0.991 [0.973, 1.000] | 0.009 (ok) | -0.000 (ok) |
| payment-failure | 1500-2500 | interpolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) |
| payment-failure | 3500-4500 | mild extrapolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) |
| traffic-spike | 4-6 | interpolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) |
| traffic-spike | 8-10 | mild extrapolation | classifier, read as a detector | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) |
| db-throttling | 2.5-3.5 | interpolation | Model 0 (earlier mean, the stronger floor) | 0.526 [0.508, 0.546] | -0.005 (ok) | 0.014 (ok) |
| db-throttling | 4.5-5.5 | mild extrapolation | Model 0 (earlier mean, the stronger floor) | 0.522 [0.504, 0.541] | 0.005 (ok) | 0.018 (ok) |
| lambda-degradation | 500-800 | interpolation | Model 0 (earlier mean, the stronger floor) | 0.313 [0.270, 0.348] | -0.013 (ok) | -0.027 (ok) |
| lambda-degradation | 1100-1400 | mild extrapolation | Model 0 (earlier mean, the stronger floor) | 0.300 [0.264, 0.345] | 0.013 (ok) | -0.014 (ok) |
| payment-failure | 1500-2500 | interpolation | Model 0 (earlier mean, the stronger floor) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| payment-failure | 3500-4500 | mild extrapolation | Model 0 (earlier mean, the stronger floor) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| traffic-spike | 4-6 | interpolation | Model 0 (earlier mean, the stronger floor) | 0.007 [0.000, 0.020] | -0.007 (ok) | 0.043 (ok) |
| traffic-spike | 8-10 | mild extrapolation | Model 0 (earlier mean, the stronger floor) | 0.000 [0.000, 0.000] | 0.007 (ok) | 0.050 (ok) |
| db-throttling | 2.5-3.5 | interpolation | Model 0 (previous window) | 0.263 [0.254, 0.275] | -0.002 (ok) | 0.027 (ok) |
| db-throttling | 4.5-5.5 | mild extrapolation | Model 0 (previous window) | 0.261 [0.254, 0.270] | 0.002 (ok) | 0.029 (ok) |
| lambda-degradation | 500-800 | interpolation | Model 0 (previous window) | 0.113 [0.070, 0.157] | -0.013 (ok) | -0.027 (ok) |
| lambda-degradation | 1100-1400 | mild extrapolation | Model 0 (previous window) | 0.100 [0.055, 0.137] | 0.013 (ok) | -0.014 (ok) |
| payment-failure | 1500-2500 | interpolation | Model 0 (previous window) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| payment-failure | 3500-4500 | mild extrapolation | Model 0 (previous window) | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) |
| traffic-spike | 4-6 | interpolation | Model 0 (previous window) | 0.007 [0.000, 0.020] | -0.007 (ok) | 0.050 (ok) |
| traffic-spike | 8-10 | mild extrapolation | Model 0 (previous window) | 0.000 [0.000, 0.000] | 0.007 (ok) | 0.057 (ok) |

Model 0 and the live classifier only; the JSON's `per_band` has every model.

## 7. Extreme slice (reported apart)

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 5 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 5 | 0.500 [0.300, 0.700] | 0.650 [0.550, 0.750] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 5 | 0.200 [0.200, 0.200] | 0.400 [0.400, 0.400] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

## 8. Varied durations

**120 s faults (10 runs)**

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 3 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 3 | 0.333 [0.000, 0.500] | 0.833 [0.500, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.750 [0.500, 1.000] |

**720 s faults (10 runs)**

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 2 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 3 | 0.091 [0.091, 0.091] | 0.182 [0.182, 0.182] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 2 | 0.000 [0.000, 0.000] | 0.083 [0.083, 0.083] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.389 [0.250, 0.500] |

## 9. Confounders, read as detection

Share of the 143 both-faults-active windows (30 runs) each detector flags. A rule names no class, so the naming shares in section 4 have no Model 0 counterpart.

| Detector | Flagged share |
|---|---|
| iforest (isolation_forest) | 1.000 [1.000, 1.000] |
| binary (logistic_regression) | 1.000 [1.000, 1.000] |
| deployed rule (forest AND live classifier) | 1.000 [1.000, 1.000] |
| Model 0 (previous window) | 0.587 [0.425, 0.747] |
| Model 0 (earlier mean, the stronger floor) | 0.692 [0.571, 0.812] |
| classifier, read as a detector | 1.000 [1.000, 1.000] |
| xgboost (xgboost), read as a detector | 1.000 [1.000, 1.000] |
| xgboost_tuned (xgboost), read as a detector | 1.000 [1.000, 1.000] |
| random_forest (random_forest), read as a detector | 1.000 [1.000, 1.000] |

## What Model 0 cannot be compared on

- **Confounder naming shares** (either fault's type, the labeled first type, `CASCADING_FAILURE`): a rule names no class. The headline leaves confounders out; the JSON's `all_test` slice includes them.
- **Accuracy, macro-F1, per-class precision and time to recognize a cascade** all need a class name, so they are n/a for Model 0. The classifiers' values are in section 4.
- **ROC AUC, average precision and recall at a fixed false-alarm rate** need a score, and a rule has none.

## 10. Against M9-2's published numbers

| M9-2 result | This run (tolerance 1e-09) |
|---|---|
| `iforest` detection, headline | matches |
| `binary` detection, headline | matches |
| `deployed` detection, headline | matches |
| `incident_type` classification, headline | matches |
| `incident_type` confounder shares | matches |
