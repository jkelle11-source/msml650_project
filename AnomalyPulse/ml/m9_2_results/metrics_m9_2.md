# M9-2 Detection + classification metrics (test set)

## Read this first

- **`iforest` cannot tell healthy windows from faulty ones on this test set.** It flags 100.0% of healthy windows and 2280 of the 2280 test windows. Its recall, detection latency, per-band, confounder and varied-duration rows are what flagging everything produces, not evidence that it detects faults. Its cut-off was not retuned on test data; `Docs/M9-2_REPORT.md` explains the cause.

## Scope and provenance

- **Split:** test = slots 701–1050 (350 runs, 2280 labeled windows), all generated after 2026-10-25. The extreme slice (slots 1051+, 20 runs) is section 4 and is in no other number.
- **Headline:** 320 test runs, the test set without confounder (a test-only probe with no clean single label; section 5). The "all test runs" lines show the same metrics with them included. Test runs by kind: cascade 30, confounder 30, normal 60, single 230.
- **Intervals:** 95% percentile bootstrap, 1000 resamples of whole runs, seed 20261118. Windows of one run are correlated, so resampling windows would make every interval too narrow.
- **Code:** commit `ee9083bfdd52d98afe8b013df42c44551dfa0231`; generated 2026-11-23T06:55:59+00:00.
- **Libraries:** python 3.10.22, numpy 2.1.0, scikit-learn 1.5.2, scipy 1.15.0, joblib 1.6.0.
- **Package `iforest`:** isolation_forest (anomaly_iforest, threshold -0.3695), 106 columns; job `anomalypulse-train-iforest-20261031T224214Z`, model.joblib sha256 `7713f5bed5438ca5`; this run's libraries match its training image.
- **Package `binary`:** logistic_regression (anomaly_binary), 106 columns; job `anomalypulse-train-binary-20261031T224214Z`, model.joblib sha256 `b0a09fe95a4c32e2`; this run's libraries match its training image.
- **Package `incident_type`:** logistic_regression (incident_type), 106 columns; job `anomalypulse-hpo-261028173159-003-28af89d1`, model.joblib sha256 `4bfcdab0a08c92a9`; this run's libraries match its training image.

Definitions:

- *detection:* window-level; every label but NORMAL is ANOMALY (ml/job/detection.py). fpr counts any flagged NORMAL window; fpr_healthy only windows with no fault active; fault_start_flagged the NORMAL fault-start minutes flagged (faults caught early, which the labels count as false positives).
- *deployed:* the live path's rule applied offline to the stored labeled windows: the forest flags the window AND the classifier names a fault. Window-level, so not a count of live incidents, and a yes/no with no score, so it has no ROC AUC.
- *latency:* per incident run, from the fault's onset to the end of the first flagged labeled anomalous window; missed runs are counted apart and left out of the median.
- *classification:* labeled windows only, six-class; macro-F1 over classes in the truth or the predictions.
- *per band:* standard-length (300s) single-fault test runs; recall on the fault's windows; flagged when more than 10 percentage points below its reference.
- *confounder windows:* labeled non-NORMAL windows with multi_fault_ms >= 30000 (both faults active).

## 1. Detection (headline)

| Metric | iforest | binary | deployed |
|---|---|---|---|
| Precision | 0.596 [0.555, 0.634] | 0.967 [0.957, 0.976] | 0.956 [0.945, 0.966] |
| Recall | 1.000 [1.000, 1.000] | 0.999 [0.998, 1.000] | 0.999 [0.997, 1.000] |
| F1 | 0.747 [0.714, 0.776] | 0.983 [0.978, 0.987] | 0.977 [0.971, 0.982] |
| FPR, against the labels | 1.000 [1.000, 1.000] | 0.051 [0.036, 0.068] | 0.069 [0.051, 0.088] |
| FPR, healthy windows only | 1.000 | 0.000 | 0.024 |
| Fault-start windows flagged | 1.000 | 0.410 | 0.381 |
| ROC AUC | 0.947 | 1.000 | n/a |

Counts (windows): iforest: tp 1247, fp 845, fn 0, tn 0; binary: tp 1246, fp 43, fn 1, tn 802; deployed: tp 1246, fp 58, fn 1, tn 787.

⚠ `iforest`: these rows follow from flagging nearly every window (see "Read this first").

`deployed` is the live path's rule applied offline: the forest flags the window AND the classifier names a fault. It is window-level, so it is not a count of live incidents.

