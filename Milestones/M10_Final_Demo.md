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
- All demo traffic sent from the runner host, never a presenter's laptop, with `infra/runner/drive.sh`: with laptop traffic the Isolation Forest flagged about half of the healthy minutes, with the runner's 2.3% on 2026-11-07 ([`Docs/DETECTION.md`](../Docs/DETECTION.md), "Where live traffic comes from"). The runner stays until after M10 ([`Docs/DATASET.md`](../Docs/DATASET.md), "The runner host").
- All demo traffic in the simulator's default endpoint mix: **never pass `--mix`**. In M9-2's shifted mix the Isolation Forest flagged every healthy window ([`Docs/M9-2_REPORT.md`](../Docs/M9-2_REPORT.md)). The default mix from the runner is the closest match to training, but it doesn't guarantee the forest behaves: two healthy checks on the same day (M10-6) had it flagging 10 of 16 healthy minutes, then 0 of 16. The model stays as M9 evaluated it.
- Takes timed for the live path: start the traffic at least 6 minutes before the healthy segment, and allow about 6 minutes after an injected fault ends before calling the system healthy again. Until then the detector still flags minutes whose look-back holds the fault or the idle time before the traffic ([`Docs/PIPELINE.md`](../Docs/PIPELINE.md), "On the minutes only the live path scores").
- After a fresh deploy of the pipeline stack, check that the window builder's runs start between about :20 and :50 of each minute, since a new schedule rule picks a new second. Later, the console shows `NO_DATA` for a moment every minute; earlier, its newest minute misses its last requests ([`Docs/PIPELINE.md`](../Docs/PIPELINE.md), "When a minute's window is ready", which has the check and the fix).

**The runbook:** [`Docs/DEMO_RUNBOOK.md`](../Docs/DEMO_RUNBOOK.md), with the evidence for each box below.

**Definition of Done**
- [x] Fresh deploy reaches a healthy baseline matching the §15 Part-1 dashboard. (`main` at `5fac754` redeployed 2026-11-26, then `exp-20261126T041940Z-ec74`: no incident, 1,192 requests a minute, 0% errors, p95 13.4 ms.)
- [ ] Runbook lets a non-owner drive the full demo.
- [x] Reset-between-takes verified; cost guardrails active. (2026-11-26: `drive.sh abort` on a live take, then a database-throttling take and a cascade, both passing `live_check`; the `anomalypulse-daily` budget, the runner's idle stop and bounded runs.)

---

### `M10-2` — Live incident-injection choreography
- **Assignee:** @rachel
- **Labels:** `demo`, `simulator`
- **Blocked by:** `M10-1`
- **Blocks:** `M10-3`

**Context.** Drive the simulator on stage — the database-throttling single incident (§15 Part 2) and the cascading failure (§15 Part 4) — with timing that fits the narration. The simulator runs on the runner host, driven from the presenter's laptop with `infra/runner/drive.sh` (M10-1).

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

**Context.** The segment where AnomalyPulse detects and classifies the injected incident, then reveals prediction-vs-ground-truth (§15 Part 3) — the quantitative punchline.

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
- The deck states M9-2's limit plainly: detection depends on a known traffic baseline and must be recalibrated when normal traffic changes ([`Docs/M9-2_REPORT.md`](../Docs/M9-2_REPORT.md)).
- The deck quotes the finalized Evaluation Integrity Statement, not M4-3's draft, and shows the classifier's margin over the threshold baseline with the false alarms beside it ([`Docs/M9-3_REPORT.md`](../Docs/M9-3_REPORT.md)).
- The deck's evaluation figures are M9-6's, in `Docs/images/m9-6/` (PNG and SVG, redrawn by `AnomalyPulse/report/make_figures.py`). Figure 4 is the margin over the baseline with its false alarms, and figure 6 the latency headline: about four minutes from fault to stored diagnosis, half of it CloudWatch's 2-minute settle ([`Docs/M9-6_REPORT.md`](../Docs/M9-6_REPORT.md)).

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
- Healthy check before each dry run and before the demo: after the 6-minute warm-up (M10-1), send at least 15 minutes of healthy default-mix traffic from the runner. It passes only if no incident opens. If it fails, stop and find out why before presenting, and don't change the forest's cut-off to get past it.
- Each healthy check also records the forest's flagged share of the full minutes after the warm-up. In CloudWatch Logs Insights, on `/aws/lambda/anomalypulse-diagnosis` over the run's time range, run `fields window_start, anomalous, label, incident_type | filter event = "diagnosis" | sort window_start asc` (`anomalous` is 1 when flagged). A share above 10% doesn't fail the check, but it means the classifier is the only safeguard that day, so tell the team before presenting.

**Healthy checks so far (2026-11-23):** both passed, with no incident and the classifier `NORMAL` on all 23 minutes. The forest flagged 10 of the 16 full minutes after the warm-up at 07:09 UTC (`exp-20261123T070939Z-ac0c`, 62.5%) and 0 of 16 at 21:10 UTC (`exp-20261123T211018Z-7dbe`), so one clean check doesn't predict the next. A third, M10-1's healthy baseline on 2026-11-26 at 04:19 UTC (`exp-20261126T041940Z-ec74`), also passed, with the forest flagging 0 of 16.

**Definition of Done**
- [ ] Two clean full dry runs completed within the time slot.
- [ ] Every presenter knows their segment and handoffs.
- [ ] Fallback path rehearsed and works.
- [ ] The healthy check passed before each dry run, with the forest's flagged share recorded.
