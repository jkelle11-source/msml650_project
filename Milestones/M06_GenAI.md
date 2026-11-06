# Milestone 6 — GenAI (Bedrock)

> **GitHub Milestone**
> **Title:** `M6 — GenAI (Bedrock)`
> **Timeframe:** Weeks 5–6 *(runs concurrently with `M5` and `M7`)*
> **Depends on:** `M4` *(needs the incident taxonomy + metric-delta shape; can develop against sample inputs before `M5` finishes)*
> **Description:**
> Build the explanation layer: Bedrock takes the **classified** incident type and structured metric deltas and produces a human-readable diagnosis — incident summary, root cause, evidence, and recommended remediation. This is a **1-person track** (Josh, freed from ML by the capability split), running in parallel with M5 and M7. The defining constraint: Bedrock is a **constrained explanation generator, not a diagnoser** — it's handed the answer, so it's built and evaluated on *evidence fidelity*, not on independently identifying the incident (§10, §20). RAG is the one well-justified stretch, because it's the only thing that gives Bedrock genuinely *new* information beyond its structured input (§11).

## Assignment summary
- **Josh** — Bedrock diagnosis pipeline: structured-evidence prompt, output schema, generation of summary/root-cause/remediation
- **Josh** *(stretch)* — RAG / knowledge-base retrieval of runbooks

## Coordination notes
- **Input contract, not a log dump.** Bedrock receives structured evidence — the classified incident, confidence, duration, metric deltas, affected services (the JSON shape in §10) — never raw logs. The exact input schema is agreed with Rachel (classifier output, `M5-3`) so the M8 seam is trivial; capture it as a checklist item here, not a separate person's issue.
- **Causation discipline.** Any ordering language ("failures began ~40s after latency rose") is phrased as an **observed sequence from correlated timestamps**, not proven causation. This wording rule is baked into the prompt and the output template so the dashboard (M7) and demo (M10) inherit it.
- GenAI **quality evaluation** (evidence fidelity, hallucination rate, actionability, completeness) is scored in `M9-5` on the held-out test set — this milestone builds the generator and a smoke-level self-check, not the formal eval.
- **Deploy to your own stack, not `anomalypulse-shared`.** The shared stack isn't deployed until M9-1 has generated the test set (README, "Status"), so the diagnosis Lambda, its Bedrock permissions and anything else this milestone adds go in a separate stack that reads the shared stack's SSM parameters.

---

## Issues

### `M6-1` — Bedrock diagnosis pipeline (structured-evidence → report)
- **Assignee:** @josh
- **Labels:** `genai`, `bedrock`, `mvp`
- **Blocked by:** `M4-4` *(taxonomy)*; soft dependency on `M5-3` *(live classifier output — mock until ready)*
- **Blocks:** `M8-4`

**Context.** The core generator. Develop against representative sample inputs (the §10 JSON) so it doesn't block on M5; swap to live classifier output at the M8 seam. Output should match the §10 template: Incident Summary, Root Cause, Evidence (traceable to input metrics), Recommended Actions.

**Deliverables**
- Prompt design that consumes the structured-evidence input contract (incident, confidence, duration, metric deltas, affected services).
- Constrained output schema/template: summary, severity/confidence, root cause, evidence bullets, numbered recommended actions.
- Evidence-fidelity constraint in the prompt: every claim must trace to a metric present in the input; observed-sequence wording for any ordering claim.
- A Diagnosis Lambda integration stub (or module) so M8 can wire classifier → Bedrock → incident store.
- A lightweight self-check that flags obviously unsupported claims (dry run before the formal M9 eval).

