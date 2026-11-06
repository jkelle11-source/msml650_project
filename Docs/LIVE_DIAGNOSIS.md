# Detect + Classify on the Live Path (M8-2)

**Owner:** Melisa · **Lambda:** `anomalypulse-diagnosis` ([`AnomalyPulse/ml/diagnosis/`](../AnomalyPulse/ml/diagnosis/):
`handler.py`, `incident.py`, `forbidden_names.json`) · **Stack:** `anomalypulse-ml`
([`AnomalyPulse/infra/ml/template.yaml`](../AnomalyPulse/infra/ml/template.yaml)) · **Uses:** M8-1's WindowReady
events ([`PIPELINE.md`](PIPELINE.md)), M5-1's loader ([`ML_INFERENCE.md`](ML_INFERENCE.md)), M5-2's Isolation
Forest ([`DETECTION.md`](DETECTION.md)), the promoted classifier ([`M5-3_REPORT.md`](M5-3_REPORT.md)) · **Used by:**
M8-3 (opens and closes incidents from the replies, then calls Bedrock), M9 (live detection latency)

How each live window goes through the Isolation Forest, then the classifier, inside the Diagnosis Lambda, and what
comes back.

---

## The path

```text
window builder  → WindowReady event on bus anomalypulse-pipeline          (M8-1)
  → rule anomalypulse-window-ready-to-diagnosis → Diagnosis Lambda (async) (M8-2)
      window → Tier-1 guard → detector (anomaly_iforest)
             → if anomalous: classifier (incident_type) → incident_type, confidence
  → on success: the reply goes back to the pipeline bus as a Lambda destination event   (M8-3 subscribes)
  → on failure (3 tries), or if EventBridge can't deliver it within the hour:
      the event goes to the anomalypulse-diagnosis-failed queue (alarmed)
```

- **Two pointers, one cold start.** The Lambda loads the classifier from `ANOMALYPULSE_MODEL_POINTER`
  (`/anomalypulse/ml/models/incident_type`, as before) and the detector from `ANOMALYPULSE_DETECTOR_POINTER`
  (`/anomalypulse/ml/models/anomaly_iforest`). Both load through M5-1's `load_model`, with all its checks. Startup
  also refuses a detector that isn't a `detector`, or one whose `columns` differ from the classifier's: one window
  feeds both models.
- **The classifier runs only on flagged windows,** as the issue asks. A window the detector clears comes back
  `NORMAL` with no probabilities.
- **Direct calls get the same answer.** A direct `{"windows": [...]}` call to the deployed Lambda goes
  detector → classifier too, and gets the reply below, but writes no latency metric line: its windows can be
  days old, and their latency would swamp the live path's. Only with no detector configured (`ANOMALYPULSE_DETECTOR_POINTER`
  and `ANOMALYPULSE_DETECTOR_DIR` unset, as in the M5-2 test) does the Lambda answer as M5-1 built it, the
  classifier alone ([`ML_INFERENCE.md`](ML_INFERENCE.md)).
- **The Lambda keeps no state.** A repeated or out-of-order window gets the same answer each time. Grouping minutes
  into incidents is `incident.py`'s rule (below), which M8-3 runs against what it stores.

## The reply

To a WindowReady event, or a direct `{"windows": [...]}`:

```json
{"model":    {"task": "incident_type", "estimator": "logistic_regression", "job": "…", "version_id": "…"},
 "detector": {"task": "anomaly_iforest", "estimator": "isolation_forest", "job": "…", "version_id": "…",
              "threshold": -0.3695},
 "key": "pipeline/windows/dt=2026-11-06/minute=14-11.json",
 "predictions": [{
   "window_start": "2026-11-06T14:11:00.000Z",
   "anomalous": true, "score": -0.52,
   "label": "TRAFFIC_SPIKE",
   "probabilities": {"CASCADING_FAILURE": 1e-06, "…": "…", "TRAFFIC_SPIKE": 0.97},
   "incident_type": "TRAFFIC_SPIKE", "confidence": 0.97,
   "latency": {"window_end": "2026-11-06T14:12:00.000Z", "announced_at": "2026-11-06T14:14:46.000Z",
               "diagnosed_at": "2026-11-06T14:14:47.250Z", "since_window_end_s": 167.25, "since_announced_s": 1.25}
 }],
 "inference_ms": 1.9, "detect_ms": 1.2, "classify_ms": 0.6}
```

