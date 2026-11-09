# Diagnosis Contract — Classified Incident → Bedrock → Diagnosis (M6-1, runbooks M6-2)

**Owner:** Josh · **Code:** `AnomalyPulse/genai/` · **Lambda:** `anomalypulse-explain`
(stack `anomalypulse-genai`, `AnomalyPulse/infra/genai/`) · **Used by:** M8-3 (builds the input, stores the
reply), M8-4 (feeds it to the dashboard), M9-5 (scores it)

**Status:** documented (M6-1, PR #86). Agreement with M8-3 (Rachel, #30) is pending on the three open
questions at the end, which carry proposals.

Bedrock is a constrained **explanation generator, not a diagnoser** (plan §10, §20). It is handed the
classifier's answer and the observed evidence, and writes the diagnosis an engineer reads. It never sees raw
logs, and never sees ground truth.

---

## Input (version 1)

One JSON object per diagnosis. `genai/contract.py` (`validate_input`) enforces every rule below; the five
files in `genai/samples/` are valid examples.

```json
{
  "contract_version": 1,
  "incident_id": "inc-20261030T143200Z",
  "incident_type": "DEPENDENCY_FAILURE",
  "confidence": 0.93,
  "started_at": "2026-10-30T14:32:00.000Z",
  "window_start": "2026-10-30T14:33:00.000Z",
  "duration_minutes": 2,
  "affected_services": ["order-service", "payment-service"],
  "metrics": [
    {"service": "payment-service", "metric": "p95_latency_ms", "baseline": 110, "current": 2900, "unit": "ms"},
    {"service": "order-service", "metric": "error_rate_5xx", "baseline": 0.0, "current": 0.167, "unit": "share"}
  ],
  "observed_sequence": [
    {"at": "2026-10-30T14:32:00.000Z", "service": "payment-service", "metric": "p95_latency_ms",
     "observation": "rose above its baseline"},
    {"at": "2026-10-30T14:32:40.000Z", "service": "order-service", "metric": "error_rate_5xx",
     "observation": "rose above its baseline"}
  ]
}
```

| Field | Meaning | From (M8-3) |
|---|---|---|
| `incident_id` | The incident's id. Returned with the diagnosis; never sent to Bedrock (nor `contract_version`) | The dashboard contract's `incident_id` |
| `incident_type` | One of `TRAFFIC_SPIKE`, `DATABASE_THROTTLING`, `LAMBDA_DEGRADATION`, `DEPENDENCY_FAILURE`, `CASCADING_FAILURE`; never `NORMAL` | The incident's type, from `ml/diagnosis/incident.py`: the type with the most of the incident's minutes so far, a tie going to the most recent (decided 2026-11-08, below) |
| `confidence` | 0–1 | That type's latest probability, `incident.py`'s `confidence` |
| `started_at`, `window_start` | The incident's first window; the latest window | `window_start` of those windows |
| `duration_minutes` | How long the incident has been active | |
| `affected_services` | Distinct service names, at least one: where the incident shows | `genai.affected.affected_services()`: see below. The same list is the dashboard's `prediction.affected_services` |
| `metrics` | The evidence: one entry per (service, metric), at least one | Feature windows: see below |
| `observed_sequence` | Optional. When each listed metric first moved, in time order | See below |

**Why the incident's type, not the latest window's label** (decided 2026-11-08). As a fault ends, the
classifier often names a different type for its last minute or two. On `v1`'s validation runs rebuilt as
the live stream, taking each minute's label closed 21 of 112 incidents with the wrong type (13 of 21
dependency failures as `LAMBDA_DEGRADATION`), and the most-minutes type closed all 112 with their run's
type ([`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md), "Opening and closing incidents"). A cascade still moves
from its first fault's type to `CASCADING_FAILURE`, once that has as many of the incident's minutes. The
dashboard's `prediction.incident_type` is the same type ([`DASHBOARD_CONTRACT.md`](DASHBOARD_CONTRACT.md)).

**`metrics`.** `metric` is a feature column without its service prefix, so `order-service`'s
`p95_latency_ms` is the column `order_p95_latency_ms` (`Docs/FEATURE_MATRIX.md`). `current` is the latest
window's value; `baseline` is the service before the incident (proposal: the mean of the lookback windows
before `started_at`), or `null` when there was nothing to measure. Units are fixed per metric:

| Metrics | Unit |
|---|---|
| `request_rate` | `rps` (per second) |
| `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms`, `lambda_duration_ms_mean`, `lambda_duration_ms_p95`, `db_latency_ms_mean`, `db_latency_ms_p95`, `dependency_latency_ms_mean`, `dependency_latency_ms_p95` | `ms` |
| `error_rate_5xx`, `error_rate_4xx` | `share` (0–1) |
| `cold_start_count`, `lambda_throttle_count`, `throttled_requests`, `read_throttle_events`, `write_throttle_events`, `dependency_error_count` | `count/min` |
| `db_consumed_read_capacity` / `db_consumed_write_capacity` | `RCU/min` / `WCU/min` |

Only the feature matrix's pairs are accepted (`contract.SERVICE_METRICS`): Payment has no table, so no `db_*`
or throttle metric, and only Order has the three `dependency_*` metrics.

Send the metrics that moved and enough context to show what didn't (Bedrock may only cite what it is sent).
About 5–10 metrics is plenty.

**`observed_sequence`.** Only what the windows show: the first window in which a metric left its baseline.
`observation` is `"rose above its baseline"` or `"fell below its baseline"`, nothing else, so no free text
reaches Bedrock. With one-minute windows, M8-3 can give minute resolution; finer times need the request
records. Without it, Bedrock makes no ordering claim at all.

**`affected_services`.** The classifier answers with a label and probabilities only, so the services come
from the windows, by a fixed rule (`genai/affected.py`; decided 2026-11-02, M6-1 review). M8-3 calls
`affected_services(baseline(lookback_windows), latest_window)` with feature rows, stores the list in the
incident record's prediction, and sends it here. A service is affected when, in the latest window, one of
its own signals crosses the severity rubric's `MEDIUM` line (5xx share at least 0.01, p95 at least 2x its
baseline, any throttle or dependency-error count above zero) or its request rate is at least 1.75x its
baseline. If none does, it is the one service whose request rate or p95 rose most, so the list is never
empty. Only Tier-1 columns decide it, never the manifest.

Calibrated on v1's 700 training and validation runs (2026-11-02; the test set was not read). Each run's
baseline was the mean of its startup minute and the NORMAL minute before its first fault (v1 runs have one),
and each labeled minute was scored against the faults active in at least half of it:

- **No false alarms:** none of the 980 later NORMAL minutes of the 140 normal runs lists a service. There,
  5xx shares, throttle counts and dependency errors are always 0, p95 stays under 1.6x its baseline and
  request rate under 1.7x.
- **The faulted service is always listed** in every database-throttling (518), dependency-failure (525),
  Lambda-degradation (542) and cascade (268) minute. A traffic spike's minutes list Product in 707 of 718.
  The rest are a mild spike's partial first minute, where the fallback names the service that rose most.
  The fallback was needed in 7 of the 2,571 incident minutes, all traffic spikes.
- **Extra services are real movement:** a slowed Order's Payment shows 5xx and a doubled p95 (8 minutes),
  for example. The list says where metrics moved, not which service was injected.
- **Why 1.75x for traffic:** at 2x, 37 spike minutes don't list Product; at 1.5x, 8 normal minutes list a
  service.

**Never in the input** (refused by `validate_input`): ground truth, the manifest, `experiment_id`, fault
parameters, `injector`, `band`, `intensity`, `label`, `error_type`, `db_throttled`. These are Tier-2: the
point of M9-5 is that Bedrock explains from what a monitor could see.

**Revising an answer** (2026-11-08, for M8-3's retry of a flagged diagnosis). The Lambda's event may carry
`revise` beside the input's fields: `{"answer": ..., "findings": [...]}`, an earlier answer to this same input
as Bedrock gave it (`severity`, `summary`, `root_cause`, `evidence` bullets with the `metrics` each cites,
`recommended_actions`) and 1 to 20 of its self-check findings, each at most 1,000 characters.
`contract.validate_revision` refuses any other shape, a cited metric that isn't in this input, or a
ground-truth key, with `BadInput`, before Bedrock. The earlier answer then goes back as the assistant's turn,
and a user turn lists the findings and asks for the whole diagnosis again, fixed (`prompt.revision_message`).
A claim the evidence really shows may stay, since the check has false positives. The new answer is checked
like any other, and the reply has the same shape. Without `revise`, the request is byte-for-byte what it was.

## Output

`genai.explain.diagnose(input)`, which the Lambda returns:

```json
{
  "incident_id": "inc-20261030T143200Z",
  "diagnosis": {
    "severity": "HIGH",
    "summary": "…",
    "root_cause": "A slow or failing payment dependency is the most likely root cause.",
    "evidence": ["Payment p95 latency rose 26x, from 110 ms to 2900 ms.", "…"],
    "recommended_actions": ["Inspect payment-service health and its recent errors.", "…"]
  },
  "evidence_metrics": [["payment-service.p95_latency_ms"], ["…"]],
  "runbook": null,
  "self_check": {"passed": true, "findings": []},
  "model": {"id": "global.anthropic.claude-sonnet-4-6", "effort": "medium"},
  "usage": {"input_tokens": 2292, "output_tokens": 1420},
  "latency_ms": 30651
}
```

- **`diagnosis` is exactly the dashboard contract's `diagnosis`** (`Docs/DASHBOARD_CONTRACT.md`): `severity`
  (`LOW`/`MEDIUM`/`HIGH`, the rubric's, computed from the input: below), `summary`, `root_cause`,
  `evidence` (strings), `recommended_actions` (strings, in the order to try them). The dashboard's open
  question 2 is answered: the fields match. Bedrock is still asked for a severity, by the rubric the prompt
  states, and `self_check` flags an answer whose severity is not the rubric's, but the diagnosis reports the
  rubric's (2026-11-08). On M8-3's first live traffic spike Sonnet said `MEDIUM` where the rubric gives `LOW`
  (traffic about 8x, no errors, no latency rise), so the console would have shown a severity the rubric
  contradicts. The finding stays in `self_check`, so M9-5 can count how often Bedrock disagrees. The classifier's `confidence` is
  not repeated here; it is the dashboard's `prediction.confidence`.
- **`evidence_metrics[i]`** lists the input metrics evidence bullet *i* rests on. Bedrock's answer is held to
  a schema built per request (structured outputs) in which a bullet can only cite this input's metrics, so
  no bullet can cite a metric the input lacks. The schema can't require a citation, though: a bullet citing
  none is a `citation` finding (`evidence[i] cites no input metric`, since 2026-11-09). The incident store
  keeps it beside the bullets, and M8-4 checks every shown bullet against it and the stored input
  ([`INCIDENT_FEED.md`](INCIDENT_FEED.md)).
- **`runbook`** (M6-2): the name of the runbook the diagnosis was given (`"dependency_failure"`), or `null`
  when runbooks are off, the default. It sits beside `diagnosis`, not in it, so the dashboard contract is
  unchanged. See "Runbooks (M6-2)" below.
- **`self_check`**: the smoke-level fidelity check below. A diagnosis that fails it is still returned, with
  `passed: false` and its findings, so M8-3 can store it flagged rather than lose it. The findings are
  plain sentences; `genai.self_check.kind()` reads each one's kind back from its text (`number`, `time`,
  `wording`, `severity`, `threshold`, `service`, `metric`, `citation`, or `shape` for
  `contract.check_output_shape`'s, except a bullet citing no metric, a `citation`), so a reader never
  matches the wording itself. M8-3 asks again on all but `severity`, `threshold` and `shape` ("Decided
  in M8-3", 3).
- **Errors.** A bad input raises `BadInput` (it never reaches Bedrock). A refusal (Bedrock declining; there
  is no server-side fallback on Bedrock) or an answer cut off at `max_tokens` raises `DiagnosisFailed`.
  A Bedrock attempt is cut off at 50 s and retried once (as are throttles and 5xx), then raises the SDK's
  error, so a stalled call fails inside the Lambda's 120 s timeout instead of the Lambda timing out (a
  throttle whose `retry-after` asks for a longer wait can still outlast it). Invoked as a Lambda, all of these
  come back as a `FunctionError` with the message.

## The wording rules

In the prompt (`genai/prompt.py`) and checked by `genai/self_check.py`:

1. **Every claim comes from the input**: only its services, metrics and values (or ratios, percent changes
   and unit conversions of them).
2. **Ordering is an observed sequence, not causation.** A sentence that orders symptoms says "observed
   sequence" and is taken from `observed_sequence`. Causal words ("caused", "driven by", "led to",
   "triggered", "due to"), negated ones included, appear only in `root_cause`, which is worded as *most
   likely* and connects symptoms only along the stack (Order calls Payment; Product, Cart and Order read
   their own tables).
3. **A cascade's faults are concurrent.** `CASCADING_FAILURE` means two or more faults are active at once
   (plan §3), and the simulator injects each one on its own (`Docs/CASCADE.md`), so its `root_cause` names
   the faults the evidence shows as concurrent, links none of them causally, and orders them only by the
   observed sequence. This is the demo's cascade narration (plan §15, Demo Part 4). The first prompt let
   Sonnet write that "the increased order volume put payment-service under load, ... which in turn produced
   a flood of dependency errors"; no metric shows Payment's load, and the payment fault was injected.
4. **Evidence** is 3–6 bullets of observed fact, each with numbers from the metrics it cites only; mechanism
   belongs in `root_cause`. The severity rubric's thresholds are used, never quoted.
5. **Recommended actions** (2–5) name parts of the stack to inspect, but state no facts that aren't in the
   input.

**The severity rubric** (`contract.severity()`; the prompt states the same constants), from the affected
services' current values: `HIGH` if any 5xx share is at least 0.05 or any p95 latency at least 10x its
baseline; `MEDIUM` if any 5xx share is at least 0.01, any p95 at least 2x its baseline, or any throttle or
dependency-error count above zero; `LOW` otherwise.

`self_check` flags:

- **Numbers** not in or derivable from the input (a value, ratio, percent change, difference, share as a
  percent or as "1 in N", ms as seconds, rps per minute, a per-minute count or capacity per second, the
  confidence, the duration, a gap between sequence steps), rounded no further than written: "2950" must be
  2945–2955 and "2.9" 2.85–2.95. "p95" and "the 95th percentile" are names, not values. A hedge or bound
  before a number says it may be cut off instead of rounded, so it gets one written unit on the side the
  word gives: "roughly 15x" is 14–16, "more than 15x" 15–16, "nearly 15x" 14–15, and the second number of a
  range ("roughly 15x to 21x") takes the first's word. In an evidence bullet, the
  numbers must come from the metrics that bullet cites (or a gap or ratio between two of them), so a real
  value on the wrong metric is caught. On the five samples a number picked at random from 1 to 1000 passes
  2–4.5% of the time in the summary and under 3% in a bullet (it was 7–56% with the first version's flat 6%
  tolerance and no per-bullet check). Hedged, it passes 4–9.5% of the time after "roughly" and 2.6–6.6% after
  a bound: a wrong number behind "roughly" is about twice as likely to slip through (2026-11-06).
- **Severity** other than the rubric's, and, in the summary, root cause or evidence, any mention of a
  threshold.
- **A clock time** not in the input.
- In the summary, root cause or evidence: **a service** the input doesn't name ("in order to" and "the
  order of" are words, not order-service); **a claim about a kind of signal** (throttling, cold starts,
  capacity, errors, latency, traffic, compute time, the database, timeouts, retries) with no metric of that
  kind in the input (retries are supported by `db_latency_ms`, defined as including the SDK's retries; "the
  4-minute duration" is the input's own, not compute time); any mention of **a deployment** or release, or
  of **how a table's capacity is set** ("provisioned", "on-demand": no metric covers either);
  **causal wording** outside `root_cause`, and in a cascade's `root_cause` any causal
  link ("in turn", "produced", "under load"; the noun "root cause" is fine); and **an ordering sentence** without
  "observed sequence" or with no `observed_sequence` in the input ("the first sign" and "15.0 before" order
  nothing, nor does "followed at roughly the same scale", two metrics moving together; a bare "latency has
  followed" still orders).

Actions are checked for numbers and times only ("approaching throttling thresholds" there is DynamoDB's
capacity, not the rubric). With a runbook, the numbers the runbook writes are allowed in actions too, held to
the precision they are written in; a runbook number in the summary, root cause or evidence is still "not in
or derivable from the input". It is a dry run for M9-5's evaluation, not that evaluation.

## Runbooks (M6-2)

A runbook per incident type (`genai/runbooks/<type>.md`), picked by the classified type
(`genai.runbooks.runbook_for`), written for an on-call engineer on this stack: what to check, with the
contract's metric names, and what to do. Scope decided 2026-11-02: five runbooks and no Bedrock Knowledge Base
(with five types the pick is a lookup).

- **Off by default:** `ANOMALYPULSE_RUNBOOKS` (`on`/`off`, `off` in `infra/genai/template.yaml`), or
  `diagnose(..., runbooks=True)`. With it off, the request is byte-for-byte the M6-1 request; a test holds it
  to a fixture saved before M6-2 changed any code (`genai/test_data/`).
- **In the user turn only,** after the evidence, under "Runbook for this incident type. Use it only for
  recommended_actions …" (`prompt.RUNBOOK_INTRO`), which also tells Bedrock to state nothing in the summary,
  root cause or evidence that only the runbook says. `SYSTEM_PROMPT` is unchanged.
- **Runbook facts can still reach a claim.** The prompt asks Bedrock to keep them out, but the self-check
  enforces that only for a runbook's numbers and for a table's capacity mode. Before the review added that
  sentence to the intro, two of seven with-runbook answers had a runbook fact in a claim: "the provisioned
  table" in a throttling root cause (2026-11-03), and "no requests have yet reached the Lambda timeout" in an
  evidence bullet (2026-11-05). After it, one of seven did: a throttling evidence bullet reading the 5xx share
  as requests "exhausting all retry attempts". So M9-5 scores a runbook-only fact in a summary, root cause or
  evidence bullet as unsupported, the same as any other fact the input doesn't hold. Only then do its
  evidence-fidelity and hallucination scores mean the same with the switch on or off.
- **Their numbers are stack facts** (verified against `infra/shared/template.yaml` and the handlers;
  `Handoffs/M6-1_Handoff.md` §4): 1,000 concurrent Lambda executions; the Products table's 200 RCU and 5 WCU,
  and DynamoDB's 5 minutes of saved burst capacity; 7 SDK attempts on a DynamoDB call; 1,769 MB, a 10 s
  timeout and Python 3.13 for the functions; the 2 s connect and 6 s read timeouts on Order's call to Payment.
  A test pins this set: a new number is a deliberate change, because the self-check allows it in every action
  of that type.
- **No simulator or Tier-2 words** (a test scans for them), and no causal claims: checks and actions only (a
  test runs the self-check's own causal and cascade-link patterns over every runbook).
- **Retrieval quality** ("does the retrieved runbook match the incident type?") equals the classifier's
  accuracy (M9-2), since the runbook is picked by the classified type: report it that way. What M6-2 measures
  instead is whether the actions get better, by a blind comparison with and without runbooks (M06), repeated
  in M9-5 on the test set, which decides whether the switch is on for the demo.

## Running it

```bash
cd AnomalyPulse
python3 -m pip install -r genai/requirements.txt                   # once, for local runs (SDK 1.x)
python -m pytest -q genai                                          # no AWS
python -m genai.run_samples --profile <your-profile>               # the five samples through Bedrock
python -m genai.run_samples --profile <your-profile> --runbooks on # the same, with runbooks (M6-2)
```

**Deploy** (its own stack, never `anomalypulse-shared`):

```bash
cd AnomalyPulse/infra/genai && sam build && AWS_PROFILE=admin sam deploy    # admin: it creates an IAM role
aws lambda invoke --function-name anomalypulse-explain --cli-binary-format raw-in-base64-out \
  --payload file://../../genai/samples/dependency_failure.json reply.json --profile <your-profile>
```

**Model.** Claude Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`, a global cross-region inference
profile; Bedrock through `bedrock-runtime`, us-east-2), adaptive thinking at effort `medium`. The model,
region and effort are environment variables (`ANOMALYPULSE_BEDROCK_MODEL`, `_REGION`, `_EFFORT`).

**Why this model (2026-11-01).** Of the Claude models, the account can call Opus 4.6 and Sonnet 4.6 on Bedrock
(3M and 6M tokens per minute, cross-region). Claude 4.7 and later (Opus 4.7, 4.8 and 5.5, Sonnet 5.5, ...) are
enabled, but their Bedrock quota is 0 tokens per minute and has never been raised (checked 2026-11-02), so calls
to them fail ("not available for this account"; for Opus 4.8, "Too many tokens per day"). Using one means
asking AWS Support to raise its quota first. The newer `bedrock-mantle` endpoint answers 404 for every model,
so the code uses `bedrock-runtime`. Run on the five samples with the first prompt, Opus 4.6 had 7 real
self-check findings (two diagnoses with 7 bullets, causal wording, a speculated deployment) and Sonnet 4.6
had 3; the prompt was tightened against those. Sonnet wrote about twice the output tokens (~1,200 a
diagnosis, thinking included) and was a little slower (median 31 s against 26 s); at list prices the two
cost about the same per diagnosis. Bedrock's own prices apply (aws.amazon.com/bedrock/pricing). Opus's
answers to that first prompt are kept in `genai/samples/results/`; the final self-check finds 12 problems in
them, while Sonnet's answers to the final prompt pass (that folder's README says what each run is).

