# Milestone 8 — Integration

> **GitHub Milestone**
> **Title:** `M8 — Integration`
> **Timeframe:** Week 7
> **Depends on:** `M5`, `M6`, `M7`
> **Description:**
> Connect the pieces into one live loop: `telemetry → ML detection → incident classification → Bedrock → dashboard`. The team **reconverges** here — everyone integrates along the seam they own. This is also where the full **event-driven telemetry pipeline** (EventBridge → SQS → Aggregator Lambda), deferred from M2, gets wired so telemetry flows to the ML path automatically rather than by batch. **MVP-first:** get the single traffic-spike incident flowing end-to-end through the *live* loop before confirming the full taxonomy flows.

## Assignment summary
- **Jake** — Full event-driven telemetry pipeline (EventBridge → SQS → Aggregator → S3/Incidents) + end-to-end IAM
- **Melisa** — Detect + classify runtime integration inside the Diagnosis Lambda
- **Rachel** — Classify → Bedrock input-contract seam + persist diagnosis to the Incidents store
- **Josh** — Bedrock output → incident store / dashboard feed
- **Linu** — Dashboard → live incident feed (replace mocks)

## Coordination notes
- The integration order follows the data flow, so the seams have a natural sequence: **Jake's pipeline → Melisa's detect/classify → Rachel's classify→Bedrock → Josh's Bedrock→feed → Linu's live UI**. Each is blocked by the prior seam but the *interfaces* were all agreed in M5–M7, so gaps should be wiring, not redesign.
- Preserve the two hard invariants end-to-end: **Tier-1-only features** into the models (M4-5 guard still green on the live path) and **observed-sequence wording** on any ordering shown to a human.
- **The pipeline is its own stack, and runs alongside the shipper until M9-1.** `anomalypulse-shared` isn't deployed until M9-1 has generated the test set (README, "Status"), so `M8-1` doesn't replace the shipper or edit its subscription filters: the test set's telemetry must reach S3 the way training's did. The pipeline reads what the shipper and the metrics extractor land, triggered by an EventBridge schedule or a second subscription filter per service log group (each has 1 of 2), never by S3 notifications on the shared bucket. It writes under its own prefix and to its own Incidents table, never `raw/`, `metrics/`, `experiments/` or `datasets/`. Replacing the shipper, if still wanted, comes after M9-1.

---

## Issues

### `M8-1` — Event-driven telemetry pipeline + end-to-end IAM
- **Assignee:** @jake
- **Labels:** `integration`, `infra`, `eventbridge`, `sqs`, `iam`
- **Blocked by:** `M2-1`
- **Blocks:** `M8-2`

**Context.** Replace batch telemetry movement with the async pipeline from the §1 architecture: telemetry → EventBridge → SQS (buffer/decouple) → Aggregator Lambda → S3 (raw) + Incidents DynamoDB. Aggregator produces the one-minute windows the models expect, live. Own the IAM that lets each hop talk to the next under least privilege.

**Deliverables**
- EventBridge routing + SQS telemetry queue for buffering/decoupling.
- Aggregator Lambda producing one-minute feature windows on the live path (reusing M4 aggregation logic).
- Writes to S3 (raw) and the Incidents DynamoDB table.
- End-to-end least-privilege IAM across the whole flow.

**Decided (2026-11-01; [`Docs/PIPELINE.md`](../Docs/PIPELINE.md)):** a second subscription filter on each service
log group feeds a forwarder Lambda, then EventBridge, SQS and the Aggregator, so every record passes through the
queue. Each minute is built twice: a health summary from the request records just after the minute ends, for
the dashboard, and the full 106-column window about 2 minutes later, once the metrics extractor's CloudWatch
snapshot lands, for the models. The window is `features.build_matrix`'s, unchanged: with no labels live, its
temporal columns look back over every earlier minute, which changes 2 of 885 validation answers (one
classifier window, one detector window). The Aggregator announces each window on the bus, and M8-2 subscribes.

**Definition of Done**
- [ ] Telemetry flows automatically from a service request to a materialized feature window without manual steps.
- [ ] SQS absorbs a burst without loss (backlog observable for §16 metrics).
- [ ] IAM verified least-privilege at each hop.

---

### `M8-2` — Detect + classify runtime integration (Diagnosis Lambda)
- **Assignee:** @melisa
- **Labels:** `integration`, `ml`, `lambda`
- **Blocked by:** `M8-1`, `M5-1`, `M5-2`, `M5-3`
- **Blocks:** `M8-3`

