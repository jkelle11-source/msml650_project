# Milestone 4 — Dataset

> **GitHub Milestone**
> **Title:** `M4 — Dataset`
> **Timeframe:** Week 4
> **Depends on:** `M3`
> **Description:**
> Turn the simulator into a **labeled, leakage-controlled dataset**: run the training experiments, aggregate telemetry into one-minute windows, apply the Window Labeling Policy, enforce the Feature/Label Boundary, and stand up the evaluation-integrity controls (interleaved parameter bands, temporal split, diversity protocol). This is the last sticky-ownership milestone, and it deliberately pre-positions people for the M5 capability split: Melisa (labeling) and Rachel (features) carry ML modeling; Josh (generation) moves to GenAI; Linu (eval controls) moves to dashboard; Jake (harness) moves to ML infra.
>
> **Scope boundary:** this milestone produces the **training + validation** set (experiments 1–700) and the *definitions/harness* for the test set. The test set (701–1000) is generated on a **separate day and sealed**, unopened until M9 — see `M9-1`.

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
  - **The stack keeps its configuration as of PR #70** for the whole dataset: 1,769 MB functions (PR #67); Product builds its table at load; the Products table `anomalypulse-products-v2`, created provisioned at 200 RCU / 5 WCU and never switched (a `GET /products` scan costs 0.5 RCU); DynamoDB retries stated in Product, Cart and Order (legacy mode, 7 attempts); the Lambda runtime pinned (`UpdateRuntimeOn: FunctionUpdate`). Deploy only from `main` or an up-to-date branch, never mid-generation.
  - **The registry holds only runs M4 launched.** The six manifests written before M4 are test and validation runs, and none ran on these settings.
  - **Labeling and features:** use `COMPLETED` runs only, deduplicate raw records by `(service, request_id)`, and consider dropping each run's first minute (startup 404s, cold starts after an idle gap).
  - **Throughput:** runs are serial, and a default run lasts 15 minutes, so 700 runs take ~175 hours back to back. Run length is the lever; decide it before generation starts.

---

## Issues

### `M4-1` — Dataset-generation harness, S3 layout & experiment registry
- **Assignee:** @jake
- **Labels:** `data`, `infra`, `tooling`, `mvp`
- **Blocked by:** `M3-1`
- **Blocks:** `M4-2`, `M4-3`, `M4-4`, `M4-5`

**Context.** The batch layer on top of the simulator: run N experiments unattended, register each with its manifest, and lay the data out in S3 so features/labels and the temporal split are trivial to compute.

