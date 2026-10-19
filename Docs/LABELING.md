# Window labels (M4-4)

**Owner:** Melisa · **Code:** `AnomalyPulse/labels/` · **Used by:** M4-5 (join), M5, M9

One label per one-minute window, derived from the run's manifest only. The rules and their order are in the
docstring of `labels/policy.py`; this page records the decisions and the file format.

```bash
cd AnomalyPulse
python -m labels --manifest path/to/manifest.json                      # print one run's windows
python -m labels --dataset v1 --bucket <bucket>                        # S3, as --profile sim
python -m labels --dataset m4-1-validation --root ~/mirror             # a local copy of the layout
```

**Only `v1`'s labels are stored in S3** (`dataset.DEFAULT_DATASET`, the dataset the integrity controls
guard). `--bucket` with any other dataset is refused, and so is any S3 write outside `datasets/v1/tier2/labels/`.
A validation dataset is labeled from a local copy of its manifests, and its labels stay local:

```bash
aws s3 sync s3://<bucket>/datasets/m4-1-validation/tier2/ ~/mirror/datasets/m4-1-validation/tier2/ \
  --exclude "*" --include experiments.jsonl --include "manifests/*" --profile dev
```

## Decisions

| Question | Decision |
|---|---|
| Window key | The UTC minute the window starts, `YYYY-MM-DDTHH:MM:00.000Z`: the same `window_start` as M4-5's `windows.request_window()`. Joined on (`experiment_id`, `window_start`). |
| Activity threshold | A fault labels a window when it is active **≥ 30 s** of it (exactly 30 s counts). |
| `CASCADING_FAILURE` | Two or more counted faults active together ≥ 30 s of the window, from `cascade.multi_fault_windows()`. Before that, the window is labeled by the single fault. Every run is held to M3's concurrency timeline (`policy._verify`), a guard on the rule; the check on timing is `test_run_kinds.py` (Validation, below). |
| `retry-amplification` | **Not counted as a fault** (`exclude_injectors=("retry-amplification",)`). It fails nothing; it re-sends what another fault failed, so payment failure + retries looks like more payment failure. A window in which only retries are active for ≥ 30 s is excluded (`uncounted_fault`). |
| DB-throttling onset | Active from `observed.label_from`. A window that overlaps `[active_from, label_from)` **at all** is excluded (`db_throttling_onset`), so the surge never sits inside a labeled window. A run with `label_from` null is not labeled (M4-1 reruns it). |
| Recovery | A window with under 30 s of fault that overlaps recovery is excluded (`recovery`). A last fault window with ≥ 30 s of fault keeps the fault's label. A NORMAL run has no recovery: its whole length is baseline. |
| Partial and startup windows | Excluded: a window not wholly inside `[started_at, ended_at)` (`partial_window`, the first and last), and any window overlapping the run's first 60 s (`startup`). What is left depends on the second the run starts at ("Windows per run", below). |
| Confounders (test set) | Two faults in a run whose manifest is not `CASCADING_FAILURE`: their overlap takes the manifest's `incident_type`, never `CASCADING_FAILURE`. |
| Ties | Two single faults of different types each ≥ 30 s with < 30 s of overlap: the type with more active time, then the one listed first in the manifest. |

## Windows per run

A 10-minute run has 11 windows: 2 partial and 1 startup, all excluded, and 8 that split by the second of the
minute the run starts at, because the fault's edges fall at that second too. DB throttling's onset exclusion
takes the window its surge starts in and the one holding `label_from` (~48 s later), or one window when both
fall in it:

| Run starts at | Single fault: NORMAL · fault · recovery | DB throttling: NORMAL · onset · labeled · recovery |
|---|---|---|
| :00–:11 | 1 · 5 · 2 | 1 · 1 · 4 · 2 |
| :12–:29 | 1 · 5 · 2 | 1 · 2 · 3 · 2 |
| :31–:59 (M4-1's runs, at about :59) | 2 · 5 · 1 | 1 · 2 · 4 · 1 |

A start at exactly :30.000 gives a single fault 1 · 6 · 1: both edge windows hold 30 s of fault, and 30 s
counts. A cascade's windows split the same way, by its first stage. The DB rows assume `label_from` 48 s in,
as on the runner (47–48 s), so the :12 boundary can move by a second.

The test set's 2-minute DB-throttling fault labels 72 s (from `label_from` to its end), so it keeps one
labeled window, or **none** when `label_from` falls in a minute's first 18 s: about 3 runs in 10. The window
holding `label_from` is onset, and under half a window is left after it. **`run-batch` reruns such a run**
(decided 2026-10-19, for M9-1): it rejects a `db-throttling` run with under 30 s of the fault past the minute
holding `label_from` (`batch.labeled_past_onset_ms`), and `test_run_kinds.py` checks that it rejects exactly
the runs this labeler can't label. A 5-minute fault always leaves at least 3 windows, so training is unaffected.

## Validation (2026-10-18)

- **Every kind of run M4 generates**, built by the simulator's own code (`test_run_kinds.py`): single faults
  in train and test bands, the stress slice, 2- and 12-minute faults, every cascade template in train and
  test bands, and the confounder pairs, each at 20 seeds and start seconds. Every window is checked against
  what the activations say was active in it, worked out in the test, not through
  `cascade.multi_fault_windows()`: a `CASCADING_FAILURE` window has two faults for half of it, and a window
  that does is never labeled anything else. A labeler that started DB throttling at `active_from` fails it
  in all six kinds with throttling. M3's timeline guard can't see that, because that timeline starts
  throttling at `active_from` too.
- **Real runs:** all 20 accepted runs in `m4-1-validation` (11: NORMAL, every single fault, one cascade) and
  `m4-3-validation` (9 cascades: all four templates, throttling before and after the spike), labeled from a
  local copy. Every run was labeled, its windows split as the table above says for its start second, and every
  window passes the same check. Totals: `m4-1-validation` 32 NORMAL, 11 TRAFFIC_SPIKE, 8 DATABASE_THROTTLING,
  10 LAMBDA_DEGRADATION, 10 DEPENDENCY_FAILURE, 4 CASCADING_FAILURE; `m4-3-validation` 12 NORMAL,
  1 TRAFFIC_SPIKE, 35 CASCADING_FAILURE.

## Files (Tier-2)

| Path | Contents |
|---|---|
| `tier2/labels/experiment_id=<id>/labels.jsonl` | One line per window of the run: `window_start`, `label` (null when excluded), `included`, `excluded_reason`, and the evidence (`fault_ms` per fault, `multi_fault_ms`, `recovery_ms`). |
| `tier2/labels/labeling_log.jsonl` | One line per experiment, all of one shape: label counts, exclusion counts, every excluded window with its reason, and `error` (null, or why the run could not be labeled; such a run has no windows). |

Excluded windows stay in the labels file so the exclusion is logged and M4-5 can see which windows are not
training rows. A training row is a feature row whose label row has `included: true`.

## For M4-5

- Drop `partial_window` rows from the matrix before computing temporal features: a 0.6 s window's
  `request_rate` is `count / 60`, so its `_change` into the next window is a false jump.
- Rolling features look back two windows, so a labeled window can carry values from an excluded one (the
  first labeled DB-throttling window holds the surge's p95 and 5xx in its `_roll_mean_3`). Either null the
  temporal features that draw on an excluded window, or accept it and say so in the report.