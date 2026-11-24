# Milestone 9 — Evaluation

> **GitHub Milestone**
> **Title:** `M9 — Evaluation`
> **Timeframe:** Weeks 8–9
> **Depends on:** `M8`
> **Description:**
> Run the full experiment suite and produce the report's quantitative spine: detection + classification metrics, detection/diagnosis/end-to-end latency, GenAI quality, cloud cost, and — the point of it all — evaluation-integrity results that show generalization **across held-out regions of the simulator's parameter space** (not to real-world incidents; §6b). The milestone **opens by generating and unsealing the test set** (experiments 701–1050), which has sat untouched since M4 by design. Everyone measures the surface they own.

## Assignment summary
- **Josh** — Generate + unseal the sealed test set (Phase 2) **+** GenAI quality evaluation
- **Jake** — Cloud cost + operational-performance measurement
- **Melisa** — Detection + classification metrics (per-band, extreme slice separate, confounder)
- **Rachel** — Threshold-baseline comparison + evaluation-integrity / generalization reporting
- **Linu** — Latency measurement (detection / diagnosis / end-to-end) + results visualizations for the report

## Coordination notes
- **Test set only.** Every headline metric is computed on the temporally held-out test set (701–1050), never on train/validation. Josh's `M9-1` is a hard predecessor for all metric issues.
- **Two things reported separately, always:** (1) the **extreme-extrapolation stress slice** is reported apart from core generalization metrics (more-extreme incidents are *easier*, so folding them in would flatter the result); (2) the **threshold baseline** is reported next to every ML metric so the margin is explicit.
- **Claim discipline in the writeup:** results support "generalizes across held-out simulator parameter regions," not "generalizes to production." Rachel's integrity report states this boundary explicitly.

---

## Issues

### `M9-1` — Generate + unseal test set (Phase 2)
- **Assignee:** @josh
- **Labels:** `evaluation`, `dataset-generation`, `leakage-control`
- **Blocked by:** `M4-3` *(bands/diversity spec)*, `M8` *(live pipeline)*
- **Blocks:** `M9-2`, `M9-3`, `M9-4`, `M9-5`

**Context.** Generate the 350-experiment test set on a **separate day** using the held-out interleaved bands and the diversity protocol, plus the separate extreme slice — then unseal it for evaluation. Its separate-day generation is what injects honest environmental variability (cold starts, background latency) into the split.

**Deliverables**
- 350 test experiments per §17 Phase 2: 60 normal, 60 traffic (4×–6×, 8×–10×), 60 DB throttling (2.5×–3.5×, 4.5×–5.5×), 45 Lambda degradation (500–800ms, 1100–1400ms), 45 dependency (1500–2500ms, 3500–4500ms), 30 cascading, 30 confounders (not labeled cascading), 20 varied-duration (2-min/12-min).
- Shifted endpoint distribution applied per the diversity protocol.
- Generated from the same EC2 runner host as the training set (`M4-1`), at the same 20 rps baseline. A different client or rate would be a train/test difference that has nothing to do with the incidents.
- Separate **extreme-extrapolation stress slice** (traffic >10×, latency >1500ms, offered-load >6×, timeout >5000ms) generated and tagged distinctly, as slots 1051 on.
- The queue is built with `simulator/integrity.py`'s builders; `run-batch` refuses a `v1` entry that breaks a control (`Docs/EVALUATION_INTEGRITY.md`). The test-set kinds (12-minute faults, confounders, the stress bands) haven't run on the runner yet: validate them in a separate dataset first.
- A 2-minute DB-throttling run that leaves no labeled window (when `label_from` falls in a minute's first 18 s, about 3 runs in 10) is rerun: `run-batch` rejects it (decided in M4-4's review, `Docs/LABELING.md`, "Windows per run"). Expect a few extra reruns among the varied-duration runs. *M9-1's validation found a rerun opens at the same second, so it never helps; `run-batch` now starts these runs so the fault opens at :35–:50, which always leaves a window.*
- Test set registered, dated, and confirmed **not inspected** until all training/validation work was complete.
- **Before the first test batch, a training job refuses test runs on its own** (Jake, from M5-1). IAM can't tell test labels from training labels, since both are `tier2/labels/experiment_id=<id>/`, so today only the launcher's channel manifests keep test runs out of a SageMaker job. A check inside `ml/job/train.py`, refusing any run outside slots 1–700, means a launcher bug can't train on the test set. *Built as a date check, since a job's channel holds only run ids: it refuses any run dated after 2026-10-25, training's last day. `ml/evaluate.py` opts out, as M9-2 scores the test set with it.*
- **Before the first test batch, check the system is the one training ran on.** `anomalypulse-shared`'s `LastUpdatedTime` still reads 2026-10-16T03:17:23Z, the time before `v1` was pinned (M5–M8 deploy to their own stack, README "Status"). A `git diff 0557121 -- AnomalyPulse/simulator/` shows only the test set's queue generator, its queues and docs, not the workload, the injectors or the batch's checks. *Since M9-1's validation it also shows `batch.py`'s start wait for 2-minute throttling runs, and its tests: it sets when such a run starts, not what it sends or measures.* The batch's own pin check covers the service functions, the metrics extractor and the Products table on top of that.

