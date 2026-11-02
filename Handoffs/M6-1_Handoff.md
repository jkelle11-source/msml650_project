# M6-1 Handoff — The Diagnosis Pipeline After Review, and Building M6-2 (Runbooks)

> **The one-sentence version:** your M6-1 pipeline is merged (PR #86), deployed as `anomalypulse-genai` and
> verified live, but the review changed a fair amount of it (§1). M6-2 is scoped down to **five runbooks for our
> stack, picked by the classified incident type and fed only into `recommended_actions`, behind a switch that is
> off by default** (§2). No Bedrock Knowledge Base. §3–§6 are the build, and §8 is the checklist.

**Where it stands (2026-11-02):**
- **Merged and deployed.** PR #86 merged as `45b9d24`, and #24 closed with it. Jake deployed the stack from main.
- **Verified live.** The deployed code is byte-identical to main, and a live call on `dependency_failure.json`
  passed the self-check (26.8 s, 2,623 tokens in, 1,152 out, 149 MB). A `NORMAL` input was refused with
  `BadInput` in 2.7 ms, without reaching Bedrock.
- **Tests:** `python -m pytest -q genai` runs 109 tests (46 before the review).
- **DoD 4 moved to M8-3.** "Input contract agreed with M5-3" is now in M8-3's Definition of Done (#30), where the
  contract's three open questions are posted for Rachel with proposals.

The reference of record is [`Docs/DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md).

---

## 1. What changed in your code during review (read this first)

The review commits on PR #86 are `f6f12d3` through `564ee6d`; `git log 60bd7f3..45b9d24 -- AnomalyPulse/genai`
shows every change.

| Where | What changed | Why |
|---|---|---|
| `prompt.py` rule 6 | `root_cause` may connect symptoms only along the stack (Order calls Payment; Product, Cart and Order read their own tables). A `CASCADING_FAILURE`'s root cause names its faults as **concurrent**, links none of them causally, and orders them only through `observed_sequence`. | The first prompt let Sonnet write that "the increased order volume put payment-service under load, … which in turn produced a flood of dependency errors". No metric shows Payment's load, and the simulator injects each cascade fault on its own (`Docs/CASCADE.md`). The plan's demo narrates cascades as an observed sequence (§15). |
| `self_check.py`: numbers | An evidence bullet's numbers must come from **the metrics that bullet cites** (or a gap or ratio between two of them). Every number is held to **the precision it is written in**: "2950" means 2945–2955 and "2.9" means 2.85–2.95, where the old check allowed a flat 6% everywhere. | With the old check, 7–56% of random numbers from 1 to 1000 passed, and "Payment p95 is 2950 ms" (Order's figure) passed. Now 2–4.5% pass in the summary and under 3% in a bullet. |
| `self_check.py`: other rules | It recomputes **severity** (`contract.severity()`) and flags a mismatch. It flags any **"threshold"** in summary, root cause or evidence. It flags causal links in a **cascade's root cause** (`CASCADE_LINKS`). It stops flagging "in order to", "the 4-minute duration", "the first sign", "15.0 before" and "500 responses". | The new checks caught Opus 4.6 quoting "the 5% threshold" and "the 10x threshold". The old rules flagged faithful wording. |
| `contract.py` | Shared constants: `EVIDENCE_BULLETS = (3, 6)` and `ACTIONS = (2, 5)`, which the prompt now states; the rubric's constants; `severity()`; `SERVICE_METRICS`, the feature matrix's (service, metric) pairs, so Payment has no `db_*` metrics and only Order has `dependency_*`; and `OBSERVATIONS`, the two phrases an `observed_sequence` step may use. | The check allowed 1–6 of each while the prompt said 3–6 and 2–5. The pair check rejects inputs naming metrics that don't exist, and fixed phrases keep free text away from Bedrock. |
| `prompt.user_message()` | `incident_id` and `contract_version` are no longer sent to Bedrock. | They're bookkeeping, and the id is free text that M8-3 makes up. |
| `explain.py` | The client is built with `TIMEOUT_S = 50` and `MAX_RETRIES = 1`. A test holds them inside the template's 120 s Lambda timeout. | The SDK's defaults (600 s, 2 retries) could run 30 minutes, past the Lambda's limit. |
| `affected.py` (new) | `affected_services(baseline(lookback_windows), latest_window)` computes the input's `affected_services` and the dashboard's `prediction.affected_services`. | The classifier returns only a label and probabilities. The rule is calibrated on v1: it lists no service in any of 980 NORMAL minutes. It lists the faulted service in every database-throttling, Lambda-degradation, dependency-failure and cascade minute, and Product in 707 of 718 traffic-spike minutes. |
| `samples/results/` | Sonnet re-run on the final prompt: all five pass. A `README.md` says which run is which, and every saved `self_check` was recomputed with the final checker. | Your Opus folder is from the **first** prompt, so it isn't a like-for-like comparison. |
| Docs | `DIAGNOSIS_CONTRACT.md` updated throughout. The README lists `genai/`, the stack, its SSM parameter, deploy and tests. M06 records the decisions. | |

The contract doc now says Claude 4.7 and later have a **Bedrock quota of 0 tokens per minute** in this account,
never raised. They aren't missing access. Opus 4.6 and Sonnet 4.6 have 3M and 6M. Stay on SDK 1.x (1.17.0)
while the model is a 4.6.

---

## 2. M6-2: what was decided, and why

**Decided 2026-11-02 (Jake): runbooks picked by the classified type, with no Bedrock Knowledge Base.**

- **The value is the content.** Today Claude writes `recommended_actions` from general knowledge, so some are
  generic (DAX, "hot partitions", "recent deployments") and some are wrong for this stack ("check DynamoDB capacity
  for payment-service", which has no table). Actions are the one field the self-check doesn't fact-check, and
  M9-5 scores them by hand ("actionability"). Runbooks written for our stack fix that.
- **A vector store adds nothing here.** With five types, picking the runbook is a lookup. A Knowledge Base would
  add a service, a standing cost and a failure mode without changing which runbook is chosen. That's the plan's
  §12 "logo" warning.
- **Be honest about "retrieval quality".** A runbook keyed on the classified type is wrong exactly when the
  classifier is wrong, so the M9 metric "does the retrieved runbook match the incident type?" just restates
  classifier accuracy (M9-2). Report it that way. What M6-2 should measure is whether actions get better: a blind
  with-and-without comparison (§6).
- **Runbooks feed `recommended_actions` only.** Summary, root cause and evidence still come only from the input,
  so M9-5's evidence-fidelity and hallucination numbers mean the same thing with the switch on or off.
- **The switch is off by default** (`ANOMALYPULSE_RUNBOOKS`). M8 builds against today's behaviour, and M9-5's
  comparison decides whether it's turned on.

If you later want retrieval as a real experiment, the upgrade is a Knowledge Base queried with the **symptoms** (built
from the evidence, not the label), stored in S3 Vectors (pay-per-use; OpenSearch Serverless, the default, bills
every hour even idle). Then "did it find the right runbook?" is a genuine second opinion on the classifier. It is
out of scope for tonight.

---

## 3. The build, step by step

**1. Runbooks:** `AnomalyPulse/genai/runbooks/<type>.md`, one per incident type, named in lowercase:
`traffic_spike.md`, `database_throttling.md`, `lambda_degradation.md`, `dependency_failure.md`,
`cascading_failure.md`. §4 has the rules and the verified stack facts.

**2. `genai/runbooks.py`:** `runbook_for(incident_type) -> (name, text)`, where name is `incident_type.lower()`.
Fail loudly if a file is missing; never return an empty runbook.

**3. The switch, in `explain.py`:**
`RUNBOOKS = os.environ.get("ANOMALYPULSE_RUNBOOKS", "off") == "on"`, plus a `runbooks=None` parameter on
`request()` and `diagnose()`, so tests and `run_samples` can choose without touching the environment. Add
`ANOMALYPULSE_RUNBOOKS: 'off'` to the template's environment variables.

**4. Put everything about runbooks in the user turn, never in `SYSTEM_PROMPT`.** That way the request with the switch
off is **byte-for-byte today's**, and today's validated behaviour can't drift. For example,
`prompt.user_message(evidence, runbook=None)` appends this when a runbook is given:

```text
Runbook for this incident type. Use it only for recommended_actions: what to check and what to do, in this stack.
summary, root_cause and evidence still come only from the evidence above.

<runbook text>
```

**5. The reply** gains `"runbook": "<name>"`, or `null` when off, **beside** `diagnosis`, not inside it, so the
dashboard contract doesn't change. M8-3 stores it and M8-4 can show it.

**6. The self-check:** `check(evidence, diagnosis, runbook=None)`. In `recommended_actions` only, numbers written in
the runbook are allowed too: parse them with `_NUMBER` and add them as values, so the written-precision rule still
applies. Summary, root cause and evidence are untouched. A runbook number there is still "not in or derivable from
the input", which is the point.

**7. The Makefile** copies files one by one (`cp __init__.py contract.py …`). Add `runbooks.py`, plus
`cp -R runbooks "$(ARTIFACTS_DIR)/genai/"`. A missing file only shows in Lambda, as an `ImportError` or a
`FileNotFoundError`, so check the build (§5).

**8. `run_samples.py`:** add `--runbooks on|off`, writing to `results/<model>+runbooks/` when on, so the off results
stay put.

**9. Docs:**
- `DIAGNOSIS_CONTRACT.md`: the request, the reply's `runbook` field, the self-check's action numbers, and the
  switch.
- M06's M6-2 Definition of Done (§8).
- The README's `genai/` row (runbooks, the switch).

---

## 4. Writing the runbooks

**Rules:**
- **Write for an on-call engineer on this stack, as if it were real.** Never mention the simulator, injectors, bands,
  fault parameters, experiment ids or the fault-config table. That is Tier-2 knowledge: in a prompt it would leak
  into diagnoses and undermine M9-5. A test should scan for those words (§5).
- **Checks and actions, not causes.** A runbook says what to look at and what to do. A line like "the cause is
  usually X" would end up in `root_cause`.
- **The cascade runbook keeps the cascade rule:** check each concurrent fault and stabilise the user-facing one
  first. Never say one fault produces another ("traffic causes throttling"); the self-check flags that in a
  cascade's root cause.
- **Point at signals with the contract's metric names** (`read_throttle_events`, `dependency_error_count`,
  `lambda_duration_ms_mean`, …), so actions line up with the evidence.
- **Every number must be a true stack fact.** The self-check will allow it in every action, so a wrong number in a
  runbook becomes a wrong number in every diagnosis. Use only the table below, and check anything new against the
  code.
- **Keep each one short** (150–300 words). It goes into every request; about 400 tokens adds roughly $0.001 a call.
- **No one applies them to `anomalypulse-shared` before M9-1.** Advice such as "raise the Products table's RCU" is
  right for an engineer, but the shared stack is frozen until the test set is generated (README, "Status"). The
  demo shows the actions; it doesn't carry them out.

**Stack facts, verified 2026-11-02** against `infra/shared/template.yaml` and the handlers:

| Fact | Where |
|---|---|
| The four service Lambdas (`anomalypulse-product`, `-cart`, `-order`, `-payment`): Python 3.13 on arm64, **1,769 MB** (one full vCPU), **10 s** timeout | template `Globals` and each function |
| The account allows **1,000** concurrent Lambda executions, shared by every function; a throttled invocation shows in `lambda_throttle_count` and never reaches the handler | README, "Lambda functions"; `Docs/FEATURE_MATRIX.md` |
| Products table `anomalypulse-products-v2`: **provisioned, 200 RCU / 5 WCU**. `GET /products` **scans** it, so its reads grow with traffic and with the table's partitions; `GET /products/{id}` is a single `get_item` | template; `services/product/handler.py`; README |
| Carts (`anomalypulse-carts`) and Orders (`anomalypulse-orders`) are **on-demand** (`PAY_PER_REQUEST`): there is no provisioned capacity to raise | template |
| **Payment has no table** | template; `contract.SERVICE_METRICS` |
| Product, Cart and Order retry DynamoDB calls in the SDK (**legacy mode, 7 attempts** in all). The retries are inside `db_latency_ms`, so throttling shows as tail latency, and as 5xx only when the retries run out. In v1's 518 throttling minutes, Product's p95 rose about 500x (median), its p50 only 1.2x, and the median 5xx share was 0 | `services/*/handler.py`; `Docs/CASCADE.md`; v1, measured 2026-11-02 |
| Order calls Payment **synchronously on every `POST /orders`** (a Lambda invoke: **2 s** connect timeout, **6 s** read timeout, **no retries**). A failed or timed-out call is answered as a 5xx and counted in `dependency_error_count`; its time is in `dependency_latency_ms` | `services/order/handler.py` |
| `lambda_duration_ms` is the CPU time of the handler body, not wall-clock time. A slow dependency raises `latency_ms` but not `lambda_duration_ms`; a slow handler raises both | `Docs/FEATURE_MATRIX.md`; `services/order/handler.py` |
| API Gateway (REST, stage `dev`) fronts all four services | template; SSM `/anomalypulse/api/base-url` |

---

## 5. Tests to write (all without AWS, like the rest)

- **Every type has a runbook:** `runbook_for(t)` returns a non-empty text for each of `contract.INCIDENT_TYPES`, and
  `runbook_for("DATABASE_THROTTLING")[0] == "database_throttling"`.
- **Off changes nothing.** Before you edit anything, save `json.dumps(explain.request(DEP), sort_keys=True)` as a
  fixture, and assert the switch-off request equals it. This is the test that protects M8.
- **On adds only the user-turn section:** the system prompt is unchanged, and the user message holds the runbook text
  and the "only for recommended_actions" sentence.
- **The self-check on the throttling sample:**
  - the action "Check whether reads exhaust their 7 SDK attempts." passes with its runbook and is flagged without it;
  - the summary "Each read gets 7 SDK attempts." is flagged even with the runbook.
- **Use precise numbers in these tests.** A round number is held only to its written precision: "200" means
  150–250, and on the throttling sample it already matches 250, which is Product's 0.4% 5xx share written as "1 in
  250". So "200 RCU" passes with or without a runbook and can't show that the runbook did anything. "7" and "1,769"
  are flagged without the runbook (checked 2026-11-02).
- **The reply** carries `"runbook": "dependency_failure"` when on and `None` when off.
- **No simulator words:** no runbook contains `inject`, `simulator`, `band`, `experiment`, `fault-config` or any of
  `contract.FORBIDDEN_KEYS`.
- **Check that a test fails on broken code.** Break the off path on purpose (add one character to the system prompt)
  and confirm the fixture test fails.
- **The built package.** Run `make -s build-ExplainFunction ARTIFACTS_DIR=<scratch dir>` from `AnomalyPulse/genai/`.
  Then, from that directory, run `python -I -c "import sys; sys.path.insert(0, '.'); from genai import runbooks;
  print(runbooks.runbook_for('TRAFFIC_SPIKE')[0])"`. The review checked the M6-1 package this way.

---

## 6. Measuring it

**Tonight, on the five samples:** run both ways on the same day, so the comparison is fair. That's 10 calls, about
27¢.

```bash
python -m genai.run_samples --profile <yours> --runbooks off
python -m genai.run_samples --profile <yours> --runbooks on
```

Both runs must pass the self-check. Then have someone who doesn't know which is which score each sample's actions,
with the pairs shuffled. Score 0 or 1 on each of:

1. **Correct for this stack:** no false fact (a payment table, raising capacity on an on-demand table, a setting that
   doesn't exist).
2. **Specific:** names a concrete component, setting or metric, not "check the logs".
3. **Ordered:** the most useful step comes first.
4. **Safe:** nothing destructive or irreversible as a first step.

Commit both result folders and record the scores in M06.

**In M9-5 (#37):** apply the same blind rubric to test-set incidents, both ways. That decides whether
`ANOMALYPULSE_RUNBOOKS` goes on for the demo. Report retrieval quality as "equals classifier accuracy (M9-2)", not
as a number of its own. Evidence fidelity and hallucination are scored on summary, root cause and evidence, which
the runbooks don't feed. And per the M4 labeling decision, no metric scores a `db_throttling_onset` minute.

---

## 7. Running, deploying, and the rules of the road

- **Tests:** `cd AnomalyPulse && pip install -r genai/requirements.txt pytest && python -m pytest -q genai`.
  `anthropic[bedrock]==1.17.0` is pinned: don't move to 2.x while the model is a 4.6 (§1).
- **Bedrock:** `run_samples` needs a profile allowed to call Bedrock. A call costs about 3¢ and takes 6–32 s. Bedrock
  compiles an output schema it hasn't seen in the last 24 h, which likely explains why first runs are slower.
- **Branch and PR:** branch `m6-2-josh` from main. PRs merge with merge commits, and Jake reviews.
- **Deploy:** Jake deploys `anomalypulse-genai` from main after the merge, and verifies it. With the switch off by
  default, that deploy changes no live behaviour.
- **Don't touch:**
  - `anomalypulse-shared`, which is frozen until M9-1 (README, "Status");
  - the M6-1 input contract, which Rachel builds M8-3 against (#30).
  Adding the reply's `runbook` field is fine, because it's additive.

---

## 8. Your M6-2 checklist (its Definition of Done, as scoped)

- [ ] Five runbooks, checked against §4's facts, with no simulator words.
- [ ] `runbooks.py`, the switch (off by default), the user-turn section and the reply's `runbook` field.
- [ ] The self-check allows runbook numbers in actions only.
- [ ] The Makefile ships the runbooks, and the built package imports them.
- [ ] Tests from §5, including the off-changes-nothing fixture.
- [ ] Both sample runs pass the self-check, with blind actionability scores recorded in M06.
- [ ] M06's M6-2 boxes:
  - **runbook retrieved and its content appears:** met by the lookup, shown in the actions;
  - **retrieval quality measurable:** state that it equals classifier accuracy, and point to the actionability
    comparison;
  - **clearly gated as stretch:** the switch, and the off fixture.
- [ ] Docs: `DIAGNOSIS_CONTRACT.md`, the README's `genai/` row, and M06.

Your next tickets touch this:
- **M8-4 (#31):** the reply's `diagnosis` is the dashboard's `diagnosis` exactly. `evidence_metrics`, `self_check`
  and `runbook` sit beside it, and M8-4 decides whether the console shows them.
- **M9-1 (#33):** the long pole. It can't start until M8's pipeline works, and it then takes about 3 days of runner
  time.
