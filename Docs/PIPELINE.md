# The Live Telemetry Pipeline (M8-1)

**Owner:** Jake · **Stack:** `anomalypulse-pipeline` (being built in M8-1) · **Used by:** M8-2 (the windows the
Diagnosis Lambda scores), the dashboard read API (`health`, M8-5), M9 (SQS backlog, live detection latency)

How a service's telemetry reaches the models while the system runs: each minute becomes a window built the way
M4 built the training set, a few minutes after the requests it describes. This document holds the design
decisions and what live windows do differently from training's. The stack, its IAM and the burst test are added
as M8-1 builds them.

---

## The path (decided 2026-11-01)

```text
service Lambda  print(json)
  → CloudWatch Logs  /aws/lambda/anomalypulse-<svc>
  → second subscription filter  { $.request_id = "*" }        (the shipper's is the first)
  → forwarder Lambda → EventBridge bus → SQS telemetry queue → Aggregator
  → pipeline/ in the telemetry bucket, and the Incidents table
  → a "window ready" event on the bus → M8-2's rule → Diagnosis Lambda
```

- **Beside the shipper, never instead of it.** A log group allows two subscription filters and the shipper uses
  one, so the second gives the pipeline its own copy of every telemetry line. `anomalypulse-shared` isn't
  deployed and `raw/` still fills as it did for training, so M9-1's test set reaches S3 the same way (README,
  "Status"). The pipeline writes only under `pipeline/` and to its own Incidents table.
- **Every record passes through SQS.** A burst waits in the queue instead of being lost, and the queue's backlog
  is the §16 number. A once-a-minute schedule reading `raw/` was rejected: SQS would carry one message a minute,
  and `raw/` keys carry no minute, so each run would list the whole day's files.
- **A filter can't send to EventBridge or SQS** (only to Lambda, Kinesis, Firehose or OpenSearch), hence the
  forwarder.
- **Every hop delivers at least once,** so the Aggregator drops duplicates by `(service, request_id)`, as the M4
  export does.
- **M8-2 listens for "window ready" events** on the bus rather than being called by the Aggregator: it adds a
  rule, and no M8-1 code changes.

## When a minute's window is ready (decided 2026-11-01)

A window named T covers [T, T + 1 min). It is built in two stages:

| Stage | Ready | Holds | Read by |
|---|---|---|---|
| Health summary | Just after the minute ends (about T + 1 min) | Requests, 5xx rate and p95 latency, for the system and each service, from the request records alone | The dashboard's `health` |
| Full window | About 2 minutes after it ends (T + 3 min) | All 106 columns of `features.build_matrix`, CloudWatch columns included | The models (M8-2) |

The Diagnosis Lambda refuses a window with any empty value, and a minute's CloudWatch columns exist only once
the metrics extractor writes its snapshot, which waits 2 minutes for CloudWatch to settle
(`METRICS_LAG_MINUTES`). Holding the dashboard's numbers back for it too would trip the contract's `NO_DATA`
rule, which fires when the latest window is more than 3 minutes old. So a live detection comes about 2 minutes
after the end of the first flagged window: later than the offline detection latency in
[`DETECTION.md`](DETECTION.md), which stops the clock at that window's end.

## Live windows and training windows (measured 2026-11-01)

The Aggregator builds each window with `build_matrix`, as M4 did, with two differences the live path forces:

- **No labels,** so the temporal columns (`_roll_mean_3`, `_roll_std_3`, `_change`) look back over every earlier
  minute. Training's looked back over the run's labeled windows and its startup minute only
  ([`FEATURE_MATRIX.md`](FEATURE_MATRIX.md), "The matrix").
- **No run edges,** so a run's first minutes look back into whatever came before: the previous run's last
  minutes, or nothing after idle time.

The live path also scores every minute, including those training never scored: each run's partial first and
last minutes, its startup minute, onset and recovery.

**How it was measured.** `v1`'s 140 validation runs (slots 561–700) were rebuilt as one live stream. Each run was
built from its own records plus the previous run's when that ran right before it (all but slots 561 and 609,
which followed idle time), with no bounds and no lookback list. The windows were scored with the promoted
logistic regression (pointer v2) and M5-2's Isolation Forest (job
`anomalypulse-train-iforest-20261031T224214Z`, at its packaged threshold). Rebuilt training-style, the same
inputs reproduced the stored features exactly (1,260 windows, 0 cells differ), so every difference below is
the live path's.

### On the windows training scored

- **Only temporal columns change, in 210 of 885 labeled windows, of two kinds.** Each run's first labeled
  (NORMAL) minute (140): its look-back now includes the partial minute before the startup minute. And the
  minutes after DB throttling's surge (70): the minutes M4-4 excludes as `db_throttling_onset` now feed the next
  minutes' rolling columns, as `FEATURE_MATRIX.md` says they would. The other 675, all with labeled minutes or
  the startup minute before them, are identical.
- **No live window has an empty cell,** so the Diagnosis Lambda refuses none.
- **The models' answers change on 2 windows:**

| | Training-style | Live-style |
|---|---|---|
| Classifier: macro F1 | 0.9923 | 0.9899 |
| Classifier: windows wrong | 9 | 10 |
| Detector: recall | 510/510 | 510/510 |
| Detector: healthy windows flagged | 4/336 | 5/336 |
| Detector: fault-start windows flagged | 38/39 | 38/39 |

Slot 668, a cascade's first fault minute right after two surge minutes: `CASCADING_FAILURE` becomes
`DATABASE_THROTTLING`. Slot 561, the batch's first run, after idle time: its first NORMAL minute is flagged
(slot 609, also after idle time, is not).

**Decided (2026-11-01): `build_matrix` runs unchanged on the live path.** One window each way is small and
explained. A rule skipping the minute after idle time would remove only the second, and matching training more
closely would take the labels or the manifest, which must never shape the models' input.

### On the minutes only the live path scores

The detector was fit on NORMAL windows whose look-back held only healthy minutes, so it flags a minute whose
look-back still holds a fault.

- **After a fault ends, the detector keeps flagging for about 3 minutes.** It flagged every recovery minute
  (168 of 168), and 107 of the 110 minutes shared with the next run after an incident run, against 0 of 28 after
  a NORMAL run. Given look-back columns from its own run instead, that shared minute was flagged 4 times in 138.
  The minute after it (the next run's startup minute) was flagged once in 138. The classifier lets go sooner: it
  names an incident in 78 of 112 first recovery minutes, 24 of 56 second ones, and 2 of 138 shared minutes.
- **After idle time, the first full minute of traffic is flagged** (2 of 2 startup minutes, against 1 of 138
  back to back), and the next one in 1 of 2 (slot 561, above). The classifier calls both startup minutes NORMAL.
  The partial minute before them has empty temporal columns, with nothing to look back over, so it can't be
  scored: the Diagnosis Lambda would refuse it.

**What this means for the others:**

- **M8-2 and M8-3:** to the detector, a live incident lasts about 3 minutes longer than its fault; to the
  classifier, 1–2 minutes longer. How flagged minutes open and close an incident is their call. On these
  numbers, requiring the classifier to name an incident, not only the detector to flag, removes most of the tail
  and both after-idle flags.
- **M9:** recovery and startup minutes are not labeled windows, so test-set metrics built from M4's features
  never score them. A metric taken from live windows would.
- **M10:** start the traffic at least 6 minutes before the healthy segment, and allow about 6 minutes after an
  injected fault ends before calling the system healthy again: the minutes above, plus the 2 minutes before a
  full window is ready.
