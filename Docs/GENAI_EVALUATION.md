# GenAI Quality Evaluation (M9-5)

**Status:** scored on the test set's stored diagnoses (2026-11-19); the runbook comparison scored blind by two
raters (2026-11-22): runbooks go on for the demo. **Owner:** Josh · **Code:** `AnomalyPulse/genai/m9_5/` (`fetch.py`, `score.py`,
`compare.py`, `sheet.py`, tests in `test_m9_5.py`) · **Results:** `AnomalyPulse/genai/m9_5/results/` ·
**Reads:** [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), [`INCIDENT_STORE.md`](INCIDENT_STORE.md),
[`INCIDENT_FEED.md`](INCIDENT_FEED.md), [`TEST_SET.md`](TEST_SET.md)

Bedrock is scored where it can be wrong: whether what it says comes from the input it was given, whether it
leaves out what moved, and whether its actions are right for this stack.

## Why root-cause correctness isn't the headline

Bedrock is handed the classifier's incident type (`incident_type` in its input), and the prompt asks for the
most likely root cause of that type. A root cause that "matches the incident" is the classifier's answer
restated, so scoring it would report M9-2's classification accuracy a second time, not anything Bedrock did.

The test set shows it plainly. 29 of the 68 diagnoses classified `DATABASE_THROTTLING` had no database
metric in their input at all: only payment-service's request rate, 5xx share and p95 latency (below,
"What the unsupported claims are"). Bedrock named throttling or the database as the root cause in 26 of
them anyway. Scored as root-cause correctness against the run's label, all 29 are "correct" (each run was a
throttling run); scored for fidelity, 26 are claims nothing in the input supports. The second is the one
that tells you whether an engineer can trust the explanation, so it's the headline.

## What is scored

- **The stored diagnoses** of the test set's incidents: the 296 the live incident store kept for v1's test
  runs (slots 701–1050, 292 runs, 2026-11-13 to 11-16), each with the input it was made from
  (`diagnosis_input`), the metrics each evidence bullet cites (`evidence_metrics`) and its self-check.
  Downloaded read-only by `fetch.py` from the Incidents table, matched to runs by `experiment_id`. The
  stress slice's 20 are reported on their own line.
- **Which runs they cover:** 292 of the 350 test runs, 285 of the 290 fault runs and 7 of the 60 healthy ones.
  The other 5 fault runs opened no incident, so they have no diagnosis to score. The 8 diagnoses on the 7
  healthy runs are false alarms. 40 of the 296, those 8 included, were handed a type other than the run's
  label. Their evidence fidelity is 99.3%, because fidelity checks a claim against the input Bedrock got,
  whether or not the type in it was right; M9-2 scores the type.