The pipeline is in `AnomalyPulse/genai/` and the contract in
[`Docs/DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md): the `anomalypulse-explain` Lambda, in a stack of
its own (`anomalypulse-genai`), which M8-3 invokes with each classified incident (PR #86). On the final prompt
all five samples' diagnoses passed the self-check on 2026-11-02 (`genai/samples/results/` as merged in
`45b9d24`). M6-2 reran them on the same prompt on 2026-11-03, and four passed (M6-2, below), five with the
self-check of 2026-11-06. Decisions:
- **Claude Sonnet 4.6 on Bedrock** (a global cross-region profile, adaptive thinking at effort `medium`), over
  Opus 4.6: fewer self-check findings on the five samples, at about the same cost (2026-11-01). Claude 4.7 and
  later have a Bedrock quota of 0 tokens per minute in this account, and the SDK stays on 1.x for 4.6.
- **The answer is held to a schema built per request** (structured outputs), so each evidence bullet cites only
  that input's metrics.
- **Severity comes from a fixed rubric**, stated in the prompt and recomputed by the self-check (2026-11-02).
- **A cascade's root cause names its faults as concurrent and links none causally** (2026-11-02). The simulator
  injects each fault on its own, and the demo narrates a cascade as an observed sequence (plan §15).
- **`affected_services` comes from the feature windows by a fixed rule** (`genai/affected.py`, 2026-11-02),
  calibrated on `v1`. It lists no service in any of 980 NORMAL minutes. It lists the faulted service in every
  database-throttling, Lambda-degradation, dependency-failure and cascade minute, and Product in 707 of 718
  traffic-spike minutes. It is also the dashboard's `prediction.affected_services`.
- **The self-check holds each bullet's numbers to the metrics it cites, and every number to the precision it is
  written in** (2026-11-02). On the samples a random number passes it 2–4.5% of the time.
- **A hedged number may be cut off instead of rounded** (2026-11-06): "roughly 15x", "more than 15x" for 15.6x,
  one written unit on the side the word gives. And "followed at roughly the same scale" orders nothing. These
  were the only false positives among the findings on the 20 saved answers and the M6-2 review's 9 live ones;
  the change clears four findings and adds none. The hedge rule doubles the chance that a wrong number behind
  "roughly" passes (4–9.5%).

**Definition of Done**
- [x] For a set of sample inputs (one per incident type), Bedrock produces a well-formed report matching the template.
- [x] No output claim references a service/metric/value absent from its input (smoke-checked).
- [x] Ordering claims are phrased as observed sequences, not causation.
- [ ] Input contract agreed and documented with Rachel (`M5-3`) for the M8 seam. *Documented; the agreement
  moved to `M8-3` (#30, 2026-11-02), whose Definition of Done now carries it. The contract's three open
  questions are posted there for Rachel, with proposals.*

---

### `M6-2` — RAG / runbook knowledge base `[stretch]`
- **Assignee:** @josh
- **Labels:** `genai`, `rag`, `bedrock-knowledge-bases`, `stretch`
- **Blocked by:** `M6-1`
- **Blocks:** —

**Context.** Optional but well-justified: retrieval is what lets Bedrock add information beyond the label+metrics it's already handed, and it comes with its own measurable axis (retrieval quality). Pick up only if the M5/M6/M7 phase has slack.

**Deliverables**
- A small knowledge base: troubleshooting guides, runbooks, known failure modes, remediation procedures (§11).
- Retrieval keyed on the classified incident type, feeding matched runbook content into the Bedrock prompt.
- Retrieval-quality hook (does the retrieved runbook match the incident type?) for `M9-5`.

**Scope decided 2026-11-02 (Jake): five runbooks for this stack, picked by the classified type, and no Bedrock
Knowledge Base.**
- **Why no Knowledge Base.** With five types the pick is a lookup, so a vector store would add a service and a
  standing cost without changing which runbook is chosen.
- **What "retrieval quality" means here.** A runbook is wrong exactly when the classification is, so retrieval
  quality restates classifier accuracy.
- **Runbooks feed `recommended_actions` only, behind a switch that is off by default** (`ANOMALYPULSE_RUNBOOKS`).
  Summary, root cause and evidence still come only from the input.
- **What is measured instead:** a blind comparison of actionability, with and without runbooks, on the samples and
  then in M9-5. M9-5's comparison decides whether the switch is on for the demo.
- **Build notes:** [`Handoffs/M6-1_Handoff.md`](../Handoffs/M6-1_Handoff.md), §2–§8.

The runbooks are in `AnomalyPulse/genai/runbooks/`, one per type, and the contract's "Runbooks (M6-2)" section
says how they reach the prompt. Results, 2026-11-03 and 2026-11-04:
- **Self-check: off passes four of five samples, on three.** Both have one finding in the Lambda-degradation
  summary, neither from a runbook: off, "followed at roughly the same scale" (an ordering word); on, "roughly
  15x" for 15.6x. They are recorded, not rerun. The on run's database-throttling root cause is also flagged,
  since the review, for "the provisioned table": a runbook fact in a claim. The two Lambda-degradation
  findings were false positives, which the self-check accepts since 2026-11-06 (M6-1, above), so recomputed
  now off passes five and on four.
- **The blind actionability comparison is a tie, 19/20 each**
  (`genai/samples/results/blind/`). Without runbooks, one list watched two on-demand tables for "provisioned
  limits". With runbooks, one list was marked unsafe for "roll back if a change is identified" in step 2, a
  rollback on timing alone (the scorer's reason). Four 0/1 criteria are too coarse to separate two reasonable
  lists, so M9-5 picks the better list per incident (ties allowed) and keeps Correct and Safe as 0/1 counts.
- **The scorer was not fully blind.** No one outside the GenAI work was free in time, so it was scored by the
  person who scoped M6-2. The with-runbook lists also name stack facts more often, which can give them away.
- **The switch stays off.** M9-5's comparison decides whether it is on for the demo.

Review, 2026-11-05 (PR #89):
- **Runbook corrections, all checked against the handlers.** A request that reaches the 10 s Lambda timeout
  writes no record, so it never shows in `error_rate_5xx`; the Lambda runbook had said it would. A cold start
  runs before the handler, so it is in neither `latency_ms` nor `lambda_duration_ms`. A throttled call to
  Payment fails at once rather than slowly. `GET /products` already pages through the whole table. Reserved
  concurrency also caps the function it protects. The cascade runbook no longer says one service's surge can
  throttle another. The Lambda runbook rolls back only if the logs place the slow path in what a recent change
  touched.
- **Runbook facts were reaching claims:** "the provisioned table" in a root cause (above), and in a review
  run "no requests have yet reached the Lambda timeout" in an evidence bullet. The runbook intro now tells
  Bedrock to state nothing in a claim that only the runbook says, and the self-check flags a claim about how a
  table's capacity is set. M9-5 scores any other runbook-only fact in a claim as unsupported.
- **The runbooks as merged** (`genai/samples/results/global.anthropic.claude-sonnet-4-6+runbooks-review/`)
  pass four of five; the one finding, an ordering word in the Lambda-degradation summary, is not from a runbook.
  Both Lambda-degradation reruns made the rollback conditional and pointed at the function's CloudWatch `Errors`
  metric for timeouts. One throttling evidence bullet still says the 5xx share means requests are "exhausting
  all retry attempts", the runbook's explanation rather than an observation.

**Definition of Done**
- [x] For a detected incident, the correct runbook is retrieved and its content appears in the diagnosis.
  *The runbook is picked by the classified type and named in the reply's `runbook` field; its numbers appear
  in `recommended_actions` (for example "order-service's 6 s read timeout" in the dependency-failure run).*
- [x] Retrieval quality is measurable (matched vs mismatched runbook) for M9. *With the pick keyed on the
  type, it equals classifier accuracy (M9-2); actionability with and without runbooks is measured instead, as
  above.*
- [x] Clearly gated as stretch — core M6 does not depend on it. *`ANOMALYPULSE_RUNBOOKS` is off by default, and
  with it off the Bedrock request is byte-for-byte M6-1's, held to a saved fixture.*
