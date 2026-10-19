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
- Lambda degradation's targets take turns (product, cart, order, payment): 21 each in training.
- Cascades cycle the four templates: 14 each in training, 4/4/3/3 in validation. A template's runs go
  through every combination of its stages' bands, in complementary pairs (all low and all high first),
  so each stage is 7/7 in training. The three-stage templates have all 8 combinations in training; the
  four-stage one (`throttle-slowdown-payment-retry`) has 14 of its 16, and validation the other two.
- Intensities are drawn by M4-3's builders from a per-slot RNG, within `INTENSITY_RANGE` (0.05–0.95).
- Every run is 10 minutes (180 s baseline, 300 s fault, 120 s recovery) at 20 rps in the default mix.
- No test or stress band appears; `run-batch --dry-run` runs M4-3's guard on every entry.

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

Then `v1-train-2` … `v1-train-4`, then `v1-val`. Running the same command again resumes a batch. After each
batch exports, from a laptop:

```bash
python -m simulator dataset-status --dataset v1
python -m simulator.spot_check --dataset v1 --profile <your-profile>
```

`spot_check` samples two accepted experiments per class and reports any telemetry field that is null in
every record of a service that should fill it (M2's contract), and any metrics block or field missing.

## Status

| Queue | Batch | Started (UTC) | Accepted | Exhausted | Spot check |
|---|---|---|---|---|---|
| v1-train-1 | | | | | |
| v1-train-2 | | | | | |
| v1-train-3 | | | | | |
| v1-train-4 | | | | | |
| v1-val | | | | | |
