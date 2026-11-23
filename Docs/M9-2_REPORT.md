# M9-2 report: detection and classification on the test set

**Status:** scored on the held-out test set (slots 701-1050), with the extreme slice (1051-1070) reported apart.
**Owner:** Melisa · **Code:** `AnomalyPulse/ml/m9_2_metrics.py`, `AnomalyPulse/ml/m9_2_diagnostics.py` ·
**Results:** `AnomalyPulse/ml/m9_2_results/` · **Reads:** [`TEST_SET.md`](TEST_SET.md),
[`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md), [`DETECTION.md`](DETECTION.md),
[`M5-3_REPORT.md`](M5-3_REPORT.md)

## The short version

- **The deployed path catches almost every fault window.** Applied offline to the test set, the live rule (the forest
  flags a window and the classifier names a fault) has recall 0.999 and precision 0.956. It raises a false alarm on 18
  of 740 healthy windows (2.4%), against 0 of 336 in validation.
- **The Isolation Forest fails on this test set.** It flags all 2,280 test windows, so its perfect recall means
  nothing. The test set's shifted traffic mix moved the healthy baseline past the forest's fixed cut-off.
- **The classifier holds up across the held-out bands for three of the four fault types.** Accuracy is 0.910 and
  macro-F1 0.869.
- **Dependency failure is the exception.** Its recall is 0.54, against 0.99 in validation, because the shifted mix
  changes Order's median latency. Most of the misses are called `CASCADING_FAILURE`.
- **The claim this supports:** the system generalizes across held-out parameter bands of the simulator, but a change
  in workload alone can break the forest's cut-off and one class. It says nothing about real production incidents.

No model, cut-off or feature was changed after the test set was scored.

## What was scored

| Model | Package (training job) | model.joblib sha256 |
|---|---|---|
| Isolation Forest, the live detector | `anomalypulse-train-iforest-20261031T224214Z` | `7713f5bed5438ca5…` |
| Supervised binary detector, for comparison | `anomalypulse-train-binary-20261031T224214Z` | `b0a09fe95a4c32e2…` |
| Logistic regression, the live classifier | `anomalypulse-hpo-261028173159-003-28af89d1` | `4bfcdab0a08c92a9…` |

The **headline** is 320 test runs (2,092 labeled windows), the test set without its 30 confounders, which have no clean
single label. The same metrics for all 350 runs (2,280 windows) sit beside it. Intervals are 95% bootstrap intervals
over whole runs, because the windows of one run are not independent.

`deployed` is the live path's decision rule (`ml/diagnosis/handler.py`) applied to the stored windows. It is
window-level, so it isn't a count of live incidents, and it wasn't measured from the live Lambda.

## Detection

| Metric | Isolation Forest | Binary detector | Deployed rule |
|---|---|---|---|
| Precision | 0.596 | 0.967 [0.957, 0.976] | 0.956 [0.945, 0.966] |
| Recall | 1.000 | 0.999 [0.998, 1.000] | 0.999 [0.997, 1.000] |
| F1 | 0.747 | 0.983 [0.978, 0.987] | 0.977 [0.971, 0.982] |
| False-positive rate, against the labels | 1.000 | 0.051 [0.036, 0.068] | 0.069 [0.051, 0.088] |
| False-positive rate, healthy windows only | 1.000 | 0.000 | 0.024 |
| Median detection latency (s) | 70.4 | 70.4 [67.3, 73.6] | 70.6 [67.3, 74.0] |
| All 350 runs: precision / recall | 0.610 / 1.000 | 0.965 / 0.999 | 0.955 / 0.999 |

**Don't read the forest's column as a good result.** A detector that always says "anomaly" gets recall 1.000, catches
every run in its first labeled window, and has a precision equal to the share of windows that are faults
(1,247 of 2,092). Latency is dominated by the one-minute window, as designed.

## Classification

Accuracy **0.910** [0.892, 0.927], macro-F1 **0.869** [0.845, 0.890]. With all 350 runs: accuracy 0.872 [0.850, 0.896],
macro-F1 0.823 [0.796, 0.851].

| Class | Precision | Recall | F1 | Windows |
|---|---|---|---|---|
| `CASCADING_FAILURE` | 0.474 | 1.000 | 0.643 | 118 |
| `DATABASE_THROTTLING` | 1.000 | 1.000 | 1.000 | 264 |
| `DEPENDENCY_FAILURE` | 0.986 | 0.540 | 0.698 | 265 |
| `LAMBDA_DEGRADATION` | 0.992 | 0.989 | 0.990 | 262 |
| `NORMAL` | 0.999 | 0.931 | 0.964 | 845 |
| `TRAFFIC_SPIKE` | 0.862 | 0.982 | 0.918 | 338 |

122 of the 189 wrong windows are dependency failures called cascades, which is also why cascade precision is low. Another
53 are `NORMAL` windows called `TRAFFIC_SPIKE`: 35 are minutes in which a fault had just started, and 18 are healthy.

## Per-band recall (classifier)

| Fault | Lower test band | Upper test band | Validation |
|---|---|---|---|
| DB throttling (load ratio) | 2.5-3.5: 1.000 | 4.5-5.5: 1.000 | 1.000 |
| Lambda degradation (ms) | 500-800: 1.000 | 1100-1400: 0.991 | 0.990 |
| **Dependency failure (timeout ms)** | **1500-2500: 0.555** | **3500-4500: 0.565** | **0.990** |
| Traffic spike (multiplier) | 4-6: 1.000 | 8-10: 0.987 | 0.971 |

Both dependency bands are flagged as degraded against validation, but they match each other, so the band isn't what
hurts them (see below). The results files hold every model's table, with intervals.

## Other slices

- **Extreme slice (20 runs, reported separately).** Every model scores 1.000. These runs differ from the test set in two
  ways at once, more extreme faults and the default traffic mix, so they don't isolate the effect of severity.
- **Confounders (30 runs, 143 windows with both faults active).** The classifier calls 46.9% one of the two true types,
  34.3% the labeled first type, and 53.1% `CASCADING_FAILURE`. Traffic spike plus dependency failure is called a cascade
  every time. The model never saw a confounder in training, so this is a finding, not a data error
  ([`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md), "Confounders").
