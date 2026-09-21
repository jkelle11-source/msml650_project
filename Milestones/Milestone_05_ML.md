# Milestone 5 — ML

> **GitHub Milestone**
> **Title:** `M5 — ML`
> **Timeframe:** Weeks 5–6 *(runs concurrently with `M6` and `M7`)*
> **Depends on:** `M4`
> **Description:**
> Build the measurable core: a rule-based baseline, unsupervised + supervised anomaly detection, and a six-class incident classifier — trained and tuned in SageMaker, with **inference running in the Diagnosis Lambda** (not on a persistent endpoint), so the runtime path stays genuinely serverless and cost-per-inference is meaningful (§9). This is where the **capability reassignment** begins: Melisa and Rachel own the modeling, Jake owns the training-and-inference infrastructure, while **Josh works `M6` (Bedrock)** and **Linu works `M7` (Dashboard)** in parallel — so the two non-modeling tracks aren't idle waiting on ML.

## Assignment summary
- **Jake** — SageMaker training + HPO infra, artifact store, **Diagnosis Lambda inference path**
- **Melisa** — Anomaly detection: Isolation Forest (unsupervised) + supervised binary detector
- **Rachel** — Incident classifier: XGBoost (+ RF / LogReg baselines) + **Model 0 threshold baseline**
- *(Josh → `M6`, Linu → `M7` — see those files)*

## Coordination notes
- **The LLM is not the anomaly detector.** Detection/classification is fully owned by this milestone's ML; Bedrock only explains (§8, §20). Keep that separation clean.
- **SageMaker for training only.** A persistent real-time endpoint is billed per idle hour and would be the single most expensive component in an otherwise serverless design; it would also undercut the §16 cost story. Artifact → S3 → loaded by the Diagnosis Lambda at cold start (or a container Lambda). A managed inference path, if ever wanted, uses **SageMaker Serverless Inference** with an explicit idle-cost comparison — not a real-time endpoint.
- **Report against the baseline, and read the two signals correctly** (§8, Model 0): the **margin over the threshold baseline** measures *multivariate learning* (most informative on cascades); the **train/test gap on held-out bands** measures *overfitting to simulator artifacts*. They are different questions — a small margin is not by itself evidence of overfitting.
- All final metrics are computed on the temporally held-out test set in M9, never on train/validation.

---

## Issues

### `M5-1` — SageMaker training/HPO infra + Diagnosis Lambda inference path
- **Assignee:** @jake
- **Labels:** `ml`, `sagemaker`, `lambda`, `infra`, `mvp`
- **Blocked by:** `M4-1`
- **Blocks:** `M5-2`, `M5-3`, `M8-2`

**Context.** The training lifecycle and the serverless runtime path. Jake owns this because it's platform work — training jobs, artifact packaging, and a Lambda that loads a tiny model from S3 and runs inference over a few thousand one-minute windows.

**Deliverables**
- SageMaker training job(s) + hyperparameter tuning wired to the M4 feature matrix in S3.
- Model **artifact store** in S3 (versioned) and a documented package format the Lambda can load.
- **Diagnosis Lambda** skeleton that loads an artifact from S3 at cold start (or container image) and runs prediction on a window vector.
- A documented "cost per inference" measurement hook for M9.

**Definition of Done**
- [ ] A trivial model trains in SageMaker and its artifact lands versioned in S3.
- [ ] Diagnosis Lambda loads that artifact and returns a prediction for a sample window.
- [ ] No persistent SageMaker endpoint exists in the runtime path (verified in the cost story).
- [ ] Training is re-runnable from the registered dataset without manual steps.

---

### `M5-2` — Anomaly detection: Isolation Forest + supervised binary detector
- **Assignee:** @melisa
- **Labels:** `ml`, `anomaly-detection`, `mvp`
- **Blocked by:** `M5-1`, `M4-2`, `M4-4`
- **Blocks:** `M8-2`

**Context.** Two detectors answering NORMAL vs ANOMALY. Isolation Forest is included **deliberately** to model the realistic no-labels-in-production case and give a detection signal independent of the taxonomy; the supervised binary detector quantifies the cost of going unsupervised, since we *do* have labels here.

**Deliverables**
- **Isolation Forest** anomaly detector on Tier-1 features (start simple/interpretable per §8).
- **Supervised binary detector** (NORMAL vs ANOMALY) as the labeled-data comparison point.
- Both trained via the M5-1 SageMaker path and packaged as loadable artifacts.
- Validation-set metrics (precision/recall/F1, FPR, detection latency) with the unsupervised-vs-supervised gap reported.

**Definition of Done**
- [ ] Both detectors train through SageMaker and produce loadable artifacts.
- [ ] Validation metrics reported for both, with the cost of going unsupervised quantified.
- [ ] Detection latency reported and understood as window-dominated (~1 min) per §16.
- [ ] Features confirmed Tier-1-only (M4-5 guard green).

---

### `M5-3` — Incident classifier (XGBoost + baselines) + Model 0 threshold baseline
- **Assignee:** @rachel
- **Labels:** `ml`, `classification`, `baseline`, `mvp`
- **Blocked by:** `M5-1`, `M4-2`, `M4-4`, `M4-5`
- **Blocks:** `M8-3`

**Context.** The six-class classifier (NORMAL / TRAFFIC_SPIKE / DATABASE_THROTTLING / LAMBDA_DEGRADATION / DEPENDENCY_FAILURE / CASCADING_FAILURE) plus the trivial rule-based floor everything is measured against. Pairing the baseline with the classifier keeps the "does ML actually beat a threshold?" comparison in one owner's hands.

**Deliverables**
- **Model 0 threshold baseline:** flag ANOMALY if `p95_latency > 2× rolling mean` OR `error_rate > 5%` — the performance floor.
- **XGBoost** six-class classifier, plus Random Forest and Logistic Regression baselines for the progression story (§8).
- Trained via the M5-1 SageMaker path; selected artifact packaged for the Diagnosis Lambda.
- Validation metrics: accuracy, macro-F1, confusion matrix, per-class precision/recall — **reported alongside the threshold baseline** so the margin is explicit.

**Definition of Done**
- [ ] Threshold baseline implemented and its metrics recorded as the floor.
- [ ] XGBoost + at least one other classifier trained; all reported against the baseline on validation data.
- [ ] The cascade margin over baseline is called out (evidence of genuine multivariate learning where no single metric fires).
- [ ] Selected model artifact loads and predicts in the Diagnosis Lambda (hand-off to M8 confirmed).
- [ ] Features confirmed Tier-1-only (M4-5 guard green).
