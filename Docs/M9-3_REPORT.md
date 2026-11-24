# M9-3 report: the threshold baseline beside the test-set results, and the evaluation-integrity statement

**Status:** final. Test-set numbers are from `AnomalyPulse/ml/m9_3_results/baseline_m9_3.json` and `.md`, generated
2026-11-24 from the clean commit `a78f86e` (seed 20261118, 1000 bootstrap resamples over whole runs). That run also
scored M9-2's detectors and live classifier again and reproduced M9-2's published numbers exactly
([`M9-2_REPORT.md`](M9-2_REPORT.md)). **Owner:** Rachel · **Code:** `AnomalyPulse/ml/m9_3_baseline.py`,
`m9_3_report.py` · **Reads:** [`TEST_SET.md`](TEST_SET.md), [`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md),
[`M5-3_REPORT.md`](M5-3_REPORT.md)

## The short version

- **The classifier finds almost every anomalous window; the threshold rule finds fewer than half.** Over the headline
  (320 test runs, 2,092 windows) the classifier, read as a detector, has recall 0.999 [0.997, 1.000]. Model 0's
  stronger floor has 0.472 [0.419, 0.521]. The paired margin is +0.528 [0.479, 0.576].
- **The classifier raises more false alarms on healthy traffic than the rule.** On healthy windows (no fault active,
  the false alarms an operator would see) Model 0 never fires and the classifier fires on 18 of 740 (2.4%), a paired
  margin of +0.024 [0.014, 0.036]. Counted against the labels, which also call a fault's first minute `NORMAL`, the
  two are not measurably different (0.069 and 0.064, margin +0.005 [-0.017, 0.029]).
- **Model 0 does as well as the classifier on one fault and almost nothing on another.** It flags every dependency
  failure window (a failing payment trips its absolute error rule) and 1.5% of traffic-spike windows (0.0% and 0.7%
  in the two held-out bands; a spike raises volume, not latency or errors).
- **On cascades the detection margin is small but real.** Model 0 misses 11 of 118 cascade windows; the paired margin
  is +0.093 [0.035, 0.156]. The classifier recognizes all 30 cascades (median 58.5 s from the moment two faults are
  active together, at most 113.5 s), which Model 0 cannot do at all. But its naming is flawed on this test set: its
  precision for `CASCADING_FAILURE` is 0.474, because dependency failures are called cascades.
- **Every classifier M5-3 trained beats the rule as a detector; none holds up as well as the live one.** XGBoost
  (defaults and tuned) and Random Forest also have recall margins of about +0.53. As six-class classifiers their
  macro-F1 falls from 0.986–0.987 on validation to 0.61–0.70 on test, against the live logistic regression's 0.992 to
  0.869, and each calls all 265 dependency-failure windows cascades. The live model was chosen on validation and is
  not revisited on these numbers.
- **The validation-to-test gap is large for the classifier and absent for Model 0.** Read as a detector, the
  classifier's recall holds (0.986 to 0.999) but its false-alarm rate rises from 0.005 to 0.069, with intervals that do
  not overlap; Model 0's recall (0.488 to 0.472) and false-alarm rate (0.061 to 0.064) barely move. As a six-class
  classifier its accuracy falls from 0.990 to 0.910 and macro-F1 from 0.992 to 0.869. Every test run uses a shifted
  traffic mix, so the gap mixes held-out bands with that shift; M9-2's diagnostics point at the mix.
- **The claim this supports:** the system generalizes across held-out parameter bands of the simulator, with a
  workload-shift weakness, and says nothing about real production incidents.

## What was compared

| | |
|---|---|
| **Model 0 (threshold rule)** | `ml/model0.py` as reviewed in M5-3, not tuned. A window is flagged if any service has p95 above 2x its baseline or a 5xx share above 5%. Two baselines: the previous retained window, and the mean of the 3 earlier lookback windows of the same run (the stronger floor). |
| **Live classifier** | Logistic regression, job `anomalypulse-hpo-261028173159-003-28af89d1`, the one deployed and the one M9-2 scored. Read as a detector it flags every window it does not call `NORMAL`. |
| **M5-3's other classifiers** | XGBoost with the script's defaults (`anomalypulse-train-incident-xgb-20261030T033735Z`), the tuned XGBoost (`anomalypulse-hpo-261031214659-004-76a607cd`) and Random Forest (`anomalypulse-train-incident-rf-20261030T034713Z`), the October packages as trained. M5 said M9 reports every classifier on the test set. Their validation accuracy and macro-F1 here match M5-3's to four decimals. |
| **M9-2's detectors** | The Isolation Forest and the binary detector (`anomalypulse-train-{iforest,binary}-20261031T224214Z`), and the deployed rule (the forest flags the window AND the live classifier names a fault), scored again so their margins over Model 0 are paired. Their headline numbers, the live classifier's and its confounder shares match M9-2's published ones exactly. |
| **Data** | The test set, slots 701-1050; headline = 320 runs without the 30 confounders; the extreme slice (slots 1051-1070) apart; validation (slots 561-700) as the reference. |
| **Intervals** | 95% percentile bootstrap over whole runs. A margin resamples the same runs for both detectors (paired), so its interval is narrower than two separate intervals suggest. |

Model 0 was fixed in M5-3, before the test set existed, and is not tuned here; the October packages are scored as they
were trained. M9-2 had already scored and seen the test set, so every result is reported whichever way it falls. A
rule has no class and no score, so accuracy, macro-F1, per-class precision, ROC AUC, average precision, recall at a
fixed false-alarm rate, confounder naming shares and time to recognize a cascade are n/a for it.

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
row is the correction: it never flags 64 to 88 of the 260 incident runs. Model 0's false alarms against the labels come
from fault-start minutes (a fault active for under half the minute, which the labels call `NORMAL`), not from healthy
traffic.

Paired margin, classifier minus Model 0:

| | Against the earlier-mean floor | Against the previous-window floor |
|---|---|---|
| Precision | +0.040 [0.018, 0.060] | +0.060 [0.035, 0.087] |
| Recall | +0.528 [0.479, 0.576] | +0.626 [0.576, 0.679] |
| F1 | +0.354 [0.310, 0.400] | +0.450 [0.400, 0.507] |
| False-positive rate, against the labels (lower is better) | +0.005 [-0.017, 0.029] | +0.005 [-0.019, 0.029] |
| Healthy windows flagged (lower is better) | +0.024 [0.014, 0.036] | +0.024 [0.015, 0.036] |

The classifier's 18 healthy-window false alarms fall in 18 different runs.

Share of each class flagged (headline), with the paired margin over the stronger floor:

| Class | Windows | Model 0 (previous window) | Model 0 (earlier mean) | Classifier as a detector | Paired margin over the earlier mean | Classifier names it (recall / precision) |
|---|---|---|---|---|---|---|
| `CASCADING_FAILURE` | 118 | 0.839 [0.733, 0.926] | 0.907 [0.846, 0.962] | 1.000 | +0.093 [0.035, 0.156] | 1.000 / 0.474 |
| `DATABASE_THROTTLING` | 264 | 0.246 [0.223, 0.269] | 0.485 [0.439, 0.532] | 1.000 | +0.515 [0.472, 0.558] | 1.000 / 1.000 |
| `DEPENDENCY_FAILURE` | 265 | 1.000 | 1.000 | 1.000 | +0.000 | 0.540 / 0.986 |
| `LAMBDA_DEGRADATION` | 262 | 0.118 [0.086, 0.153] | 0.317 [0.274, 0.363] | 0.996 [0.987, 1.000] | +0.679 [0.636, 0.721] | 0.989 / 0.992 |
| `NORMAL` (the false-alarm rate) | 845 | 0.064 [0.047, 0.083] | 0.064 [0.047, 0.084] | 0.069 [0.051, 0.086] | +0.005 [-0.018, 0.029] | 0.931 / 0.999 |
| `TRAFFIC_SPIKE` | 338 | 0.015 [0.003, 0.030] | 0.015 [0.003, 0.029] | 1.000 | +0.985 [0.971, 0.997] | 0.982 / 0.862 |
| Healthy windows (`NORMAL`, no fault active) | 740 | 0.000 | 0.000 | 0.024 | +0.024 [0.014, 0.036] | |

- **Traffic spike, throttling, Lambda degradation:** this is where the classifier learns something a rule on latency
  and errors cannot. Spikes are nearly invisible to the rule (1.5% of spike windows here; 0.0% and 0.7% in the
  standard-length runs of the two held-out bands).
- **Dependency failure:** the rule flags all of it, so the classifier's advantage is naming it, and that is its weak
  class (recall 0.540; all 122 of its misses are called cascades).
- **Cascades:** the classifier flags all 118 cascade windows and Model 0's stronger floor 107, a paired margin of
  +0.093 [0.035, 0.156] (+0.161 [0.066, 0.262] against the previous-window floor).
  The interval excludes zero, but the margin is small: most cascade windows trip the rule. The classifier's larger
  contribution is naming the multi-fault state. It recognizes all 30 cascades, median 58.5 s and at most 113.5 s after
  two faults are first active together; Model 0 cannot name a cascade, so it never recognizes one. That naming is
  flawed on this test set: its precision for `CASCADING_FAILURE` is 0.474.

### Every ML model beside Model 0

Headline (320 runs), every margin paired against Model 0's stronger floor:

| Model | Precision | Recall | False-positive rate | Healthy windows flagged | Recall margin | FPR margin | Healthy-window margin |
|---|---|---|---|---|---|---|---|
| Live classifier (logistic regression), as a detector | 0.956 [0.944, 0.965] | 0.999 [0.997, 1.000] | 0.069 [0.052, 0.089] | 0.024 | +0.528 [0.479, 0.576] | +0.005 [-0.017, 0.029] | +0.024 [0.014, 0.036] |
| XGBoost (defaults), as a detector | 0.949 [0.937, 0.959] | 1.000 [1.000, 1.000] | 0.079 [0.062, 0.100] | 0.022 | +0.528 [0.479, 0.581] | +0.015 [-0.006, 0.040] | +0.022 [0.012, 0.033] |
| XGBoost (tuned), as a detector | 0.906 [0.889, 0.921] | 1.000 [1.000, 1.000] | 0.153 [0.124, 0.184] | 0.088 | +0.528 [0.478, 0.578] | +0.089 [0.059, 0.120] | +0.088 [0.065, 0.113] |
| Random Forest, as a detector | 0.967 [0.959, 0.976] | 0.997 [0.993, 0.999] | 0.050 [0.035, 0.067] | 0.000 | +0.525 [0.472, 0.571] | -0.014 [-0.038, 0.008] | 0.000 |
| Isolation Forest (M9-2) | 0.596 [0.555, 0.634] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 | +0.528 [0.479, 0.578] | +0.936 [0.917, 0.952] | +1.000 |
| Binary detector (M9-2) | 0.967 [0.957, 0.976] | 0.999 [0.998, 1.000] | 0.051 [0.036, 0.068] | 0.000 | +0.528 [0.476, 0.576] | -0.013 [-0.035, 0.007] | 0.000 |
| Deployed rule: forest AND live classifier (M9-2) | 0.956 [0.945, 0.966] | 0.999 [0.997, 1.000] | 0.069 [0.051, 0.088] | 0.024 | +0.528 [0.475, 0.581] | +0.005 [-0.018, 0.027] | +0.024 [0.015, 0.036] |
| Model 0 (earlier mean, the stronger floor) | 0.916 [0.897, 0.935] | 0.472 [0.419, 0.521] | 0.064 [0.047, 0.082] | 0.000 | | | |

- **Every ML detector finds about half the anomalous windows more than the rule,** with intervals far from zero. They
  differ in false alarms. The binary detector and Random Forest flag no healthy window, like Model 0; the live
  classifier and the default XGBoost flag about 2%, and the tuned XGBoost 8.8%.
- **The Isolation Forest has no margin to report.** It flags every test window, so its recall of 1.000 and its
  false-positive rate of 1.000 are what flagging everything produces (`M9-2_REPORT.md`). Its precision of 0.596 is the
  share of windows that are faults, which is what flagging everything gives; it is below Model 0's.
- **The deployed rule is the live classifier on this test set.** Because the forest flags every window, "forest AND
  classifier" reduces to the classifier, so the two rows hold the same point values; their intervals differ in the
  last digit only because each is resampled under its own seed.
- **Confounders, as detection:** every ML detector flags all 143 windows where both faults are active; Model 0 flags
  0.587 [0.425, 0.747] (previous window) and 0.692 [0.571, 0.812] (earlier mean).

### Every classifier, six classes

Headline. Validation is slots 561-700; train macro-F1 is from each package's own record.

| Classifier | Macro-F1: train / validation / test | Accuracy: validation / test | Dependency-failure recall | Cascade precision | Time to recognize a cascade (30 runs) | Confounders: either type / labeled type / cascade |
|---|---|---|---|---|---|---|
| Logistic regression (live) | 0.995 / 0.992 / 0.869 [0.845, 0.890] | 0.990 / 0.910 | 0.540 | 0.474 | median 58.5 s, max 113.5 s, never 0 | 0.469 / 0.343 / 0.531 |
| XGBoost (defaults) | 1.000 / 0.987 / 0.610 [0.590, 0.628] | 0.983 / 0.740 | 0.000 | 0.195 | median 56.3 s, max 113.5 s, never 0 | 0.280 / 0.280 / 0.720 |
| XGBoost (tuned) | 1.000 / 0.987 / 0.650 [0.625, 0.672] | 0.983 / 0.755 | 0.000 | 0.225 | median 56.3 s, max 113.5 s, never 0 | 0.308 / 0.308 / 0.692 |
| Random Forest | 1.000 / 0.986 / 0.704 [0.686, 0.723] | 0.982 / 0.826 | 0.000 | 0.269 | median 59.1 s, max 113.5 s, never 0 | 0.294 / 0.294 / 0.706 |
| Model 0 | n/a | n/a | n/a | n/a | n/a: it cannot name a cascade | n/a: it names no class |

- **All three tree models call every dependency-failure window a cascade** (265 of 265), though each scored 0.99 on
  them in validation and 1.000 on the extreme slice, which uses the default traffic mix. The live classifier loses
  under half of them the same way. The workload-shift weakness M9-2 found is not specific to the live model.
- **The tree models also lose throttling and Lambda degradation windows** that the live one keeps: the default
  XGBoost names 0.37-0.41 of throttling windows in the held-out bands, Random Forest about 0.81.
- **The live model was chosen on validation (M5-3) and is not revisited.** It also scores highest on the test set;
  this report notes that and does not use it.

## Signal 2: the train/test gap (overfitting to simulator artifacts)

| | Validation | Test (headline) |
|---|---|---|
| Classifier, six-class accuracy | 0.990 [0.983, 0.996] | 0.910 [0.892, 0.927] |
| Classifier, six-class macro-F1 | 0.992 [0.987, 0.997] | 0.869 [0.845, 0.890] |
| Classifier as a detector: recall | 0.986 [0.976, 0.995] | 0.999 [0.997, 1.000] |
| Classifier as a detector: false-positive rate | 0.005 [0.000, 0.014] | 0.069 [0.052, 0.089] |
| Model 0 (earlier mean): recall | 0.488 [0.421, 0.563] | 0.472 [0.419, 0.521] |
| Model 0 (earlier mean): false-positive rate | 0.061 [0.039, 0.091] | 0.064 [0.047, 0.082] |

Validation tuned the classifier's regularization, so it is the optimistic side of any gap. Compared like for like, as
detectors, the classifier's recall holds and its false-alarm rate rises with intervals that do not overlap, while
Model 0's both hold: the classifier's clear false-alarm advantage on validation (0.005 against 0.061) is gone on the
test set. M5-3's other classifiers show larger gaps (macro-F1 0.986-0.987 on validation to 0.61-0.70 on test; table
above).

**Per held-out band.** For the live classifier, recall in the upper band is within 0.013 of the lower band for every
fault. Against validation, only dependency failure's naming is flagged as degraded (0.555 and 0.565 against 0.99), and
both of its bands score alike, so the band is not what hurts it. Model 0's per-band recall is also flat between bands
(for example payment failure 1.000 in both bands, throttling 0.526 and 0.522). For the tree models every
dependency-failure band is 0.000 against validation's 0.99; the one band-to-band drop of any model is the tuned
XGBoost's throttling recall, 0.774 in the upper band (4.5-5.5x) against 0.974 in the lower.

**What this does and does not separate.** Every test run uses the shifted endpoint mix, so the gap above combines the
held-out parameter bands with the workload shift. M9-2's diagnostics (worked out after scoring, so offline evidence,
not a controlled experiment) show that replacing only Order's median latency with validation values takes dependency
recall from 0.540 to 1.000, which points at the mix. The 20-run extreme slice uses the default mix but also more extreme
faults, so it does not isolate either cause, though every classifier names every one of its payment failures. Nothing
here says why Model 0 moved less, or why the tree models moved more.

## Slices reported apart

- **Extreme slice (20 runs, 5 per fault).** Every classifier, the live one and M5-3's other three, scores 1.000 on
  every fault. Model 0 scores 0.000 on spikes, 0.200 to 0.400 on Lambda degradation, 0.500 to 0.650 on throttling, and
  1.000 on payment failure.
- **Varied durations (10 runs of 2 minutes, 10 of 12).** Model 0's recall on sustained faults drops, for example
  Lambda degradation 0.833 at 2 minutes and 0.083 at 12, throttling 1.000 and 0.182 (earlier-mean floor). That fits how
  the stronger floor is built (a sustained fault becomes its own baseline), but this report does not test that cause.
  The live classifier keeps 1.000 as a detector; its naming of dependency failure falls to 0.750 and 0.389.
- **Confounders (30 runs, 143 windows with both faults active).** The live classifier names 46.9% one of the two true
  types, 34.3% the labeled first type and 53.1% `CASCADING_FAILURE` (M9-2's numbers, reproduced); the other classifiers
  call 69-72% cascades. A rule names no class; as detection, Model 0 flags 59% to 69% of these windows and every ML
  detector all of them.
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
> anomalous window and beats a threshold baseline by a wide margin in recall, though it raises more false alarms on
> healthy traffic than the baseline, and its recall holds across the held-out parameter bands for three of the four
> fault types. They do not make the system robust to a change in workload: the test set's shifted traffic mix moved
> the healthy baseline past the Isolation Forest's fixed cut-off (the forest flags every test window), and the forest
> also flagged 10 of 16 healthy minutes in one of two later live checks on the default mix. The shift lowered the
> classifier's recall for dependency failure from 0.99 on validation to 0.54, calling every missed window a cascade,
> and every other classifier trained for comparison lost that class entirely. Each injector's upper test band lies
> slightly above its highest training band, so results are reported per band, and the extreme-extrapolation stress
> slice is reported separately because more extreme incidents are easier to detect. We claim generalization across
> held-out regions of the simulator's parameter space, within one simulated application and one workload family.
> **We do not claim generalization to real-world production incidents;** that would require real-system telemetry
> and is out of scope.

What changed from the M4-3 draft (`EVALUATION_INTEGRITY.md`): the dates, a paragraph stating what the controls support
and what the test set showed (the margin, the healthy-traffic false alarms, the workload limit, the forest's live
checks and the other classifiers), and the explicit workload limit. The three controls and the claim boundary are
unchanged.

### Evidence for each claim

| Claim | Evidence | State |
|---|---|---|
| Training and test bands interleave and do not overlap | `integrity.check_bands` and its tests; `run-batch` refuses a `v1` entry that breaks a band ([`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md), [`TEST_SET.md`](TEST_SET.md)). Rechecked for this report in review (2026-11-24): every single-fault and confounder entry in the `v1` train, val, test and stress queues (800 entries) uses one of its phase's bands. | Verified |
| Intensities avoid the shared edges | The "Shared edges" section of `EVALUATION_INTEGRITY.md`. Rechecked in review: every one of those 800 intensities lies in 0.05-0.95, and no magnitude appears in both training and test. | Verified |
| Test set generated after training, in order | M9-2's `check_split` accepted the registry when this report ran without `--allow-partial`: all 350 test slots present, each phase agrees with its slot, every test and stress run generated after 2026-10-25. `train.check_not_test` refuses test runs in a training job (`ml/test_train_guard.py`) | Verified at run time |
| Every test run belongs to its queue entry | `tag_runs` raised no error: each registry run has a queue entry whose phase matches | Verified at run time |
| Diversity protocol applied | The 30 confounders, 20 varied-duration runs and shifted mix are in the queue files and registry ([`TEST_SET.md`](TEST_SET.md)); the varied-duration and confounder results above come from them | Documented; counts match |
| Extreme slice kept apart | Slots 1051-1070, phase `stress`, its own section in every table above; it is in no headline number | Verified in the results |
| Feature/Label Boundary on the data the models read | The guard ran over the training and validation features in M5-3 (700 files, 106 columns, no forbidden names), and again in review over every `v1` features file, training, validation, test and stress: 1,070 files, 106 columns, no forbidden names (2026-11-24, `features.guard.check_columns` in the training image's versions; column names only) | Verified |
| The numbers here are M9-2's | The run scored M9-2's detectors and live classifier again: their headline detection blocks, classification block and confounder shares match `metrics_m9_2.json` (results file, section 10) | Verified at run time |
| Results reported per band, and the claim bounded | Per-band tables above and in M9-2 | Done |

## What this report does not show

- **Why Model 0 moved less than the classifier,** or why the tree models moved more. The data show that they did, not
  why.
- **Which cause moved the classifiers.** Held-out bands and the shifted mix are confounded in every test run.
- **A claim about real systems.** All of this is one simulated application, one workload family and one AWS
  account.
- **That `payment-service` was affected in a diagnosis** is no evidence the system located a fault: the shifted mix
  doubles `POST /orders`, which lifts Payment above its training baseline in 293 of 296 test diagnoses
  ([`GENAI_EVALUATION.md`](GENAI_EVALUATION.md)).

## Reproducing

From `AnomalyPulse/`, in the training image's versions (Python 3.10, scikit-learn 1.5.2, numpy 2.1.0, scipy 1.15.0,
joblib 1.6.0, xgboost 3.0.5), with a local copy of `datasets/v1/` (`tier1/features`, `tier2/labels`,
`tier2/experiments.jsonl`) and each package's `model.tar.gz` unpacked from
`s3://anomalypulse-ml-<account>-us-east-2/training/<job>/output/`:

    python -m ml.m9_3_report --dataset ~/v1 \
        --classifier incident_type=~/pkgs/incident_type --classifier xgboost=~/pkgs/xgboost \
        --classifier xgboost_tuned=~/pkgs/xgboost_tuned --classifier random_forest=~/pkgs/random_forest \
        --detector iforest=~/pkgs/iforest --detector binary=~/pkgs/binary \
        --job incident_type=anomalypulse-hpo-261028173159-003-28af89d1 \
        --job xgboost=anomalypulse-train-incident-xgb-20261030T033735Z \
        --job xgboost_tuned=anomalypulse-hpo-261031214659-004-76a607cd \
        --job random_forest=anomalypulse-train-incident-rf-20261030T034713Z \
        --job iforest=anomalypulse-train-iforest-20261031T224214Z --job binary=anomalypulse-train-binary-20261031T224214Z \
        --m9-2-results ml/m9_2_results/metrics_m9_2.json --out ml/m9_3_results

The first `--classifier` is the live one. Run it from a clean checkout so the results record the commit; where git is
not available (a container), pass `--commit`. The tests run on synthetic data only:
`python -m pytest ml/test_m9_3_report.py ml/test_m9_3_baseline.py ml/test_m9_2_metrics.py -q`.