| Field | Meaning |
|---|---|
| `anomalous`, `score` | The detector's answer: `score` is `score_samples`, anomalous when below `detector.threshold` |
| `label`, `probabilities` | The classifier's answer when it ran; `"NORMAL"` and `null` when the detector cleared the window |
| `incident_type`, `confidence` | **The issue's output.** Set only when the detector flags the window *and* the classifier names an incident; `confidence` is that label's probability. Otherwise both are `null` |
| `latency` | Below |

**Why both must agree.** On validation's live-built windows ([`PIPELINE.md`](PIPELINE.md), "On the minutes only the
live path scores"), the detector alone keeps flagging for about 3 minutes after a fault ends, and flags the first
full minute after idle time. The classifier names an incident in only 78 of 112 first recovery minutes, 24 of 56
second ones and 2 of 138 third ones, and calls both after-idle minutes NORMAL. A minute the classifier names but the
detector clears is `null` too: the detector comes first, as the issue says.

**Errors** are M5-1's (`BadWindow`, `BadModel`). Live, an event that fails is retried twice by Lambda, then put in
`anomalypulse-diagnosis-failed` (kept 14 days), and the alarm `anomalypulse-diagnosis-failed` goes off. An event
EventBridge can't hand to the Lambda at all (a missing permission, throttling) lands in the same queue after an
hour of retries: it is the rule's dead-letter queue, so no window is dropped unseen.

## The Tier-1 guard on the live path

M4-5's guard (`features.guard`) can't run in the Lambda: its image is built from `ml/` alone, and the guard reads
the field catalog, the manifest schema and the injector registry. So `write_forbidden_names.py` copies the guard's
list into `forbidden_names.json`, and the Lambda checks two things against it, with the guard's own whole-word rule:

1. **Every model's `columns`, at startup.** A package with a Tier-2 column is never loaded (`BadModel`).
2. **Every window's keys, on every call.** A window with a Tier-2 key is refused (`BadWindow: forbidden columns in
   the window: …`). `build_vector` would refuse any name outside the model's columns anyway; this one names the
   leak.

`test_handler.py` keeps the copy honest. It fails while `forbidden_names.json` differs from
`guard.forbidden_names()`, and it checks that all 106 real feature columns pass the Lambda's check. Rerun
`python ml/diagnosis/write_forbidden_names.py` after any change to the catalog, the schema or an injector, and
commit the file.

The reply carries `incident_type`, which is a Tier-2 name *as a feature*. It is the model's output, never an input,
so the guard doesn't apply to it.

## Detection latency (plan §16)

Each window's reply holds `latency`, and each window of a WindowReady event also writes one log line in
CloudWatch's embedded metric format (a direct call writes none, above). Lambda turns that line into metrics in the
namespace `AnomalyPulse/Diagnosis` (dimension `Function`), with no API call and no extra permission:

| Metric | What it measures |
|---|---|
| `SinceWindowEndSeconds` | From the end of the window's minute to its diagnosis: the live path's share of detection latency. Expect about 2¾ min (the window waits for the minute's CloudWatch snapshot, [`PIPELINE.md`](PIPELINE.md)), plus the call |
| `SinceAnnouncedSeconds` | From the WindowReady event to the diagnosis: EventBridge, the async queue and the Lambda (cold starts included) |
| `InferenceMs` | Building the vectors, the detector and the classifier |
| `Anomalous`, `Incident` | 1 or 0 for the window: flagged by the detector; given an `incident_type` |

**End-to-end detection latency,** from the fault's start to its first incident minute, needs the fault's start.
That is ground truth (Tier-2), so the Lambda never sees it. M9 joins it offline: the first diagnosed window with an
`incident_type` after the manifest's fault start. The offline part of that wait, from the fault's start to the end
of the first flagged window, is in [`DETECTION.md`](DETECTION.md) (median 77.5 s). Live adds `SinceWindowEndSeconds`
on top.

Logs Insights on `/aws/lambda/anomalypulse-diagnosis`:

```text
filter event = "diagnosis"
| stats count(*) as windows, sum(Anomalous) as flagged, sum(Incident) as incident_minutes,
        pct(SinceWindowEndSeconds, 50) as end_p50_s, pct(SinceWindowEndSeconds, 90) as end_p90_s,
        max(SinceWindowEndSeconds) as end_max_s, pct(SinceAnnouncedSeconds, 50) as announced_p50_s
