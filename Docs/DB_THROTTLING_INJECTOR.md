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

The ratio counts the injector's own reads. The application's reads come on top: about 7.5 RCU/s at the 20 rps
baseline, so about +0.04 on the ratio at 200 RCU. `observed.offered_ratio` records the total.

## How it works

| Step | What happens |
|---|---|
| `configure()` | Pure. Resolves `intensity` inside the band into `{"load_ratio": x}`. No AWS calls, no clock, same seed gives the same value. |
| `start()` | Looks up the Products table name in SSM (`/anomalypulse/tables/products/name`), reads its provisioned read capacity (RCU) with `describe_table`, and **refuses to run if the table is on-demand** (it cannot be throttled at a low ceiling). Reads the item keys with one `Scan`, then starts the load. |
| the load | Strongly consistent `BatchGetItem` calls of up to 25 keys (every product). Each item is under 4 KB, so each key costs exactly 1 RCU, and reads per second equals RCU per second. 1,100 reads/s is 44 calls a second. The calls are **open-loop**, like the workload's requests: a scheduler thread hands each one to a pool of up to 64 workers at its planned time, whether or not earlier calls have come back. If all 64 are busy, the call is dropped and counted (`calls_dropped`), never sent late. The calls take turns over **16 clients**, each with its own connections ("Connections", below). |
| the surge | The window opens at **10× capacity until DynamoDB serves it only its capacity**: the reads it served over the last 10 s average no more than 1.5× capacity. Then the band's ratio holds for the rest of the window. See "Burst capacity" below. If the bank isn't spent after 150 s, the surge stops anyway. |
| throttles | A batch DynamoDB could partly serve returns the unread keys in `UnprocessedKeys`; one it could not serve at all raises `ProvisionedThroughputExceededException`. Both count as throttled reads. **Neither is retried**, and the client has retries off (`total_max_attempts=1`): a retry would hide the throttle and change the offered load. |
| `stop()` | Stops the scheduler and waits for calls in flight (each times out after 5 s). Safe before `start()`, after a failed `start()`, and when called twice. Returns `None`. |
| `observe()` | Runs once after the recovery period. Returns the client's counts and the table's CloudWatch counts (see below). |

## What is recorded

`params` (what was asked for) and `observed` (what was measured) are separate in the manifest.

```json
"params":   { "load_ratio": 3.0 },
"observed": {
  "table": "anomalypulse-products-v2",
  "provisioned_rcu": 200,
  "target_reads_per_s": 600.0,
  "surge_end": "bank_spent",
  "surge_s": 43.0,
  "first_throttle_at": "2026-10-16T14:21:06.818Z",
  "bank_spent_at": "2026-10-16T14:21:16.818Z",
  "label_from": "2026-10-16T14:21:21.818Z",
  "steady_reads_per_s": 598.7,
  "achieved_fraction": 0.998,
  "steady_throttle_rate": 0.6612,
  "served_per_s_by_10s": [2000.0, 2000.0, 2000.0, 733.5, 196.2, 189.8, 192.5],
  "steady_peak_served_per_s": 197.4,
  "clients": 16,
  "app_reads_per_s": 7.6,
  "offered_ratio": 3.031,
  "reads_attempted": 228500,
  "reads_throttled": 151000,
  "read_errors": 0,
  "calls_dropped": 0,
  "client_throttle_rate": 0.6608,
  "cw_throttled_requests": 2210.0,
  "cw_throttled_requests_by_operation": { "BatchGetItem": 1105.0, "GetItem": 96.0, "Scan": 1009.0 },
  "cw_read_throttle_events": 151400.0,
  "cw_consumed_read_capacity": 78000.0
}
```

(Illustrative numbers; `served_per_s_by_10s` has 30 entries for a 5-minute window.)

- `first_throttle_at`: when DynamoDB first refused one of the injector's reads. With a full bank that is
  usually when the bank ran out, but not always (from the runner host over one client, ~3 s into a full
  one), so nothing is labeled from it.
- `bank_spent_at`: when the surge saw the bank spent: the injector's reads DynamoDB served over the last
  10 s averaged no more than 1.5× capacity.