**SDK 1.x, deliberately.** Python SDK 2.0 and later send API version `2026-10-21`, which Bedrock accepts
only for Claude 4.7 and later, so `requirements.txt` pins the 1.x release tested (1.17.0). Moving to a
4.7-or-later model, once its quota is raised, is also the time to move to SDK 2.x.

## Decided in M8-3 (2026-11-07)

The three open questions, answered in `incidents/` (Docs/INCIDENT_STORE.md). Agreed on 2026-11-08 by
Jake, who made M6-1's proposals on #30, including the three ways these answers differ from them:
the baseline also leaves out minutes the detector flagged, the standard metrics come after the
others, and there are at most 5 calls.

1. **Baseline.** The mean of the last 5 windows before `started_at` that the models called
   healthy: the classifier said `NORMAL` **and** the detector didn't flag the minute (`label` is
   `NORMAL` and `anomalous` is false). The detector alone keeps flagging for about 3 minutes
   after a fault and flags the first minute after idle time (`LIVE_DIAGNOSIS.md`), so a minute it
   flags is not a clean baseline even if the classifier calls it `NORMAL`. The incident store
   keeps those windows as it sees each diagnosed minute, adds none while an incident is open,
   computes the baseline (`genai.affected.baseline()`) the moment an incident opens, and holds
   it fixed for the whole incident, so a cascade's later minutes still compare against the state
   before it. A metric with nothing to average has `baseline: null`. The windows can come from an
   earlier run, and none is too old: the store keeps the last 5 it saw, however long ago. On
   validation's replay (Docs/INCIDENT_STORE.md), whose runs have only a minute or two of
   healthy traffic before the fault, 1 of the 112 baselines came from the incident's own run, and
   the oldest window was a median of 20 minutes before the incident (at most 32). The first live
   spike (`--baseline 360`, 6 minutes before the fault) had 3, all its own: the detector flagged the
   two minutes after idle time and the minute the fault started in. A longer baseline gives more.
