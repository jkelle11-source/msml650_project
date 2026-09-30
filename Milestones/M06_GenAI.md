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

**Definition of Done**
- [ ] For a set of sample inputs (one per incident type), Bedrock produces a well-formed report matching the template.
- [ ] No output claim references a service/metric/value absent from its input (smoke-checked).
- [ ] Ordering claims are phrased as observed sequences, not causation.
- [ ] Input contract agreed and documented with Rachel (`M5-3`) for the M8 seam.

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

**Definition of Done**
- [ ] For a detected incident, the correct runbook is retrieved and its content appears in the diagnosis.
- [ ] Retrieval quality is measurable (matched vs mismatched runbook) for M9.
- [ ] Clearly gated as stretch — core M6 does not depend on it.
