# Dataset v1 — Test Set and Stress Slice (M9-1)

**Owner:** Josh · **Generator:** `AnomalyPulse/simulator/build_queues.py` (test seed `20261112`) ·
**Queues:** `AnomalyPulse/simulator/queues/v1-test-1..3.json`, `v1-stress.json`, and `m9-1-validation.json` to
run first · **Tests:** `AnomalyPulse/simulator/test_build_test_queues.py`, `AnomalyPulse/ml/test_train_guard.py`

Experiments 701–1050 of dataset `v1` (the test set) and 1051–1070 (the stress slice), generated on the runner
host after training, from M4-3's test and stress bands with the diversity protocol
([`EVALUATION_INTEGRITY.md`](EVALUATION_INTEGRITY.md)). Training and validation are
[`TRAINING_SET.md`](TRAINING_SET.md). How batches, the registry and the export work is in
[`DATASET.md`](DATASET.md).

---

## The queues

| Slots | Phase | Queue file | Blocks | Runs |
|---|---|---|---|---|
| 701–840 | `test` | `v1-test-1.json` | 2 of 70 | ~23.6 h |
| 841–980 | `test` | `v1-test-2.json` | 2 of 70 | ~23.6 h |
| 981–1050 | `test` | `v1-test-3.json` | 1 of 70 | ~11.8 h |
| 1051–1070 | `stress` | `v1-stress.json` | 1 of 20 | ~3.3 h |

About 62 hours in all, plus reruns, each batch's export, and the validation queue first (2.6 h). The test
set follows training's rules ("Building a queue"), with its own seed, so the training queues are unchanged
(`build_queues --check` rebuilds all nine files):

- **Blocks of 70** holding exactly `integrity.TEST_BLOCK`: 12 NORMAL, 12 traffic spike, 12 DB throttling,
  9 Lambda degradation, 9 payment failure, 6 cascades, 6 confounders, 4 varied-duration runs, shuffled.
- **Every test run in the shifted endpoint mix** `[40, 25, 15, 10, 10]`. The stress slice keeps the default.
- **Test bands half and half:** 30/30 for spikes and throttling; Lambda 23/22 and payment 22/23 (the extra
  run in opposite bands). Lambda's four targets take turns within each band (11 or 12 each).
- **Cascades** cycle the four templates (8/8/7/7), each going through its stages' band combinations, so each
  stage is within one run of half and half.
- **Confounders** cycle the four pairs (8/8/7/7), each labeled as its first fault, never `CASCADING_FAILURE`.
  Every pair goes through all four band combinations of its two faults.
- **Varied durations:** 5 each of traffic spike, throttling, Lambda degradation and payment failure; 10 of
  2 minutes and 10 of 12, 10 in each band; every injector has each duration in each band.
- **The stress slice:** 5 runs of each injector in its stress band (spike 10–12×, Lambda +1,500–2,000 ms,
  throttling 6–8×, payment 5,000–5,500 ms), one shuffled block, Lambda targets in turn.

| Class | Test (701–1050) |
|---|---|
| NORMAL | 60 |
| TRAFFIC_SPIKE | 60 (+5 varied, +16 confounders as first fault) |
| DATABASE_THROTTLING | 60 (+5 varied, +7 confounders) |
| LAMBDA_DEGRADATION | 45 (+5 varied, +7 confounders) |
| DEPENDENCY_FAILURE | 45 (+5 varied) |
| CASCADING_FAILURE | 30 |
| Confounders | 30 |
| Varied duration | 20 |
| **Total** | **350** |

## Before the first test batch

1. **A training job refuses test runs on its own** (Jake, from M5-1). `ml/job/train.py`'s `load_runs`, which
   every job script uses, refuses any run whose id is dated after training's last day (`LAST_TRAINING_DAY`,
   2026-10-25): a run's id starts with its UTC start time, and the batch refuses a test run on a training day,
   so every run from slot 701 on is later. A launcher bug that put a test run in a channel stops the job
   before it reads a row.
2. **The system is the one training ran on.**
   ```bash
   aws cloudformation describe-stacks --stack-name anomalypulse-shared --profile <profile> \
       --query 'Stacks[0].LastUpdatedTime'        # 2026-10-16T03:17:23Z, from before v1 was pinned
   git diff --stat 0557121 -- AnomalyPulse/simulator/   # only the queue generator, its tests and queues
   ```
   The batch's own pin check covers the service functions, the metrics extractor and the Products table.
   **Checked 2026-11-12:** the stack still reads `2026-10-16T03:17:23Z`, and the diff holds only
   `build_queues.py` and its tests (with the new queue files). `v1`'s registry holds the 700 training and
   validation runs and nothing else: slots 1–700, the last generated 2026-10-25
   (`exp-20261025T102950Z-daf1`), which is `LAST_TRAINING_DAY`. After the validation batch, `batch.py`
   changed too, but only to set when a 2-minute throttling run starts (below, "Running it"); nothing it
   sends or measures.
