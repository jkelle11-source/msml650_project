# The Live Telemetry Pipeline (M8-1)

**Owner:** Jake · **Stack:** `anomalypulse-pipeline` ([`AnomalyPulse/infra/pipeline/`](../AnomalyPulse/infra/pipeline/))
· **Used by:** M8-2 (the windows the Diagnosis Lambda scores), M8-3 (the Incidents table), the dashboard read API
(`health`, M8-5), M9 (SQS backlog, live detection latency)

How a service's telemetry reaches the models while the system runs: each minute becomes a window built the way
M4 built the training set, a few minutes after the requests it describes. This document holds the design
decisions, what the pipeline writes and where, what happens when telemetry is late, the load tests, the IAM at
each hop, and what live windows do differently from training's.

---

## The path (decided 2026-11-01, built 2026-11-02 to 11-05)

```text
service Lambda  print(json)
  → CloudWatch Logs  /aws/lambda/anomalypulse-<svc>
  → second subscription filter  anomalypulse-pipeline  { $.request_id = "*" }   (the shipper's is the first)
  → forwarder Lambda → EventBridge bus anomalypulse-pipeline → SQS queue anomalypulse-telemetry
  → Aggregator Lambda → pipeline/records/ in the telemetry bucket
  → window builder Lambda, every minute → pipeline/health/, pipeline/windows/, the Incidents table's health item
  → a WindowReady event on the bus → M8-2's rule → Diagnosis Lambda
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
- **Every hop delivers at least once,** so the window builder drops duplicates by `(service, request_id)`, as the
  M4 export does. (In the load tests below, no record arrived twice.)
- **M8-2 listens for WindowReady events** on the bus rather than being called by the window builder: it adds a
  rule, and no M8-1 code changes.

## The parts

| Part | Name | What it does |
|---|---|---|
| Forwarder Lambda | `anomalypulse-pipeline-forwarder` | Turns each batch CloudWatch Logs sends into bus events of up to 100 records, 10 events per PutEvents call, and fails the batch if any event is refused. Lambda retries a failed batch twice, then puts it in the undelivered queue |
| Bus and rule | `anomalypulse-pipeline`, `anomalypulse-telemetry-to-queue` | Routes the forwarder's events to the queue. The rule retries delivery for 1 hour (not the default 24), then puts the event in the undelivered queue |
| Telemetry queue | `anomalypulse-telemetry` | Holds each event until the Aggregator has filed it: 4 days at most. A message the Aggregator fails comes back after 185 s (6 × its 30 s timeout, plus its 5 s batching window); after 5 failures it goes to `anomalypulse-telemetry-dlq`, kept 14 days |
| Undelivered queue | `anomalypulse-telemetry-undelivered` | Telemetry that never reached the telemetry queue, kept 14 days |
| Aggregator Lambda | `anomalypulse-pipeline-aggregator` | Reads up to 100 messages at a time, waiting up to 5 s to fill a batch, at most 5 at once. Writes one file per minute per run, records unchanged and not deduplicated, and reports only the messages it failed |
| Window builder Lambda | `anomalypulse-pipeline-window-builder` | Runs every minute (at about :45, which matters: see "When a minute's window is ready"), one run at a time, never retried: the next run is the retry. Rebuilds the health of the last 3 minutes, newest first, and builds each of the last 10 minutes' windows once that minute's 4 snapshots are in |
| Incidents table | `anomalypulse-incidents` (SSM `/anomalypulse/tables/incidents/name`) | The newest minute's health for the dashboard, and the incidents M8-2 and M8-3 write |

## What it writes

| Where | Written by | What |
|---|---|---|
| `pipeline/records/dt=YYYY-MM-DD/minute=HH-MM/part-<run id>.jsonl` | Aggregator | The minute's records, one JSON record per line, exactly as the services printed them. A minute is several files, and a record can repeat |
| `pipeline/health/dt=YYYY-MM-DD/minute=HH-MM.json` | Window builder | The minute's health (below). Rewritten by each of the next two runs, so records still on their way are counted |
| `pipeline/windows/dt=YYYY-MM-DD/minute=HH-MM.json` | Window builder | `{"window_start", <the 106 columns>}`. Written once: the file marks the minute done |
| Incidents table, `pk = "HEALTH"`, `sk = "LATEST"` | Window builder | The newest minute's health, and `telemetry_late`: whether the late alarm was on at the run. What the dashboard read API reads |
| Bus event, `Source` `anomalypulse.pipeline`, `DetailType` `WindowReady` | Window builder | Its `detail` is `{"windows": [<the window>], "key": "<its file>"}`, the Diagnosis Lambda's input shape. A rule's Lambda target receives the whole event, with that under `detail`. M8-2's rule passes the whole event on, with no `InputPath`, so the Diagnosis Lambda also gets the event's `time`, when the window was announced, for its latency ([`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md)). Only for a window with no empty value, and sent before its file is written, so a failed send is retried by the next run. A failed file write after a sent event means the next run sends it again. M8-2 must accept a repeat, and windows out of order: after a gap, one run announces several, and EventBridge keeps no order |