**Context.** Wire the live feature window through anomaly detection then the classifier inside the Diagnosis Lambda, emitting a classified incident (type + confidence). Melisa owns this as the detection modeler; it uses Rachel's classifier artifact via the M5-1 loader.

**Deliverables**
- Diagnosis Lambda path: window → anomaly detector → (if anomalous) classifier → `{incident_type, confidence}`.
- Live Tier-1-only feature vector construction (guard still enforced on the live path).
- Detection-latency instrumentation on the live path for §16.

**What live windows do (measured 2026-11-01, [`Docs/PIPELINE.md`](../Docs/PIPELINE.md)):** on validation,
live-built windows change the classifier's answer once (a cascade's first minute after DB throttling's surge
reads `DATABASE_THROTTLING`) and the detector's once (the first NORMAL minute after idle time). The live path
also scores minutes training never did. The detector flags every minute whose look-back still holds a fault,
for about 3 minutes past a fault's end, and the first full minute after idle time. The classifier names an
incident in fewer of them (78 of 112 first recovery minutes, 24 of 56 second, 2 of 138 third). How flagged
minutes open and close an incident is M8-2's and M8-3's call.

**Definition of Done**
- [ ] A live traffic spike is detected and classified end-to-end (MVP loop through the live path).
- [ ] Full taxonomy classifies correctly on a live run of each injector.
- [ ] Live features confirmed Tier-1-only.

---

### `M8-3` — Classify → Bedrock seam + persist diagnosis
- **Assignee:** @rachel
- **Labels:** `integration`, `ml`, `genai`, `dynamodb`
- **Blocked by:** `M8-2`, `M5-3`, `M6-1`
- **Blocks:** `M8-4`

**Context.** Turn the classifier output into the structured-evidence input Bedrock expects (the contract agreed in M6), invoke Bedrock, and persist the resulting diagnosis to the Incidents store. This is the classify-to-explain hand-off Rachel and Josh specified in M5/M6.

**Deliverables**
- Transform `{incident_type, confidence, metric deltas, affected services}` into the §10 Bedrock input contract.
- Invoke Bedrock (M6 pipeline) and capture the structured diagnosis.
- Persist prediction + diagnosis + ground-truth reference to the Incidents DynamoDB table.

**Definition of Done**
- [ ] Classifier output is transformed into a valid Bedrock input with no manual editing.
- [ ] Diagnosis is stored against the incident record with a ground-truth reference for comparison.
- [ ] Round-trip latency captured for §16 end-to-end metrics.

---

### `M8-4` — Bedrock output → incident feed
- **Assignee:** @josh
- **Labels:** `integration`, `genai`, `api`
- **Blocked by:** `M8-3`, `M6-1`
- **Blocks:** `M8-5`

**Context.** Make the stored diagnosis consumable by the dashboard feed in the exact shape the console expects, preserving the observed-sequence wording and the evidence bullets end-to-end.

**Deliverables**
- Mapping from the stored diagnosis to the dashboard data contract fields (summary, root cause, evidence, actions).
- Verification that observed-sequence wording and evidence-to-metric traceability survive the hand-off.

**Definition of Done**
- [ ] A stored diagnosis appears in the feed contract with all sections intact.
- [ ] No ordering claim is rendered as causation.
- [ ] Every evidence bullet still traces to an input metric.

---

### `M8-5` — Dashboard live wiring
- **Assignee:** @linu
- **Labels:** `integration`, `dashboard`, `frontend`
- **Blocked by:** `M8-4`, `M7-1`, `M7-2`
- **Blocks:** —

**Context.** Swap the console from the mock feed to Jake's live read API now backed by real incidents and diagnoses — the payoff of building against a fixed contract in M7.

**Deliverables**
- Frontend pointed at the live BFF; mock feed removed or gated behind a demo flag.
- Live end-to-end smoke: inject → detect → classify → diagnose → **render on the console with ground truth**.

**Definition of Done**
- [ ] Console shows a real injected incident with live classification, confidence, diagnosis, and ground truth.
- [ ] The full `telemetry → ML → classification → Bedrock → dashboard` loop runs unattended for one incident type (MVP), then for the full taxonomy.
- [ ] Mock feed no longer required for the demo path.