3. **The validation queue** (`m9-1-validation`, never `v1`): the test-set kinds that have never run on the
   runner, each at its heaviest, every intensity 0.95 and every test fault in its upper band. That covers a
   12-minute fault of each injector, a 2-minute throttling fault, the four confounder pairs and each
   injector's stress band on its own: 13 runs, about 2.6 h. Every entry also passes the `v1` guard, so it
   runs what the test set runs. A rejected run is a finding to fix before `v1-test-1`, not a rerun to wait
   out. Its one, the 2-minute throttling fault, is run again with the fix as `m9-1-throttle-check` (its own
   dataset, the same entry): one run, about 8 minutes with the wait.
4. **Tell Jake** before each batch: one run at a time on the runner, and nothing else live while it goes.

## Running it

On the runner host, from the merged commit, one queue a day, as for training (`TRAINING_SET.md`, "Running
it"). Through `drive.sh` from a laptop (`AnomalyPulse/infra/runner`, `export AWS_PROFILE=<profile>`):

```bash
bash drive.sh start
bash drive.sh setup <merged commit>
bash drive.sh run m9-1-validation run-batch --queue simulator/queues/m9-1-validation.json
bash drive.sh log m9-1-validation
# then, on later days, in order:
bash drive.sh run v1-test-1 run-batch --queue simulator/queues/v1-test-1.json
bash drive.sh run v1-test-2 run-batch --queue simulator/queues/v1-test-2.json
bash drive.sh run v1-test-3 run-batch --queue simulator/queues/v1-test-3.json
bash drive.sh run v1-stress run-batch --queue simulator/queues/v1-stress.json
```

`run-batch` refuses a `v1` test batch on a day that already has training runs, and a training batch once any
test run exists. Unlike training's, the test queues' order isn't enforced: run them in slot order anyway, so
slot order is generation order. Each batch exports when it ends. After each one, from a laptop, run
`dataset-status` and `spot_check` (`TRAINING_SET.md`), and check the new runs' labels and features exist.
**Don't open their numbers:** the test set isn't looked at until M9-2.

- **A 2-minute throttling run waits up to a minute before it starts**, so its fault opens at :35–:50 of a
  minute and always leaves a labeled window (`Docs/LABELING.md`, "Windows per run"). Without the wait, a
  fault opening at :12–:29 leaves none, and its reruns open at the same second, since every run lasts
  whole minutes and starts as the one before ends: the validation batch's slot 705 opened at :26.969, then
  :27.356 (`batch.SHORT_THROTTLE_OPENS_S`). The rerun check stays in case `label_from` comes later than usual.
- **An exhausted slot stays empty:** record it below, and the class it leaves short.

## Status

| Queue | Batch | Started (UTC) | Accepted | Exhausted | Spot check | Labels + features |
|---|---|---|---|---|---|---|
| m9-1-validation | `batch-20261113T154822Z-a5c3` | 2026-11-13 15:48 (finished 18:37) | 12 of 13, in 15 runs (2 reruns) | 1: slot 705, the 2-minute throttling fault, opened at :23.964, :26.969 and :27.356, so no labeled window (fixed since: "Running it") | Passed | — |
| m9-1-throttle-check | `batch-20261113T184934Z-0f91` | 2026-11-13 18:49 (finished 19:06) | 1 of 1, in 1 run: the fault opened at :35.080, so it didn't need to wait | 0 | Passed | — |
| v1-test-1 | | | | | | |
| v1-test-2 | | | | | | |
| v1-test-3 | | | | | | |
| v1-stress | | | | | | |

## Definition of Done

- **Generated on a separate day:** every test and stress run's `generation_date` in `tier2/experiments.jsonl`
  is after 2026-10-25, and `integrity.temporal_split` accepts the whole registry.
- **Held-out bands, no overlap:** every test entry's bands are `BANDS[...]["test"]`, which `check_bands`
  proves interleave with training's; `run-batch` refuses a `v1` entry that breaks it.
- **The extreme slice tagged separately:** slots 1051–1070, phase `stress`, in their own queue.
- **Confounders and varied durations present and correctly labeled:** 30 confounders labeled as their first
  fault (none `CASCADING_FAILURE`) and 20 varied-duration runs, checked in the registry and labels after
  export.
- **Not inspected until training was done:** the registry dates each run, and no training job can read a
  test run (above). The models M9 scores were promoted before any test run existed: `incident_type`
  2026-10-29 18:21 UTC (`anomalypulse-hpo-261028173159-003-28af89d1`) and `anomaly_iforest` 2026-11-06 19:34
  UTC (`anomalypulse-train-iforest-20261031T224214Z`), read 2026-11-12. If either is promoted again, it must
  be from training's runs only, which the guard enforces.