- **Varied durations (20 runs).** The classifier's recall is 1.000 on every fault except dependency failure: 0.750 for
  2-minute faults and 0.389 for 12-minute faults.
- **Cascades.** All 30 were recognized, with a median of 58.5 s from the start of the multi-fault state.

## Why the two failures happened

These diagnostics were worked out **after** scoring. They are offline evidence from the stored windows, not controlled
live experiments, and no number here is a result. `ml/m9_2_diagnostics.py` reproduces them
(`m9_2_results/diagnostics_m9_2.md`).

**The Isolation Forest.** The forest was trained on healthy windows in the default endpoint mix. The test set shifts that
mix on purpose (Control 3), so a healthy system looks different: Product goes from 15 to 13 requests per second, Cart from
2 to 3, Order from 3 to 4, Payment from 1 to 2, and Order's database writes double.

- Every healthy test window scores past the cut-off of -0.3695. The most normal one scores -0.394.
- It isn't the day. On 2026-11-16, the forest flagged 154 of 154 healthy windows in the shifted mix and 0 of 20 in the
  default mix.
- Replacing only the 22 request-rate and database-capacity columns with validation values takes the flagged share from
  100% to 6.1%.

**Dependency failure.** `POST /orders` is a third of Order's traffic in the default mix and half in the shifted mix. A
POST waits on Payment and a GET doesn't, so during a payment failure Order's median latency can now land on the slow
requests.

- In validation, no dependency window has Order's p50 above 100 ms. In the test set, 47% do.
- The classifier called 122 of those 125 windows a cascade, and 0 of the other 140.
- Replacing only `order_p50_latency_ms` with validation values takes dependency recall from 0.540 to 1.000. The same swap
  leaves the real cascades at 1.000.

## What this means for the other tickets

- **M9-3:** report the workload-shift weakness beside the band result. The band claim holds for three of four fault
  types, and dependency failure's drop is explained by the mix, not its bands.
- **M9-6:** use `m9_2_results/metrics_m9_2.json` for the figures, and don't plot the forest's recall or latency as a
  success.
- **M10:** the demo runs the default mix from the runner, where the forest works. Keep it that way
  ([`M10_Final_Demo.md`](../Milestones/M10_Final_Demo.md), M10-1 and M10-6).
- **Future work:** a real deployment must recalibrate the forest's cut-off when normal traffic changes. Any new cut-off
  or retrained model needs fresh data and a new held-out set, because this test set has now been seen.

## Reproducing

Run in the training image's library versions (Python 3.10, scikit-learn 1.5.2, numpy 2.1.0, scipy 1.15.0, joblib 1.6.0):

```bash
cd AnomalyPulse
python -m ml.m9_2_metrics --dataset ~/v1 \
    --classifier incident_type=~/pkgs/incident_type \
    --detector iforest=~/pkgs/iforest --detector binary=~/pkgs/binary \
    --job iforest=anomalypulse-train-iforest-20261031T224214Z \
    --job binary=anomalypulse-train-binary-20261031T224214Z \
    --job incident_type=anomalypulse-hpo-261028173159-003-28af89d1 \
    --with-validation-reference --out ~/m9-2-results
python -m ml.m9_2_diagnostics --dataset ~/v1 --classifier ~/pkgs/incident_type --detector ~/pkgs/iforest \
    --job detector=anomalypulse-train-iforest-20261031T224214Z \
    --job classifier=anomalypulse-hpo-261028173159-003-28af89d1 --out ~/m9-2-results
```

The first official run (2026-11-18) came from uncommitted code on Python 3.12. The committed results were regenerated
from a clean commit in the training image's versions. All 5,073 result values from the first run came out the same. The
only one removed is `recall_at_healthy_fpr_1`, which picked its cut-off from test windows.
