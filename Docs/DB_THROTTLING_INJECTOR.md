# DynamoDB Throttling Injector (M3-3)

**Owner:** Linu · **File:** `AnomalyPulse/simulator/injectors/db_throttling.py` · **Tests:** `AnomalyPulse/simulator/test_db_throttling.py`
**Registry name:** `db-throttling` · **Label:** `DATABASE_THROTTLING` · **Band parameter:** `load_ratio` · **Target:** `product-service`

## What it does

It produces **real** DynamoDB throttling on the Products table. The table sits at a **fixed, low provisioned
read capacity**. For the length of the fault window the injector reads the table directly, at

```
reads per second  =  load_ratio  x  provisioned RCU
```

on top of whatever the normal workload is sending. The single knob is the **offered-load-to-capacity ratio**
(`load_ratio`), taken from a band so that M4 can keep training and test bands apart (training 1.5-2.5 and 3.5-4.5,
test 2.5-3.5 and 4.5-5.5). The throttle rate is **not** an input: it is whatever DynamoDB does, and it is measured afterwards.

The ratio counts the injector's own reads. The application's reads come on top: about 25 RCU/s at the 20 rps
baseline, so about +0.05 on the ratio at 500 RCU. In a cascade, a traffic spike adds more (about +0.5 at 10×).

## How it works

| Step | What happens |
|---|---|
| `configure()` | Pure. Resolves `intensity` inside the band into `{"load_ratio": x}`. No AWS calls, no clock, same seed gives the same value. |
| `start()` | Looks up the Products table name in SSM (`/anomalypulse/tables/products/name`), reads its provisioned read capacity (RCU) with `describe_table`, and **refuses to run if the table is on-demand** (it cannot be throttled at a low ceiling). Reads the item keys with one `Scan`, then starts background reader threads. |
| the load | Strongly consistent `BatchGetItem` calls of up to 25 keys (every product). Each item is under 4 KB, so each key costs exactly 1 RCU, and reads per second equals RCU per second. 2,000 reads/s is 80 calls a second, which one client keeps up with easily; one `GetItem` per read would need 2,000. Threads: one per 10 calls a second (at most 32), each paced against a schedule, so the rate does not sag by the time each call takes. A thread more than a second behind skips ahead rather than bursting. |
| throttles | A batch DynamoDB could partly serve returns the unread keys in `UnprocessedKeys`; one it could not serve at all raises `ProvisionedThroughputExceededException`. Both count as throttled reads. **Neither is retried**, and the client has retries off (`total_max_attempts=1`): a retry would hide the throttle and change the offered load. |
| `stop()` | Sets the stop event and joins the threads. Safe before `start()`, after a failed `start()`, and when called twice. Returns `None`. |
| `observe()` | Runs once after the recovery period. Returns the client's counts and the table's CloudWatch counts for the window (see below). |

## What is recorded

`params` (what was asked for) and `observed` (what was measured) are separate in the manifest.

```json
"params":   { "load_ratio": 2.3 },
"observed": {
  "table": "anomalypulse-products",
  "provisioned_rcu": 500,
  "target_reads_per_s": 1150.0,
  "achieved_reads_per_s": 1149.6,
  "reads_attempted": 344875,
  "reads_throttled": 61200,
  "read_errors": 0,
  "client_throttle_rate": 0.1775,
  "first_throttle_at": "2026-10-16T14:23:41.512Z",
  "cw_throttled_requests": 412.0,
  "cw_throttled_requests_by_operation": { "BatchGetItem": 3.0, "GetItem": 61.0, "Scan": 348.0 },
  "cw_read_throttle_events": 61612.0,
  "cw_consumed_read_capacity": 151000.0
}
```

(Illustrative numbers.)

- `target_reads_per_s`, `achieved_reads_per_s`: what the ratio asked for and what the client really sent. A gap means
  the client could not keep up; check it before trusting a run's ratio.
- `reads_attempted`, `reads_throttled`, `read_errors`, `client_throttle_rate`: the injector's own reads. An error is a
  failure that was not a throttle (no answer, a validation error).