- `label_from`: `bank_spent_at` plus 5 s. **M4 labels the window `DATABASE_THROTTLING` from here** (below).
- `surge_end`, `surge_s`: how the surge ended (`bank_spent`, or `time_limit` after 150 s) and how long it lasted.
- `served_per_s_by_10s`: the injector's reads DynamoDB served, per second, in each 10 s of the load.
- `steady_peak_served_per_s`: the most it served the injector per second over any minute after
  `label_from`. With the bank spent that is capacity less the app's reads (about 192 at 200 RCU).
  **M4's batch runner reruns a run where it is above 1.25× capacity**: burst capacity came back inside
  the labeled stretch, so the app wasn't throttled for part of it.
- `clients`: how many clients the reads went out over.
- `target_reads_per_s`, `steady_reads_per_s`, `achieved_fraction`: what the ratio asked for and what the client
  really sent after the surge. **M4's batch runner reruns a run whose `achieved_fraction` is below 0.97.**
- `app_reads_per_s`: the table's consumed RCU per second in the 3 minutes before the window (the application's own
  reads, including a traffic spike's in a cascade). `offered_ratio` = (`steady_reads_per_s` + `app_reads_per_s`) /
  capacity: the ratio DynamoDB actually saw. **In a cascade it is a lower bound**: a spike that began less than
  3 minutes before the window is only partly counted (validation cascade: 2.16 recorded, about 2.22 seen by
  DynamoDB; `Docs/CASCADE.md`). M4's bands use `load_ratio`, which this does not affect.
- `steady_throttle_rate`: the share of the injector's reads throttled after the surge. `client_throttle_rate` is over
  the whole window.
- `cw_throttled_requests`: CloudWatch `ThrottledRequests`, summed over **every operation** on the table for the window's
  minutes; `_by_operation` keeps the split. The application's `GetItem` (`GET /products/{id}`) and `Scan`
  (`GET /products`) appear here. A `BatchGetItem` counts only when *all* of its keys were throttled.
- `cw_read_throttle_events`: CloudWatch `ReadThrottleEvents`, which counts each throttled key, table-wide.
- `cw_consumed_read_capacity`: CloudWatch `ConsumedReadCapacityUnits` for the same minutes.

**Feature/Label Boundary.** All of this lives in the manifest only (Tier-2). The injector never writes a `db_throttled`
flag, never logs the fault, and puts nothing in telemetry. The observable signals are CloudWatch **counts**, which the
M2 metrics extractor collects on its own each minute. Its `throttled_requests` is summed over operations the same way
(it used to ask for `TableName` alone, which CloudWatch answers with nothing).

## The Products table and its capacity

**Decided (2026-10-15): a new table, `anomalypulse-products-v2`, created `PROVISIONED` at 200 RCU / 5 WCU.**

**Why a new table.** `GET /products` (60% of traffic) is a full-table `Scan`, and a Scan costs a minimum per
partition, so its cost depends on the table's history, not its data. Measured on 2026-10-15 with throwaway copies of
the same 25 products (1,490 bytes):

| Table | Scan, eventually consistent | Scan, strongly consistent |
|---|---|---|
| Created provisioned (500/5) | **0.5 RCU** | 1.0 |
| Created on-demand | 2.0 | 4.0 |
| Created on-demand, then switched to provisioned | 4.0 | 8.0 |

The old `anomalypulse-products` had been switched, so every `GET /products` cost 4.0 RCU: 49 RCU/s at the 20 rps
baseline and ~499 RCU/s in a 10× spike, all of the 500 RCU then provisioned. In the new table it costs 0.5.
**Don't switch the table's billing mode, and don't raise its capacity, during M4**: either can add partitions and
change every `GET /products` read cost mid-dataset. M4's batch runner checks before each batch that one `Scan` still
costs 0.5 RCU.

**Two constraints set the capacity**, and they pull in opposite directions.

