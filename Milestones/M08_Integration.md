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
the dashboard, and the full 106-column window about 2¾ minutes after it ends, once the metrics extractor's
CloudWatch snapshot lands, for the models. The window is `features.build_matrix`'s, unchanged: with no labels
live, its temporal columns look back over every earlier minute, which changes 2 of 885 validation answers (one
classifier window, one detector window). A window builder Lambda, run every minute after the Aggregator has
filed the records, builds both and announces each complete window on the bus (WindowReady), and M8-2
subscribes. The newest health is also an item in the Incidents table, for the dashboard.

**Definition of Done**
- [x] Telemetry flows automatically from a service request to a materialized feature window without manual steps.
- [x] SQS absorbs a burst without loss (backlog observable for §16 metrics).
- [x] IAM verified least-privilege at each hop.

**Done (2026-11-04 and 05; [`Docs/PIPELINE.md`](../Docs/PIPELINE.md), "Load tests" and "IAM at each hop"):**

- **Items 1 and 2.** Every record reached `pipeline/records/`, in a normal run, a 10× burst and a run with the
  Aggregator paused for 8½ minutes. Each was checked record by record against the shipper's `raw/` copy, and every
  complete window was built and announced. The backlog is the queue's `ApproximateAgeOfOldestMessage`, alarmed
  past 120 s (`anomalypulse-telemetry-late`).
- **Item 3.** Each role allows only what its Lambda's code calls, and each Lambda can be invoked only by its one
  caller. The checks:
  - Access Analyzer found nothing on any of the deployed policies.
  - IAM's policy simulator answered 46 of 46 allowed and denied cases as expected.
  - Every service each role is granted has been used.
  - A live run after the last change had no errors.

  The review added one restriction: each sender may put only its own events on the bus. The window builder's
  bucket-wide `ListBucket` is accepted, and the shipper's broad grants are left for after M9-1.

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