**Detection latency** (incident runs; from the fault's onset to the end of the first flagged labeled window):

| | iforest | binary | deployed |
|---|---|---|---|
| Incident runs | 260 | 260 | 260 |
| Detected | 260 | 260 | 260 |
| Missed | 0 | 0 | 0 |
| Median (s) [95% CI] | 70.4 [67.3, 74.0] | 70.4 [67.3, 73.6] | 70.6 [67.3, 74.0] |
| p90 (s) | 93.8 | 94.3 | 93.8 |
| Max (s) | 119.8 | 145.5 | 119.8 |
| Caught in first labeled window | 1.000 | 0.996 | 0.996 |

Latency is window-dominated by design (one-minute windows; `Docs/DETECTION.md`). Missed runs are not in the median.

**Share flagged, by label** (NORMAL = FPR):

| Label | iforest | binary | deployed |
|---|---|---|---|
| CASCADING_FAILURE | 1.000 | 0.992 | 1.000 |
| DATABASE_THROTTLING | 1.000 | 1.000 | 1.000 |
| DEPENDENCY_FAILURE | 1.000 | 1.000 | 1.000 |
| LAMBDA_DEGRADATION | 1.000 | 1.000 | 0.996 |
| NORMAL | 1.000 | 0.051 | 0.069 |
| TRAFFIC_SPIKE | 1.000 | 1.000 | 1.000 |

**All test runs** (350 runs, confounders included, each scored against its labeled first fault):

| Metric | iforest | binary | deployed |
|---|---|---|---|
| Precision | 0.610 [0.574, 0.646] | 0.965 [0.957, 0.973] | 0.955 [0.944, 0.964] |
| Recall | 1.000 [1.000, 1.000] | 0.999 [0.998, 1.000] | 0.999 [0.998, 1.000] |
| F1 | 0.757 [0.729, 0.785] | 0.982 [0.977, 0.986] | 0.976 [0.971, 0.981] |
| FPR, against the labels | 1.000 [1.000, 1.000] | 0.056 [0.041, 0.073] | 0.074 [0.058, 0.095] |
| FPR, healthy windows only | 1.000 | 0.000 | 0.023 |

## 2. Classification (headline): `incident_type`

- Accuracy 0.910 [0.892, 0.927]; macro-F1 0.869 [0.845, 0.890] (320 runs, 2092 windows).
- All test runs, confounders included: accuracy 0.872 [0.850, 0.896]; macro-F1 0.823 [0.796, 0.851] (350 runs, 2280 windows).

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| CASCADING_FAILURE | 0.474 | 1.000 | 0.643 | 118 |
| DATABASE_THROTTLING | 1.000 | 1.000 | 1.000 | 264 |
| DEPENDENCY_FAILURE | 0.986 | 0.540 | 0.698 | 265 |
| LAMBDA_DEGRADATION | 0.992 | 0.989 | 0.990 | 262 |
| NORMAL | 0.999 | 0.931 | 0.964 | 845 |
| TRAFFIC_SPIKE | 0.862 | 0.982 | 0.918 | 338 |

Confusion matrix (rows true, columns predicted; order: CASCADING_FAILURE, DATABASE_THROTTLING, DEPENDENCY_FAILURE, LAMBDA_DEGRADATION, NORMAL, TRAFFIC_SPIKE):

```
CASCADING_FAILURE      118     0     0     0     0     0
DATABASE_THROTTLING      0   264     0     0     0     0
DEPENDENCY_FAILURE     122     0   143     0     0     0
LAMBDA_DEGRADATION       2     0     0   259     1     0
NORMAL                   1     0     2     2   787    53
TRAFFIC_SPIKE            6     0     0     0     0   332
```

Cascades: 30 runs; time to recognize median 58.5 s, max 113.5 s, never recognized in 0 (`ml/evaluate.py`'s definition).

## 3. Per-band breakdown (held-out test bands)

Recall on each injector's fault windows, 300-second single-fault runs only. Each injector has two test bands: the lower lies between training bands (interpolation), the upper just above the highest training band (mild extrapolation; `Docs/EVALUATION_INTEGRITY.md`, "An honest caveat"). A band is flagged when its recall is more than 10 percentage points below its reference: *possible* if the intervals overlap, *degraded* if they do not. References: the injector's other test band, and validation (training bands; optimistic, since validation tuned the packages). Every test band also runs in the shifted endpoint mix, so a gap to validation can come from the mix rather than the band.

⚠ `iforest`: these rows follow from flagging nearly every window (see "Read this first").


**detection: `binary`**

| Injector | Band | Role | Runs | Windows | Recall [95% CI] | vs other band | vs validation | Median latency (s) |
|---|---|---|---|---|---|---|---|---|
| db-throttling | 2.5–3.5 | interpolation | 30 | 114 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 89.2 |
| db-throttling | 4.5–5.5 | mild extrapolation | 30 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 87.1 |
| lambda-degradation | 500–800 | interpolation | 23 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 61.1 |
| lambda-degradation | 1100–1400 | mild extrapolation | 22 | 110 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 64.8 |
| payment-failure | 1500–2500 | interpolation | 22 | 110 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 65.4 |
| payment-failure | 3500–4500 | mild extrapolation | 23 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 62.2 |
| traffic-spike | 4–6 | interpolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) | 63.7 |
| traffic-spike | 8–10 | mild extrapolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) | 63.7 |

