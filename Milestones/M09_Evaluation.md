# Milestone 9 — Evaluation

> **GitHub Milestone**
> **Title:** `M9 — Evaluation`
> **Timeframe:** Weeks 8–9
> **Depends on:** `M8`
> **Description:**
> Run the full experiment suite and produce the report's quantitative spine: detection + classification metrics, detection/diagnosis/end-to-end latency, GenAI quality, cloud cost, and — the point of it all — evaluation-integrity results that show generalization **across held-out regions of the simulator's parameter space** (not to real-world incidents; §6b). The milestone **opens by generating and unsealing the test set** (experiments 701–1000), which has sat untouched since M4 by design. Everyone measures the surface they own.

## Assignment summary
- **Josh** — Generate + unseal the sealed test set (Phase 2) **+** GenAI quality evaluation
- **Jake** — Cloud cost + operational-performance measurement
- **Melisa** — Detection + classification metrics (per-band, extreme slice separate, confounder)
- **Rachel** — Threshold-baseline comparison + evaluation-integrity / generalization reporting
- **Linu** — Latency measurement (detection / diagnosis / end-to-end) + results visualizations for the report

## Coordination notes
- **Test set only.** Every headline metric is computed on the temporally held-out test set (701–1000), never on train/validation. Josh's `M9-1` is a hard predecessor for all metric issues.
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
- Separate **extreme-extrapolation stress slice** (traffic >10×, latency >1500ms, offered-load >6×, timeout >5000ms) generated and tagged distinctly.
- Test set registered, dated, and confirmed **not inspected** until all training/validation work was complete.

**Definition of Done**
- [ ] Test set generated on a separate day from training (dates in the registry prove it).
- [ ] Held-out interleaved bands used — no overlap with training bands.
- [ ] Extreme slice tagged separately from the core test set.
- [ ] Confounder and varied-duration experiments present and correctly labeled (confounders **not** cascading).

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
- **Confounder accuracy:** how often co-occurring-but-not-cascading incidents are handled correctly.

**Definition of Done**
- [ ] All metrics computed on test set (701–1000) only.
- [ ] Per-band breakdown shows no sharp degradation on held-out bands (or the degradation is reported honestly).
- [ ] Extreme slice is a separate line item, not folded into headline numbers.

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

**Definition of Done**
- [ ] Every ML result has its baseline shown next to it.
- [ ] Baseline-margin (multivariate learning) and train/test-gap (overfitting) are reported as two separate signals.
- [ ] The report explicitly disclaims real-world generalization.

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

**Definition of Done**
- [ ] Cost-per-inference is a real measured number (near-zero idle demonstrated).
- [ ] Monthly projections given for at least two request volumes.
- [ ] Operational metrics collected from the live pipeline, not estimated.

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

**Definition of Done**
- [ ] Evidence fidelity and hallucination rate computed over test-set diagnoses.
- [ ] Actionability scored against a written rubric by ≥1 human rater.
- [ ] Root-cause correctness explicitly **not** used as the headline GenAI metric (documented why).

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

**Definition of Done**
- [ ] Full latency chain measured and attributed (how much is the aggregation window).
- [ ] A clean figure set covers detection, classification, cost, and latency.
- [ ] Figures are reproducible from the test-set results, not hand-drawn.
