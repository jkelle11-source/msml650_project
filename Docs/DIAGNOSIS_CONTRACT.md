# Diagnosis Contract — Classified Incident → Bedrock → Diagnosis (M6-1)

**Owner:** Josh · **Code:** `AnomalyPulse/genai/` · **Lambda:** `anomalypulse-explain`
(stack `anomalypulse-genai`, `AnomalyPulse/infra/genai/`) · **Used by:** M8-3 (builds the input, stores the
reply), M8-4 (feeds it to the dashboard), M9-5 (scores it)

**Status:** draft for agreement with M5-3's classifier output (Rachel) at the M8-3 seam; open questions at
the end.

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
| `incident_id` | The incident's id | The dashboard contract's `incident_id` |
| `incident_type` | One of `TRAFFIC_SPIKE`, `DATABASE_THROTTLING`, `LAMBDA_DEGRADATION`, `DEPENDENCY_FAILURE`, `CASCADING_FAILURE`; never `NORMAL` | Classifier reply: the latest window's `label` |
| `confidence` | 0–1 | That label's probability |
| `started_at`, `window_start` | The incident's first window; the latest window | `window_start` of those windows |
| `duration_minutes` | How long the incident has been active | |
| `affected_services` | Distinct service names | Same as the dashboard's `prediction.affected_services` |
| `metrics` | The evidence: one entry per (service, metric), at least one | Feature windows: see below |
| `observed_sequence` | Optional. When each listed metric first moved, in time order | See below |

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

Send the metrics that moved and enough context to show what didn't (Bedrock may only cite what it is sent).
About 5–10 metrics is plenty.

**`observed_sequence`.** Only what the windows show: the first window in which a metric left its baseline.
With one-minute windows, M8-3 can give minute resolution; finer times need the request records. Without it,
Bedrock makes no ordering claim at all.

**Never in the input** (refused by `validate_input`): ground truth, the manifest, `experiment_id`, fault
parameters, `injector`, `band`, `intensity`, `label`, `error_type`, `db_throttled`. These are Tier-2: the
point of M9-5 is that Bedrock explains from what a monitor could see.

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
  "self_check": {"passed": true, "findings": []},
  "model": {"id": "global.anthropic.claude-sonnet-4-6", "effort": "medium"},
  "usage": {"input_tokens": 2292, "output_tokens": 1420},
  "latency_ms": 30651
}
```

- **`diagnosis` is exactly the dashboard contract's `diagnosis`** (`Docs/DASHBOARD_CONTRACT.md`): `severity`
  (`LOW`/`MEDIUM`/`HIGH`, Bedrock's judgment from a fixed rubric in the prompt), `summary`, `root_cause`,
  `evidence` (strings), `recommended_actions` (strings, in the order to try them). The dashboard's open
  question 2 is answered: the fields match, and Bedrock gives a severity. The classifier's `confidence` is
  not repeated here; it is the dashboard's `prediction.confidence`.
- **`evidence_metrics[i]`** lists the input metrics evidence bullet *i* rests on. Bedrock's answer is held to
  a schema built per request (structured outputs) in which a bullet can only cite this input's metrics, so
  every bullet traces to an input metric by construction. M8-4 can keep it beside the bullets.
- **`self_check`**: the smoke-level fidelity check below. A diagnosis that fails it is still returned, with
  `passed: false` and its findings, so M8-3 can store it flagged rather than lose it.
- **Errors.** A bad input raises `BadInput` (it never reaches Bedrock). A refusal (Bedrock declining; there
  is no server-side fallback on Bedrock) or an answer cut off at `max_tokens` raises `DiagnosisFailed`.
  Invoked as a Lambda, both come back as a `FunctionError` with the message.

## The wording rules

In the prompt (`genai/prompt.py`) and checked by `genai/self_check.py`:

1. **Every claim comes from the input**: only its services, metrics and values (or ratios, percent changes
   and unit conversions of them).
2. **Ordering is an observed sequence, not causation.** A sentence that orders symptoms says "observed
   sequence" and is taken from `observed_sequence`. Causal words ("caused", "driven by", "led to",
   "triggered", "due to"), negated ones included, appear only in `root_cause`, which is worded as *most
   likely*.
3. **Evidence** is 3–6 bullets of observed fact; mechanism belongs in `root_cause`. The severity rubric's
   thresholds are used, never quoted.
4. **Recommended actions** name parts of the stack to inspect, but state no facts that aren't in the input.

`self_check` flags: a number not in or derivable from the input (a value, ratio, percent change, difference,
share as a percent or as "1 in N", ms as seconds, the confidence, the duration, a gap between sequence
steps); a clock time not in the input; in the summary, root cause or evidence, a service the input doesn't
name, a claim about a kind of signal (throttling, cold starts, capacity, errors, latency, traffic, compute
time, the database, timeouts, retries) with no metric of that kind in the input (retries are supported by
`db_latency_ms`, defined as including the SDK's retries), any mention of a deployment or release (no metric
covers one), causal wording outside `root_cause`, and an ordering sentence without "observed sequence" or
with no `observed_sequence` in the input. Actions are checked for numbers and times only. It is a dry run
for M9-5's evaluation, not that evaluation.

## Running it

```bash
cd AnomalyPulse
python3 -m pip install -r genai/requirements.txt                   # once, for local runs (SDK 1.x)
python -m pytest -q genai                                          # no AWS
python -m genai.run_samples --profile <your-profile>               # the five samples through Bedrock
```

**Deploy** (its own stack, never `anomalypulse-shared`):

```bash
cd AnomalyPulse/infra/genai && sam build && sam deploy
aws lambda invoke --function-name anomalypulse-explain --cli-binary-format raw-in-base64-out \
  --payload file://../../genai/samples/dependency_failure.json reply.json --profile <your-profile>
