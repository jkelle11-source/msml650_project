# Cloud cost and operational performance (M9-4)

What AnomalyPulse costs on AWS and how the live pipeline performed, measured on M9-1's test set: the four batches of
2026-11-13 to 11-16 (371 runs from the runner, 3,777 minutes, 8.31 million API requests, 317 incidents), with the
live pipeline scoring every minute. **Owner:** Jake. **Code:** [`AnomalyPulse/cost/`](../AnomalyPulse/cost/):
`collect_m9.py` gathers the inputs (read-only), `measured.py` computes everything here from them without AWS,
`test_collect_m9.py` and `test_measured.py` test both.

```bash
cd AnomalyPulse
python3 -m cost.measured                   # prints everything below from cost/inputs/m9/, writes cost/results/
python3 -m pytest cost -q                  # 68 tests (no AWS)
python3 -m cost.collect_m9 --profile dev   # collect again (read-only, about $0.12)
```

`cost/inputs/m9/` keeps what was read. CloudWatch keeps 1-minute points for only 15 days (the first batch's until
about 2026-11-28), so collecting again after that would read coarser data.

All costs are at **list price** (us-east-2), with no free allowance unless a line says so. The always-free
allowances belong to the account, not the architecture. Some of November's ran out during the batches: Lambda's and
SQS's on Nov 14, the logs' on Nov 15. Others hadn't run out by Nov 22, so the bill shows those lines as $0:
CloudWatch's million API requests, the 10 alarms, DynamoDB's 25 write units and the free storage.

---

## The results

### Cost per inference

One inference is the Diagnosis Lambda scoring one minute's window with the Isolation Forest and the classifier. It
costs **$5.6 × 10⁻⁷**, or **$0.56 a million**, measured from the billing lines (`REPORT`) of all 3,752 calls in the
test set. The pipeline sends one window a minute, so that's **$0.02 a month**.

- **What a call bills:** 26.7 ms on average at 1 GB. The handler runs 10.6 ms. The models' own scoring is likely
  about 2 ms of that: the example reply in [`LIVE_DIAGNOSIS.md`](LIVE_DIAGNOSIS.md) gives an `inference_ms` of 1.9.
  It wasn't collected for the test set's calls.
- **Cold starts are most of the bill:** 33 of the 3,752 calls, about one every two hours. Each takes about 1.8 s to
  start (loading the libraries and the model from S3), and together they are **58% of the billed time**.
- **A warm call** costs $3.5 × 10⁻⁷, and 57% of that is the request fee ($0.20 a million).
- **Scoring a minute end to end** (the window builder, the Diagnosis Lambda, the incident store's own work, its
  table writes and their logs) costs $5.0 × 10⁻⁵ a minute, **$2.19 a month**.

### Near-zero idle

**When there's no traffic, inference costs nothing.** On Nov 20–21 UTC (two days with no traffic) the Diagnosis
Lambda made **0 calls**: no `REPORT` lines at all. The incident store and Bedrock made none either, because the
window builder announces no window for an idle minute. The inference path's only idle cost is storage: its container
image in ECR.

The whole system's idle cost at list price, from the bill's quantities on Nov 18, 20 and 21 (mean):

| Idle, at list price | A day | A month |
|---|---|---|
| The store: the Products table's provisioned capacity, 200 read and 5 write units (the DB-throttling experiment's), and the tables' storage | $0.705 | $21.44 |
| AnomalyPulse: the metrics extractor's CloudWatch API requests ($0.389 a day), the two once-a-minute Lambdas' S3 writes and reads and their run time, the pipeline's alarms, S3 and log storage, SQS polling, the ECR images | $0.592 | $18.01 |
| Tooling: the stopped runner's disk | $0.043 | $1.30 |
| **Total** | **$1.340** | **$40.76** |

The bill charged $0.841 a day ($25.58 a month). The always-free allowances covered the difference: the extractor's
CloudWatch requests, the Products table's write units, the alarms and the storage.

Only two functions run when idle, the window builder and the metrics extractor, 1,440 calls a day each. A real-time
SageMaker endpoint for the same model would cost $40.88 to $59.57 a month idle (below).

### Cost per experiment

The test set cost **$103.97**, which is **$0.28 a run** (371 runs, 370 accepted). The four batches came to $0.287,
$0.270, $0.294 and $0.260 a run. This is what the batches added to the idle floor (above). The floor itself, about
$3.51 over the test set's 3,777 minutes at list price, is left out here and in the bill check below.

| | Total | Per run |
|---|---|---|
| The store: API Gateway, its four Lambdas, the carts and orders tables | $71.45 | $0.193 |
| AnomalyPulse: the pipeline, inference, Bedrock, telemetry logs, S3, SQS, EventBridge | $27.38 | $0.074 |
| Tooling: the runner, the simulator's tables | $5.13 | $0.014 |

