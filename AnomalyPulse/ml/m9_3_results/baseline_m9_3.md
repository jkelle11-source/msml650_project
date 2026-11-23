# M9-3 The threshold baseline beside the test-set results

## Read this first

- **Model 0 is a rule.** It says anomaly or not, with no class and no score, so accuracy, macro-F1, ROC AUC, average precision and recall at a fixed false-alarm rate are n/a for it. It is compared on the anomaly view (anything but NORMAL is an anomaly). The classifier is read the same way (`classifier, read as a detector`) for a like-for-like margin.
- **Frozen before it touched the test set.** Model 0 is `ml/model0.py` as reviewed in M5-3: the rule trips on p95 above 2.0x its baseline or a 5xx share above 5%, against the previous window or the mean of the last 3 earlier windows. Not tuned. M9-2 had already scored and seen the test set, so every result is reported whichever way it falls.
- **Only the live classifier was scored on the test set** (M9-2). The XGBoost and Random Forest of M5-3 are not here.
- **Margins are paired:** both detectors are resampled on the same runs, so a margin's interval is narrower than two separate intervals suggest.
- **The test set changes the workload on purpose,** so a change from validation to test mixes the held-out bands with the shifted traffic mix. M9-2's diagnostics attribute dependency failure's drop to the mix (`Docs/M9-2_REPORT.md`). Nothing here says which cause moved Model 0.
- **No claim about real production incidents:** these results support generalization across held-out regions of the simulator's parameter space only.

## Scope

- **Headline:** 320 test runs (2092 windows), the test set without its confounders. Stress slice: 20 runs, reported apart. Validation: 140 runs (885 windows).
- **Intervals:** 95% percentile bootstrap, 1000 resamples of whole runs, seed 20261118.
- **Classifier:** logistic_regression (incident_type), job `anomalypulse-hpo-261028173159-003-28af89d1`.
- **Code:** commit `f7ae0bf469904d2dfd6a09425ae966ea5836e233` (dirty: None); generated 2026-11-23T23:33:25+00:00.

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

Classification (the classifier alone; Model 0 cannot name a class): accuracy 0.910 [0.892, 0.927], macro-F1 0.869 [0.845, 0.890]; Model 0: n/a.

### The margin

| Classifier minus Model 0 | against the earlier-mean floor | against the previous-window floor |
|---|---|---|
| Precision | 0.040 [0.018, 0.060] | 0.060 [0.035, 0.087] |
| Recall | 0.528 [0.479, 0.576] | 0.626 [0.576, 0.679] |
| F1 | 0.354 [0.310, 0.400] | 0.450 [0.400, 0.507] |
| False-positive rate (lower is better) | 0.005 [-0.017, 0.029] | 0.005 [-0.019, 0.029] |

A positive recall difference means the classifier finds anomalous windows Model 0 misses. A positive false-positive-rate difference means the classifier raises more false alarms.

## 2. Share of each class flagged (headline)

| Label | Windows | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier recall (names it) | classifier precision |
|---|---|---|---|---|---|---|
| CASCADING_FAILURE | 118 | 0.839 [0.733, 0.926] | 0.907 [0.846, 0.962] | 1.000 [1.000, 1.000] | 1.000 | 0.474 |
| DATABASE_THROTTLING | 264 | 0.246 [0.223, 0.269] | 0.485 [0.439, 0.532] | 1.000 [1.000, 1.000] | 1.000 | 1.000 |
| DEPENDENCY_FAILURE | 265 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.540 | 0.986 |
| LAMBDA_DEGRADATION | 262 | 0.118 [0.086, 0.153] | 0.317 [0.274, 0.363] | 0.996 [0.987, 1.000] | 0.989 | 0.992 |
| NORMAL (= false-alarm rate) | 845 | 0.064 [0.047, 0.083] | 0.064 [0.047, 0.084] | 0.069 [0.051, 0.086] | 0.931 | 0.999 |
| TRAFFIC_SPIKE | 338 | 0.015 [0.003, 0.030] | 0.015 [0.003, 0.029] | 1.000 [1.000, 1.000] | 0.982 | 0.862 |

For `CASCADING_FAILURE` the margin is not the classifier's recall of 1.000 alone: M9-2 shows its precision is low because dependency failures are called cascades.

## 3. Validation to test: the shift in each detector

| Detector | Recall, validation | Recall, test | FPR, validation | FPR, test |
|---|---|---|---|---|
| Model 0 (previous window) | 0.392 [0.309, 0.467] | 0.373 [0.320, 0.424] | 0.061 [0.039, 0.092] | 0.064 [0.047, 0.084] |
| Model 0 (earlier mean, the stronger floor) | 0.488 [0.421, 0.563] | 0.472 [0.419, 0.521] | 0.061 [0.039, 0.091] | 0.064 [0.047, 0.082] |
| classifier, read as a detector | 0.986 [0.976, 0.995] | 0.999 [0.997, 1.000] | 0.005 [0.000, 0.014] | 0.069 [0.052, 0.089] |

Classifier, six-class: accuracy 0.990 [0.983, 0.996] on validation and 0.910 [0.892, 0.927] on test; macro-F1 0.992 [0.987, 0.997] and 0.869 [0.845, 0.890]. Validation tuned the packages, so it is the optimistic side of any gap.

## 4. Per held-out band

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

## 5. Extreme slice (reported apart)

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 5 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 5 | 0.200 [0.200, 0.200] | 0.400 [0.400, 0.400] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 5 | 0.500 [0.300, 0.700] | 0.650 [0.550, 0.750] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

## 6. Varied durations

**120 s faults (10 runs)**

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 3 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 3 | 0.333 [0.000, 0.500] | 0.833 [0.500, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.750 [0.500, 1.000] |

**720 s faults (10 runs)**

| Fault | Runs | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | classifier, read as a detector | classifier names the class |
|---|---|---|---|---|---|
| traffic-spike | 2 | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 2 | 0.000 [0.000, 0.000] | 0.083 [0.083, 0.083] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 3 | 0.091 [0.091, 0.091] | 0.182 [0.182, 0.182] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.389 [0.250, 0.500] |

## What Model 0 cannot be compared on

- **Confounders (30 runs).** M9-2 scores them by which class the classifier names (either fault's type, the labeled first type, or `CASCADING_FAILURE`). A rule names no class, so there is nothing to put beside them. The headline leaves them out; the JSON's `all_test` slice includes them in the anomaly view only.
- **Time to recognize a cascade, accuracy, macro-F1 and per-class precision** all need a class name, so they are n/a for Model 0.
- **ROC AUC, average precision and recall at a fixed false-alarm rate** need a score, and a rule has none.
- **XGBoost and Random Forest** were not scored on the test set (M9-2 scored only the live classifier).

## 7. M9-2's detectors, for reference (headline)

| Detector | Precision | Recall | F1 | FPR |
|---|---|---|---|---|
| iforest | 0.596 [0.555, 0.634] | 1.000 [1.000, 1.000] | 0.747 [0.714, 0.776] | 1.000 [1.000, 1.000] |
| binary | 0.967 [0.957, 0.976] | 0.999 [0.998, 1.000] | 0.983 [0.978, 0.987] | 0.051 [0.036, 0.068] |
| deployed | 0.956 [0.945, 0.966] | 0.999 [0.997, 1.000] | 0.977 [0.971, 0.982] | 0.069 [0.051, 0.088] |

From `ml/m9_2_results/metrics_m9_2.json`, not rescored here. The Isolation Forest flags every test window, so its recall and latency are not evidence that it detects faults (`Docs/M9-2_REPORT.md`).