```

```text
filter event = "diagnosis"
| fields window_start, anomalous, score, label, incident_type, confidence, SinceWindowEndSeconds
| sort window_start asc
```

## Opening and closing incidents (proposal for M8-3)

The issue leaves this to M8-2 and M8-3. `incident.step(state, prediction) -> (state, changes)` is a pure function
that M8-3 calls on each reply's predictions, in `window_start` order, with the state it stored:

- **A minute counts** when it has an `incident_type` (detector and classifier agree).
- **Opens** on the first minute that counts; `started_at` is that minute.
- **Stays open** through minutes that count. Its `incident_type` is the type with the most counted minutes so
  far, a tie going to the type counted most recently, and its `confidence` is that type's latest. A cascade
  goes `DATABASE_THROTTLING`, then `CASCADING_FAILURE` once that has as many minutes, reported as
  `type_changed`. A type the classifier names only as a fault lets go doesn't replace the fault's (below).
- **Closes** after `CLOSE_AFTER` (2) minutes in a row that don't count, or when the next window comes more than 2
  minutes after the last minute that counted. Gaps happen: the window builder announces no window with an empty
  value, so idle traffic leaves holes. `ended_at` is the end of the last minute that counted.
- **Repeats and stragglers** (EventBridge keeps no order) are ignored: a window not newer than the last one applied
  changes nothing.
- `changes` lists `opened`, `type_changed` and `closed`. The first two are the moments
  [`DIAGNOSIS_CONTRACT.md`](DIAGNOSIS_CONTRACT.md)'s open question 3 proposes calling Bedrock on.

**On validation (measured 2026-11-06, PR #93's review).** `v1`'s 140 validation runs (slots 561–700) were rebuilt
as the live stream, as [`PIPELINE.md`](PIPELINE.md) measured them ("How it was measured"), in the order they ran
(slot 608 was re-run after slot 700). Their 1,400 complete windows went through this Lambda's code with the
promoted classifier and M5-2's forest, then through `incident.py` in window order:

- **Every fault run opened exactly one incident** (112 of 112), and none split in two. No incident opened on a
  NORMAL run, or anywhere away from a fault.
- **It opens in the fault's first minute or the next** (its `started_at` is within 50 s of the fault's start,
  the median by type within 14 s) and **ends 1–2 minutes after the fault does** (`ended_at` a median 25–92 s after the fault's end by
  type, at most 160 s): the classifier's recovery tail.
- **Its type at the close is the run's own in 112 of 112**, with 13 `type_changed`, all cascades becoming
  `CASCADING_FAILURE`: 125 Bedrock calls on the contract's proposal (one per `opened` and `type_changed`).
  Taking each minute's type instead, as this rule first did, closed 21 incidents with another type: the
  classifier often names one more minute as the fault lets go, and differently (13 of 21 dependency failures
  ended as `LAMBDA_DEGRADATION`, 7 of 14 cascades as another type). That made 36 `type_changed`, 23 of them
  after the fault had ended, each a Bedrock call explaining the wrong type.
- **Per window,** 502 of the 510 fault windows M4 labeled get their run's type. The other 8 are each their
  run's first labeled fault minute: 7 the classifier calls NORMAL, and slot 668's first cascade minute reads
  `DATABASE_THROTTLING` (PIPELINE.md's one changed classifier answer). Of 375 NORMAL-labeled windows, 2 get a
  type: both are fault-start minutes, with the fault active for under half the minute.

`CLOSE_AFTER` is the one setting. Raise it if live runs show incidents splitting.

**For M8-3.** Subscribe with a rule on `anomalypulse-pipeline` matching source `lambda`, detail-type
`Lambda Function Invocation Result - Success`, and `detail.requestContext.functionArn` (prefix
`arn:aws:lambda:us-east-2:<account>:function:anomalypulse-diagnosis`). The reply is under `detail.responsePayload`.
Copy `incident.py` into your Lambda, or import it. It has no dependencies.

## Deploying

The pipeline stack (`anomalypulse-pipeline`) must already be deployed: the rule and the destination name its bus.

```bash
cd AnomalyPulse
python -m pytest -q ml/diagnosis ml/job/test_detectors.py           # no AWS