By line: Lambda $41.98, API Gateway $29.09, Bedrock $18.44, the runner $5.09 (instance and IPv4 address), logs
$3.41, S3 requests $3.37, DynamoDB $1.34, SQS $0.65, EventBridge $0.43, CloudWatch API requests $0.15.

**Checked against the bill.** The bill for Nov 14–16, priced at list price, less three idle days, and scaled from the
3,699 batch minutes inside those days to the test set's 3,777, gives **$104.47**. The model gives $103.97, which is
0.995× the bill. The bill itself charged $4.18 less for those days, because of the always-free allowances (some ran
out during them).

### Cost per incident

Each incident costs **$0.059**, nearly all of it Bedrock: 1.71 calls an incident (542 calls for 317 incidents;
retries and type changes add calls) at **$0.034 a call**. A call is 2,920 input and 1,684 output tokens of Claude
Sonnet 4.6, at $3 and $15 a million. The explanation Lambda and the incident store's wait for it add $0.0006.

- **With runbooks on,** as they have been live since #113, a call costs **$0.038**: 3,537 input and 1,818 output
  tokens, from M9-5's 38 calls on Nov 19, which were billed $1.44. That's 1.11×, about $0.065 an incident.
- **Why Bedrock was so much of the test set's cost (18%):** the test set opens an incident every 12 minutes. At
  30 incidents a month it would be $1.76 ($1.96 with runbooks).

### Monthly cost by request rate

The table below is a month (730 hours) at a steady rate, with traffic every minute and 30 incidents, at list price.
The always-free allowances every account gets are one line of their own: 400,000 Lambda GB-s, 1 million Lambda
requests, 5 GB of logs, 1 million SQS and 1 million CloudWatch requests, 25 read and 25 write units, 10 alarms and
25 GB of DynamoDB storage.

