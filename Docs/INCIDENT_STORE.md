# The incident store (M8-3)

**Status:** deployed (2026-11-08) and checked live on a traffic spike and a cascade; 126 offline tests in `incidents/` and a replay of validation. **Owner:** Rachel · **Code:** `AnomalyPulse/incidents/` · **Stack:** `infra/incidents/` (`anomalypulse-incidents`) · **Reads:** [`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md), [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md) · **Read by:** M8-4 (the feed), M8-5 (the live source)

The Lambda `anomalypulse-incidents` takes each diagnosed minute, groups minutes into incidents with
`ml/diagnosis/incident.py`, asks `anomalypulse-explain` (M6-1) to explain each incident, and stores a record the
dashboard reads in the Incidents table.

## The path of one event

1. **Event in.** The Diagnosis Lambda's success destination puts each reply on the pipeline bus. A rule
   (`anomalypulse-diagnosed-window-to-incidents`) hands the event to the Lambda. The reply is
   `detail.responsePayload`, and the feature window is `detail.requestPayload.detail.windows[0]` (read from the
   window's file in S3 if it is missing).
2. **Skip what it has seen.** A window not newer than the incident state's `last_window` changes nothing, so
   repeats and stragglers are harmless.
3. **Step.** `incident.step` returns the new state and `changes` (`opened`, `type_changed`, `closed`).
4. **Close the old incident.** When one window closes an incident after a gap and opens the next, `step`
   returns only the new incident's state, so the old one is closed from the state loaded before the call
   (`ended_at` = its last counted minute + 1 minute).
5. **Open.** The record is written first, with `diagnosis: null`, so the console shows "Severity pending". The
   baseline is fixed here, and the ground-truth reference is read from the experiment lock.
6. **Explain.** Whenever the stored diagnosis is not for the incident's current type, up to 5 calls per
   incident: when it opens, at each `type_changed`, and on the next counted minute after a failed call. Build
   the input (`incidents/builder.py`), check it with `genai.contract.validate_input`, call
   `anomalypulse-explain`, and store the diagnosis, or the error. A diagnosis that failed its self-check is
   stored, flagged.
7. **State last.** The state item is written after everything else. If the Lambda dies before that, the retry
   repeats the minute (Bedrock may be called twice) instead of leaving an incident with no diagnosis and no
   retry.

## What it stores

Everything is in `anomalypulse-incidents` (SSM `/anomalypulse/tables/incidents/name`). The `HEALTH` partition is
the window builder's.

**One item per incident:** `pk = "INCIDENT"`, `sk = started_at`.

| Field | What it holds |
|---|---|
| `incident_id` | `inc-` plus `started_at`, the same for the whole incident |
| `status`, `ended_at` | `OPEN` or `CLOSED`; `ended_at` is `null` while open |
| `prediction` | The dashboard's shape: `incident_type`, `confidence`, `started_at`, `detected_at`, `affected_services`, `model` (`estimator`, `job`). `affected_services` only grows while the incident is open: the opening minute's list, plus each counted minute's services that cross the line |
| `diagnosis` | The dashboard's `diagnosis`, or `null` until Bedrock answers (or if it failed) |
| `diagnosis_for_type` | The incident type that diagnosis was made for |
| `bedrock_calls`, `diagnosis_error` | Calls so far (at most 5), and the last call's error (`for_type`, `type`, `message`), cleared by a good call |
| `evidence_metrics`, `self_check`, `runbook` | The explanation Lambda's other outputs, for M8-4 |
| `experiment_id` | The ground-truth reference (Tier-2), at the top level and never inside `prediction` or Bedrock's input. Absent for a real incident, with no experiment running |
| `latency` | The first diagnosis's round trip: `window_end_to_diagnosis_s` (the first counted minute's end to its diagnosis), `diagnosis_to_stored_s` (that diagnosis, `detected_at`, to the first diagnosis stored), `bedrock_ms` (that call's) |

`detected_at` is the `latency.diagnosed_at` of the incident's first counted minute. `prediction.incident_type`
is the incident's own type (the most counted minutes), never the latest minute's label
([`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Why the incident's type").

**One state item:** `pk = "INCIDENT_STATE"`, `sk = "CURRENT"`. It holds `incident.py`'s state (also kept after an
incident closes, so the next window can be checked against it), the last 5 healthy windows (the classifier said
`NORMAL` and the detector did not flag the minute; from any run, however old), and the baseline fixed for the open
incident. It has its own `pk` so that a newest-first Query on `INCIDENT` never returns it.

