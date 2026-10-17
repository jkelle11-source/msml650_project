# Window labels (M4-4)

**Owner:** Melisa · **Code:** `AnomalyPulse/labels/` · **Used by:** M4-5 (join), M5, M9

One label per one-minute window, derived from the run's manifest only. The rules and their order are in the
docstring of `labels/policy.py`; this page records the decisions and the file format.

```bash
cd AnomalyPulse
python -m labels --manifest path/to/manifest.json                      # print one run's windows
python -m labels --dataset m4-1-validation --bucket <bucket>           # S3, as --profile sim
python -m labels --dataset v1 --root ~/mirror                          # a local copy of the layout
```

## Decisions

| Question | Decision |
|---|---|
| Window key | The UTC minute the window starts, `YYYY-MM-DDTHH:MM:00.000Z`: the same `window_start` as M4-5's `windows.request_window()`. Joined on (`experiment_id`, `window_start`). |
| Activity threshold | A fault labels a window when it is active **≥ 30 s** of it (exactly 30 s counts). |
| `CASCADING_FAILURE` | Two or more counted faults active together ≥ 30 s of the window, from `cascade.multi_fault_windows()`. Before that, the window is labeled by the single fault. Every run is checked against M3's concurrency timeline: a `CASCADING_FAILURE` window with under 30 s of overlap there fails the run. |
| `retry-amplification` | **Not counted as a fault** (`exclude_injectors=("retry-amplification",)`). It fails nothing; it re-sends what another fault failed, so payment failure + retries looks like more payment failure. A window in which only retries are active for ≥ 30 s is excluded (`uncounted_fault`). |
| DB-throttling onset | Active from `observed.label_from`. A window that overlaps `[active_from, label_from)` **at all** is excluded (`db_throttling_onset`), so the surge never sits inside a labeled window. A run with `label_from` null is not labeled (M4-1 reruns it). |
| Recovery | A window with under 30 s of fault that overlaps recovery is excluded (`recovery`). A last fault window with ≥ 30 s of fault keeps the fault's label. A NORMAL run has no recovery: its whole length is baseline. |
| Partial and startup windows | Excluded: a window not wholly inside `[started_at, ended_at)` (`partial_window`, the first and last), and any window overlapping the run's first 60 s (`startup`). On M4's 10-minute runs, which start at about :59, that leaves 2 NORMAL baseline windows and 5 fault windows (4 for DB throttling). |
| Confounders (test set) | Two faults in a run whose manifest is not `CASCADING_FAILURE`: their overlap takes the manifest's `incident_type`, never `CASCADING_FAILURE`. |
| Ties | Two single faults of different types each ≥ 30 s with < 30 s of overlap: the type with more active time, then the one listed first in the manifest. |

## Files (Tier-2)

| Path | Contents |
|---|---|
| `tier2/labels/experiment_id=<id>/labels.jsonl` | One line per window of the run: `window_start`, `label` (null when excluded), `included`, `excluded_reason`, and the evidence (`fault_ms` per fault, `multi_fault_ms`, `recovery_ms`). |
| `tier2/labels/labeling_log.jsonl` | One line per experiment: label counts, exclusion counts and every excluded window with its reason; a last line lists runs that could not be labeled. |

Excluded windows stay in the labels file so the exclusion is logged and M4-5 can see which windows are not
training rows. A training row is a feature row whose label row has `included: true`.

## For M4-5

- Drop `partial_window` rows from the matrix before computing temporal features: a 0.6 s window's
  `request_rate` is `count / 60`, so its `_change` into the next window is a false jump.
- Rolling features look back two windows, so a labeled window can carry values from an excluded one (the
  first labeled DB-throttling window holds the surge's p95 and 5xx in its `_roll_mean_3`). Either null the
  temporal features that draw on an excluded window, or accept it and say so in the report.