| Requests a second | Requests a month | The store | AnomalyPulse | AnomalyPulse's share | Total | After always-free |
|---|---|---|---|---|---|---|
| 1 | 2.6 M | $32.08 | $24.33 | 43% | $56.41 | $36.77 |
| 20 (the test set's healthy rate) | 52.6 M | $234.21 | $74.75 | 24% | $308.96 | $284.93 |
| 100 | 262.8 M | $1,085.27 | $287.03 | 21% | $1,372.30 | $1,348.27 |

The full grid (1, 2, 5, 10, 20, 50 and 100) is in `cost/results/projections.json`, for M9-6's cost curve. Every
line is a straight line in the rate.

- **The store** is mostly API Gateway's $3.50 a million requests: 85% of its cost at 100 rps ($919.80).
- **AnomalyPulse's cost is moving telemetry, not ML.** At 100 rps:
  - S3: $111, nearly all requests, one object written per shipper and aggregator call
  - the telemetry logs: $106 (the four services' $103, the shipper's and the pipeline's $4)
  - SQS and EventBridge: $34
  - its Lambdas that scale with traffic: $13

  The ML is tiny next to that. The four once-a-minute Lambdas (the window builder, the metrics extractor, the
  Diagnosis Lambda and the incident store) cost $1.89 a month with their logs, and Bedrock at 30 incidents $1.75.
  Batching the shipper's and the aggregator's writes is where its cost would come down.
- **At 1 rps the fixed costs dominate:**
  - the Products table's provisioned capacity: $21.35
  - the metrics extractor's 1.18 million CloudWatch requests a month (27 a minute): $11.87, most of it inside the
    free million

### SageMaker Serverless Inference against the Lambda, on paper

No managed inference path was built (§9), so this sets the Diagnosis model's measured Lambda cost against SageMaker's
list prices (AWS Price List API, us-east-2, 2026-11-23).

| The Diagnosis model, a month | Idle | One call a minute (43,800) | One a second (2.6 M) |
|---|---|---|---|
| **Lambda, measured** | **$0** | **$0.02** | **$0.91** |
| SageMaker Serverless Inference, 1 GB | $0 | $0.01 | $0.78 |
| Serverless Inference with 1 provisioned concurrency | $30.75 | $30.75 | $31.11 |
| Real-time endpoint, ml.t2.medium | $40.88 | $40.88 | $41.10 |
| Real-time endpoint, ml.c6g.large | $59.57 | $59.57 | $59.79 |

- **Both serverless options cost nothing idle.** An endpoint that stays up costs $31 to $60 a month for a model
  that needs $0.02 of compute at the pipeline's rate: 1,300 to 2,400 times as much.
- **Serverless Inference comes out a little cheaper per call:** $3.0 × 10⁻⁷, against the Lambda's $5.6 × 10⁻⁷
  measured at one call a minute and $3.5 × 10⁻⁷ for a warm call. Two things favour it:
  - it has no request fee;
  - AWS bills only the time the inference code runs, so here it gets the Lambda's measured handler time (10.6 ms)
    and no cold-start time.

  Its data charge ($0.016 a GB in and out, for a 4.7 KB window and a reply of about 1 KB) is 28% of its cost per
  call. The difference is cents a month at either rate, and the Lambda keeps the runtime path in one service.
- At one call a second, the Lambda row uses a warm call's cost: with calls a second apart, it stays warm.

### Operational performance

This was measured from CloudWatch over the four batch windows while the pipeline was live. Percentiles are
CloudWatch's over each whole window. On the stress batch they matched exact percentiles from the `REPORT` lines to
about 1% at p50 and p95, and 3% at p99.

- **Throughput:** 8.31 million requests in 3,777 minutes, **36.7 rps** on average. The busiest minute was
  **243 rps** (14,557 requests in one minute, during an injected surge). Healthy runs held 20.0 rps.
- **API latency, healthy** (the 60 NORMAL test runs, 648,114 requests, 0 errors): **p50 16.0 ms, p95 30.7 ms, p99
  36.8 ms**. These are the medians of the 60 runs; the worst run's p99 was 39.2 ms.
- **API latency, whole test set:** p50 was 15.5–15.9 ms in every batch. In the three test batches, p95 was 0.9–1.4
  s and p99 3.2–4.0 s. Those tails are the injected faults, not the system: Lambda degradation adds 0.5–1.4 s, and
  dependency failure times out after 1.5–4.5 s. There were 159,189 5xx responses and 437 4xx, none of them in the
  healthy runs.
- **The pipeline kept up:**
  - the telemetry queue never had more than 1 message waiting, and its oldest message was at most 20 s old
    (median 5 s);
  - 418,309 messages were sent and 418,307 deleted inside the windows;
  - the four failure queues (telemetry dead-letter, undelivered, diagnosis failures, incident failures) held
    nothing. They were all empty on 2026-11-23, and they keep messages 14 days, so **no message failed anywhere**.
- **Lambda:** every function had **0 errors and 0 throttles**. The table gives the median over the batches for p50,
  the worst batch for p95 and p99, and the overall maximum:

| Function | p50 | p95 | p99 | Max | Peak concurrency |
|---|---|---|---|---|---|
| pipeline-forwarder | 14.7 ms | 45.0 ms | 108 ms | 1.6 s | 16 |
| telemetry-shipper | 31.4 ms | 43.0 ms | 66.8 ms | 3.1 s | 16 |
| pipeline-aggregator | 38.6 ms | 110 ms | 169 ms | 0.7 s | 4 |
| pipeline-window-builder | 4.07 s | 9.89 s | 11.6 s | 14.3 s | 1 |
| metrics-extractor | 398 ms | 459 ms | 488 ms | 1.4 s | 1 |
| diagnosis | 9.4 ms | 19.1 ms | 27.9 ms | 2.6 s (a cold start) | 1 |
| incidents | 48.9 ms | 29.6 s | 44.9 s | 56.6 s | 1 |
| explain (Bedrock) | 25.2 s | 45.6 s | 50.6 s | 56.2 s | 1 |
| product | 5.2 ms | 772 ms | 3.1 s | 4.8 s | 328 |
| cart | 6.0 ms | 11.7 ms | 1.5 s | 1.8 s | 59 |
| order | 15.9 ms | 3.92 s | 5.19 s | 7.5 s | 332 |
| payment | 1.5 ms | 4.88 s | 5.35 s | 5.4 s | 330 |

The services' tails and their concurrency come from the injected faults. In healthy runs their p95s were product
7.4 ms, cart 9.5 ms, order 24.1 ms and payment 5.6 ms. The incident store's p95 and p99 are its wait for the
explanation Lambda, which waits for Bedrock. That step belongs to diagnosis latency, which is M9-6's.

---

## How it was measured

1. **The windows** (`collect_m9.py`):
   - each batch from its first attempt's start to its last attempt's end plus 5 minutes, in whole minutes (1,423,
     1,423, 714 and 217). The 5 minutes catch the last minute's diagnosis, which lands 1–3 minutes later;
   - the 60 NORMAL test runs, each over its 9 whole minutes;
   - the UTC days Nov 14–16 (batches running) and Nov 18, 20 and 21 (idle). Nov 17 had check traffic and Nov 19
     M9-5's Bedrock calls.
2. **The usage:**
   - CloudWatch's 1-minute sums: API Gateway requests; each Lambda's invocations, GB-seconds (duration × memory)
     and log bytes; each table's consumed read and write units;
   - the Diagnosis Lambda from its `REPORT` lines (Logs Insights);
   - incidents and their Bedrock calls from the Incidents table;
   - the bill from Cost Explorer, by day.
3. **Checked against the bill, day by day:**
   - API requests within 0.001%, Lambda requests exact, DynamoDB on-demand units within 0.03%;
   - log bytes are exactly 2³⁰ per billed GB;
   - CloudWatch's GB-seconds are 0.56% under the bill. Its `Duration` leaves out two things the bill counts, about
     half each on the batch days: each call rounded up to the next millisecond, and each cold start's start-up
     (init) time. For example, the metrics extractor on Nov 20 was billed 565,962 ms: 560,845 ms of run time,
     4,400 ms of start-up in 12 cold starts, and 717 ms of rounding (`REPORT` lines, checked 2026-11-23). The
     GB-seconds are scaled by the bill's own ratio (1.0056);
   - all 13 unit prices checked (API Gateway, Lambda, logs, DynamoDB, Bedrock, EventBridge, S3, SQS and the runner)
     equal the bill's on Nov 16.
4. **The bill lines CloudWatch can't attribute** (S3, SQS, EventBridge and CloudWatch API requests) are the batch
   days' quantities above the idle days', per API request; the idle level is a fixed monthly cost. The pipeline's
   own S3 writes, one object per shipper and aggregator call, explain 95% of the S3 PUT-class requests above idle.
5. **Whose cost:**
   - **The store:** API Gateway, the product, cart, order and payment Lambdas, the carts and orders tables, and the
     Products table's capacity.
   - **AnomalyPulse:** the shipper, the pipeline's Lambdas, the metrics extractor, the Diagnosis Lambda, the
     incident store, the explanation Lambda and Bedrock, the Incidents table, S3, SQS, EventBridge, CloudWatch API
     requests, and the services' log ingestion. Their logs are mostly the per-request telemetry records AnomalyPulse
     reads.
   - **Tooling:** the runner, the simulator's tables (`experiments`, `fault-config`) and the stub.

   The dashboard Lambda runs once per console viewer, so it's counted in the runs' cost but not projected.
6. **The projections'** unit costs, each from where it was measured:
   - **per request:** the Lambdas and tables from the 60 healthy runs (the test mix at 20 rps), and the bill lines
     in 4 from the batch days;
   - **per minute,** from the batches: the window builder, the metrics extractor, the Diagnosis Lambda, and the
     incident store's own work;
   - **per incident:** Bedrock, the explanation Lambda, and the incident store's wait for it. The store calls the
     explanation Lambda and waits, and both have 512 MB, so 6,592 of the store's 6,773 GB-s are waiting;
   - **per month:** the idle days' fixed costs (S3, ECR, log and table storage, the pipeline's alarms, the idle level
     of S3, SQS and CloudWatch requests), and the Products table's 200 read and 5 write units, all at list price.

## What it leaves out

- **Straight-line scaling.** The Lambdas' and tables' per-request costs come from healthy runs at 20 rps. The window
  builder's run time grows with traffic (a 4 s median at the test set's 36.7 rps). It costs $1.35 a month, so even
  tripling it stays under $5.