**When an incident closes.** Only when later minutes arrive: two in a row that don't count, or a window more than
2 minutes after the last minute that counted. Idle time announces no window, so if traffic stops within about
4 minutes of a fault's end (the last counted minute is often the first one after the fault, then two quiet minutes
must be announced), the record stays `OPEN` until the next traffic, and a console reading it would show an active
incident while the system is idle. The next run's first window then closes it with the right `ended_at`. On
validation's replay, 108 of the 112 incidents were closed by a later run's windows, because those runs end 2
minutes after their fault. Live runs are long enough: the simulator's default `--recovery` and the cascade spec's
`recovery_s` are both 300 s. **For M8-5 and the M10 demo:** keep traffic running at least 4 minutes after a fault
ends.

**For the dashboard.** `build_dashboard` treats any incident it is given as active (`active_incidents` is 1 and the
status is `INCIDENT`), so the live source must pass only an `OPEN` record. `incidents.record.to_incident` returns
the three fields it reads. Numbers come back from DynamoDB as `Decimal`; `record.from_dynamodb` converts them.

## Round-trip latency

Each stored diagnosis writes one embedded-metric log line (namespace `AnomalyPulse/Incidents`, dimension
`Function`) with `SinceWindowEndSeconds` (the minute's end to its diagnosis, about 170 s), `DiagnosisToStoredSeconds`
(the diagnosis to the record stored, including Bedrock's 6 to 32 s), `RoundTripSeconds` (the two added) and
`BedrockMs`, each from the minute that triggered that call, so M9-4 can total them in Logs Insights with
`filter event = "incident_diagnosis"`. The record's `latency` keeps only the first diagnosis's, so its stretches
belong to one minute: the incident's first counted minute's end to its diagnosis (`detected_at`), then
`detected_at` to the first diagnosis stored, which includes the wait when a failed first call is retried.

## IAM and failures

- **Role `anomalypulse-incidents`:** its own log group; `s3:GetObject` on `pipeline/windows/*`; `GetItem` and
  `PutItem` on the Incidents table with `LeadingKeys` limited to `INCIDENT` and `INCIDENT_STATE` (and the `Null`
  check, because `ForAllValues` alone is true of an empty list); `GetItem` on the experiments table for `LOCK`
  only; `lambda:InvokeFunction` on `anomalypulse-explain`; `ssm:GetParameter` on the four names it reads. It
  cannot write the window builder's `HEALTH` item.
- **One event at a time:** reserved concurrency 1, so two minutes can't read the same state and lose one.
- **Failures:** EventBridge invokes the Lambda asynchronously. It retries twice, then the event goes to
  `anomalypulse-incidents-failed` (kept 14 days; the event is in the message), as does an event EventBridge cannot
  deliver within the hour. The alarm `anomalypulse-incidents-failed` is on while the queue holds a message.
- **Timeout 150 s,** longer than the explanation Lambda's 120 s; the call itself is cut off at 140 s.

## Tests, deploying and checking

```bash
cd AnomalyPulse
python3 -m pytest incidents -q          # 126 tests; none reach AWS
cfn-lint infra/incidents/template.yaml  # no findings (cfn-lint 1.60.0, 2026-11-07)
```

`sam build` (SAM CLI 1.166.2, 2026-11-08) makes the bundle, which imports in Python 3.13 with nothing else from
the repository.

Deploy `anomalypulse-ml`, `anomalypulse-pipeline` and `anomalypulse-genai` first. Jake deploys this stack with
`admin`, because it creates a role. The live checks (a traffic spike with `--baseline 360`, then a cascade) run
from the runner host, one at a time and never during M9-1 ([`M8-3_Handoff.md`](../Handoffs/M8-3_Handoff.md), §9).

## Checking a live run

`incidents/live_check.py` reads one finished run and prints PASS or FAIL. It changes nothing. Run it at least
5 minutes after the run ends:

```bash
cd AnomalyPulse
python3 -m incidents.live_check <experiment_id> --profile anomalypulse
```

It reads the run's manifest (to know its type and window), the incident records the run opened, and the
Lambda's own log lines and health. For a fault run it passes with exactly one incident, closed, of the run's
type, with its diagnosis stored, 1 to 5 Bedrock calls, the run's `experiment_id` beside (never inside) the
prediction, and a clean read-back through `build_dashboard`. A NORMAL run passes with none. A diagnosis that
failed its self-check, or was made for an earlier type, is printed as a note. The judging is tested offline,
and it ran on the live traffic spike below with the AnomalyPulseDeveloper role.

## The live traffic spike (2026-11-08)

Run by Rachel from the runner with `drive.sh`: `exp-20261108T200346Z-6040`, a traffic spike in the ×6-8 band
(multiplier 7.4), 300 s of fault after `--baseline 360`, 20:03:46 to 20:19:46 UTC. `live_check.py` passed, run by
Rachel and again independently, and the minutes were rebuilt one by one from the two Lambdas' log lines and the
windows in S3:

- **One incident,** `inc-20261108T201000Z`, `TRAFFIC_SPIKE` (0.962), opened on the fault's first full minute
  (20:10) and closed as its run's type, with `ended_at` 20:16, 74 s after the fault ended. Its 6 counted minutes
  were 20:10 to 20:15; 20:16 and 20:17 didn't count, and the 20:17 event closed it. No other minute had an
  incident type.
- **The ground-truth reference:** the run's `experiment_id`, beside the prediction. `build_dashboard` read the
  record back with the run's manifest (detection latency 247.8 s from the fault's start).
- **Bedrock:** 1 call, at opening, and its input passed `validate_input`. The answer was stored, flagged: it said
  `MEDIUM`, and the rubric gives `LOW` for that input (traffic about 8x on every service, no 5xx, no p95 rise,
  no throttles). The explanation Lambda now reports the rubric's severity (a separate genai PR).
- **Round trip:** 174.6 s from the minute's end to its diagnosis, then 42.3 s to the stored diagnosis, of which
  Bedrock took 41.4 s (above the 6 to 32 s M6-1 measured): 216.9 s.
- **The event carries the window:** `window_from` was `event` on all 16 steps, so the store never read a window
  from S3.
- **Health:** 16 invocations, 0 errors, 0 throttles, 0 failed deliveries or dropped events, the failure queue
  empty, the alarm OK.
- **Baseline:** 3 healthy windows (20:06 to 20:08). The detector flagged 20:04 and 20:05, the first minutes after
  idle time, and 20:09, the minute the fault started in.
- **Found:** the record's `affected_services` ended as `["order-service"]`. The minutes 20:10 to 20:14 listed all
  four services; 20:15, the first minute after the fault, still counted, but nothing crossed the line, so the
  list fell back to one service and replaced the four. The list now only grows (above, "What it stores"); this
  record was stored before that change.

## The live cascade (2026-11-08)

Run by Rachel from the runner with `drive.sh`, chained in one tmux session after a noop warm-up: the noop
`exp-20261108T214244Z-7b76` (21:42:44 to 21:58:44 UTC; 19,173 requests, none dropped; `live_check` passed, with 19
steps, no incident and no Bedrock call), then the cascade `exp-20261108T215845Z-3480` from
`simulator/cascades/example.json` (21:58:45 to 22:18:45 UTC; 50,104 requests, 114 5xx, none dropped). Four faults
stacked: a traffic spike from 22:03:45, db-throttling from 22:05:15, lambda-degradation from 22:06:17 and
retry-amplification from 22:07:26 until 22:13:45. `live_check.py` passed:

- **One incident,** `inc-20261108T220400Z`, `CASCADING_FAILURE` (1.000), opened at 22:04 and closed at 22:16 as the
  run's type. One `type_changed` on the way.
- **`affected_services` ended as all four services:** the list only grows while an incident is open, so the
  cascade's later minutes added services instead of replacing them (the spike's record, stored before that
  change, ended as one).
