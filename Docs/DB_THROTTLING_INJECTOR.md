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

## How it works

| Step | What happens |
|---|---|
| `configure()` | Pure. Resolves `intensity` inside the band into `{"load_ratio": x}`. No AWS calls, no clock, same seed gives the same value. |
| `start()` | Looks up the Products table name in SSM (`/anomalypulse/tables/products/name`), reads its provisioned read capacity (RCU) with `describe_table`, and **refuses to run if the table is on-demand** (it cannot be throttled at a low ceiling). Samples a few product ids, then starts background reader threads. |
| the load | `ceil(reads_per_s / 40)` threads (at most 32), each paced against a schedule so the rate does not sag by the time each call takes. Reads are strongly consistent `GetItem`s, so an item of 4 KB or less costs exactly 1 RCU and reads per second equals RCU per second. |
| retries | **Off** on the load client (`total_max_attempts=1`). boto3 would otherwise retry a throttled call, which hides the throttle and slows the generator down. |
| `stop()` | Sets the stop event and joins the threads. Safe before `start()`, after a failed `start()`, and when called twice. Returns `None`. |
| `observe()` | Runs once after the recovery period. Reads the throttle counts back from CloudWatch for the fault window and returns them (see below). |

## What is recorded

`params` (what was asked for) and `observed` (what was measured) are separate in the manifest.

```json
"params":   { "load_ratio": 2.3 },
"observed": {
  "table": "anomalypulse-products",
  "provisioned_rcu": 50,
  "reads_attempted": 34500,
  "reads_throttled_client": 1210,
  "client_throttle_rate": 0.0351,
  "cw_throttled_requests": 1210.0,
  "cw_read_throttle_events": 1215.0
}
```

- `reads_attempted`, `reads_throttled_client`, `client_throttle_rate`: the injector's own count of the reads it sent.
- `cw_throttled_requests`: CloudWatch `ThrottledRequests` (operation `GetItem`) summed over the window's minutes.
- `cw_read_throttle_events`: CloudWatch `ReadThrottleEvents` summed over the same minutes.
- The CloudWatch figures also include any throttling of normal traffic, which is the point: this is what a monitor would see.

**Feature/Label Boundary.** All of this lives in the manifest only (Tier-2). The injector never writes a `db_throttled`
flag, never logs the fault, and puts nothing in telemetry. The only observable signal is the CloudWatch
`ThrottledRequests` **count**, which the M2 metrics extractor collects on its own.

## Capacity (decision for Jake)

The handoff for M3-1 says not to change capacity per experiment (DynamoDB allows only a few billing-mode switches and
capacity decreases per day, and M4 needs 140+ throttling runs), so the Products table must sit at a **fixed** provisioned capacity.
Two constraints decide the number:

1. A **10x traffic spike must not throttle by itself**, or `TRAFFIC_SPIKE` runs would carry throttling symptoms. Roughly:
   `capacity >= 1.5 x (10 x baseline_peak_rps x RCU_per_request)`, with about 0.5 RCU per eventually consistent `GetItem`.
2. The top of the planned bands (5.5x) must stay cheap and easy to drive: at 50 RCU that is about 275 reads/s, around 7 threads.

The baseline request rate comes from Josh's workload generator (M3-2), so the number is not final until that rate is known.
**Proposed starting point: Products `BillingMode: PROVISIONED`, 50 RCU, 5 WCU** (writes are unused by this injector).
This is a `template.yaml` change and deploy that Jake makes. This PR does not change the table.

## Teardown

- **Per run:** `stop()` ends the reader threads. The injector changes **nothing persistent** (no capacity change, no
  fault-config row, no items written), so there is nothing to restore after a run. If the simulator crashes, the threads die with the
  process and the load simply stops. The framework also calls `stop()` on any failure or Ctrl-C.
- **After the project (idle cost):** the only thing that lingers is the provisioned capacity (see cost). To go back to on-demand,
  set `BillingMode: PAY_PER_REQUEST` on `ProductsTable` in `template.yaml` and redeploy (billing-mode switches are limited to
  a few per 24 hours). To retire the injector, also remove the `simulator-db-throttling` policy from `SimulatorRole`.

## Cost

Prices are for us-east-2 and should be re-checked on the AWS pricing page.

| Item | Estimate |
|---|---|
| Load per run | `load_ratio x RCU x seconds` strongly consistent reads. At 50 RCU, ratio 5.5, 5 minutes: about 82,500 reads, a few cents at on-demand read prices. A typical run is lower. |
| Provisioned table (idle) | About `RCU x $0.00013/hr + WCU x $0.00065/hr`. At 50 / 5 that is about $0.01 per hour, roughly $7 per month if left running. |
| CloudWatch | Three `GetMetricStatistics` calls per run, negligible. |

140 training runs of a few cents each is well under a few dollars. The standing provisioned capacity is the larger cost.

## Known risk: burst capacity

DynamoDB banks up to **300 seconds** of unused capacity as burst. Offered load above capacity only throttles once that bank is used up,
which at ratio `r` takes roughly `300 / (r - 1)` seconds (about 600 s at 1.5x, 150 s at 3x, 67 s at 5.5x).
With a 5-minute fault window, low-ratio runs may therefore throttle little or not at all, depending on how full the bank was.
This has to be measured on the real table before M4 relies on it. If it is a problem, options are a longer window for low ratios,
a short drain at the start of the window, or accepting it, since the throttle rate is an emergent outcome by design.

## Permissions

`SimulatorRole` in `template.yaml` gets the policy `simulator-db-throttling`:
`dynamodb:DescribeTable`, `GetItem`, `Scan` on the Products table, and `cloudwatch:GetMetricStatistics` (on `*`, because CloudWatch
metric reads cannot be scoped to a resource). SSM reads are already covered by `simulator-discovery`.

## Running it

```bash
cd AnomalyPulse
# No AWS: prints the manifest a run would start with.
python -m simulator start-incident --type db-throttling --intensity 0.8 --band 1.5 2.5 --duration 300 --seed 7 --dry-run

# Real run (needs the Products table provisioned and the sim profile):
python -m simulator start-incident --type db-throttling --intensity 0.8 --band 1.5 2.5 --duration 300

# Tests (no AWS):
cd simulator && python -m pytest test_db_throttling.py -q
```

Do not curl the API or run smoke tests while an experiment is running: telemetry is tied to an experiment by time alone.

## Definition of Done (issue #13)

- [ ] `ThrottledRequests > 0` at target ratios. **Needs the real run** once the Products table is provisioned.
- [x] The controllable parameter is the ratio; the throttle rate is recorded as an outcome in `observed`.
- [x] No `db_throttled` flag is written anywhere; only CloudWatch counts are used.
- [x] Capacity is never changed by the injector, so nothing needs restoring; idle cost and teardown are documented above.
