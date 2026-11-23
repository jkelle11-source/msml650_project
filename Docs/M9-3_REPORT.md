# M9-3 report: the threshold baseline beside the test-set results, and the evaluation-integrity statement

**Status:** DRAFT for review. Test-set numbers are from `AnomalyPulse/ml/m9_3_results/baseline_m9_3.json` and `.md`
(commit `f7ae0bf469904d2dfd6a09425ae966ea5836e233`, seed 20261118, 1000 bootstrap resamples over whole runs). M9-2's
numbers are from [`M9-2_REPORT.md`](M9-2_REPORT.md). **Owner:** Rachel · **Code:** `AnomalyPulse/ml/m9_3_baseline.py`,
`m9_3_report.py` · **Reads:** [`TEST_SET.md`](TEST_SET.md), [`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md),
[`M5-3_REPORT.md`](M5-3_REPORT.md)

## The short version

- **The classifier finds almost every anomalous window; the threshold rule finds fewer than half.** Over the headline
  (320 test runs, 2,092 windows) the classifier, read as a detector, has recall 0.999 [0.997, 1.000]. Model 0's
  stronger floor has 0.472 [0.419, 0.521]. The paired margin is +0.528 [0.479, 0.576].
- **The classifier's advantage is in what it finds, not in fewer false alarms.** The false-positive-rate margin is
  +0.005 [-0.017, 0.029], so the two are not measurably different (0.069 and 0.064). On healthy windows alone, Model 0
  never fires and the classifier fires on 2.4%.
- **Model 0 does as well as the classifier on one fault and almost nothing on another.** It flags every dependency
  failure window (a failing payment trips its absolute error rule) and 0.0% to 0.7% of traffic-spike windows (a spike
  raises volume, not latency or errors).
- **On cascades the detection margin is small.** Model 0 flags 0.907 [0.846, 0.962] of cascade windows against 1.000.
  The classifier's real contribution there is naming the class, and its naming is flawed on this test set
  (precision 0.474).
- **The train/test gap is large for the classifier and absent for Model 0.** Six-class accuracy falls from 0.990 to
  0.910 and macro-F1 from 0.992 to 0.869. Model 0's recall and false-alarm rate barely move. Every test run uses a
  shifted traffic mix, so the gap mixes held-out bands with that shift; M9-2's diagnostics point at the mix.
- **The claim this supports:** the system generalizes across held-out parameter bands of the simulator, with a
  workload-shift weakness, and says nothing about real production incidents.

## What was compared

| | |
|---|---|
| **Model 0 (threshold rule)** | `ml/model0.py` as reviewed in M5-3, not tuned. A window is flagged if any service has p95 above 2x its baseline or a 5xx share above 5%. Two baselines: the previous retained window, and the mean of the 3 earlier lookback windows of the same run (the stronger floor). |
| **Classifier** | The live one, logistic regression, job `anomalypulse-hpo-261028173159-003-28af89d1`, the only classifier M9-2 scored on the test set. Read as a detector it flags every window it does not call `NORMAL`. |
| **Data** | The test set, slots 701-1050; headline = 320 runs without the 30 confounders; the extreme slice (slots 1051-1070) apart; validation (slots 561-700) as the reference. |
| **Intervals** | 95% percentile bootstrap over whole runs. A margin resamples the same runs for both detectors (paired), so its interval is narrower than two separate intervals suggest. |

Model 0 was frozen before it touched the test set. M9-2 had already scored and seen the test set, so nothing was
adjusted after any number was seen, and every result is reported whichever way it falls. A rule has no class and no
score, so accuracy, macro-F1, per-class precision, ROC AUC, average precision, recall at a fixed false-alarm rate,
confounder shares and time to recognize a cascade are n/a for it.

## Signal 1: the margin over the baseline (multivariate learning)

Anomaly view, headline (anything but `NORMAL` is an anomaly):

| Metric | Model 0 (previous window) | Model 0 (earlier mean, the stronger floor) | Classifier, read as a detector |
|---|---|---|---|
| Precision | 0.896 [0.868, 0.918] | 0.916 [0.897, 0.935] | 0.956 [0.944, 0.965] |
| Recall | 0.373 [0.320, 0.424] | 0.472 [0.419, 0.521] | 0.999 [0.997, 1.000] |
| F1 | 0.527 [0.468, 0.577] | 0.623 [0.574, 0.665] | 0.977 [0.971, 0.982] |
| False-positive rate, against the labels | 0.064 [0.047, 0.084] | 0.064 [0.047, 0.082] | 0.069 [0.052, 0.089] |
| False-positive rate, healthy windows only | 0.000 | 0.000 | 0.024 |
| Fault-start windows flagged | 0.514 | 0.514 | 0.381 |
| Median detection latency, s | 73.2 [69.1, 77.3] | 74.2 [70.7, 77.3] | 70.6 [67.1, 73.8] |
| Incident runs never flagged | 88 of 260 | 64 of 260 | 0 of 260 |

The latency median covers only the runs a detector flagged at all, so Model 0's latency looks competitive. The last
row is the correction: it never flags 64 to 88 of the 260 incident runs. Model 0's false alarms come from fault-start
minutes (the labels call them `NORMAL`), not from healthy traffic.

Paired margin, classifier minus Model 0 (earlier-mean floor): precision +0.040 [0.018, 0.060]; recall +0.528
[0.479, 0.576]; F1 +0.354 [0.310, 0.400]; false-positive rate +0.005 [-0.017, 0.029]. Against the previous-window
floor: recall +0.626 [0.576, 0.679].

Share of each class flagged (headline):

| Class | Windows | Model 0 (previous window) | Model 0 (earlier mean) | Classifier as a detector | Classifier names it (recall / precision) |
|---|---|---|---|---|---|
| `CASCADING_FAILURE` | 118 | 0.839 [0.733, 0.926] | 0.907 [0.846, 0.962] | 1.000 | 1.000 / 0.474 |
| `DATABASE_THROTTLING` | 264 | 0.246 [0.223, 0.269] | 0.485 [0.439, 0.532] | 1.000 | 1.000 / 1.000 |
| `DEPENDENCY_FAILURE` | 265 | 1.000 | 1.000 | 1.000 | 0.540 / 0.986 |
| `LAMBDA_DEGRADATION` | 262 | 0.118 [0.086, 0.153] | 0.317 [0.274, 0.363] | 0.996 [0.987, 1.000] | 0.989 / 0.992 |
| `NORMAL` (the false-alarm rate) | 845 | 0.064 [0.047, 0.083] | 0.064 [0.047, 0.084] | 0.069 [0.051, 0.086] | 0.931 / 0.999 |
| `TRAFFIC_SPIKE` | 338 | 0.015 [0.003, 0.030] | 0.015 [0.003, 0.029] | 1.000 | 0.982 / 0.862 |

- **Traffic spike, throttling, Lambda degradation:** this is where the classifier learns something a rule on latency
  and errors cannot. Spikes are nearly invisible to the rule.
- **Dependency failure:** the rule flags all of it, so the classifier's advantage is naming it, and that is its weak
  class (recall 0.540; 122 of its misses are called cascades).
- **Cascades:** the detection difference is 0.093 (earlier-mean floor) to 0.161 (previous window), a difference of two
  shares, not a paired test. The classifier flags every cascade window, but its precision for `CASCADING_FAILURE` is
  0.474. Model 0 cannot name a cascade, so it has no time to recognize and no confounder shares.

## Signal 2: the train/test gap (overfitting to simulator artifacts)

| | Validation | Test (headline) |
|---|---|---|
| Classifier, six-class accuracy | 0.990 [0.983, 0.996] | 0.910 [0.892, 0.927] |
| Classifier, six-class macro-F1 | 0.992 [0.987, 0.997] | 0.869 [0.845, 0.890] |
| Classifier as a detector: recall | 0.986 [0.976, 0.995] | 0.999 [0.997, 1.000] |
| Classifier as a detector: false-positive rate | 0.005 [0.000, 0.014] | 0.069 [0.052, 0.089] |
| Model 0 (earlier mean): recall | 0.488 [0.421, 0.563] | 0.472 [0.419, 0.521] |
| Model 0 (earlier mean): false-positive rate | 0.061 [0.039, 0.091] | 0.064 [0.047, 0.082] |

Validation tuned the classifier's regularization, so it is the optimistic side of any gap. The classifier's clear
false-alarm advantage on validation (0.005 against 0.061) is gone on the test set.

**Per held-out band.** For the classifier, recall in the upper band is within 0.013 of the lower band for every fault.
Against validation, only dependency failure's naming is flagged as degraded (0.555 and 0.565 against 0.99), and both
of its bands score alike, so the band is not what hurts it. Model 0's per-band recall is also flat between bands
(for example payment failure 1.000 in both bands, throttling 0.526 and 0.522).

**What this does and does not separate.** Every test run uses the shifted endpoint mix, so the gap above combines the
held-out parameter bands with the workload shift. M9-2's diagnostics (worked out after scoring, so offline evidence,
not a controlled experiment) show that replacing only Order's median latency with validation values takes dependency
recall from 0.540 to 1.000, which points at the mix. The 20-run extreme slice uses the default mix but also more extreme
faults, so it does not isolate either cause. Nothing here says why Model 0 moved less.

## M9-2's other detectors beside Model 0

Headline (320 runs). Model 0's rows are from this report's run; M9-2's three rows are copied from
`ml/m9_2_results/metrics_m9_2.json` and were not rescored here.

| Detector | Precision | Recall | F1 | False-positive rate |
|---|---|---|---|---|
| Model 0 (previous window) | 0.896 [0.868, 0.918] | 0.373 [0.320, 0.424] | 0.527 [0.468, 0.577] | 0.064 [0.047, 0.084] |
| Model 0 (earlier mean, the stronger floor) | 0.916 [0.897, 0.935] | 0.472 [0.419, 0.521] | 0.623 [0.574, 0.665] | 0.064 [0.047, 0.082] |
| Isolation Forest (M9-2) | 0.596 [0.555, 0.634] | 1.000 [1.000, 1.000] | 0.747 [0.714, 0.776] | 1.000 [1.000, 1.000] |
| Binary detector (M9-2) | 0.967 [0.957, 0.976] | 0.999 [0.998, 1.000] | 0.983 [0.978, 0.987] | 0.051 [0.036, 0.068] |
| Deployed rule: forest AND classifier (M9-2) | 0.956 [0.945, 0.966] | 0.999 [0.997, 1.000] | 0.977 [0.971, 0.982] | 0.069 [0.051, 0.088] |

- **The Isolation Forest has no margin to report.** It flags every test window, so its recall of 1.000 and its
  false-positive rate of 1.000 are what flagging everything produces (`M9-2_REPORT.md`). Its precision of 0.596 is the share of windows that are
  faults, which is what flagging everything gives (`M9-2_REPORT.md`); it is below Model 0's.
- **The binary detector and the deployed rule** have recall near 0.999 against Model 0's 0.472, with intervals far
  apart, and false-positive rates that overlap Model 0's. These margins are read from two separate intervals, not
  paired, because their packages were not scored again for this report, so only a difference between non-overlapping
  intervals should be read as a difference.

## Slices reported apart

- **Extreme slice (20 runs, 5 per fault).** The classifier scores 1.000 everywhere. Model 0 scores 0.000 on spikes,
  0.200 to 0.400 on Lambda degradation, 0.500 to 0.650 on throttling, and 1.000 on payment failure.
- **Varied durations (10 runs of 2 minutes, 10 of 12).** Model 0's recall on sustained faults drops, for example
  Lambda degradation 0.833 at 2 minutes and 0.083 at 12, throttling 1.000 and 0.182. That fits how the stronger floor
  is built (a sustained fault becomes its own baseline), but this report does not test that cause. The classifier
  keeps 1.000 as a detector; its naming of dependency failure falls to 0.750 and 0.389.
- **Confounders (30 runs).** Scored by M9-2 by which class the classifier names: 46.9% one of the two true types,
  34.3% the labeled first type, 53.1% `CASCADING_FAILURE`. A rule names no class, so there is no Model 0 counterpart.
- **Groups of 2 to 5 runs** (the extreme slice and the varied durations) show intervals such as [1.000, 1.000]. That
  is not precision: every run in the group scored the same, so resampling changes nothing.

## The Evaluation Integrity Statement (finalized)

> Because the training and test datasets are both generated by the same incident simulator, we explicitly control for
> data leakage using three mechanisms. (1) **Interleaved parameter-space partitioning:** every incident type's
> magnitude axis is split into alternating training and test bands, checked automatically, so test incidents use
> magnitudes never seen in training; intensities avoid the bands' shared edges, so no experiment's magnitude appears
> in both sets. (2) **Strict temporal splitting:** experiments 1-700 (train and validation) were generated first and
> the test set (701-1050) on later days (2026-11-13 to 11-16, after training's last day, 2026-10-25), so it carries
> the AWS environment's day-to-day variation; this is enforced when the data is generated and checked again when it
> is used. (3) **A simulator diversity protocol:** the test set shifts the endpoint mix, varies incident duration
> (2 and 12 minutes against training's 5), and adds confounders, two incident types co-occurring without being
> labeled a cascade. A strict Feature/Label Boundary (plan §3) keeps near-label fields out of the model input.
>
> These controls support a bounded claim. Scored on the held-out test set, the classifier detects nearly every
> anomalous window and beats a threshold baseline by a wide margin in recall, and its recall holds across the held-out
> parameter bands for three of the four fault types. They do not make the system robust to a change in workload: the
> test set's shifted traffic mix alone moved the healthy baseline past the Isolation Forest's fixed cut-off (the
> forest flags every test window) and lowered the classifier's recall for dependency failure from 0.99 on validation
> to 0.54, mostly by calling those windows cascades. Each injector's upper test band lies slightly above its highest
> training band, so results are reported per band, and the extreme-extrapolation stress slice is reported separately
> because more extreme incidents are easier to detect. We claim generalization across held-out regions of the
> simulator's parameter space, within one simulated application and one workload family. **We do not claim
> generalization to real-world production incidents;** that would require real-system telemetry and is out of scope.

What changed from the M4-3 draft: the dates, a sentence stating what the controls support and what the test set
showed, and the explicit workload limit. The three controls and the claim boundary are unchanged.

### Evidence for each claim

| Claim | Evidence | State |
|---|---|---|
| Training and test bands interleave and do not overlap | `integrity.check_bands` and its tests; `run-batch` refuses a `v1` entry that breaks a band ([`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md), [`TEST_SET.md`](TEST_SET.md)). The per-band table here lists exactly the test bands of M9-1's spec. | Documented by M4-3 and M9-1; the training bands were not rechecked for this report |
| Intensities avoid the shared edges | The "Shared edges" section of `EVALUATION_INTEGRITY.md` | Documented; not rechecked here |
| Test set generated after training, in order | M9-2's `check_split` accepted the registry when this report ran without `--allow-partial`: all 350 test slots present, each phase agrees with its slot, every test and stress run generated after 2026-10-25. `train.check_not_test` refuses test runs in a training job (`ml/test_train_guard.py`) | Verified at run time |
| Every test run belongs to its queue entry | `tag_runs` raised no error: each registry run has a queue entry whose phase matches | Verified at run time |
| Diversity protocol applied | The 30 confounders, 20 varied-duration runs and shifted mix are in the queue files and registry ([`TEST_SET.md`](TEST_SET.md)); the varied-duration and confounder results above come from them | Documented; counts match |
| Extreme slice kept apart | Slots 1051-1070, phase `stress`, its own section in every table above; it is in no headline number | Verified in the results |
| Feature/Label Boundary on the data the models read | The guard ran over the training and validation features in M5-3 (700 files, 106 columns, no forbidden names), and again over every downloaded features file for this report: validation, test and stress, 510 files, 106 columns, no forbidden names (2026-11-23, `features.guard.check_columns` in the training image; column names only) | Verified |
| Results reported per band, and the claim bounded | Per-band tables above and in M9-2 | Done |

## What this report does not show

- **XGBoost and Random Forest on the test set.** M9-2 scored only the live classifier, and the test set has been seen,
  so these were not scored for this report.
- **Why Model 0 moved less than the classifier.** The data show that it did, not why.
- **Which cause moved the classifier.** Held-out bands and the shifted mix are confounded in every test run.
- **A claim about real systems.** All of this is one simulated application, one workload family and one AWS
  account.
- **That `payment-service` was affected in a diagnosis** is no evidence the system located a fault: the shifted mix
  doubles `POST /orders`, which lifts Payment above its training baseline in 293 of 296 test diagnoses
  ([`GENAI_EVALUATION.md`](GENAI_EVALUATION.md)).

## Reproducing

From `AnomalyPulse/`, in the training image's versions (Python 3.10, scikit-learn 1.5.2, numpy 2.1.0, scipy 1.15.0,
joblib 1.6.0), with a local copy of `datasets/v1/` for slots 561-1070:

    python -m ml.m9_3_report --dataset ~/v1 --classifier incident_type=~/pkgs/incident_type \
        --job incident_type=anomalypulse-hpo-261028173159-003-28af89d1 --commit <commit> \
        --m9-2-results ml/m9_2_results/metrics_m9_2.json --out ml/m9_3_results