- **The ground-truth reference:** the run's `experiment_id`, beside the prediction. `build_dashboard` read the
  record back with the run's manifest (detection latency 247.2 s from the fault's start).
- **Bedrock:** 2 calls, at opening and at the type change; the diagnosis stored is for `CASCADING_FAILURE`, and the
  self-check raised no finding.
- **Round trip:** median 203.4 s (max 209.5 s): 172.7 s from the minute's end to its diagnosis, then 30.7 s to the
  stored diagnosis (max 36.4 s), of which Bedrock took 30.1 s (max 36.1 s).
- **The event carries the window:** `window_from` was `event` on all 25 steps.
- **Health:** 0 errors, 0 failed deliveries, 0 dropped events, the failure queue empty, the alarm OK.

Not exercised on AWS: the cap of 5 calls and the retry after a failed call (no call failed; the validation replay
covers both).

## Checked on validation (replay, 2026-11-08)

Validation's 140 runs (slots 561-700) were rebuilt as the live stream, as in M8-2's review: each complete window
through the Diagnosis Lambda's `detect_then_classify`, wrapped as the event the rule delivers, then through
`core.process` in window order. The table (items serialized as DynamoDB would, so a float fails), the lock (the run
whose time holds the minute) and the explanation Lambda (`validate_input` again, a canned reply) were faked. Of
1,400 minutes, none failed:

