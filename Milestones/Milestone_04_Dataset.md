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

**Definition of Done**
- [ ] A batch of ≥10 experiments runs unattended and registers cleanly.
- [ ] Tier-1 and Tier-2 data are physically separated in S3, not interleaved.
- [ ] Registry records generation date + phase per experiment (needed for the temporal split).

---

### `M4-2` — Generate training + validation set (experiments 1–700)
- **Assignee:** @josh
- **Labels:** `data`, `dataset-generation`, `mvp`
- **Blocked by:** `M4-1`, `M4-3` *(training bands must be defined)*
- **Blocks:** `M5-2`, `M5-3`

**Context.** Execute the Phase 1 generation using the **training bands** (interleaved with the held-out test bands per §6b). Josh owns this because he owns the workload/traffic tooling. Class mix per §17 Phase 1.

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