**Health** is the input `AnomalyPulse/dashboard/builder.py` takes, 14 keys: `window_start`,
`system_p95_latency_ms`, and for `product`, `cart`, `order` and `payment` the features' own `_request_rate` (per
second), `_error_rate_5xx` and `_p95_latency_ms`. A service with no requests has `0.0`, `null`, `null`. The system
p95 is the 95th percentile of Product's, Cart's and Order's requests themselves (Payment is reached only through
Order). On a busy minute, health's columns equal the full window's.

**The health item, for M8-5.** Its numbers come back from DynamoDB as `Decimal`s, which `build_dashboard` takes
as they are (checked on the live item, 2026-11-05) but `json.dumps` can't write: the live source converts them
before replying. A write never replaces a newer minute, and the window builder's role can write no item whose
`pk` isn't `HEALTH`. Beside the 14 keys, the item holds `telemetry_late` (true or false), and `build_dashboard`
answers `NO_DATA` while it is true ("When telemetry is late"). The minute's health file leaves it out: it is the
pipeline's state at the run, not the minute's.

**The Incidents table, for M8-2, M8-3 and M8-5.** Keys `pk` and `sk`, both strings, so each writer lays out its own
items; the `HEALTH` partition is the window builder's. How incidents are laid out is M8-2's and M8-3's call. One
layout that works: `pk = "INCIDENT"`, `sk = started_at`. UTC ISO times sort in time order, so a Query on
`INCIDENT`, newest first with a limit of 1, returns the latest incident. A date in `pk` would leave the newest
incident out of the query just after midnight. The table is on-demand and kept if the stack is deleted or the
table replaced.

## When a minute's window is ready (decided 2026-11-01, measured 2026-11-04 and 05)

A window named T covers [T, T + 1 min). It is built in two stages:

| Stage | Ready | Holds | Read by |
|---|---|---|---|
| Health | About T + 1¾ min, rebuilt at T + 2¾ and T + 3¾ | Requests, 5xx rate and p95 latency for each service, and the system p95, from the request records alone | The dashboard's `health` |
| Full window | About T + 3¾ min, once the minute's snapshots are in | All 106 columns of `features.build_matrix`, CloudWatch columns included | The models (M8-2) |

The Diagnosis Lambda refuses a window with any empty value, and a minute's CloudWatch columns exist only once
the metrics extractor writes its snapshot, which waits 2 minutes for CloudWatch to settle
(`METRICS_LAG_MINUTES`). Holding the dashboard's numbers back for it too would trip the contract's `NO_DATA`
rule, which fires when the latest window is more than 3 minutes old. So a live detection comes about 2¾ minutes
after the end of the first flagged window, plus the Diagnosis Lambda's own time: later than the offline detection
latency in [`DETECTION.md`](DETECTION.md), which stops the clock at that window's end.

**Health stays inside the dashboard's 3 minutes.** Just before a run, the newest health is about 2¾ minutes old,
and the run writes the next one within 1 s when idle and 3.3 s at 10× traffic (measured: the health item was at
most 167 s old). A run that fails leaves no newer minute, so the dashboard shows `NO_DATA` for about 45 s until
the next run: correct, since the pipeline has stopped for that long.

**Both depend on the second the runs start at, and a new rule picks a new one.** EventBridge counts a
`rate(1 minute)` rule's minutes from when the rule was created, so the runs start at about :45 only because
that is when this rule was created. A run starting at second s has two deadlines:

- **The newest health must be written within 60 − s seconds,** before the one before it turns 3 minutes old. At
  :45 that leaves 15 s, against 3.3 s at 10× traffic. Past about :55, the dashboard would show `NO_DATA` for a
  moment every minute.
- **The newest minute's last records must have landed when the run reads them.** They can wait 13 s in the queue
  alone. Before about :20, the health item would miss them: the next two runs count them in the minute's health
  file, but the item has moved on to the next minute by then.

So the runs should start between about :20 and :50. Any deploy that creates the rule draws a new second: a new
stack, as M10 plans, or a template change that replaces the rule. After one, read the runs' start times (macOS
`date`; the last 10 minutes):

```bash
AWS_PROFILE=dev AWS_REGION=us-east-2 aws logs filter-log-events \
  --log-group-name /aws/lambda/anomalypulse-pipeline-window-builder \
  --filter-pattern '"START RequestId"' --start-time $(( ($(date +%s) - 600) * 1000 )) \
  --query 'events[].timestamp' --output text | tr '\t' '\n' | while read t; do date -u -r $((t / 1000)) +%H:%M:%S; done
# 22:37:45, 22:38:45, ... (2026-11-05)
```

Outside :20–:50, rename the window builder's schedule event (`EveryMinute` in the template) and deploy again:
CloudFormation replaces the rule, which draws a new second.

## When telemetry is late (measured 2026-11-05)

**The window builder assumes a minute's records are in about 3¾ minutes after it starts,** when it rebuilds that
minute's health for the last time and builds its window: a minute's last records have about 165 s. Normally the
oldest message in the queue has waited at most 13 s (12 s at 10× traffic). Records are late only when the
Aggregator falls behind or fails: a message it fails waits out the queue's 185 s before it is tried again.

**What late records do** (the pause test below):

- **Health undercounts, for good.** The minutes whose records came after their last rebuild say 0 requests in
  the test. The dashboard shows them as healthy and idle until the late alarm goes off, then `NO_DATA` until it
  clears (below).
- **Windows are built without them, for good.** A window with no records has empty columns and isn't announced.
  A window with some of its records can have no empty value, and is then announced with wrong numbers.

**The signal is the alarm `anomalypulse-telemetry-late`:** the queue's oldest message has waited over 120 s. In the
test it went off about 2¼ minutes after the age passed 120 s, since SQS reports to CloudWatch a minute or two
late. While it is in alarm, and for the minutes just before it, treat windows, announcements and health as
possibly missing records.

**Decided (2026-11-05, with Linu): the dashboard shows `NO_DATA` while the alarm is on.** Before, the console
couldn't tell idle from stuck. Now each run reads the alarm's state and puts it in the health item as
`telemetry_late`, and `build_dashboard` treats a late minute as it treats a stale one, since its numbers may be
missing records. The console needed no change: the reply's shape is the same, and `NO_DATA` already means
"don't trust these numbers". Checked live (2026-11-06 00:36): with the alarm forced into `ALARM`, a run wrote
`telemetry_late = true` and `build_dashboard` answered `NO_DATA` for the item; once the alarm cleared by itself,
72 s later, the next run wrote `false`.

The alarm runs behind the queue at both ends. Over the pause test's times, the paused minutes still show as idle
from the 19:36:45 run to the 19:39:45 one, the first after the alarm went off at 19:39:23. `NO_DATA` then lasts
until the 19:45:45 run, about 3 minutes past the drain, since the alarm clears late too. A status of its own
would have told the viewer more, at the cost of a console change, for a case no load test hit at normal or 10×
traffic.

**Not done:** holding windows back until their records are in. An SQS standard queue keeps no order and its age
metric arrives late, so the window builder has no exact way to know.

## Alarms

None has an action: each one's state shows in the CloudWatch console. The late alarm's state also reaches the
dashboard, as `NO_DATA`.

| Alarm | In alarm while |
|---|---|
| `anomalypulse-telemetry-undelivered` | Anything is in the undelivered queue: telemetry lost before the telemetry queue |
| `anomalypulse-telemetry-dlq` | Anything is in `anomalypulse-telemetry-dlq`: the Aggregator failed it 5 times |
| `anomalypulse-telemetry-late` | The telemetry queue's oldest message has waited over 120 s (above). The dashboard shows `NO_DATA` meanwhile |
| `anomalypulse-pipeline-window-builder-errors` | The window builder has failed 3 runs in a row: windows and health have stopped |