- `first_throttle_at`: when DynamoDB first refused one of the injector's reads. Burst capacity can hold this off for
  minutes (below).
- `cw_throttled_requests`: CloudWatch `ThrottledRequests`, summed over **every operation** on the table for the window's
  minutes. CloudWatch publishes it only per operation; `_by_operation` keeps the split. The application's
  `GetItem` (`GET /products/{id}`) and `Scan` (`GET /products`) appear here. A `BatchGetItem` counts only when *all*
  of its keys were throttled.
- `cw_read_throttle_events`: CloudWatch `ReadThrottleEvents`, which counts each throttled key, table-wide.
- `cw_consumed_read_capacity`: CloudWatch `ConsumedReadCapacityUnits` for the same minutes.
- The CloudWatch figures cover whole minutes and include normal traffic, which is the point: this is what a monitor would see.

**Feature/Label Boundary.** All of this lives in the manifest only (Tier-2). The injector never writes a `db_throttled`
flag, never logs the fault, and puts nothing in telemetry. The observable signals are CloudWatch **counts**, which the
M2 metrics extractor collects on its own each minute. Its `throttled_requests` is summed over operations the same way
(it used to ask for `TableName` alone, which CloudWatch answers with nothing).

## Capacity (decision for Jake and Linu)

The Products table must sit at a **fixed** provisioned capacity. DynamoDB allows only 4 switches to on-demand and
about 27 capacity decreases per day, and M4 needs 140+ throttling runs, so the capacity can't change per experiment.
It is a `template.yaml` change that Jake deploys. Two constraints set the number, and they pull in opposite directions.