- **Matched by time instead (M9-6, 2026-11-25):** the store reads `experiment_id` from the experiment lock when an
  incident opens, about 4 minutes after its first minute, so 4 of the 296 are filed under the next run. They are the
  detections of 2-minute faults in slots 733, 839 and 975 (839's is one of the 8 "false alarms" above), and slot
  751's false alarm. Matched by the run that holds the first minute, 288 fault runs and 8 healthy runs opened an
  incident, and the previous run's incident, still open, covered the other 2 fault runs
  ([`M9-6_REPORT.md`](M9-6_REPORT.md)). The scores here are not redone.
- **Runbooks were off** for all of them (`ANOMALYPULSE_RUNBOOKS=off`), as deployed at the time.
- **One set of rules:** each diagnosis is checked again with today's self-check against its stored input,
  as the feed check does (`incidents/feed.py`).

Definitions (`score.py`):

- A **claim** is the summary, the root cause or one evidence bullet. Actions are not claims: they say what
  to look at, and people score them (below).
- A claim is **unsupported** when it states a number, time, service or kind of metric the input doesn't
  hold, puts a number on a metric the bullet doesn't cite, or cites no input metric. **Evidence fidelity**
  is the share of claims that are supported; the unsupported-claim rate is the rest.
- A **mention** is one service, number, clock time or metric word (throttling, latency, database, …) in a
  claim. It is **hallucinated** when the input doesn't have it. A real number put on the wrong metric is
  misattributed, not hallucinated: it counts against fidelity only.
- **Salient evidence** is each input metric that moved, by the window builder's own rule
  (`incidents.builder._moved`), and each affected service. **Completeness** is the share of moved metrics
  that an evidence bullet cites, and of affected services a claim names.

## Results

| Test set | Diagnoses | Evidence fidelity (claims supported) | Hallucination (mentions) | Completeness (moved metrics cited) | Stack error in actions (capacity / X-Ray) |
|---|---|---|---|---|---|
| **All** | **296** | **2,045 of 2,095 (97.6%)** | **68 of 14,844 (0.46%)** | **2,085 of 2,207 (94.5%)** | **128 (82 / 50)** |
| TRAFFIC_SPIKE | 77 | 555 of 556 (99.8%) | 0 of 4,042 (0.00%) | 560 of 564 (99.3%) | 68 (68 / 0) |
| DATABASE_THROTTLING | 68 | 400 of 447 (89.5%) | 66 of 2,861 (2.31%) | 331 of 357 (92.7%) | 9 (7 / 5) |
| LAMBDA_DEGRADATION | 60 | 435 of 436 (99.8%) | 1 of 2,978 (0.03%) | 391 of 414 (94.4%) | 44 (2 / 43) |
| DEPENDENCY_FAILURE | 38 | 278 of 279 (99.6%) | 1 of 1,949 (0.05%) | 300 of 352 (85.2%) | 1 (0 / 1) |
| CASCADING_FAILURE | 53 | 377 of 377 (100%) | 0 of 3,014 (0.00%) | 503 of 520 (96.7%) | 6 (5 / 1) |
| *Stress slice* | *20* | *151 of 151 (100%)* | *0 of 1,011 (0.00%)* | *154 of 168 (91.7%)* | *8 (4 / 4)* |

Rows are by the classified type, the one Bedrock was given. A diagnosis with both stack errors counts once in
the total.

- **Unsupported-claim rate 2.4%** (50 of 2,095), in 29 of the 296 diagnoses. By field: summary 22 of 296
  (7.4%), root cause 27 of 296 (9.1%), evidence bullets 1 of 1,503 (0.07%). The bullets are almost always
  right because each must cite an input metric and its numbers are checked against the ones it cites; the
  summary and root cause have no citation, and that's where the slips are.
- **Hallucination rate 0.46%** (68 of 14,844 mentions), in 28 diagnoses: metric words 63 of 3,692 (1.7%),
  services 1 of 3,304, numbers 4 of 7,846, clock times 0 of 2. One more number was real but on the wrong
  metric (misattributed).
- **Completeness:** 94.5% of moved metrics cited, and every affected service named (736 of 736). 196 of the
  293 diagnoses with a moved metric cite every one. Dependency failures leave out the most (85.2%): mostly
  payment-service's request rate (17 times; the mix shift below) and order-service's p50 latency (16), then
  the mean dependency latency when the p95 is cited (10).
- **Severity:** Bedrock's own severity differed from the rubric's in 35 of 296. The stored diagnosis always
  carries the rubric's (computed from the input, not generated), so none reached the console.

### What the unsupported claims are

47 of the 50 are in `DATABASE_THROTTLING` diagnoses, all from the 29 whose input held only payment-service
(no table, so no database metric) and nothing from product-service. Bedrock was told the type is database
throttling and repeated it ("payment-service is flagged for a DATABASE_THROTTLING incident"; "DATABASE_THROTTLING
on payment-service is the most likely root cause") with no metric to show it. The other three: a summary's
"490" that isn't in the input, a root cause naming order-service when the input has none of it, and an
evidence bullet putting an "8" on a metric it doesn't cite.

Why the input held only payment-service: payment's request rate is above its baseline in nearly every test
incident (about 0.94 → 2 rps), because the test set's endpoint mix doubles `POST /orders` (5% → 10%) and
the baseline comes from the training mix. So payment counts as affected, and in these windows product-service
didn't (yet). The window builder adds a type's own signals only for affected services that have them
(`builder.TYPE_SIGNALS`), so Bedrock got none of throttling's. **Not fixed here:** the system is evaluated as
it ran. Two things for others to weigh:

- **For M9-2 and M9-3:** payment-service looks affected in most test windows from the mix shift alone.
- **For the builder:** sending the classified type's signals for the service that has them, affected or
  not, would give Bedrock the evidence for (or against) the type it's handed.

### The retries and what the console showed

The incident store retries a flagged diagnosis once per type with its findings (M8-3). Over the test set's
incidents: **82 retries, 55 kept** (their self-check came back clean), 0 errors. Of the 27 not kept, 26 still
claimed throttling without a metric for it (the case above: a retry can't add evidence the input lacks) and
2 still had a number off. No test diagnosis ended held back for causal wording or a bullet citing nothing.

**The console showed 293 of the 296.** The other three (slots 877, 934 and 1031) used all 5 of their Bedrock
calls, then changed type once more, to `DEPENDENCY_FAILURE`. Their stored diagnosis was for the earlier type,
so the console ended on "Severity pending" (`record.shown_diagnosis`). They are still scored here, against
the input and type Bedrock was given.

## Actionability

### Stack errors, counted in every diagnosis

**128 of the 296 diagnoses (43.2%) have an action that is wrong about this stack**, in one of two ways. Both
counts are pattern matches, so they're approximate; `test_m9_5.py` holds the cases each leaves out.

- **The capacity slip (M8-4), 82 diagnoses (27.7%), 68 of them traffic spikes** (68 of 77). An action that
  checks or raises a table's capacity on the Carts or Orders table (both on-demand) or on a payment-service
  table (it has none) (`score.capacity_slips`), for example: "Check DynamoDB auto-scaling policies and
  provisioned capacity on the product-service, cart-service, and order-service tables". The input never says
  how a table's capacity is set; the runbooks do. The count leaves out Lambda's provisioned concurrency and
  watching consumed capacity.
- **The X-Ray slip, 50 diagnoses (16.9%), 43 of them Lambda degradations** (43 of 60). An action that sends
  the engineer to X-Ray traces or a service map (`score.xray_slips`), for example: "Inspect payment-service
  Lambda logs and X-Ray traces from the incident window". Active tracing is off on the four service
  functions and the explain function, and tracing is off on the API stage (checked 2026-11-23), so there
  are no traces to read. The two actions that suggest enabling X-Ray are a possible change, not a false
  fact, and aren't counted.

### With and without runbooks, blind

This decides whether `ANOMALYPULSE_RUNBOOKS` goes on for the demo (M6-2). **30 test incidents**, 6 of each
classified type (`compare.py`, seed 20261119):

- **Without runbooks:** the stored diagnosis, what the live store kept, retry included. With the switch off
  the request is byte-for-byte M6-1's, so this is exactly what runbooks-off produces.
- **With runbooks:** a new call on the same stored input, through the store's own retry rule, same model
  (Claude Sonnet 4.6) and effort. 38 calls for the 30 (8 retries), 2026-11-19, $1.44 from
  their recorded tokens at $3 in / $15 out per million.

**Scored by people, blind** (`sheet.py`): each incident's two action lists as A and B in a shuffled order
(seed 20261120; runbooks are A in 12 of 30), with the input's metrics and the stack facts M6-2's scorer had.
Each rater picks the better list (ties allowed) and scores each list 0/1 on Correct and Safe. Two raters,
Josh and Jake, each on their own copy; both are informed non-specialists (no cloud-operations background),
working from the sheet's stack facts. Jake's committed copy leaves out the sheet's instructions and stack
facts, which Jake had in view while scoring. Scored 2026-11-22 (`results/blind/scores.json`).

**Neither rater came to the runbooks cold.** Josh wrote them (M6-2) and Jake corrected them in that review, so
a rater could recognize their style even with the lists unlabeled and shuffled.

| Of 30 incidents | Josh | Jake | Both agree |
|---|---|---|---|
| Better: with runbooks | 17 | 28 | 17 |
| Better: without runbooks | 4 | 2 | 1 |
| Better: tie | 9 | 0 | 0 |
| Correct, with runbooks | 28 | 26 | 26 of 30 scored alike |
| Correct, without runbooks | 25 | 4 | 9 of 30 scored alike |
| Safe, with runbooks | 25 | 28 | 23 of 30 scored alike |
| Safe, without runbooks | 25 | 19 | 22 of 30 scored alike |

- **Both raters prefer the lists made with runbooks.** Both picked them on 17 incidents, and on 8 more one
  rater picked them and the other called it a tie. Both raters picked the list without runbooks on 1 (incident
  24); on 3 more the raters split outright. Neither rater favored a position: runbooks were list A in 12 of 30,
  and the picks follow the list, not the letter.
- **The lists without runbooks are where the raters differ most, on Correct.** Josh marked 5 incorrect, each one
  an incident the capacity-slip count also flags. Jake marked 26 incorrect and, asked afterward, named two
  reasons: pointing to X-Ray, which this stack doesn't run, and checking capacity on on-demand tables. The
  automatic count finds one of those two errors in 15 of the 30 lists. Jake marked all 15 incorrect. Josh marked
  5 of them and none of the 7 whose only error is X-Ray; the sheet's stack facts never say X-Ray is off. The
  other 11 of Jake's 26 have neither error and weren't itemized. So the raters agree runbooks help, but applied
  "no false fact about this stack" differently.
- **Decision:** `ANOMALYPULSE_RUNBOOKS` goes **on** for the demo. Both raters' preference and the automatic
  stack-error count (15 → 2) point the same way. One caveat: one runbook-only fact reached a claim (1 of 211,
  below). `infra/genai/template.yaml` sets the switch. The tables above still describe the runbooks-off
  system the test set ran on; the evidence for runbooks on is this 30-incident sample.
- **Deployed and checked live, 2026-11-23.** `anomalypulse-genai` went out at 05:49 UTC with the switch on.
  One traffic spike from the runner (`exp-20261123T055215Z-c61d`, 57,495 requests, none dropped) opened
  `inc-20261123T055800Z`, and its record holds the `traffic_spike` runbook. The self-check flagged the first
  answer (`metric`); the retry came back clean and was kept. That diagnosis has all 8 claims supported, all 8
  moved metrics cited and no capacity or X-Ray slip. `live_check` and the feed check passed, with no Lambda
  errors. The kept Bedrock call took 41 s, inside the 50 s limit per attempt. One run shows the switch works
  end to end. It isn't a quality measurement.

**Scored automatically, the same 30:**

| | Without runbooks | With runbooks |
|---|---|---|
| Diagnoses with a stack error in an action | 15 | 2 |
| … with a capacity slip | 8 | 2 |
| … with an X-Ray slip | 8 | 0 |
| Evidence fidelity (claims supported) | 204 of 208 (98.1%) | 204 of 211 (96.7%) |
| Hallucinated mentions | 9 of 1,462 | 8 of 1,482 |
| Moved metrics cited | 206 of 220 | 203 of 220 |

- **Stack errors mostly go away.** X-Ray drops from 8 lists to none. The two capacity slips left with runbooks
  are milder: "whether capacity limits are being approached" on the order-service table, and "review the
  cart-service DynamoDB table capacity mode".
- **One runbook-only fact reached a claim.** M6-2 asked that a runbook-only fact in a summary, root cause or
  evidence bullet count as unsupported. Reading all 211 with-runbooks claims found one: a throttling bullet
  saying "no requests have yet exhausted retries" (`inc-20261116T124800Z`), which the throttling runbook says
  and neither the input nor the prompt does. The rules can't see it, so it's listed in
  `score.RUNBOOK_ONLY_CLAIMS` and counted in the table (205 of 211 by the rules alone).
- **Fidelity is slightly lower with runbooks,** by three claims over the 30 incidents. Every other unsupported
  claim on either side is the throttling-without-a-metric case above (two incidents on both sides, a third only
  with runbooks, whose live retry had come back clean).

### Retrieval quality

The runbook is picked by the classified type (`genai.runbooks.runbook_for`), so the retrieved runbook matches
the incident type exactly when the classifier is right: **retrieval quality equals the classifier's test-set
accuracy, reported in M9-2.** It isn't a number of its own.

## How far to trust these numbers

- **They come from rules, not a reader.** The self-check is the same rule set the live store retried
  against, so a slip it can't see is invisible here too. To bound that, 10 diagnoses it passed (2 per type,
  drawn with seed 20261119; 64 claims) were read claim by claim against their inputs on 2026-11-19, by
  Claude Code, not a person. It found no unsupported claim the rules missed. That's a small sample. The 211
  claims made with runbooks were read the same way on 2026-11-23, for facts only the runbook holds, and that
  read found the one above.
- **The rules can be stricter than a reader.** "99.99%" for 0.99999985 counts as unsupported, and so does
  "throttling" wherever the input has no throttling metric, even as the restated label.
- **Test set only for the headline.** The stress slice is the separate line in the table.

## Running it

```bash
cd AnomalyPulse
python -m genai.m9_5.fetch --profile <profile>              # read-only: data/records.json, data/retries.jsonl
python -m genai.m9_5.score                                  # results/scores.json
python -m genai.m9_5.compare --profile <profile> --dry-run  # the 30 incidents and the cost
python -m genai.m9_5.compare --profile <profile>            # Bedrock with runbooks, about $1.50
python -m genai.m9_5.sheet build                            # results/blind/sheet.md, key.json
python -m genai.m9_5.sheet score <filled sheet>...          # results/blind/scores.json
python -m pytest -q genai/m9_5
```