## Load tests (2026-11-04 and 05)

**How.** The simulator's own baseline traffic (`simulator.workload`, the plan's endpoint mix) from a script, with no
experiment record and the experiment lock checked free. The shipper's separate copy in `raw/` (the same log
groups, its own subscription filter) is the reference: every record in it must be in `pipeline/records/`,
matched by `(service, request_id)`, minute by minute. The client's own count can't be: an order leaves an Order and
a Payment record, and a request refused before a service runs leaves none. Health was recomputed from `raw/`,
and the windows compared with one `build_matrix` over the whole run.

| Run | Traffic | Records | Result |
|---|---|---|---|
| Normal (2026-11-04 22:40) | 5 min at 20 rps | 6,191 | None missing, extra or repeated. Health and windows match; 5 complete windows, 5 announced |
| 10× burst (2026-11-05 18:41) | 3 min at 20 rps, 3 min at 198.6 rps, 3 min at 20 rps: 43,077 requests | 45,256 | None missing, extra or repeated. Health and windows match; 9 complete windows, 9 announced, none refused |
| Aggregator paused (2026-11-05 19:32) | 3 min at 20 rps, with the Aggregator's queue reader off for 8½ min | 3,797 | None missing, extra or repeated; every message received once. The paused minutes' health and windows are wrong, as above |

**At 10× traffic**, about 170 messages a minute reached the queue, against 40 at 20 rps: each message carried
more records. None ever waited to be read; at most 22 were being processed at once, and the oldest had waited 12 s. The Aggregator ran
38 to 44 times a minute, 0.11 s each, at most 3 of its 5 at once. The window builder's longest run took 10.6 s of
its 60 s. No Lambda, the services' included, had an error or a throttle, and the account peaked at 18 concurrent
executions of its 1,000. The queue's drain has a wide margin, so M9-1's spikes, up to 12×, can run with the
pipeline alongside.

**With the Aggregator paused**, the queue held 148 messages, the oldest waiting up to 397 s, and the late alarm
went off. Once the reader was back on, the backlog drained in about 45 s, in 4 Aggregator runs: the queue absorbed
the backlog without losing a record.

## IAM at each hop (checked 2026-11-05)

Each Lambda's role allows what its code calls, on the resources it uses, and nothing else, and each Lambda can
be invoked only by its one caller. Every role trusts only Lambda, has no AWS managed policy, and logs only to its
own log group. None uses `AWSLambdaBasicExecutionRole`, which may write to any log group, the services' included,
and those feed `raw/` and this pipeline.

| Lambda | Its role may | Invoked by |
|---|---|---|
| Forwarder | `PutEvents` on the pipeline bus, for `anomalypulse.telemetry` / `TelemetryRecords` events only. `SendMessage` to the undelivered queue, its on-failure destination | CloudWatch Logs, for the 4 service log groups only, each named, in this account |
| Aggregator | `ReceiveMessage`, `DeleteMessage` and `GetQueueAttributes` on the telemetry queue: Lambda's queue reader uses the Aggregator's role. `PutObject` under `pipeline/records/` | Lambda's queue reader. No resource policy is needed |
| Window builder | `ListBucket` on the bucket. `GetObject` under `pipeline/records/`, `pipeline/windows/` and `metrics/`. `PutObject` under `pipeline/health/` and `pipeline/windows/`. `PutItem` on the Incidents table, for `pk = "HEALTH"` only. `PutEvents` on the pipeline bus, for `anomalypulse.pipeline` / `WindowReady` events only. `DescribeAlarms` on the late alarm only | Its schedule rule only |

