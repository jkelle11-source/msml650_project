# Milestone 4 — Dataset

> **GitHub Milestone**
> **Title:** `M4 — Dataset`
> **Timeframe:** Week 4
> **Depends on:** `M3`
> **Description:**
> Turn the simulator into a **labeled, leakage-controlled dataset**: run the training experiments, aggregate telemetry into one-minute windows, apply the Window Labeling Policy, enforce the Feature/Label Boundary, and stand up the evaluation-integrity controls (interleaved parameter bands, temporal split, diversity protocol). This is the last sticky-ownership milestone, and it deliberately pre-positions people for the M5 capability split: Melisa (labeling) and Rachel (features) carry ML modeling; Josh (generation) moves to GenAI; Linu (eval controls) moves to dashboard; Jake (harness) moves to ML infra.
>
> **Scope boundary:** this milestone produces the **training + validation** set (experiments 1–700) and the *definitions/harness* for the test set. The test set (701–1050) is generated on a **separate day and sealed**, unopened until M9 — see `M9-1`.

## Assignment summary
- **Jake** — Dataset-generation harness, S3 dataset layout, experiment registry
- **Josh** — Execute training-set generation (experiments 1–700)
- **Linu** — Evaluation-integrity controls (interleaved bands, temporal-split logic, diversity protocol)
- **Melisa** — Window Labeling Policy + label assignment
- **Rachel** — Feature engineering (Tier-1 matrix) + Feature/Label Boundary guard

## Coordination notes
- **The ML dataset unit is the one-minute aggregated window, not the individual request** (README §3). Every feature/label decision operates on windows.
- **Interleaved bands, not extrapolation** (§6b): test bands sit *inside* the trained range, filling held-out gaps, so evaluation is genuine interpolation. A separate extreme-extrapolation slice is generated and reported **separately** — it does not support the core generalization claim.
- Claim scope is fixed here: these controls license "generalization across held-out regions of the **simulator's** parameter space," **not** generalization to real-world production incidents.
- **Generation settings, fixed by M3-2's validation** (details in [`Docs/SIMULATOR_INTERFACE.md`](../Docs/SIMULATOR_INTERFACE.md), "Running an experiment" and "Reading a manifest"):
  - **20 rps baseline for all 1,000 experiments**, test set included. It is the CLI default. A rate change partway through would put a shift in `request_rate` that tracks generation date, which is the temporal-split variable.
  - **Batches run on an EC2 instance in us-east-2, not on a laptop** (set up in `M4-1`). Generation takes days of continuous running. A laptop that sleeps fails the run in progress (the runner records it `FAILED`), and one that drops its Wi-Fi can record a `COMPLETED` run with a gap in its traffic. The instance does neither, and being in-region lets the DB-throttling injector reach its higher read rates. Every experiment runs from that instance, test set included: a change of client partway through would track the split, as a rate change would. `caffeinate -is` stays the rule for one-off runs from a laptop.
  - **The stack keeps its configuration as of PR #70** for the whole dataset: 1,769 MB functions (PR #67); Product builds its table at load; the Products table `anomalypulse-products-v2`, created provisioned at 200 RCU / 5 WCU and never switched (a `GET /products` scan costs 0.5 RCU); DynamoDB retries stated in Product, Cart and Order (legacy mode, 7 attempts); the Lambda runtime pinned (since M4-1 to one version: `UpdateRuntimeOn: Manual` with its `RuntimeVersionArn`). Deploy only from `main` or an up-to-date branch, never mid-generation.
  - **Deploys no longer change the functions by accident (M4-1).** Until M4-1, every `sam build` published a new telemetry-layer version even with no change (unpinned `jsonschema`, and bytecode stamped with the build time); there were 17 versions by M4-1. A new layer version updates every function, and with `UpdateRuntimeOn: FunctionUpdate` an updated function moved to the newest python3.13 runtime. Now the layer's requirements are pinned and its build drops bytecode, so it builds the same bytes every time, and the runtime is pinned to one version (`UpdateRuntimeOn: Manual`). Layer 17 (M4-1's IAM deploy) had the same files as 16 apart from bytecode; every function kept its code hash, memory and runtime version through it. **Still: no deploy during generation.** The batch runner pins the stack at a dataset's first batch and refuses a batch after any change (`Docs/DATASET.md`).
  - **The registry holds only runs M4 launched.** The six manifests written before M4 are test and validation runs, and none ran on these settings.
  - **Labeling and features:** use `COMPLETED` runs only, deduplicate raw records by `(service, request_id)`, and consider dropping each run's first minute (startup 404s, cold starts after an idle gap). The export does the first two (`Docs/DATASET.md`).
  - **Run shape (decided 2026-10-15): every run is 10 minutes — 3 minutes of baseline, a 5-minute fault, 2 minutes of recovery** — so 700 serial runs take ~117 hours instead of the CLI default's 175. The fault keeps five one-minute windows; dropping the first minute leaves two NORMAL baseline windows per run; recovery is excluded from training anyway. A cascade is `baseline_s` 180, `cascade_s` 300, `recovery_s` 120. Queue entries state the shape explicitly; the CLI's defaults are unchanged.
  - **Batches, the registry and the S3 layout** are in [`Docs/DATASET.md`](../Docs/DATASET.md) (M4-1). The main dataset is `datasets/v1/`.

