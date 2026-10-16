# AnomalyPulse Dataset — Batch Runner, Registry, S3 Layout & Runner Host

**Status:** built in M4-1 · **Owner:** Jake (infra) · **Used by:** M4-2 (generation), M4-3 (bands, split),
M4-4 (labels), M4-5 (features), M5, M9-1 (test set)

M4 generates its experiments in **batches** on one EC2 instance, and registers each one. This page is how
to run a batch, what a run must pass to be accepted, where everything lands in S3, and how to run the
instance. The simulator itself (injectors, manifests, one run) is in
[`SIMULATOR_INTERFACE.md`](SIMULATOR_INTERFACE.md).

| Artifact | Path |
|---|---|
| Batch runner, queue format, run checks | `AnomalyPulse/simulator/batch.py` |
| S3 layout and the registry | `AnomalyPulse/simulator/dataset.py` |
| Export into the layout | `AnomalyPulse/simulator/export.py` |
| Frozen-configuration check | `AnomalyPulse/simulator/stack_config.py` |
| Queues | `AnomalyPulse/simulator/queues/` |
| Runner host stack, setup, tmux helper | `AnomalyPulse/infra/runner/` |

---

## The run shape (decided 2026-10-15)

**Every M4 run is 10 minutes: 3 minutes of baseline, a 5-minute fault, 2 minutes of recovery**, at 20 rps.
700 runs take about 117 hours back to back (15-minute runs would take 175).