1. **A traffic spike must not throttle by itself**, or `TRAFFIC_SPIKE` runs carry throttling symptoms. The read load
   is ~7.5 RCU/s at 20 rps (`GET /products` and `GET /products/{id}` cost 0.5 each), so a 10× spike reads ~75 RCU/s,
   38% of 200. Even every request a `GET /products` at 10× (100 RCU/s), or a 20× stress spike (150 RCU/s), stays
   below capacity.
2. **The top load ratio must be drivable.** 5.5× at 200 RCU is 1,100 reads/s, 44 calls a second.

Prices: $0.00013 per RCU-hour and $0.00065 per WCU-hour (us-east-2, standard table class; re-check the pricing
page). The standing capacity costs about **$0.70 a day**; reads up to it cost nothing extra, and throttled reads are
not billed, so a throttling run itself is free. Delete the table after M9.

## Burst capacity, and when the incident starts

DynamoDB banks up to **300 seconds** of unused capacity as burst, on a best-effort basis. A load above capacity
only throttles once that bank is spent, which at ratio `r` takes `300 / (r - 1)` seconds, **whatever the capacity**.
M3-3's validation (2026-10-15, 500 RCU, 5-minute windows, no surge) matched it: no throttling at all at 1.75×, the
first throttle 174 s in at 3.0× and 80 s in at 5.5×. Without a fix, the lower training band (1.5–2.5×) would barely
throttle.

So the window opens with the **surge**: 10× capacity spends a full bank in about 33 s. It ends on DynamoDB's own
answer, not on a prediction, so it works whether the bank is full, partly spent, or used by AWS for maintenance:
when the injector's reads DynamoDB served over the last 10 s average no more than 1.5× capacity, which is all a
spent bank serves. From then on the throttle rate reflects the band's ratio.

**A throttled read is not that answer.** Until M4-1 the surge ended on the first throttled read. From a laptop
that came as the bank ran out, but from the runner host it came ~3 s into a full bank, and the window then ran on
burst capacity: at 2.0× the table served the whole load for the whole window, and Product's p95 didn't move
(`Docs/DATASET.md`, "Validation"). A rule over 1 s fails the same way (below); over 10 s it doesn't.

### Connections (M4-1's investigation, 2026-10-16)

From the runner host, one client sends every call over one to three connections (DynamoDB answers in ~6 ms), and
over those DynamoDB spent burst at about half the surge's rate. Probes at 10× (2,000 reads/s) for 120 s from a
full bank, without app traffic, a few minutes apart:

| Client | Connections | While the bank lasted | Bank spent after |
|---|---|---|---|
| Runner host, 1 client | 1–3 | ~950 RCU/s: a second or two at 2,000, then a second or two at none | ~80 s |
| Laptop, 1 client (p50 latency 153 ms) | up to 64 | 2,000 RCU/s | 33 s |
| Runner host, 16 clients | at least 16 | 2,000 RCU/s | 33 s |

The bank was full each time (about 60,000 RCU: 300 s × 200). Every throttle was
`TableReadProvisionedThroughputExceeded` (the exception's `ThrottlingReasons`; CloudWatch's
`ReadProvisionedThroughputThrottleEvents` equals `ReadThrottleEvents` for every run on this table), never a
partition limit. AWS doesn't document why fewer connections spend burst more slowly, so the injector relies only on
what was measured: it uses 16 clients, and its end-of-surge rule holds either way. Over 10 s the one-client
seesaw never fell below 780 RCU/s, and a spent bank served about 200, at most 232 over any 10 s.

**Burst can come back.** In one 10× probe through the simulator (`exp-20261016T200807Z-3b54`, one client), a
second full bank (~60,000 RCU) was served about 3 minutes into the window, a minute after the first ran out, on
a table that still had one partition. The cause is unknown. `steady_peak_served_per_s` shows it, and the batch
reruns such a run.

