# What the rest of the plan will cost (M9-4)

An estimate of the AWS bill for the rest of the project plan, built from the account's own measured usage. Written
2026-11-12, before M9-1. **Owner:** Jake. **Code:** `estimate.py` (the model), `collect.py` (its inputs), and
`test_estimate.py`.

```bash
cd AnomalyPulse
python3 -m cost.estimate              # the breakdown, from the committed inputs (no AWS)
python3 -m pytest cost -q             # 23 tests
python3 -m cost.collect --profile dev # refresh inputs/ (read-only; about 6 Cost Explorer calls at $0.01)
```

## The result (2026-11-12)

The student credit ended on 2026-10-31, so all of this is billed. M9 is due Nov 22 and M10 Nov 29, so nearly all
of the remaining spend falls in **November: about $127 with tax (range $106–$173)**, $16.43 of it already spent.

| November 2026, before tax | Low | Expected | High |
|---|---|---|---|
| So far (Nov 1 to Nov 12 morning UTC) | $16.43 | $16.43 | $16.43 |
| M9-1 API Gateway requests | $25.02 | $30.25 | $37.49 |
| Lambda (November, after the free allowance) | $19.71 | $25.64 | $33.72 |
| Bedrock: test-set and M10 diagnoses, M9-5's runbook comparison | $10.92 | $17.64 | $42.77 |
| Fixed to Nov 30: Products table at 200 RCU, runner disk, S3, ECR | $14.89 | $14.89 | $14.89 |
| M9-1 S3, DynamoDB writes, EventBridge (including the pipeline) | $5.46 | $5.61 | $5.97 |
| M9-1 runner time (runs, batch overheads, idle tails) | $5.12 | $5.48 | $6.14 |
| CloudWatch logs and API requests, SQS (after free allowances) | $3.62 | $4.30 | $5.28 |
| M10 dry runs and demo | $0.91 | $1.22 | $1.83 |
| Log queries and Cost Explorer calls | $0.20 | $0.50 | $2.00 |
| **November total** | **$102.28** | **$121.97** | **$166.52** |
| **With tax** (at November's rate so far, 3.8%) | **$106.21** | **$126.65** | **$172.91** |

**December**, with no planned work after the demo (before tax): $0 if everything is deleted by Nov 30; $4.07 if
the runner and the Products table are deleted after M10, as the plan says, and the rest is kept; $22.36 if nothing
is deleted.

The expected case has 390 runs on the runner for M9-1 (350 test, 20 stress, 16 validation, 4 reruns), 326
incidents and 594 Bedrock calls.

## How it is built

1. **Usage per run, measured.** Every October run's API requests, each service Lambda's GB-seconds and
   invocations, and its log bytes, from CloudWatch's 5-minute metrics over that run's own window
   (`inputs/attempts.jsonl`), averaged by kind and band over v1's 700 accepted runs (`inputs/kinds.json`). It
   reproduces October's bill: 14.01M API requests by CloudWatch against 14.07M billed, the requests inside run
   windows within 0.3% of the workloads' own `sent` counts, and 2.95M service GB-seconds against 3.0M billed.
2. **The test set, run by run** (`test_set`, from `simulator/integrity.py`): 60 normal runs, 60 spikes, 60 DB
   throttling, 45 Lambda degradation, 45 payment failures, 30 cascades, 30 confounders and 20 varied-duration
   runs, plus the 20-run stress slice and a validation dataset of the new kinds first (M9-1).
   - **Bands:** each fault type's usage is fitted on its two training bands, then evaluated at the test and
     stress bands' midpoints (intensity is drawn uniformly inside a band).
   - **Endpoint mix:** the test mix (40/25/15/10/10) changes each function's share of invocations. Payment, which
     only POST /orders reaches, doubles, and so does a payment failure's cost.
   - **The rest:** a 2- or 12-minute fault scales the fault's usage by its length; a confounder is the sum of its
     two faults; a cascade scales its measured template by its stages' test/train ratios.
3. **Priced at the bill's own lines** (`check_prices` compares them). November's free allowances (400k Lambda
   GB-seconds, 1M Lambda requests, 5 GB of logs, 1M SQS and 1M CloudWatch API requests) are subtracted once, net
   of what November has used so far and the idle days left. The pipeline's per-run cost (forwarder, aggregator,
   Diagnosis and incident Lambdas, S3, SQS, EventBridge) and Bedrock's ($0.029 a call) come from Nov 11's live
   batch, net of an idle day.
4. **Low, expected, high** (`SCENARIOS`) vary what isn't known yet:
   - the size of M9-5's runbook comparison: 30 incidents up to all 315, the largest swing;
   - Bedrock calls per incident (live so far: 1.43 for a single fault, 2.5 for a cascade) and per call;
   - the test mix's effect on Product's cost (between all of it in GET /products scans and none);
   - ±15% on usage for the bands no run has used yet;
   - validation runs, reruns, M10 sessions and the runner's idle tails.

**Checked:** the model predicts Nov 11's seven live runs, which it wasn't fitted on, at 0.96× their API requests
and 1.00× their GB-seconds. The tests fail on each of eight deliberate errors (a fit's slope, the mix factors,
scaling training-band cascades, a negative free allowance, the varied-duration split, the API price, incidents on
normal runs).

## What it leaves out

- Regenerating the test set if a batch has to be thrown away (about $70 a time).
- Experiments outside the plan (the 15-second-window stretch, retraining) and ad hoc use. This month that's $0.13
  of Claude Opus 4.6 and an hour each of a NAT gateway and a t3.micro.
- Tax beyond November's effective rate so far.

## What it means

- The "Student" budget ($100 a month) will alert during M9-1. Its forecast alert comes first, then the actual-spend
  alerts at $50, $80 and $100. That's expected, not a fault.
- November's invoice, about $127, is issued in early December: the account needs a payment method by then.
- Deleting the runner (`anomalypulse-runner`, with `anomalypulse-runner-idle-stop`) and the Products table right
  after the demo saves about $18 in December. Delete `anomalypulse-runner`; never update it (Docs/DATASET.md).
- For M9-4's deliverables, `estimate.py`'s per-run usage gives the cost per experiment and per inference once
  M9-1's runs are in: rerun `collect.py` with the test set's window.

## The inputs (`inputs/`)

| File | What | From |
|---|---|---|
| `attempts.jsonl` | Each registry attempt's spec, requests sent, run window and the usage inside it | S3 registry + CloudWatch, Oct 15–26 |
| `kinds.json` | Mean usage per kind and band, v1's accepted runs | `attempts.jsonl` |
| `ce.json` | Usage cost and quantity by service and usage type: October, November so far, Nov 11, Nov 12 | Cost Explorer |
| `record_types.json` | November so far by record type (Usage, Tax) | Cost Explorer |
| `lambda.json` | Each Lambda's memory, and its invocations and GB-seconds on Nov 11 and Nov 12 | Lambda + CloudWatch |

CloudWatch keeps 5-minute points for 63 days, so October's attribution can't be collected again after about
2026-12-17; `attempts.jsonl` keeps it.
