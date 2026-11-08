# The incident store (M8-3)

**Status:** built and tested offline (125 tests in `incidents/`, none reach AWS, and a replay of validation). Not deployed yet, and not checked live. **Owner:** Rachel · **Code:** `AnomalyPulse/incidents/` · **Stack:** `infra/incidents/` (`anomalypulse-incidents`) · **Reads:** [`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md), [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md) · **Read by:** M8-4 (the feed), M8-5 (the live source)

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
| `prediction` | The dashboard's shape: `incident_type`, `confidence`, `started_at`, `detected_at`, `affected_services`, `model` (`estimator`, `job`) |
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
python3 -m pytest incidents -q          # 125 tests; none reach AWS
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
failed its self-check, or was made for an earlier type, is printed as a note. The judging is tested offline;
its AWS reads have not run yet.

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
  34 KB.
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
  agreed by Jake (M6-1's proposals) on 2026-11-08. `affected_services` is refreshed on each counted minute.
- **The ground-truth reference** comes from the experiment lock, read when the incident opens. A lock whose
  `expires_at` has passed (a crashed run) is ignored. No lock means a real incident. A failed read fails the event
  and lands in the queue, instead of storing a wrong reference.
- **Order after a backlog:** `step`'s skipping is accepted. A counting window delivered after a newer one is dropped.
- **The stack** is its own, not `anomalypulse-genai`.

## Not verified yet

- **Nothing has run on AWS.** The tests and the replay fake the table, S3, the lock and the explanation Lambda.
  The Makefile finds the repository with `git`, so the stack's `samconfig.toml` builds in the source folder, as the
  pipeline's does.
- **Whether the event carries the full window** (`detail.requestPayload`) is unchecked: the handoff says to check it
  on the first live event, and the Lambda falls back to the window's file.
- **Josh and Linu have not replied** on the layout (#30).
- **`observed_sequence` is not sent,** so Bedrock makes no ordering claim.