2. **Which metrics.** The proposal, at most 10, in a fixed order. For each affected service,
   a metric is sent when it moved (the lines `affected.py` uses: 5xx share at least 0.01, p95
   or any other latency or duration at least 2x its baseline, request rate at least 1.75x, any
   throttle or dependency-error count above zero), or it is one of the incident type's own signals
   (listed in `incidents/builder.py`), or it is one of the three standard metrics (`request_rate`,
   `p95_latency_ms`, `error_rate_5xx`), which give Bedrock the context to say what did not move.
   Moved and type-signal metrics come first, then the standard ones, so a cap of 10 drops
   context before it drops evidence. Values are rounded to 4 decimals. A metric empty in the
   latest window is left out. `observed_sequence` is not sent in this version, so Bedrock makes
   no ordering claim. On validation's replay, 98 of the 125 inputs reached the cap. What it cut
   was mostly context, the unmoved standard metrics (333 service-metric pairs), and percentiles
   beside the p95 that moved (`p99_latency_ms` 79 times, `p50_latency_ms` 28). In 16 inputs it
   kept one of the type's own signals that had not moved (such as `write_throttle_events` at 0)
   and cut one that had. No input named an affected service without sending one of its metrics.
3. **When to call.** Whenever the stored diagnosis is not for the incident's current type, at
   most 5 calls per incident: when an incident opens, each time its type changes (a cascade that
   changed type 3 times on the live run would make 4), and on the next counted minute after a
   call that failed, so a throttle or a timeout doesn't leave the console on "Severity pending"
   for the rest of the incident. On validation's replay that is 125 calls for 112 incidents. Each
   stored diagnosis records the type it was made for (`diagnosis_for_type`), so a console showing
   a newer type can tell. A call that fails is counted and stored with its error, and an earlier
   diagnosis is kept; a diagnosis that failed its self-check is stored, flagged.

   **And once per type, a retry for a flagged diagnosis** (decided 2026-11-08 by Jake, from Josh's
   request for M8-4): when the stored diagnosis is for the current type but its self-check found
   something the input doesn't support (a number, a clock time, a service, a claim with no metric of
   its kind, a bullet citing a metric not in the input or none) or causal or ordering wording (`kind`
   `number`, `time`, `service`, `metric`, `citation`, `wording`), the next counted minute asks again,
   within the 5 calls. It resends the diagnosis's own input (`diagnosis_input`) with the flagged answer
   and its findings, leaving out a severity disagreement ("Revising an answer", above), so Bedrock is
   told what to fix; a record stored before inputs were kept gets no retry. Its answer replaces the
   flagged one if its self-check is clean, apart from a severity disagreement (the rubric's severity is
   reported anyway), or if the flagged one is held back from the dashboard and the retry has nothing
   that would hold it (`record.retry_kept`, 2026-11-09: an answer the console can show beats one it
   can't, so a retry that fixes causal wording but rounds a number too far is kept). Otherwise, or if
   the call fails, the flagged one stays. M8-4 holds a diagnosis flagged for wording or a citation back
   from the dashboard (`diagnosis: null`, "Severity pending") and shows one with any other finding
   ([`INCIDENT_FEED.md`](INCIDENT_FEED.md)). One retry per type, so retries can't use up
   the calls a later type change needs. A severity disagreement, a quoted threshold and a shape
   problem (such as 7 evidence bullets) are not retried. Of the 17 findings on the 20 saved answers
   (`genai/samples/results`), 13 would be (all but 2 thresholds and 2 shape problems). The record
   keeps the input each stored diagnosis was made from (`diagnosis_input`), replaced with it. On real
   Bedrock (Sonnet 4.6, 2026-11-08), the three saved Opus answers with findings (13 in all, 2 to 6
   each, shape and threshold ones included) came back from a revision with none, in 5 to 10 s each,
   against 15 s for a plain call on the cascade sample: a revision reads about 700 more tokens and
   writes about half as many. On validation's replay with each type's first answer flagged (for
   causal wording since 2026-11-09; Docs/INCIDENT_STORE.md, "Checked on validation"), every incident
   got its retry, each resending the flagged diagnosis's input, and kept the clean answer: 114
   retries, 239 calls, at most 4 for an incident.

`prediction.affected_services` only grows while an incident is open (decided 2026-11-08): it starts as
the opening minute's `affected_services`, and each counted minute adds the services that cross the line
(`genai.affected.crossing`), never removing one. Replacing it with each counted minute's list, as first
built, let the last counted minute shrink it: that minute is often the first one after the fault, when the
classifier still names the incident but nothing crosses any more, so `affected_services` falls back to the
one service that rose most. The first live spike's record ended as `["order-service"]` that way, and on
validation's replay 88 of the 112 records ended with one service, every dependency failure among them.
Growing, every traffic spike keeps all four services and every dependency failure keeps Order and Payment.
Each Bedrock input still lists its own minute's `affected_services`.