**What the application reads** (measured at 20 rps, run `exp-20261015T000130Z-85b4`, from telemetry's `db_consumed_capacity`):

| | RCU |
|---|---|
| `GET /products` (a full-table `Scan`) | 2.0 per request |
| `GET /products/{id}` | 0.5 per request |
| Baseline, 20 rps | mean 25/s, p99 44/s |
| 10× traffic spike, 200 rps | mean 254/s, max 333/s |

1. **A traffic spike must not throttle by itself**, or `TRAFFIC_SPIKE` runs carry throttling symptoms. The test set's
   spikes reach 10×, run for up to 12 minutes, and use a shifted endpoint mix. With a margin of 1.5× over the 10× mean,
   that needs about 400 RCU. At the previously proposed 50 RCU, every spike of 2× or more would throttle.
2. **The top load ratio must be drivable.** 5.5× (test band) at 400 RCU is 2,200 reads/s; the stress slice (above 6×)
   is more. With `BatchGetItem` that is under 100 calls a second, which is no longer a limit.

**Recommendation: `BillingMode: PROVISIONED`, 500 RCU, 5 WCU.** 500 leaves the 10× spike at about half the capacity
and room for the test set's shifted mix and longer spikes. The app writes nothing to Products, so WCU only matters
for re-seeding.

| Capacity (with 5 WCU) | 10× spike mean | 5.5× load | Standing cost |
|---|---|---|---|
| 50 RCU | 508% (throttles) | 275 reads/s | ~$0.23/day |
| 400 RCU | 64% | 2,200 reads/s | ~$1.33/day |
| **500 RCU** | **51%** | **2,750 reads/s** | **~$1.64/day** |

Prices: $0.00013 per RCU-hour and $0.00065 per WCU-hour (us-east-2, standard table class; re-check the pricing
page). Reads up to the provisioned capacity cost nothing extra, and throttled reads are not billed, so a throttling
run itself is free. The standing capacity is the whole cost: about $49 a month at 500 RCU, so go back to on-demand
after M9 (below).

Alternatives considered: making `GET /products` cheaper or changing the endpoint mix (changes the application and
plan §7); accepting throttling in strong spikes (mislabels them); throttling a different table (departs from the
plan's "Products").

## Known risk: burst capacity

DynamoDB banks up to **300 seconds** of unused capacity as burst, on a best-effort basis. Offered load above capacity
only throttles once that bank is used up. With a full bank, at ratio `r` that takes `300 / (r - 1)` seconds,
**whatever the capacity is**:

| Ratio | 1.5× | 2.0× | 2.5× | 3.5× | 4.5× | 5.5× |
|---|---|---|---|---|---|---|
| Throttling starts after | 600 s | 300 s | 200 s | 120 s | 86 s | 67 s |

At 500 RCU the 20 rps baseline leaves 475 RCU/s unused, so the bank refills in about 5 minutes and nearly every run
starts full. **In a 5-minute window, ratios below 2.0 would never throttle**, and the rest of the lower training band
would throttle only near the end. Those windows would still be labeled `DATABASE_THROTTLING`. The bank is also drawn
down by whatever ran before, so back-to-back runs may differ.

This must be measured on the real table before M4 relies on it: `observed.first_throttle_at` records the onset.
If it holds, the options are a longer window for throttling runs, a drain at the start of the window, or accepting
it, since the throttle rate is an emergent outcome by design.

## How the application sees it

Product's DynamoDB client sets no retry configuration. With boto3's default (legacy) retry mode, DynamoDB calls are
tried up to 10 times with exponential backoff from 50 ms, about 25 s at worst, and Product's Lambda timeout is 10 s.
Under heavy throttling some Product requests may therefore time out instead of answering 500, and **a timed-out
request leaves no telemetry record**. Check this in validation: compare the client's 5xx count with Product's 5xx
records.

## Teardown

- **Per run:** `stop()` ends the reader threads. The injector changes **nothing persistent** (no capacity change, no
  fault-config row, no items written), so there is nothing to restore after a run. If the simulator crashes, the
  threads die with the process and the load simply stops. The framework also calls `stop()` on any failure or Ctrl-C.
- **After the project (idle cost):** the only thing that lingers is the provisioned capacity. To go back to on-demand,
  set `BillingMode: PAY_PER_REQUEST` on `ProductsTable` in `template.yaml` and redeploy (billing-mode switches are
  limited per 24 hours). To retire the injector, also remove the `simulator-db-throttling` policy from `SimulatorRole`.

## Permissions

`SimulatorRole` in `template.yaml` gets the policy `simulator-db-throttling`: `dynamodb:DescribeTable`, `Scan` and
`BatchGetItem` on the Products table, and `cloudwatch:GetMetricStatistics` and `ListMetrics` (on `*`, because
CloudWatch metric reads cannot be scoped to a resource). SSM reads are already covered by `simulator-discovery`.
The metrics extractor's role also gets `cloudwatch:ListMetrics`, to find the operations `ThrottledRequests` is
published under.

## Running it

```bash
cd AnomalyPulse
# No AWS: prints the manifest a run would start with.
python -m simulator start-incident --type db-throttling --intensity 0.8 --band 1.5 2.5 --duration 300 --seed 7 --dry-run

# Real run (needs the Products table provisioned and the sim profile):
caffeinate -is python -m simulator start-incident --type db-throttling --intensity 0.8 --band 1.5 2.5 --duration 300

# Tests (no AWS):
cd simulator && python -m pytest test_db_throttling.py -q
```

Do not curl the API or run smoke tests while an experiment is running: telemetry is tied to an experiment by time alone.

## Definition of Done (issue #13)

- [ ] `ThrottledRequests > 0` at target ratios. **Needs the real run** once the Products table is provisioned.
- [x] The controllable parameter is the ratio; the throttle rate is recorded as an outcome in `observed`.
- [x] No `db_throttled` flag is written anywhere; only CloudWatch counts are used.
- [x] Capacity is never changed by the injector, so nothing needs restoring; idle cost and teardown are documented above.

Before the PR, the validation runs also need to show:

- [ ] A 10× traffic spike at the chosen capacity does not throttle.
- [ ] When throttling starts at a low, a middle and a top ratio (`first_throttle_at`), against the burst table above.
- [ ] `achieved_reads_per_s` matches `target_reads_per_s` at the top ratio.
- [ ] The metrics extractor's `throttled_requests` is non-zero during a throttling window.
- [ ] Whether Product requests time out under throttling (the client's 5xx against Product's 5xx records).