**Deliverables**
- Batch runner that executes a queue of experiments (params from bands) and records completion status.
- S3 dataset layout separating **Tier-1 features** from **Tier-2 ground truth**, keyed by `experiment_id` and window.
- Experiment **registry** (which experiments exist, their bands, phase = train/val/test, generation date).
- Idempotent re-run / resume support so a failed batch doesn't corrupt the set.
- **Checks that keep the dataset on one configuration** (from M3-3's validation; `Docs/DB_THROTTLING_INJECTOR.md`):
  - *Before each batch:* refuse to start unless the stack matches the frozen configuration: function memory 1,769 MB, Products provisioned at 200 RCU, and one `Scan` of Products costing 0.5 RCU (a partition change would raise it). Record the configuration with the batch.
  - *After each run that has no `db-throttling` fault:* read the Products table's `ThrottledRequests` and `ReadThrottleEvents` for the run's window. Any throttling marks the run contaminated, and it is rerun.
  - *After each `db-throttling` run:* rerun it if `observed.achieved_fraction` is below 0.97 or `observed.label_from` is null (it never throttled).
  - *After every run:* check the client kept sending. The batch runner holds the `Workload` and reads `outcomes()` after `run()`: rerun the run if any requests were dropped or more than 0.1% got no response (`error`). The manifest records the workload's settings, not what it sent, so a network drop mid-run can otherwise leave a `COMPLETED` run with a gap in its traffic.
- **Runner host:** one small EC2 instance in us-east-2 that runs every batch (training and test). Non-burstable, such as `c7g.large` (about $0.07 an hour; check current pricing), because a burstable T instance runs short of CPU credits over days of load. It runs as `anomalypulse-simulator`, as `--profile sim` does on a laptop: an instance role allowed to assume it (added to `SimulatorRole`'s trust policy in `template.yaml`; Jake deploys) and a `sim` profile on the instance with `credential_source = Ec2InstanceMetadata`. Reach it through Session Manager (no SSH keys or open ports) and run each batch in `tmux`, so a dropped session doesn't stop it. Stop it between batches and terminate it after `M9-1`.

**Definition of Done**
- [ ] The runner host repeats M3-2's 20 rps capacity check before generation starts: one full-length NORMAL run and one 10× spike, checked with `telemetry_report` and the spike's `observed.achieved_rps`. Neither goes in the dataset.
- [ ] A batch of ≥10 experiments runs unattended on the runner host and registers cleanly.
- [ ] Tier-1 and Tier-2 data are physically separated in S3, not interleaved.
- [ ] Registry records generation date + phase per experiment (needed for the temporal split).

---

### `M4-2` — Generate training + validation set (experiments 1–700)
- **Assignee:** @josh
- **Labels:** `data`, `dataset-generation`, `mvp`
- **Blocked by:** `M4-1`, `M4-3` *(training bands must be defined)*
- **Blocks:** `M5-2`, `M5-3`

**Context.** Execute the Phase 1 generation using the **training bands** (interleaved with the held-out test bands per §6b). Josh owns this because he owns the workload/traffic tooling. Class mix per §17 Phase 1. It runs on `M4-1`'s EC2 runner host.

**Deliverables**
- 700 experiments generated per the §17 mix: 140 normal, 140 traffic spike (2×–4×, 6×–8×), 140 DB throttling (offered-load 1.5×–2.5×, 3.5×–4.5×), 105 Lambda degradation (200–500ms, 800–1100ms), 105 dependency failure (500–1500ms, 2500–3500ms), 70 cascading.
- All experiments registered with manifests and Tier-1/Tier-2 separation intact.
- A documented train/val cut (1–560 train, 561–700 validation) for M5 hyperparameter tuning.

**Definition of Done**
- [ ] 700 experiments present in the registry with correct class balance and training-band parameters.
- [ ] Spot-check confirms telemetry richness matches M2 (no silently-empty fields).
- [ ] Train/validation boundary documented and reproducible.
- [ ] **No test-band parameters used** — test bands remain unseen.

---

### `M4-3` — Evaluation-integrity controls (bands, temporal split, diversity protocol)
- **Assignee:** @linu
- **Labels:** `data`, `evaluation`, `leakage-control`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M4-2`, `M9-1`

**Context.** Implement the three structural leakage controls so results mean something. This defines the exact bands the generators consume and the split logic the evaluation enforces — the intellectual core of the "is this real or memorized?" defense.

**Deliverables**
- **Interleaved parameter-space partitioning:** codify the training vs held-out test bands from §6b for every incident type; test bands sit inside the trained range.
- **Cascade specs and retry bands** (M3-5 left both to M4; `Docs/CASCADE.md`, "Open for M4"): the stage lists, bands and onset delays for the 70 training and 30 test cascades, and interleaved bands for `retry-amplification`'s `retries_per_failure`, which §6b doesn't cover. A spec with `retry-amplification` must overlap it with a stage that fails requests, or it has nothing to amplify (M3-5's validation retried 1 request). In training cascades that stage is `payment-failure`: DB throttling's training bands stop at 4.5×, where Product barely fails.
- **Temporal-split logic:** enforce 1–700 = train/val, 701–1000 = test, keyed off generation date (test generated on a separate day).
- **Simulator diversity protocol** for the test set: varied incident durations (2-min, 12-min), shifted endpoint distribution, and confounder experiments (co-occurring types **not** labeled cascading).
- Definition (not yet execution) of the separate **extreme-extrapolation stress slice** (traffic >10×, added latency >1500ms, offered-load >6×, timeout >5000ms).

**Definition of Done**
- [ ] Band definitions committed and importable by the generators; training and test bands provably interleaved, not overlapping.
- [ ] Temporal-split function returns the correct partition for any `experiment_id`.
- [ ] Diversity-protocol and extreme-slice specs written and ready for M9 execution.
- [ ] A short "Evaluation Integrity Statement" drafted from §6b for the report.

---

### `M4-4` — Window Labeling Policy + label assignment
- **Assignee:** @melisa
- **Labels:** `data`, `labeling`, `feature-engineering`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M5-2`

**Context.** Aggregate telemetry into one-minute windows and assign labels under the explicit policy from §3 — the step that turns raw runs into clean ML observations and keeps boundary windows from injecting label noise. Owning this positions Melisa for the anomaly-detection modeling in M5.

**Deliverables**
- One-minute window aggregation of Tier-1 telemetry (including temporal features: rolling mean/std, rate of change).
- **Activity threshold:** a window is labeled with an incident class only if the incident is active ≥50% of the window; else `NORMAL`.
- **Cascade rule:** `CASCADING_FAILURE` only when ≥2 injected faults are concurrently active; early single-fault windows labeled by the observable single fault.
- **Recovery exclusion:** recovery-phase windows excluded from the MVP training set (no fuzzy sixth class).
- **Does `retry-amplification` count as a fault** for "two or more faults concurrently active"? It adds load and fails nothing itself. Decide, and if not, pass `exclude_injectors=("retry-amplification",)` to `cascade.multi_fault_windows()`.
- **DB-throttling onset:** a `db-throttling` activation counts as active from `observed.label_from` (5 s after the first throttle), not `active_from`. Until then DynamoDB's burst capacity is absorbing the load (the injector surges to spend it) and then the surge's aftermath settles, so the stretch before it is excluded like recovery (`Docs/DB_THROTTLING_INJECTOR.md`).

**Definition of Done**
- [ ] Every window carries exactly one label under the policy; boundary windows resolved deterministically.
- [ ] Cascade windows verified against the M3 concurrency timeline (no premature `CASCADING_FAILURE`).
- [ ] Recovery windows are present in the raw data but excluded from the labeled training set, and that exclusion is logged.

---

### `M4-5` — Feature engineering + Feature/Label Boundary guard
- **Assignee:** @rachel
- **Labels:** `data`, `feature-engineering`, `leakage-control`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M5-2`, `M5-3`

**Context.** Build the Tier-1 feature matrix the models consume, and enforce the boundary that keeps near-labels out of it. Rachel owns this because she knows exactly which fields are fault-injection ground truth (Tier-2). Positions her for the classifier in M5.

**Deliverables**
- Tier-1 feature matrix per window: `request_rate`, latency percentiles (p50/p95/p99), error rates, `lambda_duration_ms`, `cold_start`, DynamoDB `ConsumedCapacity`, **`ThrottledRequests` counts**, dependency latency/error/timeout counts, temporal features.
- An **automated guard that fails the build** if any Tier-2 field name (e.g. `error_type`, `db_throttled`, any fault-injection param, the label) appears in the feature matrix.
- Feature matrix materialized to S3 in the M4-1 layout, joined to Melisa's labels by window key.

**Definition of Done**
- [ ] Feature matrix contains **only** Tier-1 fields; a deliberate Tier-2 injection makes the guard fail the build (test the guard).
- [ ] Features and labels join cleanly on window key with no orphans.
- [ ] Matrix documented (column → definition → tier) for the report.