# 1. Promote M5-2's forest as the detector (its packaged 1%-of-healthy threshold, -0.3695).
python -m ml.launch promote anomalypulse-train-iforest-20261031T224214Z --dry-run
python -m ml.launch promote anomalypulse-train-iforest-20261031T224214Z

# 2. Deploy (admin: it changes an IAM role). Do this after step 1, or every cold start fails reading
#    a pointer that isn't there.
cd infra/ml && sam build && AWS_PROFILE=admin sam deploy

# 3. A direct call: the reply now has "detector", and each prediction "incident_type". It writes no
#    metric line (only WindowReady events do).
aws lambda invoke --function-name anomalypulse-diagnosis --cli-binary-format raw-in-base64-out \
  --payload file://event.json reply.json --profile dev
```

**The role's event lock.** Lambda puts the destination events as the function's role, and the role may put only
`lambda` / `Lambda Function Invocation Result - Success` events: the pipeline's event locks
([`PIPELINE.md`](PIPELINE.md), "The event locks"), without their `'Null'` lines. When the destination is
created, Lambda checks the role's `PutEvents` with no source or detail-type, and those lines refused the check:
the first deploy (2026-11-06) failed with `CREATE_FAILED` on `DiagnosisFunctionEventInvokeConfig`, "The function
execution role does not have permissions to call PutEvents", and rolled back. The lines add nothing here:
EventBridge fails any entry without a `Source` and `DetailType`, so every event that lands carries both, and
`ForAllValues` holds each to the allowed value.

**Check after the first live window** that the destination events get through: the function's
`DestinationDeliveryFailures` metric (namespace `AWS/Lambda`) must stay at 0. If it doesn't, the lock's values
don't match what Lambda sends. Remove the `Condition` block in `diagnosis-destinations` and tell Jake, so the IAM
review records it.

## Definition of Done: how to check each item live

Start traffic at least 6 minutes before the fault (M10's note in [`PIPELINE.md`](PIPELINE.md)), so the first
windows aren't after-idle ones: pass `--baseline 360` (the default is 300). A cascade's baseline is its spec's
`baseline_s` (300 in `example.json`), so run it right after another run, whose recovery traffic covers the rest.
Then read the second Logs Insights query above for the run's minutes, and run `incident.py` over them in
`window_start` order.

1. **A live traffic spike is detected and classified end-to-end.**

   ```bash
   caffeinate -is python -m simulator start-incident --type traffic-spike --intensity 0.7 --band 6 8 --duration 300 --baseline 360
   ```

   Pass: the fault's minutes come back `incident_type: TRAFFIC_SPIKE`, with `SinceWindowEndSeconds` around 165–180 s.
2. **Full taxonomy on a live run of each injector.** One run each, a NORMAL run included. The bands are `v1`'s
   training bands, which its validation runs used too; the test bands stay unseen until M9-1:

   ```bash
   caffeinate -is python -m simulator start-incident --type noop --duration 300 --baseline 360
   caffeinate -is python -m simulator start-incident --type db-throttling --intensity 0.7 --band 3.5 4.5 --duration 300 --baseline 360
   caffeinate -is python -m simulator start-incident --type lambda-degradation --intensity 0.7 --band 800 1100 --duration 300 --target order-service --baseline 360
   caffeinate -is python -m simulator start-incident --type payment-failure --intensity 0.7 --band 500 1500 --duration 300 --baseline 360
   caffeinate -is python -m simulator start-cascade --spec simulator/cascades/example.json
   ```

   Pass: the `noop` run's minutes carry no `incident_type` and open no incident. Each fault run opens one
   incident, which closes as `DATABASE_THROTTLING`, `LAMBDA_DEGRADATION`, `DEPENDENCY_FAILURE` and
   `CASCADING_FAILURE` (the cascade's first minutes may read as its first fault's type).
3. **Live features confirmed Tier-1-only.** `python -m pytest -q ml/diagnosis` passes; the Lambda starts (the
   guard checked both models' columns); and no live call is refused as `forbidden columns` (the failure queue stays
   empty).

These are live runs against the stack, so they hold the experiment lock: one at a time, and not during M9-1.
