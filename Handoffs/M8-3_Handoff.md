# M8-3 Handoff — From the Live Diagnoses to Stored, Explained Incidents

> **The one-sentence version:** everything M8-3 joins together is merged, deployed and checked live. Each live
> minute's diagnosis arrives on the pipeline bus, `incident.py` groups minutes into incidents, M6-1's explanation
> Lambda writes the diagnosis, and the Incidents table is waiting. M8-3 is the Lambda in between: it keeps each
> incident's state, builds Bedrock's input, calls the explanation Lambda at the right moments, and stores a record
> the dashboard can read. §8 lists the decisions that are yours, and §9 is the checklist against your Definition
> of Done.

**Where it stands (2026-11-07):**
- **M8-2 is done.** PRs #93 and #95 are merged, the Diagnosis Lambda is deployed with the Isolation Forest in
  front of the classifier, and #29 is closed. Its three Definition of Done items were checked live: a traffic
  spike, then one run of each injector plus a NORMAL run ([`Docs/LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md)).
- **M6-1's explanation Lambda** (`anomalypulse-explain`, stack `anomalypulse-genai`) is deployed and answers the
  contract in [`Docs/DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md).
- **The pipeline (M8-1)** builds a window every minute and owns the Incidents table
  ([`Docs/PIPELINE.md`](../Docs/PIPELINE.md)).
- **Live runs send their traffic from the runner host,** never a laptop: laptop traffic made the Isolation Forest
  flag about half the healthy minutes ([`Docs/DETECTION.md`](../Docs/DETECTION.md), "Where live traffic comes
  from").

---

## 1. What arrives: one event per diagnosed minute

The Diagnosis Lambda's success destination is the pipeline bus, so each answer arrives there as a Lambda
invocation record. Subscribe with a rule on bus `anomalypulse-pipeline`:

```json
{"source": ["lambda"],
 "detail-type": ["Lambda Function Invocation Result - Success"],
 "detail": {"requestContext": {"functionArn": [{"prefix": "arn:aws:lambda:us-east-2:<account>:function:anomalypulse-diagnosis"}]}}}
```

| In the event | What it is |
|---|---|
| `detail.responsePayload` | The reply ([`LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md), "The reply"). `predictions[0]` has `window_start`, `anomalous`, `label`, `probabilities`, `incident_type`, `confidence` and `latency`; `model` has `estimator` and `job` |
| `detail.requestPayload` | The WindowReady event the Lambda answered (Lambda puts the request in the invocation record). So `detail.requestPayload.detail.windows[0]` is that minute's full feature window, 106 columns, and `.key` is its file in S3. Check this on the first live event; if it isn't there, read the window from `key` |

- **About one a minute while traffic flows, none while it's idle.** The window builder announces only windows with
  no empty value, so idle time leaves gaps. Each window is diagnosed about 170 s after its minute ends.
- **Earlier minutes' windows,** which the baseline needs, are files in the telemetry bucket (SSM
  `/anomalypulse/telemetry/bucket/name`): `pipeline/windows/dt=YYYY-MM-DD/minute=HH-MM.json`.
- **At least once, and in no guaranteed order.** After a backlog, several windows are announced at once, and a
  retried one can arrive after newer ones. See my comment on #30.

---

## 2. Grouping minutes into incidents: `incident.py`

`ml/diagnosis/incident.py` is a pure function with no dependencies; copy it into your Lambda or import it:

```python
state, changes = incident.step(state, prediction)    # prediction: responsePayload["predictions"][0]
```

- **A minute counts** when the detector flags it *and* the classifier names an incident (`incident_type` set).
- **It opens** on the first minute that counts, and **closes** after 2 minutes in a row that don't, or after a gap
  of more than 2 minutes since the last minute that counted.
- **Its type is the type with the most counted minutes so far** (a tie goes to the most recent), and its
  confidence is that type's latest. `changes` lists `opened`, `type_changed` and `closed`.
- **It ignores a window that isn't newer than the last one it applied:** that drops repeats, and also
  stragglers.

Results: on validation's live-built windows, each of the 112 fault runs opened exactly one incident of its own
type, and no healthy run opened one. Live, all six runs did the same ([`LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md),
"Opening and closing incidents").

**You keep the state between invocations** (the dict `step` returns), in the Incidents table. Two events handled
at once could both read the same state and lose one minute, so either give the Lambda a reserved concurrency of 1
(simple, and enough at one event a minute) or make the write conditional on `last_window`.

---

## 3. Building Bedrock's input

[`DIAGNOSIS_CONTRACT.md`](../Docs/DIAGNOSIS_CONTRACT.md), "Input", has every field; `genai/contract.py`'s
`validate_input` checks them. From what you hold:

| Field | From |
|---|---|
| `incident_id` | Yours, the same for the whole incident, e.g. built from `started_at` |
| `incident_type`, `confidence` | **The incident's**, from `incident.py`'s state, not the latest window's label (decided 2026-11-08; both contracts say so) |
| `started_at`, `window_start`, `duration_minutes` | The incident's first window, the latest window, and the minutes between |
| `affected_services` | `genai.affected.affected_services(genai.affected.baseline(earlier), latest)` with feature windows: `earlier` the windows before `started_at` classified `NORMAL`, up to the last 5 (open question 1); `latest` the latest window |
| `metrics` | About 5–10 (service, metric) pairs, each with `baseline` and `current` from the feature windows (open question 2) |
| `observed_sequence` | Optional: the first window in which each listed metric left its baseline |

**Never in the input:** anything from the manifest or the experiment (`experiment_id`, fault parameters, the
run's type or severity). `validate_input` refuses them: Bedrock explains only from what a monitor could see.

---

## 4. Calling Bedrock

Invoke `anomalypulse-explain` (its name is in SSM `/anomalypulse/genai/explain-function`) with the input, and
wait for the reply (`InvocationType: RequestResponse`). A call takes 6–32 s and costs about 3¢, and that Lambda's
own timeout is 120 s, so give yours longer (150 s, say). The reply ("Output" in the contract) holds `diagnosis`,
which is exactly the dashboard's `diagnosis`, plus `evidence_metrics`, `self_check`, `runbook`, `usage` and
`latency_ms`.

- **When to call is open question 3, your first decision** (§8). The contract proposes a call when an incident
  opens and again at each `type_changed`. On validation that would be 125 calls for 112 incidents. Live, though,
  the cascade changed type 3 times (`TRAFFIC_SPIKE` → `CASCADING_FAILURE` → `DATABASE_THROTTLING` →
  `CASCADING_FAILURE`), so 4 calls for one incident.
- **A diagnosis that fails `self_check` is still returned,** with `passed: false`: store it flagged, don't drop it.
- **Errors:** `BadInput` is a bug in the input you built. `DiagnosisFailed` (a refusal, or an answer cut off), or a
  timeout or throttle after Bedrock's one retry, comes back as a `FunctionError`. Store the incident with
  `diagnosis: null` and the error, so the console shows "Severity pending" rather than nothing.
- **Model:** Claude Sonnet 4.6 through Bedrock. Claude 4.7 and later have a quota of 0 on this account.

---

## 5. The incident record

**The table:** `anomalypulse-incidents` (SSM `/anomalypulse/tables/incidents/name`), keys `pk` and `sk`, both
strings, on-demand. The `HEALTH` partition is the window builder's. Your items get their own `pk`; one layout that
works is `pk = "INCIDENT"`, `sk = started_at`, so a Query on `INCIDENT`, newest first with a limit of 1, returns the
latest incident ([`PIPELINE.md`](../Docs/PIPELINE.md), "What it writes").

**What the dashboard reads.** `dashboard/builder.py`'s `build_dashboard` takes the incident as:

```json
{"incident_id": "inc-…",
 "prediction": {"incident_type": "…", "confidence": 0.93, "started_at": "…", "detected_at": "…",
                "affected_services": ["…"], "model": {"estimator": "…", "job": "…"}},
 "diagnosis": {"severity": "…", "summary": "…", "root_cause": "…", "evidence": ["…"], "recommended_actions": ["…"]}}
```

`diagnosis` is `null` until Bedrock answers. `detected_at` is the `latency.diagnosed_at` of the incident's first
counted minute. Store those fields as they are, plus what the rest of M8 needs:
- the incident's `status` (`OPEN`/`CLOSED`) and `ended_at`, so M8-5's live source can tell an active incident;
- the `incident.py` state;
- `evidence_metrics`, `self_check` and `runbook`, for M8-4;
- the latency below.

Agree the layout with Josh (M8-4, the stored diagnosis → the feed) and Linu (M8-5, the live source) before
building it.

**Numbers:** DynamoDB takes no floats, and reads come back as `Decimal`. The window builder converts with
`json.loads(json.dumps(item), parse_float=Decimal)`, which keeps each float's own digits.

**The ground-truth reference (your Definition of Done, item 3)** is Tier-2, so it is stored beside the
prediction and never inside it, nor in Bedrock's input. The dashboard contract's rule is that "the sections never
borrow from each other". The dashboard's read API reads the manifest itself, for `ground_truth` and `comparison`,
so the record needs only the reference: the run's `experiment_id`. Two ways to find it (§8, decision 4):
- **(a)** the experiment lock (`anomalypulse-experiments`, `pk = "LOCK"`), which holds the running
  `experiment_id` for the whole run, recovery included; read it when the incident opens;
- **(b)** the manifest under `experiments/` in the telemetry bucket whose run covers `started_at`.

No reference at all is valid too: an incident with no experiment running is "real" (the dashboard's
`no_ground_truth` scenario).

---

## 6. Where it runs, its IAM, and deploying

- **A stack of its own** (or added to `anomalypulse-genai`); never `anomalypulse-shared`, which isn't deployed
  until M9-1. Jake deploys, with `admin` because the stack creates a role.
- **Python 3.13 on arm64,** like the other Lambdas. Package `genai/` (for `affected.py` and `contract.py`) and
  `incident.py` with your handler. The explanation Lambda packages `genai/` with a Makefile build
  (`infra/genai/template.yaml`, `genai/Makefile`).
- **Least privilege, as in [`PIPELINE.md`](../Docs/PIPELINE.md), "IAM at each hop":** your own log group;
  `s3:GetObject` on `pipeline/windows/*` only; `GetItem`, `PutItem`, `UpdateItem` and `Query` on the Incidents
  table with `dynamodb:LeadingKeys` locked to your `pk` (and the `'Null'` check: `ForAllValues` alone is true of
  an empty list); `lambda:InvokeFunction` on `anomalypulse-explain`; `ssm:GetParameter` on the names you read;
  and, for (a), `GetItem` on the experiments table's `LOCK` item. A Lambda permission whose `SourceArn` is your
  rule.
- **Failures, as M8-2 did them:** EventBridge invokes your Lambda asynchronously. Set its retries, an on-failure
  destination and the rule's dead-letter queue to one queue, and alarm on that queue
  (`AnomalyPulse/infra/ml/template.yaml`, the "Live path (M8-2)" block).

---

## 7. Round-trip latency (your Definition of Done, item 4)

Each prediction's `latency` gives `window_end`, `announced_at` and `diagnosed_at`. The explanation Lambda's reply
gives `latency_ms`. Add the time your record is stored, and keep the stretches separately:
- from the minute's end to its diagnosis, about 170 s, nearly all of it waiting for the minute's CloudWatch
  snapshot;
- from the diagnosis to Bedrock's answer stored, 6–32 s plus your own time.

Write them as embedded-metric log lines too, as the Diagnosis Lambda does ([`LIVE_DIAGNOSIS.md`](../Docs/LIVE_DIAGNOSIS.md),
"Detection latency"), so M9-4 can total them in Logs Insights.

---

## 8. Your decisions

| # | Decision | The proposal on the table |
|---|---|---|
| 1 | **When to call Bedrock** (contract question 3) | At `opened` and each `type_changed`. Given the live cascade's 3 changes, consider a limit: for example, call on a type change only once the new type has held 2 minutes, or only on a change to `CASCADING_FAILURE` |
| 2 | **Baseline and which metrics** (contract questions 1 and 2) | Up to 5 `NORMAL` windows before `started_at`, fixed for the incident; each affected service's request rate, p95 and 5xx share, plus whatever crossed the line, plus the type's own signals |
| 3 | **The record's layout** | §5, agreed with Josh and Linu |
| 4 | **Where the ground-truth reference comes from** | (a) the lock, or (b) the manifest by time |
| 5 | **Order after a backlog** | Accept `step`'s skipping, or hold replies a minute or two and apply them in order (#30) |
| 6 | **The stack** | Your own, or `anomalypulse-genai` |

**Already decided (2026-11-08): the type an incident reports** is the incident's, the type with the most of its
minutes (`incident.py`), in Bedrock's input and in the dashboard's `prediction.incident_type`, never the latest
window's label. Taking each minute's label closed 21 of validation's 112 incidents with the wrong type. Both
contracts say so (`DIAGNOSIS_CONTRACT.md`, "Why the incident's type").

Record each one in the repo with the PR that builds it: decisions 1 and 2 in `DIAGNOSIS_CONTRACT.md`, replacing
its "Open questions" section.

---

## 9. Checklist

Against your Definition of Done (M08, `M8-3`):

- [ ] **Item 1, the contract's open questions answered:** decisions 1 and 2, recorded in `DIAGNOSIS_CONTRACT.md`.
- [ ] **Item 2, Bedrock input with no manual editing:** your Lambda builds it from the events and windows, and
  `validate_input` accepts it on every live incident.
- [ ] **Item 3, the diagnosis stored with a ground-truth reference:** the record in §5, read back by
  `build_dashboard` without errors.
- [ ] **Item 4, round-trip latency:** §7's stretches stored and logged.
- [ ] **Tests that reach no AWS:** recorded events in, records out, Bedrock and the table faked (the pipeline's
  tests show the pattern).
- [ ] **Live:** from the runner (`AnomalyPulse/infra/runner/drive.sh`, [`DATASET.md`](../Docs/DATASET.md), "The
  runner host"). First a traffic spike with `--baseline 360`, then a cascade, which changes type the most. Tell
  Jake first: one run at a time, and never during M9-1.
- [ ] **Docs:** a page for M8-3 in `Docs/`, its README rows, M08's "Decided" and "Done" notes, and #30 synced.

Jake has validation rebuilt as the live stream: 140 runs and every minute's reply. Ask if you want it to count
Bedrock calls under a rule, or to test the whole path offline.