**detection: `deployed`**

| Injector | Band | Role | Runs | Windows | Recall [95% CI] | vs other band | vs validation | Median latency (s) |
|---|---|---|---|---|---|---|---|---|
| db-throttling | 2.5–3.5 | interpolation | 30 | 114 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 89.2 |
| db-throttling | 4.5–5.5 | mild extrapolation | 30 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 87.1 |
| lambda-degradation | 500–800 | interpolation | 23 | 115 | 1.000 [1.000, 1.000] | -0.009 (ok) | -0.010 (ok) | 61.1 |
| lambda-degradation | 1100–1400 | mild extrapolation | 22 | 110 | 0.991 [0.973, 1.000] | 0.009 (ok) | -0.000 (ok) | 65.8 |
| payment-failure | 1500–2500 | interpolation | 22 | 110 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 65.4 |
| payment-failure | 3500–4500 | mild extrapolation | 23 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.010 (ok) | 62.2 |
| traffic-spike | 4–6 | interpolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) | 63.7 |
| traffic-spike | 8–10 | mild extrapolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | -0.029 (ok) | 63.7 |

**detection: `iforest`**

| Injector | Band | Role | Runs | Windows | Recall [95% CI] | vs other band | vs validation | Median latency (s) |
|---|---|---|---|---|---|---|---|---|
| db-throttling | 2.5–3.5 | interpolation | 30 | 114 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 89.2 |
| db-throttling | 4.5–5.5 | mild extrapolation | 30 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 87.1 |
| lambda-degradation | 500–800 | interpolation | 23 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 61.1 |
| lambda-degradation | 1100–1400 | mild extrapolation | 22 | 110 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 64.8 |
| payment-failure | 1500–2500 | interpolation | 22 | 110 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 65.4 |
| payment-failure | 3500–4500 | mild extrapolation | 23 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 62.2 |
| traffic-spike | 4–6 | interpolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 63.7 |
| traffic-spike | 8–10 | mild extrapolation | 30 | 150 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | 63.7 |

**classification: `incident_type`**

| Injector | Band | Role | Runs | Windows | Recall [95% CI] | vs other band | vs validation | Most common wrong call |
|---|---|---|---|---|---|---|---|---|
| db-throttling | 2.5–3.5 | interpolation | 30 | 114 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | none |
| db-throttling | 4.5–5.5 | mild extrapolation | 30 | 115 | 1.000 [1.000, 1.000] | 0.000 (ok) | 0.000 (ok) | none |
| lambda-degradation | 500–800 | interpolation | 23 | 115 | 1.000 [1.000, 1.000] | -0.009 (ok) | -0.010 (ok) | none |
| lambda-degradation | 1100–1400 | mild extrapolation | 22 | 110 | 0.991 [0.973, 1.000] | 0.009 (ok) | -0.000 (ok) | NORMAL ×1 |
| payment-failure | 1500–2500 | interpolation | 22 | 110 | 0.555 [0.491, 0.627] | 0.011 (ok) | 0.436 (degraded) | CASCADING_FAILURE ×49 |
| payment-failure | 3500–4500 | mild extrapolation | 23 | 115 | 0.565 [0.470, 0.652] | -0.011 (ok) | 0.425 (degraded) | CASCADING_FAILURE ×50 |
| traffic-spike | 4–6 | interpolation | 30 | 150 | 1.000 [1.000, 1.000] | -0.013 (ok) | -0.029 (ok) | none |
| traffic-spike | 8–10 | mild extrapolation | 30 | 150 | 0.987 [0.967, 1.000] | 0.013 (ok) | -0.015 (ok) | CASCADING_FAILURE ×2 |

Gaps are reference recall minus band recall (positive = the band is worse).

**Flagged bands:** incident_type payment-failure 1500–2500 (classification, degraded vs validation); incident_type payment-failure 3500–4500 (classification, degraded vs validation). Report these as findings; do not tune around them.

## 4. Extreme-extrapolation slice (reported separately; not in any number above)

20 runs (traffic >10×, latency >1500 ms, load >6×, timeout >5000 ms), the default endpoint mix. More extreme incidents are easier, so these numbers say nothing about interpolation and must not be averaged into the headline. With about five runs per injector the intervals are wide. These runs differ from the test set in two ways at once, more extreme faults and the default endpoint mix instead of the shifted one, so comparing them with the headline does not isolate the effect of severity.

