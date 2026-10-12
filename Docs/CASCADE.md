# Cascading failure (M3-5)

A cascade is one run with several faults whose windows start one after another and overlap.
It is a `runner.Plan`, so the lock, the manifest and the teardown are the framework's.

```bash
cd AnomalyPulse
python -m simulator start-cascade --spec simulator/cascades/example.json --seed 7 --dry-run
python -m simulator start-cascade --spec simulator/cascades/<your-spec>.json
```

`--dry-run` prints the manifest the run would start with and the planned concurrency (no AWS).
The same `--seed` gives the same params and the same onset jitter.

## Spec

`baseline_s`, `cascade_s` (first onset to last window closing), `recovery_s`, and `stages` in the
order they begin. A stage: `injector`, `intensity`, `band`, `target`, `onset_delay_s` (after the
previous stage begins), `onset_jitter_s` (seeded, +/-), `duration_s` (default: until the cascade
ends). Unknown keys are errors. Magnitudes come from bands in the spec, never from code.

## Retry amplification

`retry-amplification` (parameter `retries_per_failure`) switches on retries in the workload:
a request that times out is sent again that many times on average, so the retries follow the
application's real timeouts. It needs `set_retry_policy(retries_per_failure)` on the workload
(optionally `retry_stats()`, recorded as `observed`). Agree the method names with M3-2.

## Which windows are multi-fault

The manifest's `activations` already hold every window, so nothing extra is stored. Derive it:

```python
from simulator import cascade
cascade.concurrency_timeline(manifest)   # segments with count, faults, injectors
cascade.multi_fault_windows(manifest)    # only the stretches with 2+ active
cascade.multi_fault_windows(manifest, exclude_injectors=("retry-amplification",))
```

Use only COMPLETED manifests. Windows before the second fault begins have one fault active and
are labeled by it. Decide with M4 whether retry amplification counts as a fault for this
(it adds load; it is not a service failure) and pass `exclude_injectors` if not.

## Cost and teardown

- `build_plan` refuses a run longer than 30 minutes (baseline + cascade + recovery); `--max-run`
  raises it on purpose. The workload sends traffic the whole run, so length is cost.
- Ctrl-C or a failure: the runner stops every injector (retries go back to 0), deletes the
  experiment's fault-config items, writes FAILED/ABORTED and releases the lock.
- A killed process: `python -m simulator cleanup` (add `--force` only if the run is dead).