**Built, before the first batch (2026-11-12; [`Docs/TEST_SET.md`](../Docs/TEST_SET.md)):** `build_queues.py`
writes the test set (701–1050) in 5 blocks of 70 from its own seed, `20261112`, so the training queues are
unchanged. The queues are `v1-test-1` (701–840), `v1-test-2` (841–980) and `v1-test-3` (981–1050), about 59 h,
then `v1-stress` (1051–1070, 3.3 h). Each block holds exactly `TEST_BLOCK`, every run is in the shifted mix, and
bands, Lambda targets, templates, confounder pairs and durations come out half and half or within a run; every
entry passes the `v1` guard. `ml/job/train.py` refuses any run dated after 2026-10-25, training's last day, so no
job script can train on a test run; `ml/evaluate.py` opts out, so M9-2 can score the test set with it. `m9-1-validation.json` (dataset `m9-1-validation`) runs the untried kinds at
their heaviest first: four 12-minute faults, a 2-minute throttling fault, the four confounder pairs and the four
stress bands, 13 runs, 2.6 h. **Checked 2026-11-12:** `anomalypulse-shared` still reads 2026-10-16T03:17:23Z;
the simulator diff since `0557121` is only the generator, its tests and queues; `v1`'s registry holds slots
1–700 and nothing later (last generated 2026-10-25); both promoted models predate any test run (10-29 and
11-06). **Validation batch (2026-11-13):** 12 of 13 accepted with nothing dropped, spot check passed. The
2-minute throttling run opened at :23.964, :26.969 and :27.356 and never left a labeled window, so `run-batch`
now starts it so its fault opens at :35–:50 (`batch.py`'s only change since `0557121`). Run again with the fix
(`m9-1-throttle-check`), its fault opened at :35.080 and the run was accepted first time, without needing the wait;
the first run that waits is slot 829 in `v1-test-1`. **Generated 2026-11-13 to 11-16** from `80d5932`, in four
batches (`Docs/TEST_SET.md`, "Status"): all 350 test slots and 20 stress slots accepted, none exhausted, in 371 runs
(one rerun, stress slot 1069, whose first window ran on burst capacity). Both 2-minute throttling runs waited and
opened at :35 (slot 829 at :35.001, 945 at :35.000). All 1,070 `v1` runs labeled and built, 0 test runs with a
feature error or a missing snapshot. Not inspected: only counts were read, no feature values and no scores.

**Definition of Done**
- [x] Test set generated on a separate day from training (dates in the registry prove it). *`integrity.temporal_split`
  accepts the whole registry (560 train, 140 val, 350 test, 20 stress); every test and stress run is dated
  2026-11-13 to 11-16, after training's last day, 2026-10-25.*
- [x] Held-out interleaved bands used — no overlap with training bands. *`run-batch`'s `v1` guard checks every
  entry's bands against `BANDS[...]["test"]`, which `check_bands` proves interleave with training's (#107).*
- [x] Extreme slice tagged separately from the core test set. *Slots 1051–1070, phase `stress`, in their own queue
  and batch.*
- [x] Confounder and varied-duration experiments present and correctly labeled (confounders **not** cascading).
  *30 of 30 confounders and 20 of 20 varied-duration runs labeled with no errors; no confounder has a
  `CASCADING_FAILURE` window.*

---

### `M9-2` — Detection + classification metrics
- **Assignee:** @melisa
- **Labels:** `evaluation`, `ml`, `metrics`
- **Blocked by:** `M9-1`
- **Blocks:** —

**Context.** Score the detectors and classifier on the held-out test set and break the results down the way the integrity story requires.

**Deliverables**
- Detection: precision, recall, F1, false-positive rate, detection latency.
- Classification: accuracy, macro-F1, confusion matrix, per-class precision/recall.
- **Per-parameter-band breakdown** for key incident types (confirming performance doesn't fall off on held-out interleaved bands).
- **Extreme-extrapolation slice reported separately** from core metrics.
- **Confounder accuracy:** how often co-occurring-but-not-cascading incidents are handled correctly. Over the confounder windows with both faults active, report three shares: predicted as either fault's type, as the labeled (first) type, and as `CASCADING_FAILURE`. Confounders are a test-only probe: the model never sees one in training, so a high `CASCADING_FAILURE` share is a finding to report, not a data error (`Docs/EVALUATION_INTEGRITY.md`, "Confounders").

**Scored 2026-11-18, regenerated from a clean commit 2026-11-23 ([`Docs/M9-2_REPORT.md`](../Docs/M9-2_REPORT.md);
results in `AnomalyPulse/ml/m9_2_results/`).** The deployed rule (the forest flags a window and the classifier names a
fault), applied offline, has recall 0.999 and precision 0.956, with false alarms on 18 of 740 healthy windows. The
classifier has accuracy 0.910 and macro-F1 0.869. **The Isolation Forest flags all 2,280 test windows,** because the
test set's shifted endpoint mix moved the healthy baseline past its cut-off. **Dependency-failure recall is 0.54**
(validation 0.99), in both bands, because the same shift changes Order's median latency. No model or cut-off was
changed after scoring.

**Definition of Done**
- [x] All metrics computed on test set (701–1050) only. *The headline is 320 runs without confounders, with all 350
  beside it; the extreme slice and the validation reference are labeled apart.*
- [x] Per-band breakdown shows no sharp degradation on held-out bands (or the degradation is reported honestly).
  *Both dependency-failure bands are reported as degraded against validation, with the cause; the other three faults
  hold.*
- [x] Extreme slice is a separate line item, not folded into headline numbers. *Its own section (slots 1051–1070),
  which also notes that it differs from the test set in traffic mix as well as severity.*

---

### `M9-3` — Threshold-baseline comparison + evaluation-integrity report
- **Assignee:** @rachel
- **Labels:** `evaluation`, `ml`, `leakage-control`, `report`
- **Blocked by:** `M9-1`, `M9-2`
- **Blocks:** —

**Context.** Assemble the "is this real?" argument: ML vs the threshold floor, and the generalization case with its explicit limits. This is the section that separates AnomalyPulse from "an LLM reads logs."

**Deliverables**
- Threshold-baseline metrics reported **beside** every ML metric; margin called out, especially on cascades.
- **Train/test gap** on held-out bands reported as the overfitting signal (distinct from baseline margin, per §8).
- The Evaluation Integrity Statement (§6b) finalized, stating the claim boundary: generalization across held-out simulator regions, **not** to real-world incidents.

**From `M9-2`** ([`Docs/M9-2_REPORT.md`](../Docs/M9-2_REPORT.md)): state the workload-shift weakness beside the band
result. The classifier holds across held-out bands for three of the four fault types, but the shifted endpoint mix
alone breaks the forest's cut-off and drops dependency-failure recall to 0.54.

**Reported 2026-11-24 ([`Docs/M9-3_REPORT.md`](../Docs/M9-3_REPORT.md); results in
`AnomalyPulse/ml/m9_3_results/`).** Model 0, unchanged since M5-3, scored on M9-2's runs and windows beside every
model, with paired margins:
- **The margin:** the live classifier, read as a detector, has recall 0.999 against Model 0's 0.472, a margin of
  +0.528 [0.479, 0.576]. On cascades the margin is small but real, +0.093 [0.035, 0.156]. The classifier recognizes all
  30 cascades (median 58.5 s), which a rule cannot.
- **False alarms:** the classifier raises more on healthy traffic than the rule: 18 of 740 healthy windows against 0,
  +0.024 [0.014, 0.036].
- **Every classifier:** M5-3's XGBoost (defaults and tuned) and Random Forest were scored too. They beat the rule by
  the same recall margin, but their macro-F1 falls to 0.61-0.70 on test (live 0.869), and each calls every
  dependency-failure window a cascade. The live choice is not revisited.
- **M9-2's detectors:** scored again, they reproduce M9-2's published numbers exactly.
- **The gap:** as a detector the classifier's false-alarm rate rises from 0.005 on validation to 0.069 on test, while
  Model 0's barely moves; held-out bands and the shifted mix are confounded in every test run.
- **The statement:** the Evaluation Integrity Statement is finalized with its workload limit, the forest's live checks
  and the real-world disclaimer.

**Definition of Done**
- [x] Every ML result has its baseline shown next to it. *Model 0's two floors beside the live classifier, M5-3's other
  three classifiers, the Isolation Forest, the binary detector and the deployed rule, with paired margins; where a rule
  has no counterpart (class names, scores, time to recognize a cascade) the report says n/a and why.*
- [x] Baseline-margin (multivariate learning) and train/test-gap (overfitting) are reported as two separate signals.
  *"Signal 1" and "Signal 2" in the report; the gap's confound with the shifted mix is stated.*
- [x] The report explicitly disclaims real-world generalization. *In the finalized Evaluation Integrity Statement and
  "What this report does not show".*

---

### `M9-4` — Cloud cost + operational performance
- **Assignee:** @jake
- **Labels:** `evaluation`, `cost`, `performance`, `infra`
- **Blocked by:** `M9-1`
- **Blocks:** —

**Context.** Quantify the serverless economics the architecture was designed around — meaningful precisely because inference runs in Lambda, not on an idle endpoint (§9).

**Deliverables**
- Cost per experiment, per incident, per **inference**; approximate monthly cost at several request volumes.
- Operational performance: Lambda duration, API latency, throughput, SQS backlog.
- If a managed inference path was prototyped, an explicit **idle-cost comparison** (SageMaker Serverless Inference vs Lambda) per §9.

**Estimated before M9-1 (2026-11-12; [`AnomalyPulse/cost/README.md`](../AnomalyPulse/cost/README.md)):** the rest of
the plan costs about $127 in November with tax (range $106–$173), $16.43 of it already spent, and $0–$22 in
December depending on what is deleted after the demo. The student credit ended 2026-10-31, so all of it is billed.
The model (`AnomalyPulse/cost/estimate.py`) prices the test set run by run from October's measured usage per run
kind and band, and predicts Nov 11's live batch at 0.96x its API requests and 1.00x its Lambda GB-seconds. Its
per-run usage is the starting point for this ticket's cost per experiment and per inference once M9-1's runs are in.

**Measured (2026-11-23; [`Docs/COST_AND_PERFORMANCE.md`](../Docs/COST_AND_PERFORMANCE.md)):** from M9-1's four
batches (371 runs, 3,777 minutes, 317 incidents) with the live pipeline running, and no new live runs:
- **The data:** CloudWatch's 1-minute usage, the Diagnosis Lambda's `REPORT` lines, the Incidents table and the
  bill by day (`AnomalyPulse/cost/collect_m9.py`), turned into results without AWS by `measured.py`.
- **Checked against the bill:** each day's usage matches the bill's quantities (API requests within 0.001%, log
  bytes exact). Priced at list price, the test set comes to $103.97; the bill gives $104.47.
- **The costs:**
  - **per experiment $0.28:** the store $0.19, AnomalyPulse $0.07, tooling $0.01;
  - **per incident $0.059**, nearly all Bedrock ($0.034 a call, 1.71 calls an incident; $0.038 a call with
    runbooks on);
  - **per inference $5.6 × 10⁻⁷** ($0.56 a million, $0.02 a month at the pipeline's one a minute).
- **A month:** $56, $309 and $1,372 at 1, 20 and 100 rps at list price, with AnomalyPulse's share falling from 43%
  to 21%. Most of AnomalyPulse's cost is moving telemetry (S3 objects, the telemetry logs), not ML.
- **SageMaker on paper:** Serverless Inference is about as cheap as the Lambda and also $0 idle; a real-time
  endpoint is $41–$60 a month idle. `cost/results/projections.json` is M9-6's cost curve.

**Definition of Done**
- [x] Cost-per-inference is a real measured number (near-zero idle demonstrated). *$5.6 × 10⁻⁷ a call, from the
  `REPORT` lines of all 3,752 Diagnosis calls in the test set. On Nov 20–21 UTC, with no traffic, the Lambda made
  0 calls, and so did the incident store and Bedrock. The whole system costs $1.34 a day idle at list price ($0.84
  billed, after the always-free allowances). $0.70 of it is the store's provisioned Products table, $0.59
  AnomalyPulse (most of it the metrics extractor's CloudWatch requests, then S3 and the two once-a-minute Lambdas),
  and $0.04 the stopped runner's disk.*
- [x] Monthly projections given for at least two request volumes. *Seven rates from 1 to 100 rps, each split into
  the store's cost and AnomalyPulse's, with the always-free allowances as one line
  (`cost/results/projections.json`).*
- [x] Operational metrics collected from the live pipeline, not estimated. *CloudWatch over the four batch windows:
  36.7 rps mean and a 243 rps peak minute; healthy API p50/p95/p99 of 16.0/30.7/36.8 ms; every Lambda's duration
  percentiles with 0 errors and 0 throttles; the telemetry queue never had more than 1 message waiting (oldest at
  most 20 s); all four failure queues stayed empty.*

---

### `M9-5` — GenAI quality evaluation
- **Assignee:** @josh
- **Labels:** `evaluation`, `genai`, `metrics`
- **Blocked by:** `M9-1`
- **Blocks:** —

**Context.** Score Bedrock where it can actually be wrong. "Correct root cause" is **not** the primary metric — Bedrock is handed the classification, so that's near-tautological (§16). Evaluate it as a constrained explanation generator.

**Deliverables**
- **Evidence fidelity:** % of diagnosis claims that trace to a metric in the structured input (report the unsupported-claim rate).
- **Hallucination rate:** mentions of services/metrics/values absent from the input.
- **Actionability:** are recommended actions correct/specific for the incident type? (human rubric).
- **Completeness:** does the diagnosis cover the salient evidence?
- If RAG (`M6-2`) shipped: **retrieval quality** (retrieved runbook matches incident type).

**From `M6-2`** (shipped as five runbooks behind `ANOMALYPULSE_RUNBOOKS`, off by default; M06):
- **Retrieval quality equals classifier accuracy** (`M9-2`): the runbook is picked by the classified type.
  Report it that way, not as a number of its own.
- **Actionability with and without runbooks**, blind and shuffled, on the same test-set incidents: for each
  incident pick the better list (ties allowed), and score each list 0/1 on Correct and Safe. This comparison
  decides whether the switch is on for the demo. On the five samples it was a tie, too coarse on four 0/1
  criteria to separate them.
- **With runbooks on, a runbook-only fact in a summary, root cause or evidence bullet is unsupported**, like
  any fact the input doesn't hold. The self-check catches runbook numbers and capacity modes only.

**From `M8-4`** (2026-11-09; [`Docs/INCIDENT_FEED.md`](../Docs/INCIDENT_FEED.md)):
- **Count the capacity-mode slip as an actionability error.** M8-4's live diagnosis
  (`exp-20261109T025523Z-bbf1`) told the engineer to check provisioned capacity on the Carts and Orders tables,
  which are on-demand; M6-2's blind comparison scored the same slip without runbooks. The prompt is left as it
  is: the runbooks state each table's mode, so the with-and-without comparison above measures the fix.
- **What the console showed is not every stored diagnosis.** One flagged for causal or ordering wording or a
  bullet citing no metric is held back ("Severity pending") until a retry clears it; every other finding is
  shown. Score the stored diagnoses (`diagnosis`, `self_check`, `evidence_metrics`, `diagnosis_input` in the
  record), and the store's `incident_retry` log lines give how often a retry was kept and what it still found.

**Scored 2026-11-19** ([`Docs/GENAI_EVALUATION.md`](../Docs/GENAI_EVALUATION.md); `AnomalyPulse/genai/m9_5/`), over
the 296 stored test-set diagnoses (runbooks off, as deployed at the time): evidence fidelity 97.6% (50 of 2,095 claims
unsupported), hallucination 0.46% of mentions, 94.5% of moved metrics cited. 47 of the 50 unsupported claims are
`DATABASE_THROTTLING` diagnoses whose input held only payment-service, which the test mix makes look affected:
Bedrock restated the type with no metric for it, and retries couldn't fix that. 128 of 296 diagnoses have an action
that's wrong about this stack: the capacity slip in 82 (68 of 77 traffic spikes), and X-Ray, which the stack doesn't
run, in 50 (43 of 60 Lambda degradations). The runbook comparison ran on 30 incidents (38 Bedrock calls): stack errors
15 → 2, one runbook-only fact in 211 claims. **Rated blind 2026-11-22** by two raters: both preferred the runbook
lists on 17 of 30 (one called 8 more a tie, the other picked runbooks), both preferred the lists without on 1.
**`ANOMALYPULSE_RUNBOOKS` goes on for the demo.** The raters split on how many lists without runbooks were incorrect
(5 vs 26): Jake also marked down X-Ray, which the sheet's stack facts don't rule out.

**Deployed 2026-11-23** (05:49 UTC, runbooks on). One live traffic spike (`exp-20261123T055215Z-c61d`,
`inc-20261123T055800Z`) stored the `traffic_spike` runbook and passed `live_check` and the feed check, with every
claim supported and no capacity or X-Ray slip. The scores above still describe the runbooks-off test set.

**Definition of Done**
- [x] Evidence fidelity and hallucination rate computed over test-set diagnoses. *296 diagnoses: 97.6% of claims
  supported, 0.46% of mentions hallucinated; the stress slice's 20 on their own line.*
- [x] Actionability scored against a written rubric by ≥1 human rater. *Two raters, blind, on 30 test incidents
  with and without runbooks (`sheet.py`'s rubric: better list, Correct, Safe); `GENAI_EVALUATION.md`,
  "With and without runbooks, blind".*
- [x] Root-cause correctness explicitly **not** used as the headline GenAI metric (documented why). *Bedrock is
  handed the type, so it restates the classifier; 26 of 29 throttling diagnoses with no database metric still
  named throttling as the root cause (`GENAI_EVALUATION.md`, "Why root-cause correctness isn't the headline").*

---

### `M9-6` — Latency measurement + report visualizations
- **Assignee:** @linu
- **Labels:** `evaluation`, `performance`, `dashboard`, `report`
- **Blocked by:** `M9-1`, `M9-2`
- **Blocks:** —

**Context.** Measure the latency chain and turn the whole evaluation into the figures the report and demo will use — Linu's dashboard/viz skills applied to results.

**Deliverables**
- Detection latency, diagnosis latency, end-to-end latency measured on the live path.
- Detection latency reported as **window-dominated (~1 min)** by design (§16); if the 15-second-window stretch experiment ran, include the latency/stability tradeoff.
- Report-ready visualizations: confusion matrix, per-band performance, baseline-vs-ML margins, cost curves, latency breakdown.

**From `M9-2`:** build the figures from `AnomalyPulse/ml/m9_2_results/metrics_m9_2.json`. Don't plot the Isolation
Forest's recall or latency as a success: it flags every test window ([`Docs/M9-2_REPORT.md`](../Docs/M9-2_REPORT.md)).

**From `M9-3`:** the baseline-vs-ML margins are in `AnomalyPulse/ml/m9_3_results/baseline_m9_3.json` (`margins`,
`share_margins`, and `other_models` for every other model), paired over runs, so plot each margin with its own
interval rather than as the gap between two bars' intervals. Show healthy-window false alarms beside recall: the rule
flags no healthy window and the live classifier 2.4% ([`Docs/M9-3_REPORT.md`](../Docs/M9-3_REPORT.md)).

**Definition of Done**
- [ ] Full latency chain measured and attributed (how much is the aggregation window).
- [ ] A clean figure set covers detection, classification, cost, and latency.
- [ ] Figures are reproducible from the test-set results, not hand-drawn.
