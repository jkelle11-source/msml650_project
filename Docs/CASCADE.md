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

### It needs failures to amplify

Retries answer to failures, so `retry-amplification` does nothing unless a stage active at the same
time makes requests fail. In the validation run below, 2.1× DB throttling and a 380 ms Order
slowdown failed only 19 of 50,490 requests, so the stage retried **1** request and its phase looks
like the one before it. Throttling degrades as latency first (Product's retries absorb it):

| `db-throttling` ratio | Product 5xx (validation, 200 RCU) |
|---|---|
| 1.75×, 3.0× | 0 |
| 5.5× | ~1% |
| 8.0× (stress) | ~8–11% |

So **a spec that uses `retry-amplification` must overlap it with a stage that fails requests**:

- **`payment-failure`**, which fails every `POST /orders` in its window at any band. This is the
  only choice for **training** cascades: they must use training bands, and DB throttling's
  training bands stop at 4.5×, where Product barely fails.
- **`db-throttling` at 5.5× or above** fails ~1% of Product requests and more beyond; usable in
  test-band and stress-slice cascades only.

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

## Validation (2026-10-15)

`exp-20261015T181216Z-ab5c`: `simulator/cascades/example.json`, seed 301, 20 rps, on the deployed
stack (200 RCU `anomalypulse-products-v2`), from a laptop under `caffeinate -is`. `COMPLETED`; the lock
was released and no fault items were left.

| Stage | Window (UTC) | Measured |
|---|---|---|
| `traffic-spike` 3.2× | 18:17:16–18:27:16 | 64.19 rps sent, 0 dropped |
| `db-throttling` 2.1× | 18:18:34–18:27:16 | surge ended on a throttle after 33.5 s; 420 reads/s (`achieved_fraction` 1.00); 56.5% steady throttle rate; the app's `GetItem` 2,338 and `Scan` 11,545 throttled requests |
| `lambda-degradation` 380 ms, Order | 18:19:33–18:27:16 | (see Order below) |
| `retry-amplification` 1.7 | 18:20:23–18:27:16 | 1 request retried (see "It needs failures to amplify") |

Landed telemetry, by phase (`python -m simulator.telemetry_report`):

| Phase | Product rps, p95 | Cart rps | Order rps, p50/p95 | Payment rps |
|---|---|---|---|---|
| baseline | 14.9, 8 ms | 2.1 | 2.9, 4/26 ms | 0.9 |
| + spike | 47.8, 7 ms | 6.3 | 10.2, 4/24 ms | 3.3 |
| + db-throttling | 48.1, 782 ms (0.6% 5xx) | 7.1 | 9.8, 4/23 ms | 3.4 |
| + lambda-degradation | 47.7, 1,586 ms | 6.9 | 9.3, 454/474 ms | 3.4 |
| + retry-amplification | 48.2, 787 ms | 6.4 | 9.5, 453/472 ms | 3.2 |
| recovery | 15.0, 7 ms | 2.1 | 3.0, 4/25 ms | 1.0 |

M3-5's Definition of Done:

- [x] Correlated symptoms across ≥3 services: every service carries the spike's 3.2×, Product's
  latency rises with the throttling and Order's with the degradation, and all of it recovers.
- [x] The concurrency timeline is recorded: the manifest's activations, the CLI's printout and the
  telemetry report's phases cut at the same boundaries (also a unit test).
- [x] Reproducible and cost-bounded: seeded params and onsets, a 30-minute cap, the framework's teardown.

## Open for M4

- **Cascade specs** (M4-3): which stages, bands and onset delays the 70 training and 30 test
  cascades use. Any spec with `retry-amplification` overlaps it with a failing stage (above).
- **Bands for `retries_per_failure`** (M4-3), interleaved like the plan's other bands.
- **Does `retry-amplification` count as a fault** for "two or more faults active" (M4-4)? It adds
  load and fails nothing itself. If not, label with `exclude_injectors=("retry-amplification",)`.
- **`offered_ratio` in a cascade is a lower bound.** `db-throttling` measures the application's own
  reads over the 3 minutes before its window; a spike that began within those minutes is only partly
  counted (here 11.4 RCU/s against ~24 during the window: 2.16 recorded, ~2.22 seen by DynamoDB; the
  56.5% throttle rate fits the higher figure). The band parameter is `load_ratio`, which this does not
  affect.