- The fault keeps five one-minute windows. A `db-throttling` fault is labeled from `observed.label_from`,
  about 47 s in (M4-1's validation), which leaves four.
- Dropping each run's first minute (M4-4) leaves two NORMAL windows of baseline per run.
- Recovery windows are excluded from training anyway (plan §3). Two minutes lets the system settle, and the
  next run's dropped first minute adds a third.

A cascade fits the same 10 minutes (`baseline_s` 180, `cascade_s` 300, `recovery_s` 120). The test set's
varied-duration experiments (2- and 12-minute incidents) change only `duration_s`.

## Queue files

A queue is a JSON file naming the dataset and listing experiments in the order to run them:

```json
{
  "dataset": "v1",
  "experiments": [
    {"slot": 1, "phase": "train", "seed": 4101,
     "incident": {"injector": "noop", "baseline_s": 180, "duration_s": 300, "recovery_s": 120}},
    {"slot": 2, "phase": "train", "seed": 4102,
     "incident": {"injector": "traffic-spike", "intensity": 0.5,
                  "band": {"parameter": "multiplier", "low": 2, "high": 4},
                  "baseline_s": 180, "duration_s": 300, "recovery_s": 120}},
    {"slot": 641, "phase": "val", "seed": 4641,
     "cascade": {"baseline_s": 180, "cascade_s": 300, "recovery_s": 120, "stages": ["..."]}},
    {"slot": 905, "phase": "test", "seed": 4905, "mix": [40, 25, 15, 10, 10],
     "plan": {"incident_type": "TRAFFIC_SPIKE", "recovery_s": 120, "faults": [
       {"injector": "traffic-spike", "offset_s": 180, "duration_s": 300, "intensity": 0.5,
        "band": {"parameter": "multiplier", "low": 4, "high": 6}},
       {"injector": "lambda-degradation", "target": "order-service", "offset_s": 180, "duration_s": 300,
        "intensity": 0.5, "band": {"parameter": "added_latency_ms", "low": 500, "high": 800}}]}}
  ]
}
```

| Key | Meaning |
|---|---|
| `slot` | The experiment's number, 1–1000 (plan §17). One number is one experiment: its spec can't change once it is registered. |
| `phase` | `train` for 1–560, `val` for 561–700, `test` or `stress` (the extreme-extrapolation slice) from 701. Anything else is refused. |
| `seed` | The run's seed. A rerun uses it again, so a rerun is the same experiment. |
| `incident` | One fault, as `start-incident` takes it: `injector`, `intensity`, `band`, `target`, `baseline_s`, `duration_s`, `recovery_s`. |
| `cascade` | A cascade spec, as `start-cascade` takes it (`Docs/CASCADE.md`). |
| `plan` | Any other plan: `incident_type`, `faults` (each `injector`, `offset_s`, `duration_s`, `intensity`, `band`, `target`) and `recovery_s`. A confounder is this: two faults, labeled with one type. |
| `mix` | Optional endpoint weights (the test set's shifted mix). |

There is no `rps`: every run is sent at 20 rps. Generating a queue from M4-3's bands is M4-2's job; the
queue file is the record of what was asked for. `run-batch --dry-run` checks a queue without AWS: each
entry is resolved exactly as its run would be.

## Running a batch

On the runner host, as `ec2-user` (setup below):

```bash
bash ~/msml650_project/AnomalyPulse/infra/runner/in-tmux.sh batch \
    run-batch --queue simulator/queues/<queue>.json
tmux attach -t batch            # Ctrl-b d to detach; the batch keeps running
```

Anywhere with the code:

```bash
cd AnomalyPulse
python -m simulator run-batch --queue simulator/queues/<queue>.json --dry-run   # no AWS
python -m simulator check-stack                    # the frozen-configuration check alone
python -m simulator dataset-status --dataset v1    # what the registry holds
```

**Resume by running the same command again.** Accepted experiments are skipped; a run still waiting for its
CloudWatch check is finished first; the rest run in queue order.

### Before the first run, the batch refuses to start unless

- **The stack is M4's frozen configuration:** all four service functions at 1,769 MB, Products provisioned
  at 200 RCU, and one `Scan` of Products costing 0.5 RCU. A partition change would raise that cost, and with
  it every `GET /products`' read cost.
- **The stack and the runner are the ones the dataset is pinned to.** A dataset's first batch records the
  stack's configuration (every function's code hash, layers, runtime version, memory and timeout; the
  table's capacity) and the runner's instance id in `pins.json`. A later batch that sees any difference
  (someone deployed, or the batch is on a laptop) is refused with the list of differences. `--repin` accepts
  them, and the batch record says it did. Use it only for a change you meant.
- **No experiment holds the lock.**
- **The queue's slots are new, or registered with the same spec.**
- **Training comes before the test set, on different days.** A batch is either train/val or test/stress, never
  both. A train/val batch is refused once any test experiment is accepted. A test/stress batch is refused
  on any UTC day on or before the last training run's day (plan §6b, Control 2).

### Every run is judged before it is accepted

| Check | Rejected when |
|---|---|
| The run | It didn't end `COMPLETED` (a `FAILED` run includes `FellBehind`), or wasn't at 20 rps. |
| The client (`Workload.outcomes()`) | Any request was dropped, or more than 0.1% got no response (`error`). A manifest records the workload's settings, not what it sent, so a network drop would otherwise leave a `COMPLETED` run with a gap. |
| `db-throttling` | `observed.achieved_fraction` below 0.97; `observed.label_from` null (the bank was never seen spent); a steady throttle rate more than 0.1 from `1 − 1/offered_ratio` (the window ran on burst capacity, below); or `observed.steady_peak_served_per_s` above 1.25× capacity (DynamoDB served more than a spent bank over a minute after `label_from`: burst came back). |
| Products throttling (CloudWatch) | Any `ThrottledRequests` (summed over operations) or `ReadThrottleEvents` on Products in the run's minutes, except minutes that overlap a `db-throttling` window. |

The CloudWatch check waits 5 minutes after a run ends, for the metrics to settle. It doesn't hold the
batch up: it is done during the next run, and the batch waits only after its last run.

A rejected run is **rerun with the same seed**, at the end of the queue, up to 3 times in all
(`--max-attempts`); then the slot is `EXHAUSTED` and the batch carries on. **Three rejected runs in a row
stop the batch**: that is a setup problem, not three bad runs. A held lock (someone else's run) and Ctrl-C
also stop it. Exit code 0 means every slot in the queue was accepted.

When the batch ends it waits for the shipper (10 minutes after the last run) and exports the dataset
(below). `--no-export` skips that.

## The registry

The registry is in S3, beside the data: one JSON record per slot, written as the batch goes.

| Field | Meaning |
|---|---|
| `slot`, `phase`, `incident_type`, `spec` | What the queue asked for. |
| `status` | `PENDING`, `CHECKING` (its last run waits for CloudWatch), `ACCEPTED` or `EXHAUSTED`. |
| `experiment_id` | The accepted run. Its manifest is the ground truth. |
| `generated_at`, `generation_date` | When the accepted run started, and its UTC date. **The temporal split keys off `generation_date`** (M4-3). |
| `ended_at`, `batch_id` | When it ended; the batch that accepted it. |
| `attempts` | Every run of the slot: `experiment_id`, `batch_id`, `status`, `verdict` (`accepted`, `rejected`, `checking`), `reasons`, `client` (the outcomes), `products_throttling`. |

Each batch also writes a record under `registry/batches/`: the queue file's hash, the stack configuration,
the runner (hostname, Python, EC2 instance id, type and AMI), the git commit and whether the simulator's
files differed from it, and which slots ended in which status.

**The registry holds only experiments M4 launched.** Nothing else reads as part of the dataset: not the
manifests written before M4, not the capacity checks, not runs from the CLI.

## The S3 layout

Everything is under `datasets/<name>/` in the telemetry bucket. The main dataset is `v1`. That prefix has no
lifecycle rule; `raw/` and `metrics/` expire after 90 days, so the export below is what keeps the data.

```text
datasets/<name>/
  tier1/                                   observable: may feed features
    requests/experiment_id=<id>/requests.jsonl
    metrics/experiment_id=<id>/metrics.jsonl
    windows/                               M4-4: one-minute windows
    features/                              M4-5: the feature matrix
  tier2/                                   ground truth: never a feature
    experiments.jsonl                      one line per accepted experiment
    manifests/experiment_id=<id>/manifest.json
    labels/                                M4-4: window labels
    registry/slots/<slot>.json
    registry/batches/<batch_id>.json
    registry/pins.json
```

**Tier-1 and Tier-2 are separate trees**, so code (or an IAM policy) can be given one and not the other.
Within an experiment, a window is keyed by **(`experiment_id`, window start)**: features under `tier1/` and
labels under `tier2/` join on that key. The slot number, phase and every band and parameter are Tier-2 and
stay in `tier2/`. `experiment_id` is a key for joining, never a column of the feature matrix.

### The export

`python -m simulator export --dataset <name>` (run-batch runs it when it ends) writes, for each accepted
experiment not yet exported:

| File | Contents |
|---|---|
| `requests.jsonl` | Every request record of the run, from `raw/`: `started_at <= timestamp < ended_at`, **deduplicated** by `(service, request_id)` (the shipper delivers at least once), sorted by time. Exactly the telemetry contract's fields. |
| `metrics.jsonl` | The metrics extractor's CloudWatch snapshots whose minute overlaps the run, one per line. A snapshot stamped `T` covers `[T − 1 min, T)`. |
| `manifest.json` (tier2) | The run's manifest, copied from `experiments/`. Written last, so an export cut short is redone. |
| `experiments.jsonl` (tier2) | Rewritten every export: `slot`, `phase`, `incident_type`, `experiment_id`, `generation_date`, `generated_at`, `ended_at`, `batch_id`, `seed`. |

**The export refuses a raw record holding any field the field catalog doesn't mark correlation or Tier-1**
(`infra/shared/field_catalog.json`), so `tier1/` can't hold a near-label even if a service emitted one.
It is idempotent: `--force` re-exports. Don't export by hand while a batch runs on the same host: it competes
with the workload for the CPU.

Labeling and features (M4-4, M4-5) read `requests.jsonl` and `metrics.jsonl` for the time series, and the
manifest for the labels. Use only experiments in `experiments.jsonl`: every one is `COMPLETED` and passed
the checks above.

## The runner host

One `c7g.large` in us-east-2 (Amazon Linux 2023, arm64), stack `anomalypulse-runner`
(`infra/runner/template.yaml`). It runs every batch, training and test: a change of client partway
through would track the train/test split. It has no inbound port; it is reached through Session Manager.
Its role can only assume `anomalypulse-simulator`, which trusts it by name (`infra/shared/template.yaml`).
On the instance the simulator runs as `--profile sim`, as on a laptop, but the credentials come from the
instance and renew themselves: no SSO session to expire mid-batch.

| Step | Command |
|---|---|
| Create (once; the shared stack must already trust `anomalypulse-runner`) | `aws cloudformation deploy --stack-name anomalypulse-runner --template-file AnomalyPulse/infra/runner/template.yaml --capabilities CAPABILITY_NAMED_IAM --profile admin` |
| Instance id | `aws cloudformation describe-stacks --stack-name anomalypulse-runner --query "Stacks[0].Outputs" --profile admin` |
| Connect | `aws ssm start-session --target <instance-id> --profile admin` (needs the Session Manager plugin: `brew install --cask session-manager-plugin`), or EC2 console → Connect → Session Manager. Then `sudo -iu ec2-user`. |
| Set up, or move to another commit | `bash ~/msml650_project/AnomalyPulse/infra/runner/setup.sh <commit or branch>`. The first time: `git clone https://github.com/jkelle11-source/msml650_project.git ~/msml650_project` first. |
| Run | `bash ~/msml650_project/AnomalyPulse/infra/runner/in-tmux.sh <session> <simulator arguments>`; logs in `~/logs/`. |
| Stop between batches | `aws ec2 stop-instances --instance-ids <id> --profile admin` (`start-instances` to resume; the instance id stays, so the dataset's pin holds) |
| After M9-1 | `aws cloudformation delete-stack --stack-name anomalypulse-runner --profile admin` |

Run every batch from a commit, not from files you are editing: setup.sh checks out a commit detached, and
each batch records it.

**Cost:** about $0.07 an hour for the instance, $0.005 an hour for its public IPv4 address while running,
and about $1.30 a month for its 16 GB volume. 117 hours of training generation is about $9.
The runs themselves cost what `SIMULATOR_INTERFACE.md` says ("Teardown and cost").

## Validation (M4-1, 2026-10-16)

On the runner host (`c7g.large`), on the stack as deployed at 03:17 UTC (layer 18, runtime pinned).

**Capacity check** (`--created-by m4-capacity-check`, in no dataset), with `telemetry_report`:

| Run | Result |
|---|---|
| NORMAL, 10 min (`exp-20261016T025315Z-0096`) | 19.81 client rps landed, per-minute records steady (CV 0.018); nothing dropped, no errors, no 5xx; Product p50/p95 5/7 ms |
| 10× spike (`exp-20261016T030316Z-60d9`) | `achieved_rps` 200.5 (a laptop managed 200.04 in M3-2); 200.49 rps landed in the window; nothing dropped, no errors, no 5xx; Product p95 6–7 ms |

**Batch** `batch-20261016T031953Z-120b`: `simulator/queues/m4-1-validation.json` into dataset
`m4-1-validation` (never part of `v1`), commit `927ab57`. Unattended from 03:19 to 05:25 UTC, export
included. **All 11 runs were accepted at the first attempt**: no client errors, nothing dropped, no
Products throttling outside a `db-throttling` window.

| Slot | Experiment | What the landed telemetry shows |
|---|---|---|
| 1, 6 | NORMAL | steady 20 rps, no 5xx |
| 2, 7 | traffic spike 3×, 7× | `achieved_rps` 60.3, 139.7; no 5xx |
| 3, 8 | DB throttling 2.0×, 4.0× | **see below** |
| 4 | Lambda degradation, Order +350 ms | Order `lambda_duration_ms` p50 2 → 352 → 2 ms (baseline, fault, recovery) |
| 9 | Lambda degradation, Product +950 ms | Product `lambda_duration_ms` p50 3 → 953 → 3 ms |
| 5, 10 | payment failure 1,000 ms, 3,000 ms | Order p95 24 → 1,021 / 3,023 ms; 287 / 298 Order 5xx in the window |
| 11 | cascade: spike 3×, payment failure, retries 1.5 | 1,496 Order 5xx; 778 retries sent |

The export wrote `requests.jsonl` and `metrics.jsonl` for each run under `tier1/` (12,500–50,300 records;
44 snapshots, 4 services × 11 minutes), the manifests, registry and `experiments.jsonl` under `tier2/`.
Every record is inside its run, every field is in the catalog as correlation or Tier-1, and 94 duplicate
records the shipper delivered twice were dropped.

### DB throttling from the runner host: found and fixed

Both `db-throttling` runs passed the checks, and neither was the incident its label says. Slots 3 and 8
stay in this dataset as that record; neither is DB-throttling data.

| Minute of the window | 2.0× (`…033954Z-d548`): consumed RCU/s, throttled reads | 4.0× (`…042956Z-f85a`) |
|---|---|---|
| 1 | 103, 72 | 108, 30 |
| 2 | 405, 14 | 778, 142 |
| 3 | 408, 0 | 540, 1,663 |
| 4 | 408, 0 | 205, 3,806 |
| 5 | 408, 0 | 204, 3,893 |
| 6 (window ends) | 289, 467 | 187, 3,854 |

The surge ended on the first throttled read, about **3 s** into the window (`surge_s` 2.9 and 3.0). From
a laptop it always ended at ~33.7 s, when the burst bank ran out. From the runner an isolated early
throttle ended it with the bank still full. So at 2.0× the table served twice its capacity from burst
for the whole window, with almost no throttling, and Product's p95 didn't move (7 ms baseline, 6 ms in
the window). At 4.0× the bank lasted about 2 minutes; then Product's p95 reached 1.59 s. `label_from` is
5 s after that early throttle, so it marks the wrong moment.

**The batch now rejects such a run:** a `db-throttling` run whose steady throttle rate is more than 0.1
from `1 − 1/offered_ratio` (M3-3 measured within a point of it) is rerun. Two changes to when the surge
ends were tried and reverted (half of the last second's reads throttled; DynamoDB serving no more than
1.25× capacity for a second): both fired 3–9 s in, on a full bank, and that check rejected all six runs.

**Cause: the client's connections.** From the runner the injector's one boto3 client sent every call over
one to three connections (DynamoDB answers in ~6 ms), and over those DynamoDB spent burst at only
~950 RCU/s, in a seesaw: a second or two served in full, then a second or two refused. So a read was
refused within seconds of a full bank, and any rule judged over one second saw a spent bank. A laptop's
slower round trips spread the same calls over dozens of connections. Probes at 10× from a full bank
(120 s, no app traffic, 20:27–20:46 UTC): one client from the runner took ~80 s to spend the bank; the
laptop, and 16 clients from the runner, were served 2,000 RCU/s until it ran out at 33 s. The table, its
one partition and every throttle's reason (`TableReadProvisionedThroughputExceeded`) were the same
throughout. `Docs/DB_THROTTLING_INJECTOR.md` ("Connections") has the table.

**Fix** (`1f013f1`): the reads take turns over 16 clients; the surge ends when the reads DynamoDB served
it over the last 10 s average no more than 1.5× capacity, with `label_from` 5 s after that; and the batch
also rejects a run where DynamoDB served the injector more than 1.25× capacity over any minute after
`label_from` (burst came back, as it once did in a probe). The steady-throttle-rate check stays.

**Validated** in batch `batch-20261016T223858Z-ea22` (commit `3666cdb`, on the runner, 22:38–23:25 UTC),
at the training bands' edges and middles. All four runs were accepted at their first attempt in it.

| Slot | Experiment | Ratio | First throttle → bank seen spent | `label_from` | Steady throttle (expected) | Most served in a labeled minute | Product p95, baseline → labeled |
|---|---|---|---|---|---|---|---|
| 14 | `…225859Z-fbb1` | 1.5× | +32.9 s → +43.1 s | +48.1 s | 0.349 (0.350) | 196.7 RCU/s | 6 ms → 1,586 ms |
| 12 | `…223859Z-0550` | 2.0× | +33.7 s → +43.1 s | +48.1 s | 0.514 (0.506) | 199.6 RCU/s | 6 ms → 3,191 ms |
| 13 | `…224859Z-48f4` | 4.0× | +32.9 s → +42.1 s | +47.1 s | 0.757 (0.752) | 200.6 RCU/s | 6 ms → 1,594 ms |
| 15 | `…230900Z-3be8` | 4.5× | +32.9 s → +42.1 s | +47.1 s | 0.783 (0.780) | 202.1 RCU/s | 7 ms → 3,191 ms |

In every run CloudWatch shows the surge's minute at 1,172–1,188 RCU/s and every labeled minute at
197–206 (capacity): the bank stayed spent. `ReadThrottleEvents` in a labeled minute rose with the ratio,
about 1,400 at 1.5×, 2,100 at 2.0×, 4,500 at 4.0× and 5,000 at 4.5×. Product's p50 stayed at 5–7 ms,
and 0–5 requests a minute failed in the labeled stretch (22–35 in the surge's minute, which is excluded).

**Open for M4-3:** in every labeled minute Product's p95 sat on a step of the app's retry ladder, 1.59 s
(five retries) or 3.19 s (six), in no order by ratio: 1.59 s at 1.5× and 4.0×, 3.19 s at 2.0× and 4.5×.
From a laptop M3-3 measured 0.78 s at 1.75×, 1.59 s at 3.0× and 3.2 s at 5.5×. The labels are right
either way, and the throttle counts scale with the ratio; an analysis of the app's latency by band would
not see the ratio.