- **The request lines come from the batch days, faults included.** S3, SQS, EventBridge and CloudWatch API requests
  per API request are the batch days' (step 4 above), not the healthy runs'. The faults change how many pipeline
  calls a request makes. Per 1,000 requests, the shipper made 47.9 calls on the batch days and 35.0 in the healthy
  runs; with the aggregator, the pipeline made 61.9 object writes and 55.5. On the healthy runs' basis, S3 PUTs
  (nearly all those writes) would be 10% lower: $80 instead of $89 a month at 100 rps. The total would then be
  $1,363 instead of $1,372 at 100 rps, and $307 instead of $309 at 20 rps (`request_basis` in `measured.json`).
  SQS and EventBridge have no per-call measure like that, so they stay on the batch days' basis.
- **The Products table's 200 read units** are the DB-throttling experiment's capacity, not a size chosen for
  100 rps. Nobody measured whether 40 GET /products a second would throttle it.
- **S3 GET-class requests above idle** include the post-batch label and feature builds, which read the same bucket,
  so the pipeline's share of that line is overstated by an unknown amount. The whole line is $21.57 at 100 rps, 1.6%
  of the total.
- **Runbooks were off during M9-1.** With them on, a Bedrock call costs 11% more (above).
- **30 incidents a month is an assumption.** Each incident costs $0.059.
- **Not counted:** tax (November's rate so far, 3.8%), data transfer out (free under 100 GB a month), the M10 demo,
  retraining, and Cost Explorer's own $0.01 a call.

## For M9-6

- **The cost curve:** `cost/results/projections.json` has one row per rate, with fields `rps`, `requests`, `app`,
  `anomalypulse`, `total`, `always_free` and `total_after_free`.
- **Everything else** is in `cost/results/measured.json`.
- **Latency:** detection, diagnosis and end-to-end latency are M9-6's. The explanation Lambda's 25 s median, and the
  incident store's wait for it, are the diagnosis step.
