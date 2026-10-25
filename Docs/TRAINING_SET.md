# Dataset v1 — Training and Validation Set (M4-2)

**Owner:** Josh · **Generator:** `AnomalyPulse/simulator/build_queues.py` (seed `20261018`) ·
**Queues:** `AnomalyPulse/simulator/queues/v1-*.json` · **Tests:** `AnomalyPulse/simulator/test_build_queues.py`

Experiments 1–700 of dataset `v1`, generated on the runner host from M4-3's training bands, following the
rules in [`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md) ("Building a queue"). How batches, the registry
and the export work is in [`DATASET.md`](DATASET.md).

---

## The train / validation cut

| Slots | Phase | Queue file | Blocks of 20 | Runs |
|---|---|---|---|---|
| 1–140 | `train` | `v1-train-1.json` | 0–6 | ~23.3 h |
| 141–280 | `train` | `v1-train-2.json` | 7–13 | ~23.3 h |
| 281–420 | `train` | `v1-train-3.json` | 14–20 | ~23.3 h |
| 421–560 | `train` | `v1-train-4.json` | 21–27 | ~23.3 h |
| 561–700 | `val` | `v1-val.json` | 28–34 | ~23.3 h |

**1–560 is training and 561–700 is validation** (for M5 hyperparameter tuning). The cut is by slot, and slot
order is run order, so validation is also the last-generated stretch of Phase 1. The same boundary is in
`dataset.TRAIN_LAST` (560) and `integrity.split_of()`; every entry's `phase` agrees with it, and the batch
runner refuses one that doesn't. Validation uses the training bands.

Each registry record and `tier2/experiments.jsonl` line carries `slot` and `phase`, so the split is read
from the data, not recomputed.

## What the queues hold

| Class | Train (1–560) | Val (561–700) | Bands, train / val |
|---|---|---|---|
| NORMAL | 112 | 28 | — |
| TRAFFIC_SPIKE | 112 | 28 | 2–4× / 6–8×: 56/56, 14/14 |
| DATABASE_THROTTLING | 112 | 28 | 1.5–2.5× / 3.5–4.5×: 56/56, 14/14 |
| LAMBDA_DEGRADATION | 84 | 21 | 200–500 / 800–1100 ms: 42/42, 11/10 |
| DEPENDENCY_FAILURE | 84 | 21 | 500–1500 / 2500–3500 ms: 42/42, 10/11 |
| CASCADING_FAILURE | 56 | 14 | per stage: 7/7 per template in training |

- Every 20 consecutive slots hold exactly 4/4/4/3/3/2 of the classes above, shuffled.
- Lambda degradation's targets (product, cart, order, payment) take turns within each band, the high band
  starting at order: 21 each in training, 10 or 11 of them in each band; 5 or 6 each in validation, 2 or 3
  in each band.
- Cascades cycle the four templates: 14 each in training, 4/4/3/3 in validation. A template's runs go
  through every combination of its stages' bands, in complementary pairs (all low and all high first),
  so each stage is 7/7 in training. The three-stage templates have all 8 combinations in training; the
  four-stage one (`throttle-slowdown-payment-retry`) has 14 of its 16, and validation the other two.
- Intensities are drawn by M4-3's builders from a per-slot RNG, within `INTENSITY_RANGE` (0.05–0.95).
- Every run is 10 minutes (180 s baseline, 300 s fault, 120 s recovery) at 20 rps in the default mix.
- No test or stress band appears; `run-batch --dry-run` runs M4-3's guard on every entry.

### The heaviest cascades, run on the runner (2026-10-19)

Going through every combination puts every template at all-high in training. M4-3's validation had run that
corner only for the two templates with a spike (`EVALUATION_INTEGRITY.md`, "Validation on the runner"), so
M4-2's review ran the other two there, every intensity 0.95: one batch into dataset `m4-2-validation`
(`batch-20261019T230019Z-b99d`, commit `90ff01a`, queue `simulator/queues/m4-2-validation.json`).

| Slot | Cascade | Result |
|---|---|---|
| 1 | throttling 4.45× → Product +1,085 ms → payment failure 3,450 ms → retries 1.975 | Accepted at the first attempt; 12,300 sent, none dropped; steady throttle rate 0.781 (0.777 expected); 360 retries |
| 2 | Order +1,085 ms → payment failure 3,450 ms → retries 1.975 | Accepted at the first attempt; 12,332 sent, none dropped; 426 retries |

- **The client:** the batch process's thread count topped out at 72 and 34 (one thread per open request,
  plus about 18 of the process's own and, in slot 1, the throttling injector's), against the 512 the client
  keeps open. Neither template has a spike, so traffic stays at 20 rps.
- **Lambda:** at most 86 functions ran at once across the account (Product 46, Order 29, Payment 23), and
  Lambda throttled nothing.
- **Latency:** Product's p95 was 2.9 s under throttling with the slowdown on (3.2 s in the minute
  `label_from` fell in); Order's 3.5 s in slot 1 and 4.8 s in slot 2, Payment's 3.5 s.
- `spot_check` passes both.

## Reproducing the queues

```bash
cd AnomalyPulse
python -m simulator.build_queues --check      # exit 0: the committed files are what seed 20261018 builds
python -m simulator.build_queues              # rewrite them (only before generation starts)
python -m simulator run-batch --queue simulator/queues/v1-train-1.json --dry-run
cd simulator && python -m pytest -q test_build_queues.py
```

The tests pin every count above, check the committed files match a rebuild, and pass each file through
`batch.load_queue`. **Don't change the seed or the generator once generation starts:** a slot's spec can't
change once it is registered, and the batch refuses a queue whose registered slots differ.

## Running it

On the runner host (`DATASET.md`, "The runner host"), from the merged commit, one queue a day:

```bash
bash ~/msml650_project/AnomalyPulse/infra/runner/setup.sh <merged commit>
bash ~/msml650_project/AnomalyPulse/infra/runner/in-tmux.sh v1-train-1 \
    run-batch --queue simulator/queues/v1-train-1.json
tmux attach -t v1-train-1            # Ctrl-b d to detach
```

Then `v1-train-2` … `v1-train-4`, then `v1-val`. **The queues run in slot order:** `run-batch` refuses a `v1`
training or validation queue until every slot before its first is accepted or exhausted. A batch that
stopped short (three rejected runs in a row, a held lock, Ctrl-C) is resumed by running the same command
again, before the next queue. Each batch exports when it ends, about 10 minutes after its last run.

After each batch exports, from a laptop, in `AnomalyPulse/`, in this order:

```bash
python -m simulator dataset-status --dataset v1
python -m simulator.spot_check --dataset v1 --profile dev    # every run in the newest batch's queue
python -m labels --dataset v1 --bucket <bucket>              # as --profile sim; relabels all of v1
python -m features --dataset v1 --bucket <bucket>            # then the feature matrix, from those labels
```

`spot_check` exits 0 only when every accepted run in the queue has all its telemetry: every schema field on
every record, the fields M2's contract fills not null in every record of a service, one Product, Cart or
Order record per request the client sent (within 0.1%), and one metrics snapshot per service for every
minute of the run, with every field the metrics extractor writes. `--batch <id>` checks an earlier batch's
queue, `--batch all` the whole dataset. Labels and features rewrite all of `v1` each time
(`Docs/LABELING.md`, `Docs/FEATURE_MATRIX.md`); check `tier1/features/feature_log.jsonl` for runs with an
error or a window missing its snapshot. Running them each day means a problem shows within a day, not after
the fifth.

**An exhausted slot stays empty.** A rejected run is rerun up to 3 times in all, at the end of its queue;
after that its slot is `EXHAUSTED`, and nothing runs it again: a registered slot can't change its spec, and
`run-batch` skips it. Record it in the table below and raise it before the next queue: `v1` is then a run
short of the counts above, in that run's class.

## Status

| Queue | Batch | Started (UTC) | Accepted | Exhausted | Spot check | Labels + features |
|---|---|---|---|---|---|---|
| v1-train-1 | `batch-20261020T003325Z-4b00` | 2026-10-20 00:33 (finished 23:59) | 140 of 140, in 140 runs (no reruns) | 0 | Passed | 905 training rows, 635 windows excluded; 0 errors |
| v1-train-2 | `batch-20261021T030239Z-0416` | 2026-10-21 03:02 (finished 10-22 02:38) | 140 of 140, in 141 runs (1 rerun) | 0 | Passed | 886 training rows, 654 windows excluded; 0 errors |
| v1-train-3 | `batch-20261022T033044Z-6a4f` | 2026-10-22 03:30 (finished 10-23 02:56) | 140 of 140, in 140 runs (no reruns) | 0 | Passed | 876 training rows, 665 windows excluded; 0 errors |
| v1-train-4 | `batch-20261023T051312Z-cbee` (`FAILED` at slot 542), then `batch-20261024T015359Z-2f9a` | 2026-10-23 05:13 (finished 10-24 05:09) | 140 of 140, in 141 runs (slot 542's first attempt interrupted; no reruns) | 0 | Passed | 886 training rows, 654 windows excluded; 0 errors |
| v1-val | `batch-20261024T110907Z-1bf3` | 2026-10-24 11:09 (finished 10-25 10:44) | 140 of 140, in 141 runs (1 rerun) | 0 | Passed | 885 training rows, 655 windows excluded; 0 errors |

**Done: 700 of 700 accepted, none exhausted,** generated 2026-10-20 to 25 UTC. The registry's class counts are
the queues' (112/112/112/84/84/56 in training, 28/28/28/21/21/14 in validation), and the labels give M5
4,438 training rows (labeled windows with features):

| Label | Train (1–560) | Val (561–700) |
|---|---|---|
| NORMAL | 1,492 | 375 |
| TRAFFIC_SPIKE | 574 | 144 |
| LAMBDA_DEGRADATION | 434 | 108 |
| DEPENDENCY_FAILURE | 420 | 105 |
| DATABASE_THROTTLING | 418 | 100 |
| CASCADING_FAILURE | 215 | 53 |
| **Total** | **3,553** | **885** |

NORMAL leads because every run contributes its two baseline minutes as well as its own label's windows.

**Labels + features** counts each batch's runs in the logs of the latest rebuild (`labeling_log.jsonl`,
`feature_log.jsonl`; both rewrite all of `v1`): training rows are labeled windows, each joined to its
features; excluded windows are the startup minute, partial windows, recovery and the DB-throttling onset;
errors are runs with an error in either log. No window has been missing a CloudWatch snapshot. Rows per
batch vary with each run's start second and where `label_from` falls (`LABELING.md`, "Windows per run").

**`v1-train-4` took two batches.** A Ctrl+C in its tmux window stopped it during slot 542. Ctrl+C there also
stops the `tee` that writes the log, so the batch's next print failed and it ended `FAILED` with a broken
pipe, not `ABORTED`. `python -m simulator cleanup`, then the same `in-tmux.sh` command, resumed it: slot 541's
CloudWatch check, slot 542 again with the same seed, then 543–560. The interrupted attempt is never
exported. To watch a batch, use `tail -f ~/logs/<session>-*.log`; to leave an attached tmux window, press
Ctrl-b then d, never Ctrl+C.