- **Incidents:** 112, one per fault run, each closed as its run's type and referencing that run's `experiment_id`.
  No NORMAL run opened one.
- **Bedrock:** 125 calls (1 for 99 incidents, 2 for 13), and every input passed `validate_input`.
- **A failing first call:** with every incident's first call made to fail, all 112 still got a diagnosis, stored at
  the next counted minute, a minute later (226 calls). Calling only at opening and at type changes, as before the
  review, only the 13 incidents that changed type would have got one.
- **The record:** each read back through `build_dashboard` with its run's manifest. The largest item was the state,
  34 KB. `affected_services` at close: all four services on every traffic spike, Order and Payment on every
  dependency failure, Product alone on 23 of the 28 DB throttling incidents (88 of the 112 had one service before
  the list was made to only grow).
- **Baseline and metrics:** the numbers are in [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Decided in M8-3".
  Closing is in "When an incident closes", above.

**Running it again** (after a change to `incidents/`, `incident.py`, the builder's rules or a promoted model). The
scripts are in `incidents/replay/`; the replay reaches no AWS. It needs Python 3.13 with the Diagnosis Lambda's
libraries, because it runs the Lambda's own `detect_then_classify`:

```bash
cd AnomalyPulse
python3.13 -m venv <venv> && <venv>/bin/pip install -r ml/requirements-image.txt boto3 jsonschema
incidents/replay/download.sh <dir>                       # read-only, about 940 MB: the runs and both promoted models
<venv>/bin/python incidents/replay/replay.py <dir> <out.json>     # about a minute; add --fail-first for that variant
<venv>/bin/python incidents/replay/analyze.py <out.json>
```

`download.sh` takes each model from its promoted pointer (`/anomalypulse/ml/models/incident_type` and
`anomaly_iforest`), the versions the Diagnosis Lambda serves; the numbers above are from the classifier version
`kNUehxgF…` and the forest version `He.BgX3O…`. `replay.py`'s docstring says how each window and event is built.

## Decisions

- **The type an incident reports** is the incident's (2026-11-08, Jake).
- **Baseline, metrics, when to call Bedrock** are in [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Decided in M8-3",
  agreed by Jake (M6-1's proposals) on 2026-11-08. `affected_services` only grows while an incident is open.
- **The ground-truth reference** comes from the experiment lock, read when the incident opens. A lock whose
  `expires_at` has passed (a crashed run) is ignored. No lock means a real incident. A failed read fails the event
  and lands in the queue, instead of storing a wrong reference.
- **Order after a backlog:** `step`'s skipping is accepted. A counting window delivered after a newer one is dropped.
- **The stack** is its own, not `anomalypulse-genai`.

## Not verified yet

- **Two live runs only:** a traffic spike and a cascade. The call cap and the retry after a failed call have
  not run on AWS, only in the replay.
- **Josh and Linu have not replied** on the layout (#30).
- **`observed_sequence` is not sent,** so Bedrock makes no ordering claim.
