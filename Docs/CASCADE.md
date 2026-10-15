# Cascading failure (M3-5)

A cascade is one run with several faults whose windows start one after another and overlap.
It is a `runner.Plan`, so the lock, the manifest and the teardown are the framework's.

```bash
cd AnomalyPulse
python -m simulator start-cascade --spec simulator/cascades/example.json --seed 7 --dry-run
caffeinate -is python -m simulator start-cascade --spec simulator/cascades/<your-spec>.json
```

Run it from `AnomalyPulse/`, under `caffeinate -is` like every real run (a sleeping laptop fails
the run). `--dry-run` prints the manifest the run would start with and the planned concurrency
(no AWS). The same `--seed` gives the same params and the same onset jitter.

Like `start-incident`, it sends baseline traffic for the whole run: 20 rps in the plan's mix by
default, the rate M4 uses for every run. `--rps`, `--users` and `--mix` change it. The traffic
spike and retry amplification act through that traffic, so a cascade with either needs it.
The example spec runs 20 minutes (300 s baseline, 600 s cascade, 300 s recovery) and uses
`db-throttling` (M3-3).

## Spec

`baseline_s`, `cascade_s` (first onset to last window closing), `recovery_s`, and `stages` in the
order they begin. A stage: `injector`, `intensity`, `band`, `target`, `onset_delay_s` (after the
previous stage begins), `onset_jitter_s` (seeded, +/-), `duration_s` (default: until the cascade
ends). Unknown keys are errors. Magnitudes come from bands in the spec, never from code.

## Retry amplification

`retry-amplification` (parameter `retries_per_failure`) switches on retries in the workload
(`Workload.set_retry_policy()`) for its window:

- A **failure** is a 5xx or no response at all (a timeout, a reset). A 4xx is never retried.
- Each failed request gets `retries_per_failure` retries on average: 2.5 means two, and a third
  half of the time. Each goes out one second (`retry_delay_s`) after the attempt before it
  failed, and a success ends them.
- Retries are extra arrivals on top of the open-loop rate, so the slower the services get, the
  more traffic there is. They count as sent requests, and they draw from their own RNG, so the
  baseline request sequence does not change.
- When the window closes, retries still waiting are cancelled.
- `observed` is `retry_stats()`: `requests_retried`, `retries_sent`, `retries_dropped` (over
  `max_in_flight`), `retries_cancelled`.

Its label is `CASCADING_FAILURE`, and a `CASCADING_FAILURE` manifest needs two or more faults
active at once, so a run of `retry-amplification` alone is refused. Its bands are not in the
plan's §6b table yet; M4-3 defines them.

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