---

## Issues

### `M4-1` — Dataset-generation harness, S3 layout & experiment registry
- **Assignee:** @jake
- **Labels:** `data`, `infra`, `tooling`, `mvp`
- **Blocked by:** `M3-1`
- **Blocks:** `M4-2`, `M4-3`, `M4-4`, `M4-5`

**Context.** The batch layer on top of the simulator: run N experiments unattended, register each with its manifest, and lay the data out in S3 so features/labels and the temporal split are trivial to compute.

**Status:** built — `python -m simulator run-batch`, the registry and the `datasets/` layout, the export, the frozen-configuration check, and the runner host stack (`AnomalyPulse/infra/runner/`). How to use it: [`Docs/DATASET.md`](../Docs/DATASET.md). Validated on the runner host 2026-10-16 (Definition of Done below); one open problem with DB throttling from the runner, below the Definition of Done.

**Deliverables**
- Batch runner that executes a queue of experiments (params from bands) and records completion status.
- S3 dataset layout separating **Tier-1 features** from **Tier-2 ground truth**, keyed by `experiment_id` and window.
- Experiment **registry** (which experiments exist, their bands, phase = train/val/test, generation date).
- Idempotent re-run / resume support so a failed batch doesn't corrupt the set.
- **Checks that keep the dataset on one configuration** (from M3-3's validation; `Docs/DB_THROTTLING_INJECTOR.md`):
  - *Before each batch:* refuse to start unless the stack matches the frozen configuration: function memory 1,769 MB, Products provisioned at 200 RCU, and one `Scan` of Products costing 0.5 RCU (a partition change would raise it). Record the configuration with the batch.
  - *After each run that has no `db-throttling` fault:* read the Products table's `ThrottledRequests` and `ReadThrottleEvents` for the run's window. Any throttling marks the run contaminated, and it is rerun.
  - *After each `db-throttling` run:* rerun it if `observed.achieved_fraction` is below 0.97 or `observed.label_from` is null (the bank was never seen spent). Added by M4-1's validation: also if the steady throttle rate is more than 0.1 below `1 − 1/offered_ratio` (the window ran on burst capacity; one-sided since M4-3, whose validation saw a spike in the same cascade raise it), or if DynamoDB served the injector more than 1.25× capacity over any minute after `label_from` (burst capacity came back). Added by M4-4's review: also if under 30 s of the fault is left past the minute holding `label_from`, so the run has no window to label (the test set's 2-minute fault, about 3 runs in 10).
  - *After every run:* check the client kept sending. The batch runner holds the `Workload` and reads `outcomes()` after `run()`: rerun the run if any requests were dropped or more than 0.1% got no response (`error`). The manifest records the workload's settings, not what it sent, so a network drop mid-run can otherwise leave a `COMPLETED` run with a gap in its traffic.
- **Runner host:** one small EC2 instance in us-east-2 that runs every batch (training and test). Non-burstable, such as `c7g.large` (about $0.07 an hour; check current pricing), because a burstable T instance runs short of CPU credits over days of load. It runs as `anomalypulse-simulator`, as `--profile sim` does on a laptop: an instance role allowed to assume it (added to `SimulatorRole`'s trust policy in `template.yaml`; Jake deploys) and a `sim` profile on the instance with `credential_source = Ec2InstanceMetadata`. Reach it through Session Manager (no SSH keys or open ports) and run each batch in `tmux`, so a dropped session doesn't stop it. Stop it between batches and terminate it after `M9-1`.

**Definition of Done**
- [x] The runner host repeats M3-2's 20 rps capacity check before generation starts: one full-length NORMAL run and one 10× spike, checked with `telemetry_report` and the spike's `observed.achieved_rps`. Neither goes in the dataset. (`exp-20261016T025315Z-0096`: 10 min, 19.81 rps landed, per-minute CV 0.018, no errors; `exp-20261016T030316Z-60d9`: `achieved_rps` 200.5, 200.49 rps landed, nothing dropped, no 5xx)
- [x] A batch of ≥10 experiments runs unattended on the runner host and registers cleanly. (`batch-20261016T031953Z-120b`: 11 experiments, every injector, all accepted at the first attempt and exported, 03:19–05:25 UTC; `Docs/DATASET.md`, "Validation")
- [x] Tier-1 and Tier-2 data are physically separated in S3, not interleaved. (`datasets/m4-1-validation/tier1/` holds each run's requests and metrics, only catalog Tier-1 and correlation fields; manifests, registry and index are under `tier2/`)
- [x] Registry records generation date + phase per experiment (needed for the temporal split). (`generation_date` and `phase` in every slot record, and in `tier2/experiments.jsonl`)

**Found and fixed in M4-1's validation:** from the runner host the DB-throttling surge ended on a read refused about 3 s into a full burst bank, so the window ran on burst capacity: at 2.0× the table served the whole load for the whole window and Product's p95 didn't move (`exp-20261016T033954Z-d548`). Cause: the injector's one client used one to three connections from inside the region, and over those DynamoDB spent burst at about half the surge's rate, in a seesaw of full and refused seconds. Over 16 clients, as from a laptop, it served the surge in full and the bank ran out at 33 s. Fix: the reads go over 16 clients; the surge ends on 10 s of served reads, not on a throttle; and the batch rejects a run where burst capacity comes back after `label_from`. Validated on the runner at 1.5×, 2.0×, 4.0× and 4.5×, each accepted at its first attempt: labeling starts ~47 s into the window, the steady throttle rate is within a point of `1 − 1/ratio`, and Product's p95 goes from 6 ms to 1.6 or 3.2 s. The fault window stays 5 minutes. That p95 doesn't rise with the ratio the way M3-3 measured, though the throttle counts do; closed in M4-3 without further runs: the bands vary the ratio, and the throttle counts track it (`Docs/EVALUATION_INTEGRITY.md`, "Throttling bands and latency"). `Docs/DATASET.md` has the numbers.

---

### `M4-2` — Generate training + validation set (experiments 1–700)
- **Assignee:** @josh
- **Labels:** `data`, `dataset-generation`, `mvp`
- **Blocked by:** `M4-1`, `M4-3` *(training bands must be defined)*
- **Blocks:** `M5-2`, `M5-3`

**Context.** Execute the Phase 1 generation using the **training bands** (interleaved with the held-out test bands per §6b). Josh owns this because he owns the workload/traffic tooling. Class mix per §17 Phase 1. It runs on `M4-1`'s EC2 runner host.

**Status:** queues built, generation not started — `AnomalyPulse/simulator/build_queues.py` (seed `20261018`), the five queue files `simulator/queues/v1-*.json`, `python -m simulator.spot_check` and [`Docs/TRAINING_SET.md`](../Docs/TRAINING_SET.md), PR #75. Decided in its review (2026-10-19): a cascade template's runs go through every combination of its stages' bands, each stage still 7/7 in training (stages in opposite bands gave a template only two of its combinations); Lambda degradation's targets take turns within each band (in turn over all runs, cart-service went 5/0 by band in validation); the spot check requires every metric field the extractor writes, a snapshot per service for every minute, and a record per request the client sent, and checks every run in the newest batch's queue (it had missed all four kinds of missing telemetry it was tried on); `run-batch` runs `v1`'s train/val queues in slot order. After each batch: the spot check, then labels and features (`Docs/TRAINING_SET.md`, "Running it").

**Deliverables**
- 700 experiments generated per the §17 mix: 140 normal, 140 traffic spike (2×–4×, 6×–8×), 140 DB throttling (offered-load 1.5×–2.5×, 3.5×–4.5×), 105 Lambda degradation (200–500ms, 800–1100ms), 105 dependency failure (500–1500ms, 2500–3500ms), 70 cascading.
- All experiments registered with manifests and Tier-1/Tier-2 separation intact.
- A documented train/val cut (1–560 train, 561–700 validation) for M5 hyperparameter tuning.
- A committed, seeded queue generator that follows `Docs/EVALUATION_INTEGRITY.md`, "Building a queue" (agreed 2026-10-17): blocks of 20 with `integrity.TRAIN_BLOCK`'s mix, bands half and half within each split, Lambda targets and cascade templates in turn, slots numbered in run order, training (four daily queues of 140) before validation. About 5 days nonstop on the runner, 5.5–6 with daily batches.

**Definition of Done**
- [ ] 700 experiments present in the registry with correct class balance and training-band parameters.
- [ ] Spot-check confirms telemetry richness matches M2 (no silently-empty fields). (`python -m simulator.spot_check` after each batch; it passes all 20 accepted runs of `m4-1-validation` and `m4-3-validation`)
- [x] Train/validation boundary documented and reproducible. (`Docs/TRAINING_SET.md`; `dataset.TRAIN_LAST` and `integrity.split_of`; `build_queues --check` and `test_build_queues.py` rebuild the queues from the seed)
- [x] **No test-band parameters used** — test bands remain unseen. (Enforced since M4-3: `run-batch` refuses a `v1` queue entry outside its phase's bands, run shape or mix, `integrity.check_entry`. All five queues pass it, `run-batch --dry-run`.)

---

### `M4-3` — Evaluation-integrity controls (bands, temporal split, diversity protocol)
- **Assignee:** @linu
- **Labels:** `data`, `evaluation`, `leakage-control`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M4-2`, `M9-1`

**Context.** Implement the three structural leakage controls so results mean something. This defines the exact bands the generators consume and the split logic the evaluation enforces — the intellectual core of the "is this real or memorized?" defense.

**Status:** built — `AnomalyPulse/simulator/integrity.py` (bands, split, diversity protocol, stress slice, cascade templates, seeded spec builders, and a queue guard `run-batch` applies to every `v1` queue) and [`Docs/EVALUATION_INTEGRITY.md`](../Docs/EVALUATION_INTEGRITY.md). Decided 2026-10-17: the test set is slots 701–1050 and the stress slice 1051 on; confounders stay a test-only probe, which M9-2 scores three ways; M4-1's p95-by-ratio item is closed without further runs. Validated on the runner 2026-10-18 in three batches into `m4-3-validation`: every cascade template is accepted at its heaviest corners, the test set's heaviest included, after three fixes the validation found: DB throttling now starts before the spike in its cascade, the batch's client keeps 512 requests open (was 256), and the steady-throttle check is one-sided (`Docs/EVALUATION_INTEGRITY.md`, "Validation on the runner").

**Deliverables**
- **Interleaved parameter-space partitioning:** codify the training vs held-out test bands from §6b for every incident type; test bands sit inside the trained range.
- **Cascade specs and retry bands** (M3-5 left both to M4; `Docs/CASCADE.md`, "Open for M4"): the stage lists, bands and onset delays for the 70 training and 30 test cascades, and interleaved bands for `retry-amplification`'s `retries_per_failure`, which §6b doesn't cover. A spec with `retry-amplification` must overlap it with a stage that fails requests, or it has nothing to amplify (M3-5's validation retried 1 request). In training cascades that stage is `payment-failure`: DB throttling's training bands stop at 4.5×, where Product barely fails.
- **Temporal-split logic:** enforce 1–700 = train/val, 701–1050 = test (§17's 350 experiments), 1051 on = the stress slice, keyed off generation date (test generated on a separate day).
- **Simulator diversity protocol** for the test set: varied incident durations (2-min, 12-min), shifted endpoint distribution, and confounder experiments (co-occurring types **not** labeled cascading).
- Definition (not yet execution) of the separate **extreme-extrapolation stress slice** (traffic >10×, added latency >1500ms, offered-load >6×, timeout >5000ms).

**Definition of Done**
- [x] Band definitions committed and importable by the generators; training and test bands provably interleaved, not overlapping. (`integrity.BANDS`, `band()`, the spec builders; `check_bands()` proves the interleaving and its tests feed it broken tables; the guard holds every `v1` queue to the bands)
- [x] Temporal-split function returns the correct partition for any `experiment_id`. (`temporal_split(records)` → `{experiment_id: partition}`, refusing a test run generated on or before the last training day; `split_of(slot)`)
- [x] Diversity-protocol and extreme-slice specs written and ready for M9 execution. (`TEST_MIX`, `VARIED_DURATIONS_S`, `CONFOUNDER_PAIRS`, the stress bands and `COUNTS_*`; M9-1 validates the test-set kinds on the runner first)
- [x] A short "Evaluation Integrity Statement" drafted from §6b for the report. (`Docs/EVALUATION_INTEGRITY.md`)

---

### `M4-4` — Window Labeling Policy + label assignment
- **Assignee:** @melisa
- **Labels:** `data`, `labeling`, `feature-engineering`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M5-2`

**Context.** Aggregate telemetry into one-minute windows and assign labels under the explicit policy from §3 — the step that turns raw runs into clean ML observations and keeps boundary windows from injecting label noise. Owning this positions Melisa for the anomaly-detection modeling in M5.

**Status:** built — `AnomalyPulse/labels/` (`python -m labels`) and [`Docs/LABELING.md`](../Docs/LABELING.md), PR #73. Decided: `retry-amplification` is not counted as a fault; a window that overlaps the DB-throttling surge at all is excluded; only `v1`'s labels are stored in S3 (a validation dataset is labeled from a local copy); the window aggregation is M4-5's (below). Reviewed 2026-10-18–19 and validated on every kind of run M4 generates and on the 20 accepted runs of `m4-1-validation` and `m4-3-validation`. The review also found that the test set's 2-minute DB-throttling fault can leave no labeled window; `run-batch` now reruns such a run (M9-1).

**Deliverables**
- One-minute window aggregation of Tier-1 telemetry (including temporal features: rolling mean/std, rate of change). *Covered by M4-5's feature code, which keys windows by the same `window_start` the labels use, rather than a second copy here (agreed 2026-10-19).*
- **Activity threshold:** a window is labeled with an incident class only if the incident is active ≥50% of the window; else `NORMAL`.
- **Cascade rule:** `CASCADING_FAILURE` only when ≥2 injected faults are concurrently active; early single-fault windows labeled by the observable single fault.
- **Recovery exclusion:** recovery-phase windows excluded from the MVP training set (no fuzzy sixth class).
- **Does `retry-amplification` count as a fault** for "two or more faults concurrently active"? It adds load and fails nothing itself. Decide, and if not, pass `exclude_injectors=("retry-amplification",)` to `cascade.multi_fault_windows()`. *Decided: not counted. It re-sends what another fault failed, so it looks like more of that fault.*
- **DB-throttling onset:** a `db-throttling` activation counts as active from `observed.label_from` (5 s after the injector saw the burst bank spent), not `active_from`. Until then DynamoDB's burst capacity is absorbing the load (the injector surges to spend it) and then the surge's aftermath settles, so the stretch before it is excluded like recovery (`Docs/DB_THROTTLING_INJECTOR.md`).

**Definition of Done**
- [x] Every window carries exactly one label under the policy; boundary windows resolved deterministically. (`policy._verify` on every run; a run's windows split by its start second as `Docs/LABELING.md`, "Windows per run", tabulates)
- [x] Cascade windows verified against the M3 concurrency timeline (no premature `CASCADING_FAILURE`). (`labels/test_run_kinds.py` checks every window against the activations' own times, on every cascade template in train and test bands and on the 20 real validation runs; `policy._verify` holds each to M3's timeline)
- [x] Recovery windows are present in the raw data but excluded from the labeled training set, and that exclusion is logged. (`excluded_reason: recovery` in each run's `labels.jsonl` and in `labeling_log.jsonl`; the requests stay in `tier1/`)

---

### `M4-5` — Feature engineering + Feature/Label Boundary guard
- **Assignee:** @rachel
- **Labels:** `data`, `feature-engineering`, `leakage-control`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M5-2`, `M5-3`

**Context.** Build the Tier-1 feature matrix the models consume, and enforce the boundary that keeps near-labels out of it. Rachel owns this because she knows exactly which fields are fault-injection ground truth (Tier-2). Positions her for the classifier in M5.

**Status:** built — `AnomalyPulse/features/` (`python -m features`) and [`Docs/FEATURE_MATRIX.md`](../Docs/FEATURE_MATRIX.md), PR #76: 106 columns per complete one-minute window. Decided: temporal features look back over a run's labeled windows and its startup minute only, so the DB-throttling surge never reaches a labeled window and no training row has empty temporal columns; DynamoDB latency per service is a feature (it separates DB throttling from a Lambda slowdown); the guard catches a forbidden name anywhere in a column name; only `v1`'s features are stored in S3, and a re-run rewrites them, as for labels. Reviewed 2026-10-19 and validated on the 20 accepted runs of `m4-1-validation` and `m4-3-validation`. `v1`'s matrix is written once M4-2 has generated it and M4-4's labeler has labeled it.

**Deliverables**
- Tier-1 feature matrix per window: `request_rate`, latency percentiles (p50/p95/p99), error rates, `lambda_duration_ms`, `cold_start`, DynamoDB `ConsumedCapacity`, **`ThrottledRequests` counts**, dependency latency/error/timeout counts, temporal features. *Per service, plus DynamoDB latency. Telemetry can't tell a dependency timeout from another dependency failure, so the two are one count.*
- An **automated guard that fails the build** if any Tier-2 field name (e.g. `error_type`, `db_throttled`, any fault-injection param, the label) appears in the feature matrix.
- Feature matrix materialized to S3 in the M4-1 layout, joined to Melisa's labels by window key. *`python -m features --dataset v1 --bucket <bucket>`, after generation and labeling: `v1` doesn't exist yet.*

**Definition of Done**
- [x] Feature matrix contains **only** Tier-1 fields; a deliberate Tier-2 injection makes the guard fail the build (test the guard). (`features/guard.py`: the field catalog's Tier-2 fields, the manifest schema's activation fields, every injector's parameter, the join keys and the label, matched as whole words anywhere in a column name. A planted column stops `build_matrix`, the writer and `python -m features` before anything is written: `test_guard.py`, `test_build_matrix.py`, `test_write_matrix.py`, `test_cli.py`)
- [x] Features and labels join cleanly on window key with no orphans. (Checked on every run before it is written. All 20 validation runs: 180 feature rows, 123 training rows, 0 orphans)
- [x] Matrix documented (column → definition → tier) for the report. (`Docs/FEATURE_MATRIX.md`, generated from the code; a test fails when the two disagree)