The policies of the telemetry queue and the undelivered queue (the rule's dead-letter queue) let EventBridge send
to them only for the telemetry rule (`aws:SourceArn`), so no other rule, in this account or another, can fill
them. They don't stop a role in this account that has its own `sqs:SendMessage` grant: of this stack's roles,
only the forwarder's has one, for the undelivered queue, its on-failure destination. The Aggregator's DLQ has no
policy: SQS moves failed messages into it itself. The bus has no resource policy, since within one account the
role policies decide.

**The event locks.** One `PutEvents` call carries up to 10 entries, so EventBridge checks `events:source` and
`events:detail-type` as lists, with one value per entry:

- `ForAllValues:StringEquals` makes every entry match, so one wrong entry refuses the whole call.
- `Null: false` requires both keys, because `ForAllValues` is true of an empty list.

With the locks, the forwarder can't put a WindowReady event, so it can't make the Diagnosis Lambda score a
made-up window. The window builder can't put telemetry. A new event type on this bus needs its sender's
condition widened.

**The health item.** The same pattern, on `dynamodb:LeadingKeys`: the window builder can write the `HEALTH`
item, and no incident. The writers of incidents (M8-2, M8-3) get their own roles, with their own `pk`.

**Accepted: `ListBucket` on the whole bucket.** Without it, S3 answers a missing key with 403 instead of 404, and
the window builder reads only a 404 as "not there yet". It lists key names, never contents:

- The names show experiment IDs, and that each experiment has a labels file under `datasets/v1/tier2/labels/`.
  They don't show what any label says.
- The role can't read `raw/`, `experiments/` or `datasets/`.
- Narrowing it would mean replacing the reads of windows and snapshots with listings, which takes more S3 calls.

**How it was checked:**

- **Access Analyzer** (`validate-policy`): no findings on the 11 role policies, the 3 trust policies or the 2 queue
  policies, as deployed.
- **IAM's policy simulator**, on the deployed roles. It asked 46 questions: every action the code calls must be
  allowed, and each neighbouring action must be denied. The denied actions it checked:
  - the Aggregator writing `raw/` or `pipeline/health/`;
  - the window builder reading `raw/`, `experiments/` or the label files;
  - the window builder writing `raw/`, `metrics/` or `experiments/`, deleting a window, or writing an `INCIDENT`
    item or an item with no key;
  - either sender putting the other's events, or a call mixing one good entry with a bad one.

  Before the event locks, 2 of the then 41 checks failed: the forwarder could put pipeline events, and the window
  builder telemetry events. After the locks, 46 of 46 passed.
- **IAM's last-accessed report:** each role has used every service it is granted: events, logs and sqs for the
  forwarder; logs, s3 and sqs for the Aggregator; dynamodb, events, logs and s3 for the window builder.
- **Live,** after the locks (2026-11-05 21:37, 3 minutes at 20 rps):
  - neither Lambda had an error or an `AccessDenied`;
  - every record reached `pipeline/records/`, equal to `raw/`;
  - the complete windows were announced;
  - the undelivered queue and the DLQ stayed empty.

The simulator sends no request. So the allowed cases were also seen working live. The denied cases can only be
checked in the simulator, since only Lambda can assume these roles.

**The late-alarm read (added 2026-11-06), which the simulator can't check.** Asked about `DescribeAlarms` on the
late alarm and on the DLQ alarm, it gave one answer, not two: `implicitDeny` on a placeholder ARN ending
`${ResourceType}/${ResourcePath}`, neither of the two it was given. It never matched them; alarm ARNs end
`:alarm:<name>`, with a colon. So the grant is checked by what it does instead:

- **Allowed, seen live.** The role's only CloudWatch grant names the late alarm's ARN, and every run since the
  deploy has read that alarm; in the live check one read `ALARM`. So `DescribeAlarms` is checked against the named
  alarm's ARN.
- **Every other alarm refused.** No policy of the role names another alarm, and it has no managed policies, so IAM
  refuses the rest by default.

**After M9-1, outside this stack:** the shipper (`anomalypulse-shared`, frozen until M9-1) has two broad grants:

- it may be invoked from any `/aws/lambda/anomalypulse-*` log group;
- it uses `AWSLambdaBasicExecutionRole`.

Narrow both: name the 4 service log groups, and let it log only to its own.

## Live windows and training windows (measured 2026-11-01)

The window builder builds each window with `build_matrix`, as M4 did, with two differences the live path forces:

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
- **M10:** send the traffic from the runner host, never a laptop ([`DETECTION.md`](DETECTION.md), "Where live
  traffic comes from"). Start it at least 6 minutes before the healthy segment, and allow about 6 minutes after an
  injected fault ends before calling the system healthy again: the minutes above, plus the almost 3 minutes
  after a minute ends before its full window is ready.