```

**Model.** Claude Sonnet 4.6 (`global.anthropic.claude-sonnet-4-6`, a global cross-region inference
profile; Bedrock through `bedrock-runtime`, us-east-2), adaptive thinking at effort `medium`. The model,
region and effort are environment variables (`ANOMALYPULSE_BEDROCK_MODEL`, `_REGION`, `_EFFORT`).

**Why this model (2026-11-01).** The account can use Claude Opus 4.6 and Sonnet 4.6 on Bedrock, but none
of Claude 4.7 or later ("not available for this account"), and the newer `bedrock-mantle` endpoint answers
404 for every model, so the code uses `bedrock-runtime`. Run on the five samples, Opus 4.6 had 7 real
self-check findings (two diagnoses with 7 bullets, causal wording, a speculated deployment) and Sonnet 4.6
had 3; the prompt was tightened against those. Sonnet wrote about twice the output tokens (~1,200 a
diagnosis, thinking included) and was a little slower (median 31 s against 26 s); at list prices the two
cost about the same per diagnosis. Bedrock's own prices apply (aws.amazon.com/bedrock/pricing).

**SDK 1.x, deliberately.** Python SDK 2.0 and later send API version `2026-10-21`, which Bedrock accepts
only for Claude 4.7 and later, so `requirements.txt` pins the 1.x release tested (1.17.0). Moving to a
4.7-or-later model, once the account has one, is also the time to move to SDK 2.x.

## Open questions (for M8-3)

1. **Baseline:** the mean of the lookback windows before `started_at`? A cascade's later windows would
   then compare against the pre-cascade baseline, which is what an engineer wants to see.
2. **Which metrics to send:** proposal: per affected service, `request_rate`, `p95_latency_ms`,
   `error_rate_5xx`, plus the type's own signals (throttle counts and DB latency for throttling,
   `lambda_duration_ms_mean` for Lambda degradation, the three `dependency_*` for dependency failures), and
   any other metric that moved by 2x or more.
3. **When to call:** once when the incident is first classified, and again when its type changes (a cascade
   is first classified as its first fault)?