**Labeling (M4-4): a `DATABASE_THROTTLING` window starts at `observed.label_from`, not at `active_from`.** That is
5 s after the surge saw the bank spent. When the bank runs out, the application meets the surge's throttling for
the up to 10 s the surge takes to see it, before the load drops to the band's ratio, and its retries take up to
3.15 s: at 1.75× in M3-3's validation, 3 requests failed as the bank ran out and none after. The stretch from
`active_from` to `label_from`, which holds the surge and that moment, is excluded from the training set, as recovery
is, so neither the surge's consumed-capacity spike nor those failures reach the features. A run whose surge ended
on its time limit (`surge_end: "time_limit"`, `label_from: null`) never saw the bank spent, and M4 reruns it.

## How the application sees it

Throttling shows as **latency, not errors**. At 500 RCU in M3-3's validation, Product's p95 rose from ~15 ms to
807 ms at 3.0× and 3,202 ms at 5.5×, while its p50 stayed near 12 ms and no request failed: boto3 retried each
throttled read. Those retries wait 0.05 s, then 0.1, 0.2, ... (legacy mode), so a p95 of 3.2 s is six retries.

Product, Cart and Order now **state their DynamoDB retries** instead of using the runtime's default: legacy mode,
7 attempts. That keeps exactly the behaviour measured above (up to six retries, 3.15 s of waiting) and turns a
longer streak into a 500 with telemetry. The default would have tried 10 times, waiting up to 25.5 s in all, past
Product's 10 s timeout, and a request that times out leaves no telemetry. The template also pins the Lambda runtime
to one version (`UpdateRuntimeOn: Manual` with its `RuntimeVersionArn`, since M4-1), so the bundled boto3 changes only
if someone changes that ARN.

## Teardown

- **Per run:** `stop()` ends the load. The injector changes **nothing persistent** (no capacity change, no
  fault-config row, no items written), so there is nothing to restore after a run. If the simulator crashes, the
  threads die with the process and the load simply stops. The framework also calls `stop()` on any failure or Ctrl-C.
- **After the project (idle cost):** the only thing that lingers is the provisioned table. Delete it (and the
  `simulator-db-throttling` policy on `SimulatorRole`) when the injector is retired.

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

- [x] `ThrottledRequests > 0` at target ratios. Validated 2026-10-15 at 3.0× and 5.5× (500 RCU, before the new
  table): the application's `GetItem` and `Scan` were throttled, and the metrics extractor recorded them.
- [x] The controllable parameter is the ratio; the throttle rate is recorded as an outcome in `observed`.
- [x] No `db_throttled` flag is written anywhere; only CloudWatch counts are used.
- [x] Capacity is never changed by the injector, so nothing needs restoring; idle cost and teardown are documented above.

Validated on the new table, 2026-10-15 (200 RCU, from a laptop, 20 rps):

- [x] One `GET /products` scan costs 0.5 RCU.
- [x] A 10× traffic spike does not throttle: 73.6–75.1 RCU/s (38% of capacity), no `ThrottledRequests` or
  `ReadThrottleEvents` on any operation (`exp-20261015T165252Z-cd32`).
- [x] The surge ends `throttled` after 33.6–33.7 s at every ratio (a full bank, as predicted), `achieved_fraction`
  is 1.00, no calls are dropped, and the steady throttle rate is within a point of 1 − 1/`offered_ratio`.
- [x] Product's p95 rises with the ratio, and every 5xx the client saw has a telemetry record.

| Ratio (run) | Steady throttle rate | Product p50 / p95, steady | Product 5xx, steady |
|---|---|---|---|
| 1.75× (`exp-20261015T170156Z-0031`) | 44.9% | 5 ms / ~0.78 s | 0 |
| 3.0× (`exp-20261015T171702Z-c4c8`) | 67.9% | 5–6 ms / ~1.59 s | 0 |
| 5.5× (`exp-20261015T173208Z-a378`) | 82.4% | 9–15 ms / ~3.2 s | ~1% |
| 8.0×, stress (`exp-20261015T174713Z-7f32`) | 87.6% | 380–780 ms / ~3.2 s | ~8–11% (344, all with telemetry) |

In the minute the bank runs out, a few requests fail as the app meets the surge's throttling (3 at 1.75×); that is
why labeling starts at `label_from`, 5 s after the bank was spent. These runs predate `label_from`, and none of them
belongs in the dataset.