- `iforest` detection: precision 1.000, recall 1.000 [1.000, 1.000], F1 1.000, FPR 0.000; latency median 59.8 s, missed runs 0.
- `binary` detection: precision 1.000, recall 1.000 [1.000, 1.000], F1 1.000, FPR 0.000; latency median 59.8 s, missed runs 0.
- `deployed` detection: precision 1.000, recall 1.000 [1.000, 1.000], F1 1.000, FPR 0.000; latency median 59.8 s, missed runs 0.
- `incident_type` classification: accuracy 1.000 [1.000, 1.000], macro-F1 1.000.

| Injector | Runs | detection iforest recall | detection binary recall | detection deployed recall | classification incident_type recall |
|---|---|---|---|---|---|
| traffic-spike | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 5 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

## 5. Confounders (a test-only probe)

30 runs: two unrelated faults over the same window, labeled with the first one's type, never CASCADING_FAILURE. The model never sees one in training, and every training example with two faults active is a cascade, so a high CASCADING_FAILURE share is a finding about what the model learned, not a data error (`Docs/EVALUATION_INTEGRITY.md`, "Confounders"). Windows scored: 143 with both faults active (multi_fault_ms ≥ 30 s). The shares overlap by design (the labeled type is one of the two), so they do not sum to 1; "neither" is everything else, NORMAL included.

⚠ `iforest`: these rows follow from flagging nearly every window (see "Read this first").

**`incident_type`**, 143 windows from 30 runs:

| Predicted as | Share [95% CI] | All fault-labeled windows (sensitivity) |
|---|---|---|
| either fault's type | 0.469 [0.317, 0.615] | 0.469 |
| the labeled (first) type | 0.343 [0.212, 0.486] | 0.343 |
| CASCADING_FAILURE | 0.531 [0.378, 0.678] | 0.531 |
| neither (other / NORMAL) | 0.000 [0.000, 0.000] | 0.000 |

Predicted classes: CASCADING_FAILURE 76, LAMBDA_DEGRADATION 31, TRAFFIC_SPIKE 24, DATABASE_THROTTLING 9, DEPENDENCY_FAILURE 3.

| Pair (first + second) | Runs | Windows | Either | First | CASCADING | Neither |
|---|---|---|---|---|---|---|
| DATABASE_THROTTLING+LAMBDA_DEGRADATION | 7 | 28 | 0.821 | 0.321 | 0.179 | 0.000 |
| LAMBDA_DEGRADATION+DEPENDENCY_FAILURE | 7 | 35 | 0.543 | 0.457 | 0.457 | 0.000 |
| TRAFFIC_SPIKE+DEPENDENCY_FAILURE | 8 | 40 | 0.000 | 0.000 | 1.000 | 0.000 |
| TRAFFIC_SPIKE+LAMBDA_DEGRADATION | 8 | 40 | 0.625 | 0.600 | 0.375 | 0.000 |

`iforest` flagged 1.000 [1.000, 1.000] of the both-active windows as anomalous.
`binary` flagged 1.000 [1.000, 1.000] of the both-active windows as anomalous.
`deployed` flagged 1.000 [1.000, 1.000] of the both-active windows as anomalous.

## 6. Varied-duration faults (2 and 12 minutes)

⚠ `iforest`: these rows follow from flagging nearly every window (see "Read this first").

**2-minute faults**, 10 runs (per injector):

| Injector | Runs | detection iforest recall | detection binary recall | detection deployed recall | classification incident_type recall |
|---|---|---|---|---|---|
| traffic-spike | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.750 [0.500, 1.000] |

**12-minute faults**, 10 runs (per injector):

| Injector | Runs | detection iforest recall | detection binary recall | detection deployed recall | classification incident_type recall |
|---|---|---|---|---|---|
| traffic-spike | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| db-throttling | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| lambda-degradation | 2 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| payment-failure | 3 | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.389 [0.250, 0.500] |

## Reading these numbers

- Everything is window-level, on labeled windows only: onset, recovery, startup and partial windows are excluded by the labeler.
- Bands are the simulator's held-out *parameter regions*. The claim is generalization across them, not to production incidents (`Docs/EVALUATION_INTEGRITY.md`).
- DB throttling's bands are separated by throttle counts, not latency (the retry ladder sets Product's p95), so read its per-band rows with that in mind.
- The Isolation Forest's cut-off is the package's, set on validation; nothing was fitted or tuned on the test set.
- Why the forest and the dependency-failure results look as they do is diagnosed separately, after scoring (`Docs/M9-2_REPORT.md`).
- The threshold baseline sits beside these numbers in M9-3.
