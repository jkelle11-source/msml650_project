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
- **Runbooks were off** for all of them (`ANOMALYPULSE_RUNBOOKS=off`), as deployed.
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

| Test set | Diagnoses | Evidence fidelity (claims supported) | Hallucination (mentions) | Completeness (moved metrics cited) | Capacity slip in actions |
|---|---|---|---|---|---|
| **All** | **296** | **2,045 of 2,095 (97.6%)** | **68 of 14,844 (0.46%)** | **2,085 of 2,207 (94.5%)** | **82** |
| TRAFFIC_SPIKE | 77 | 555 of 556 (99.8%) | 0 of 4,042 (0.00%) | 560 of 564 (99.3%) | 68 |
| DATABASE_THROTTLING | 68 | 400 of 447 (89.5%) | 66 of 2,861 (2.31%) | 331 of 357 (92.7%) | 7 |
| LAMBDA_DEGRADATION | 60 | 435 of 436 (99.8%) | 1 of 2,978 (0.03%) | 391 of 414 (94.4%) | 2 |
| DEPENDENCY_FAILURE | 38 | 278 of 279 (99.6%) | 1 of 1,949 (0.05%) | 300 of 352 (85.2%) | 0 |
| CASCADING_FAILURE | 53 | 377 of 377 (100%) | 0 of 3,014 (0.00%) | 503 of 520 (96.7%) | 5 |
| *Stress slice* | *20* | *151 of 151 (100%)* | *0 of 1,011 (0.00%)* | *154 of 168 (91.7%)* | *4* |

Rows are by the classified type, the one Bedrock was given.

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
on payment-service is the most likely root cause") with no metric to show it. The other three: a confidence
written "99.99%" where the input's is 0.99999985 (truncated, not rounded), a "490" that isn't in the input,
and one bullet naming order-service where the input had only payment-service.

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
2 still had a number off. No test diagnosis ended held back ("Severity pending"): none finished with causal
wording or a bullet citing nothing, so the console showed every stored diagnosis.

## Actionability

### The capacity slip (M8-4), counted in every diagnosis

An action that checks or raises a table's capacity on the Carts or Orders table (both on-demand) or on a
payment-service table (it has none) is an actionability error (`score.capacity_slips`). **82 of the 296
diagnoses (27.7%) have one, 68 of them traffic spikes** (68 of 77), for example: "Check DynamoDB auto-scaling
policies and provisioned capacity on the product-service, cart-service, and order-service tables". The
input never says how a table's capacity is set; the runbooks do.

This count is a pattern match, so it's approximate. It was tuned against the actions it matched to leave out
Lambda's provisioned concurrency and watching consumed capacity (`test_m9_5.py` holds both).

### With and without runbooks, blind

This decides whether `ANOMALYPULSE_RUNBOOKS` goes on for the demo (M6-2). **30 test incidents**, 6 of each
classified type (`compare.py`, seed 20261119):

- **Without runbooks:** the stored diagnosis, what the live store kept, retry included. With the switch off
  the request is byte-for-byte M6-1's, so this is exactly what runbooks-off produces.
- **With runbooks:** a new call on the same stored input, through the store's own retry rule, same model
  (Claude Sonnet 4.6) and effort. 38 calls for the 30 (8 retries), 2026-11-19, about $1.10.

**Scored by people, blind** (`sheet.py`): each incident's two action lists as A and B in a shuffled order
(seed 20261120; runbooks are A in 12 of 30), with the input's metrics and the stack facts M6-2's scorer had.
Each rater picks the better list (ties allowed) and scores each list 0/1 on Correct and Safe. Two raters,
Josh and Jake, each on their own copy, neither having read the runbooks; both are informed non-specialists
(no cloud-operations background), working from the sheet's stack facts. Scored 2026-11-22
(`results/blind/scores.json`).

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
  an incident the automatic count also flags for the capacity slip. Jake marked 26 incorrect: all 8 with the slip, and 18
  with none, and left no notes. So the two raters agree runbooks help, but not on how much was wrong
  without them. Jake's count isn't explained here.
- **Decision:** `ANOMALYPULSE_RUNBOOKS` goes **on** for the demo. Both raters' preference and the automatic slip
  count (8 → 2) point the same way, and no runbook fact reached a claim. The switch itself is flipped in the
  deploy, not here.

**Scored automatically, the same 30:**

| | Without runbooks | With runbooks |
|---|---|---|
| Diagnoses with a capacity slip in an action | 8 | 2 |
| Evidence fidelity (claims supported) | 204 of 208 | 205 of 211 |
| Hallucinated mentions | 9 of 1,462 | 8 of 1,482 |
| Moved metrics cited | 206 of 220 | 203 of 220 |

- **The capacity slip mostly goes away.** The two left with runbooks are milder: "whether capacity limits are
  being approached" on the order-service table, and "review the cart-service DynamoDB table capacity mode".
- **No runbook fact reached a claim.** M6-2 asked that a runbook-only fact in a summary, root cause or evidence
  bullet count as unsupported; none did. Every unsupported claim on either side is the throttling-without-a-metric
  case above (two incidents on both sides, a third only with runbooks, whose live retry had come back clean).

### Retrieval quality

The runbook is picked by the classified type (`genai.runbooks.runbook_for`), so the retrieved runbook matches
the incident type exactly when the classifier is right: **retrieval quality equals the classifier's test-set
accuracy, reported in M9-2.** It isn't a number of its own.

## How far to trust these numbers

- **They come from rules, not a reader.** The self-check is the same rule set the live store retried
  against, so a slip it can't see is invisible here too. To bound that, 10 diagnoses it passed (2 per type,
  drawn with seed 20261119; 64 claims) were read claim by claim against their inputs on 2026-11-19, by
  Claude Code, not a person. It found no unsupported claim the rules missed. That's a small sample.
- **The rules can be stricter than a reader.** "99.99%" for 0.99999985 counts as unsupported, and so does
  "throttling" wherever the input has no throttling metric, even as the restated label.
- **Test set only for the headline.** The stress slice is the separate line in the table.

## Running it

```bash
cd AnomalyPulse
python -m genai.m9_5.fetch --profile <profile>              # read-only: data/records.json, data/retries.jsonl
python -m genai.m9_5.score                                  # results/scores.json
python -m genai.m9_5.compare --profile <profile> --dry-run  # the 30 incidents and the cost
python -m genai.m9_5.compare --profile <profile>            # Bedrock with runbooks, about $1
python -m genai.m9_5.sheet build                            # results/blind/sheet.md, key.json
python -m genai.m9_5.sheet score <filled sheet>...          # results/blind/scores.json
python -m pytest -q genai/m9_5
```