**Decided (2026-11-06; [`Docs/LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md)):** a rule on the pipeline bus sends
each WindowReady event to the Diagnosis Lambda. It runs M5-2's Isolation Forest (promoted as `anomaly_iforest`),
then the classifier on the windows the forest flags, and a window gets an `incident_type` only when both agree.
The reply goes back on the bus as the Lambda's success destination, for M8-3. An event that fails three times, or
that EventBridge can't deliver, goes to an alarmed failure queue. The Lambda keeps no state: `incident.py` is the
open/close rule proposed for M8-3. It opens on the first minute with a type, closes after 2 minutes without one,
and reports the type with the most minutes. On validation's live-built windows, every fault run opened exactly one
incident, of its own type (112 of 112), and no healthy run opened one.

**Definition of Done**
- [x] A live traffic spike is detected and classified end-to-end (MVP loop through the live path).
- [x] Full taxonomy classifies correctly on a live run of each injector.
- [x] Live features confirmed Tier-1-only.

**Done (2026-11-06 and 07; [`Docs/LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md), "The live traffic spike" and "The
live run of each injector"):**

- **Item 1.** One live traffic spike (`exp-20261106T234446Z-809e`, ×6–8 band, 5 minutes) went through the pipeline
  and the deployed Diagnosis Lambda with no manual step. The fault's 5 full minutes and the first minute after it
  came back `TRAFFIC_SPIKE` (confidence 0.987–1.0), and `incident.py` opened one incident on them and closed it
  74 s after the fault ended. No other minute had an incident type, including the 5 the forest alone flagged (the
  two after idle time, the onset minute, the second minute after the fault, and the run's last, part-traffic one).
  Each window was diagnosed 169–177 s after its minute ended, with no errors and no failed or dropped deliveries.
- **Item 2.** One live run of each injector plus a NORMAL run, on training bands at intensity 0.7. Each fault run
  opened exactly one incident, which closed as its own type, and the NORMAL run opened none:
  - `exp-20261107T020723Z-592b` (noop): no incident.
  - `exp-20261107T022559Z-048a` (db-throttling): `DATABASE_THROTTLING`, opened 1 s after the fault started, ended
    1 s after it ended.
  - `exp-20261107T025044Z-2a61` (lambda-degradation, order-service): `LAMBDA_DEGRADATION`, opened 16 s after the
    start, ended 2 min 16 s after the end.
  - `exp-20261107T030745Z-595a` (payment-failure): `DEPENDENCY_FAILURE`, opened 15 s after the start, ended
    1 min 15 s after the end.
  - `exp-20261107T032517Z-e1fc` (cascade, `example.json`): `CASCADING_FAILURE`, opened 43 s after the start, ended
    1 min 43 s after the end. It changed type 3 times on the way (`TRAFFIC_SPIKE`, `CASCADING_FAILURE`,
    `DATABASE_THROTTLING`, then `CASCADING_FAILURE`).

  Every run: 0 errors, 0 failed or dropped deliveries, the failure queue empty, alarm OK, each window diagnosed
  167–174 s after its minute ended.

- **Item 3.** The Lambda started with both models' columns checked against the Tier-1 guard's list, and none of
  the run's 16 windows was refused (the failure queue stayed empty). `ml/diagnosis`'s tests check the list against
  `features.guard` and the 106 real columns against the Lambda's rule.

---

### `M8-3` — Classify → Bedrock seam + persist diagnosis
- **Assignee:** @rachel
- **Labels:** `integration`, `ml`, `genai`, `dynamodb`
- **Blocked by:** `M8-2`, `M5-3`, `M6-1`
- **Blocks:** `M8-4`

**Context.** Turn the classifier output into the structured-evidence input Bedrock expects (the contract agreed in M6), invoke Bedrock, and persist the resulting diagnosis to the Incidents store. This is the classify-to-explain hand-off Rachel and Josh specified in M5/M6.

**Deliverables**
- Transform `{incident_type, confidence, metric deltas, affected services}` into the §10 Bedrock input contract ([`Docs/DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md); `affected_services` from `genai.affected`).
- Invoke Bedrock (M6 pipeline) and capture the structured diagnosis.
- Persist prediction + diagnosis + ground-truth reference to the Incidents DynamoDB table.

**Decided (2026-11-07; [`Docs/DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md), "Decided in M8-3", and
[`Docs/INCIDENT_STORE.md`](../Docs/INCIDENT_STORE.md)):** the incident store is a stack of its own,
`anomalypulse-incidents`: one Lambda that takes each diagnosed minute from the pipeline bus, one at a time
(reserved concurrency 1). It groups minutes with `incident.py`, keeps the last 5 healthy windows (the classifier
said `NORMAL` and the detector did not flag the minute) and fixes the baseline from them when an incident opens,
sends at most 10 metrics (those that moved and the type's own signals first, then the standard three), and calls
`anomalypulse-explain` when an incident opens and each time its type changes, at most 5 calls per incident. It
stores one record per incident (`pk` `INCIDENT`, `sk` `started_at`: the dashboard's `prediction` and `diagnosis`,
the explanation Lambda's other outputs, and the ground-truth reference beside them) and its running state under
`INCIDENT_STATE`. The reference is the running experiment's id from the experiment lock, read when the incident
opens.

**Definition of Done**
- [ ] Input contract agreed with M6-1 for this seam: the contract's open questions (baseline, which metrics, when to call) answered. Carried from M6-1's Definition of Done (2026-11-02).
- [ ] Classifier output is transformed into a valid Bedrock input with no manual editing.
- [ ] Diagnosis is stored against the incident record with a ground-truth reference for comparison.
- [ ] Round-trip latency captured for §16 end-to-end metrics.

**Built, not yet live (2026-11-07; [`Docs/INCIDENT_STORE.md`](../Docs/INCIDENT_STORE.md)):** the code, the stack and
the tests are on the branch; nothing has run on AWS.

- **Done:** 99 offline tests in `incidents/` (the table, S3, the lock and the explanation Lambda are faked), among
  them one that reads a stored record through the dashboard's `build_dashboard`; `cfn-lint` finds nothing in
  `infra/incidents/template.yaml`; the Makefile's bundle imports on its own; `incidents/live_check.py` (read-only, run after each live run) is written and its judging is tested offline.
- **Not done:** deploying (Jake, with `admin`); the live checks (a traffic spike with `--baseline 360`, then a
  cascade, from the runner); `sam build`, which has not run; the contract's questions agreed with M6-1 (Josh) and
  the record's layout agreed with Josh and Linu (posted on #30, unanswered); `observed_sequence` is not sent.
- **So all four boxes stay unchecked.**

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
