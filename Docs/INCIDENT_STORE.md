# The incident store (M8-3)

**Status:** built and tested offline (99 tests in `incidents/`, none reach AWS). Not deployed yet, and not checked live. **Owner:** Rachel · **Code:** `AnomalyPulse/incidents/` · **Stack:** `infra/incidents/` (`anomalypulse-incidents`) · **Reads:** [`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md), [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md) · **Read by:** M8-4 (the feed), M8-5 (the live source)

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
6. **Explain.** On `opened` and each `type_changed`, up to 5 calls per incident: build the input
   (`incidents/builder.py`), check it with `genai.contract.validate_input`, call `anomalypulse-explain`, and
   store the diagnosis, or the error. A diagnosis that failed its self-check is stored, flagged.
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
| `prediction` | The dashboard's shape: `incident_type`, `confidence`, `started_at`, `detected_at`, `affected_services`, `model` (`estimator`, `job`) |
| `diagnosis` | The dashboard's `diagnosis`, or `null` until Bedrock answers (or if it failed) |
| `diagnosis_for_type` | The incident type that diagnosis was made for |
| `bedrock_calls`, `diagnosis_error` | Calls so far (at most 5), and the last call's error (`for_type`, `type`, `message`), cleared by a good call |
| `evidence_metrics`, `self_check`, `runbook` | The explanation Lambda's other outputs, for M8-4 |
| `experiment_id` | The ground-truth reference (Tier-2), at the top level and never inside `prediction` or Bedrock's input. Absent for a real incident, with no experiment running |
| `latency` | `window_end_to_diagnosis_s`, `diagnosis_to_stored_s`, `bedrock_ms` |

`detected_at` is the `latency.diagnosed_at` of the incident's first counted minute. `prediction.incident_type`
is the incident's own type (the most counted minutes), never the latest minute's label
([`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Why the incident's type").

**One state item:** `pk = "INCIDENT_STATE"`, `sk = "CURRENT"`. It holds `incident.py`'s state (also kept after an
incident closes, so the next window can be checked against it), the last 5 healthy windows (the classifier said
`NORMAL` and the detector did not flag the minute), and the baseline fixed for the open incident. It has its own
`pk` so that a newest-first Query on `INCIDENT` never returns it.

**For the dashboard.** `build_dashboard` treats any incident it is given as active (`active_incidents` is 1 and the
status is `INCIDENT`), so the live source must pass only an `OPEN` record. `incidents.record.to_incident` returns
the three fields it reads. Numbers come back from DynamoDB as `Decimal`; `record.from_dynamodb` converts them.

## Round-trip latency

Each stored diagnosis writes one embedded-metric log line (namespace `AnomalyPulse/Incidents`, dimension
`Function`) with `SinceWindowEndSeconds` (the minute's end to its diagnosis, about 170 s), `DiagnosisToStoredSeconds`
(the diagnosis to the record stored, including Bedrock's 6 to 32 s), `RoundTripSeconds` (the two added) and
`BedrockMs`. The same stretches are in the record's `latency`, so M9-4 can total them in Logs Insights with
`filter event = "incident_diagnosis"`.

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
python3 -m pytest incidents -q          # 99 tests; none reach AWS
cfn-lint infra/incidents/template.yaml  # no findings (cfn-lint 1.60.0, 2026-11-07)
```

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
failed its self-check, or was made for an earlier type, is printed as a note. The judging is tested offline;
its AWS reads have not run yet.

## Decisions

- **The type an incident reports** is the incident's (2026-11-08, Jake).
- **Baseline, metrics, when to call Bedrock** are in [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md), "Decided in M8-3".
- **The ground-truth reference** comes from the experiment lock, read when the incident opens. A lock whose
  `expires_at` has passed (a crashed run) is ignored. No lock means a real incident. A failed read fails the event
  and lands in the queue, instead of storing a wrong reference.
- **Order after a backlog:** `step`'s skipping is accepted. A counting window delivered after a newer one is dropped.
- **The stack** is its own, not `anomalypulse-genai`.

## Not verified yet

- **Nothing has run on AWS.** The tests fake the table, S3, the lock and the explanation Lambda.
- **`sam build` has not run.** The Makefile finds the repository with `git`, so the stack's `samconfig.toml` builds
  in the source folder, as the pipeline's does. The bundle was built and imported by hand.
- **Whether the event carries the full window** (`detail.requestPayload`) is unchecked: the handoff says to check it
  on the first live event, and the Lambda falls back to the window's file.
- **Josh and Linu have not replied** on the layout (#30). `affected_services` is refreshed on each counted minute;
  whether the console wants it fixed at opening is open.
- **`observed_sequence` is not sent,** so Bedrock makes no ordering claim.