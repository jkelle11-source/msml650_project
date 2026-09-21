# Milestone 10 — Final Demo

> **GitHub Milestone**
> **Title:** `M10 — Final Demo`
> **Timeframe:** Week 10
> **Depends on:** `M9`
> **Description:**
> Prepare and deliver the live demo: a healthy system, a **live incident injection**, ML detection, AI diagnosis, a **ground-truth reveal**, and the ambitious cascading-failure narration. The strength of the demo is that it's not a plausible story — the audience sees the ML prediction next to the injected truth, quantitatively (§15). Work is assigned by **demo segment**, mapping each person to the surface they've owned, with a shared rehearsal + deck task so the run is tight.

## Assignment summary
- **Jake** — Demo environment, deploy/seed, cost guardrails, live-injection runbook
- **Rachel** — Live incident-injection choreography (drives the simulator on stage)
- **Melisa** — ML detection/classification live path + ground-truth reveal segment
- **Josh** — Bedrock diagnosis segment + demo script/narrative + slide deck
- **Linu** — Dashboard polish + cascading-failure narration (observed-sequence captions)
- **All** — Dry runs (`M10-6`)

## Coordination notes
- **Rehearse the cascade caption explicitly.** The cascading-failure narration presents ordering as an **observed sequence from correlated timestamps**, and the demo caption must say so, so the audience doesn't read the narration as the LLM proving causation (§15, §4). This is the one wording trap most likely to undercut an otherwise strong demo.
- **The ground-truth reveal is the money shot.** Prediction + probability shown against the injected truth (§15 Part 3) — rehearse it as the beat the whole demo builds toward.
- Demo runs against the **clean deployed stack with cost guardrails**, not a dev environment mid-experiment.

---

## Issues

### `M10-1` — Demo environment, deploy & live-injection runbook
- **Assignee:** @jake
- **Labels:** `demo`, `infra`, `runbook`
- **Blocked by:** `M9`
- **Blocks:** `M10-2`, `M10-6`

**Context.** A clean, reproducible environment for the live run, with the guardrails that keep an on-stage injection from becoming an on-stage AWS bill. Own the runbook so any presenter can drive it.

**Deliverables**
- Clean stack deploy + seed data; verified healthy baseline (§15 Part 1 numbers).
- Cost guardrails (capacity caps, teardown, budget alarms) for live injection.
- A step-by-step live-injection runbook + a rollback/reset path between takes.

**Definition of Done**
- [ ] Fresh deploy reaches a healthy baseline matching the §15 Part-1 dashboard.
- [ ] Runbook lets a non-owner drive the full demo.
- [ ] Reset-between-takes verified; cost guardrails active.

---

### `M10-2` — Live incident-injection choreography
- **Assignee:** @rachel
- **Labels:** `demo`, `simulator`
- **Blocked by:** `M10-1`
- **Blocks:** `M10-3`

**Context.** Drive the simulator on stage — the database-throttling single incident (§15 Part 2) and the cascading failure (§15 Part 4) — with timing that fits the narration.

**Deliverables**
- Scripted injection sequence: healthy → DB throttling → (later) cascade, with rehearsed timing.
- Pre-set intensities/durations chosen for a legible on-stage telemetry shift.
- Fallback pre-recorded run in case of live AWS flakiness.

**Definition of Done**
- [ ] DB-throttling and cascade injections produce clean, legible telemetry shifts on cue.
- [ ] Timing matches the narration beats.
- [ ] A recorded fallback exists.

---

### `M10-3` — ML detection/classification + ground-truth reveal segment
- **Assignee:** @melisa
- **Labels:** `demo`, `ml`
- **Blocked by:** `M10-2`
- **Blocks:** —

**Context.** The segment where CloudSentinel detects and classifies the injected incident, then reveals prediction-vs-ground-truth (§15 Part 3) — the quantitative punchline.

**Deliverables**
- Live detection + classification shown with confidence for the DB-throttling incident.
- The ground-truth reveal: injected truth beside model prediction + probability.
- A one-line explanation of why this is quantitative, not a plausible story.

**Definition of Done**
- [ ] Detection + classification fire live on the injected incident with confidence shown.
- [ ] Ground-truth-vs-prediction reveal lands as a clear, correct match.
- [ ] Segment rehearsed to time.

---

### `M10-4` — Bedrock diagnosis segment + script + deck
- **Assignee:** @josh
- **Labels:** `demo`, `genai`, `presentation`
- **Blocked by:** `M10-3`
- **Blocks:** `M10-6`

**Context.** Show Bedrock turning the classified incident into a human-readable diagnosis, and own the overall narrative + slides that frame the demo (concept, architecture, the SageMaker-vs-Bedrock division of labor, evaluation-integrity story).

**Deliverables**
- Live Bedrock diagnosis for the injected incident (summary, root cause, evidence, actions), rendered on the console.
- Demo script/narrative tying the segments together.
- Slide deck: concept, architecture, "SageMaker tells us what / Bedrock tells us why" (§20), key eval results from M9.

**Definition of Done**
- [ ] Live diagnosis renders with evidence tracing to metrics and observed-sequence wording.
- [ ] Script covers all six §15 beats end-to-end.
- [ ] Deck presents the evaluation-integrity claim with its real-world disclaimer.

---

### `M10-5` — Dashboard polish + cascading-failure narration
- **Assignee:** @linu
- **Labels:** `demo`, `dashboard`, `frontend`
- **Blocked by:** `M10-2`
- **Blocks:** `M10-6`

**Context.** Final console polish for stage legibility, and the cascade segment (§15 Part 4) where the correlated signals are narrated in the order observed — with the caption that marks it an observed sequence, not proven causation.

**Deliverables**
- Visual polish for projector legibility (contrast, sizing, motion on incident onset).
- Cascade narration view: correlated signals surfaced in observed order across services.
- Explicit **observed-sequence caption** on the cascade narration.

**Definition of Done**
- [ ] Console is legible on a projector at distance.
- [ ] Cascade narration presents signals in observed order with the causation caveat visible.
- [ ] No UI copy asserts causation.

---

### `M10-6` — Dry runs & final rehearsal
- **Assignee:** @jake, @josh, @linu, @melisa, @rachel *(shared)*
- **Labels:** `demo`, `coordination`
- **Blocked by:** `M10-1`, `M10-4`, `M10-5`
- **Blocks:** —

**Context.** End-to-end rehearsals against the clean stack, including failure drills (AWS flakiness → fallback recording), so the live run is boring in the best way.

**Deliverables**
- ≥2 full end-to-end dry runs on the demo environment.
- Timing pass against the presentation slot; role/handoff assignments per segment.
- Failure drill: switch to the recorded fallback cleanly.

**Definition of Done**
- [ ] Two clean full dry runs completed within the time slot.
- [ ] Every presenter knows their segment and handoffs.
- [ ] Fallback path rehearsed and works.
