# Milestone 7 — Dashboard

> **GitHub Milestone**
> **Title:** `M7 — Dashboard`
> **Timeframe:** Weeks 5–6 *(runs concurrently with `M5` and `M6`)*
> **Depends on:** `M2` *(telemetry shape)*; consumes `M5`/`M6` output at the `M8` seam
> **Description:**
> Build the CloudSentinel web interface — an **SRE/operations console**, not a generic chatbot (§14): live KPIs (requests, error rate, p95, incident count), per-service health, and an active-incident panel showing the ML classification (with confidence) and the Bedrock diagnosis side-by-side with ground truth. Under the capability split this is a **1–2 person track**: **Linu owns the frontend** (freed from service work), and **Jake supplies the thin read API** it consumes (small; can slip into early M8 if ML infra runs long). It develops against mock data so it's not blocked on M5/M6, then switches to the live feed in M8.

## Assignment summary
- **Linu** — Dashboard frontend (SRE console): KPI tiles, service health, active-incident + AI-diagnosis panel, ground-truth comparison
- **Jake** — Dashboard read API (BFF) serving current health/incident/diagnosis from the Incidents store

## Coordination notes
- **Build against a mock feed first.** Agree a dashboard data contract (the JSON the BFF returns) up front so the frontend and API proceed in parallel and the M8 swap to live data is a config change, not a rewrite.
- **Causation discipline carries through to the UI.** The AI-diagnosis panel presents symptom ordering as an **observed sequence from correlated timestamps** (the §14 "began approximately 40 seconds later (observed sequence…)" wording), never as asserted causation. This is a hard requirement on the diagnosis panel copy.
- **Ground truth is a first-class panel.** The console must show model prediction *and* injected ground truth together — that side-by-side is what makes the demo quantitatively convincing rather than a plausible story (§15).

---

## Issues

### `M7-1` — SRE console frontend
- **Assignee:** @linu
- **Labels:** `dashboard`, `frontend`, `mvp`
- **Blocked by:** `M2-1` *(telemetry shape)*; develops against mock feed
- **Blocks:** `M8-5`

**Context.** The operator-facing console from §14. Prioritize a clean, legible ops layout over chat aesthetics. Everything renders from the dashboard data contract, so it runs fully on mock data until the M8 swap.

**Deliverables**
- Top-line KPI tiles: requests/min, error rate, p95 latency, active incident count, overall system-health indicator.
- Per-service health rows (Product / Cart / Orders / Payments) with healthy/degraded states.
- **Active-incident panel:** incident type, severity, confidence, start time, affected services.
- **AI-diagnosis panel:** Bedrock summary, root cause, evidence, recommended actions — with observed-sequence wording on any ordering.
- **Ground-truth comparison** view (prediction vs injected truth) for demo/eval.
- Renders entirely from the agreed data contract against a mock feed.

**Definition of Done**
- [ ] Console renders all panels from mock data with no hardcoded values.
- [ ] Diagnosis panel copy uses observed-sequence wording, not causal assertions (reviewed).
- [ ] Prediction and ground truth are shown side-by-side.
- [ ] Layout reads as an SRE console (reviewer sign-off on §14 fidelity).

---

### `M7-2` — Dashboard read API (BFF)
- **Assignee:** @jake
- **Labels:** `dashboard`, `api`, `infra`
- **Blocked by:** `M1-1`; soft dependency on the Incidents store shape
- **Blocks:** `M8-5`

**Context.** The thin backend-for-frontend the console reads from: current health, the active incident, and its diagnosis, sourced from the Incidents DynamoDB table + the Diagnosis Lambda output. Small and well-defined; Jake owns it because it's API/infra, and it can slip into early M8 if SageMaker infra (`M5-1`) overruns.

**Deliverables**
- A read endpoint returning the dashboard data contract (health snapshot, active incident, diagnosis, ground truth).
- Wiring to the Incidents store / Diagnosis Lambda output (mockable until M8).
- Documented data contract shared with Linu so frontend and API stay in lockstep.

**Definition of Done**
- [ ] Endpoint returns the agreed contract shape (mock-sourced acceptable at M7 exit).
- [ ] Contract documented and matches what the frontend consumes.
- [ ] Ready to swap to the live Incidents feed in `M8-5` without contract changes.
