# Evaluation Integrity Controls (M4-3)

**Owner:** Linu · **Code:** `AnomalyPulse/simulator/integrity.py` · **Tests:** `AnomalyPulse/simulator/test_integrity.py`
**Used by:** M4-2 (training queue), M9-1 (test queue), M4-4/M5 (split), M9-3 (integrity report)

Training and test experiments come from the same simulator, so a model could score well by memorising the
simulator's settings rather than learning what an incident looks like. Project plan §6b sets three controls
against that. `integrity.py` is their single source of truth: generators import the bands and spec builders
from it, and nothing hardcodes a magnitude.

```python
from simulator import integrity
integrity.check_bands()                                   # raises unless the bands are interleaved
integrity.band("traffic-spike", "train", 0)               # {"parameter": "multiplier", "low": 2, "high": 4}
integrity.incident_spec("db-throttling", "train", rng)    # a queue entry's "incident"
integrity.cascade_spec("spike-payment-retry", "test", rng)  # a queue entry's "cascade"
integrity.confounder_plan("traffic-spike", "lambda-degradation", rng)   # a queue entry's "plan"
integrity.split_of(705)                                   # "test"
integrity.temporal_split(records)                         # {experiment_id: "train" | "val" | "test" | "stress"}
```

Every builder draws only from the `rng` it is given, so a queue is reproducible from its generator's seed.
`test_integrity.py` builds one queue entry of every kind and passes it through `batch.load_queue`, the batch
runner's own check, so a spec that would be refused fails the tests first.

---

## Control 1 — Interleaved parameter bands

Test magnitudes sit in the gaps between training magnitudes, so the test set asks the model to interpolate
into values it never saw.

| Injector (label) | Parameter | Train + val bands | Test bands | Stress slice |
|---|---|---|---|---|
| `traffic-spike` (TRAFFIC_SPIKE) | `multiplier` | 2–4×, 6–8× | 4–6×, 8–10× | 10–12× |
| `lambda-degradation` (LAMBDA_DEGRADATION) | `added_latency_ms` | 200–500, 800–1100 | 500–800, 1100–1400 | 1500–2000 |
| `db-throttling` (DATABASE_THROTTLING) | `load_ratio` | 1.5–2.5×, 3.5–4.5× | 2.5–3.5×, 4.5–5.5× | 6–8× |
| `payment-failure` (DEPENDENCY_FAILURE) | `timeout_ms` | 500–1500, 2500–3500 | 1500–2500, 3500–4500 | 5000–6000 |
| `retry-amplification` (cascades only) | `retries_per_failure` | 0.5–1.0, 1.5–2.0 | 1.0–1.5, 2.0–2.5 | — |

`check_bands()` proves, for every injector: the injector exists and its parameter matches; no train band
overlaps a test band; sorted by value the bands **alternate** train, test, train, test (interleaved, not side
by side); and every stress band lies above the whole train/test range. A test feeds it deliberately broken
tables (an overlap, side-by-side bands, low stress, a wrong parameter, an empty band) and checks each is caught.

### Where each number comes from

- **Train and test bands** for the first four injectors: the plan's §6b table, unchanged. A test pins them,
  so the plan and the code cannot drift apart silently.
- **Stress lower ends** (10×, 1500 ms, 6×, 5000 ms): the plan's thresholds.
- **Stress upper ends:** chosen here, each with a reason.
  - 12× traffic: M3-2 validated 10× (200 rps) end to end; 12× (240 rps) is a short step past it. Wider
    (the throttling doc mentions 20×) would risk the client dropping requests, which the batch rejects.
  - 8× throttling: the highest ratio validated on the stack (M3-3, ~8–11% Product 5xx).
  - 2000 ms latency and 6000 ms timeout: well under the services' 10 s Lambda timeout, so a run measures a
    slow service, not a timeout of the caller.
- **Retry-amplification bands:** not in the plan; `CASCADE.md` leaves them to M4-3. M3-5's example cascade
  used 0.5–2.5; that range is split into four interleaved pieces like the others. There is no stress band:
  the plan's stress slice has no retry axis.

### Shared edges

Neighbouring bands share an edge (train 2–4×, test 4–6×). A training run at intensity 1.0 and a test run at
intensity 0.0 would both be exactly 4.0×, the same experiment in both sets. So intensities are drawn from
`INTENSITY_RANGE = (0.05, 0.95)`, never 0–1: training reaches at most 3.9× and test starts at 4.1×. A test
checks this for every band.

### An honest caveat

The plan says test bands sit *inside* the trained range. That holds for the lower test band of each injector
(4–6× lies between 2–4× and 6–8×), but the upper test band (8–10×, 1100–1400 ms, 4.5–5.5×, 3500–4500 ms)
sits just above the highest training band, so it is mild extrapolation. We keep the plan's table and report
results per test band, so the two kinds can be told apart.

M4-1 found that Product's p95 under throttling sits on the app's retry-ladder steps (1.59 s or 3.19 s)
regardless of ratio, while throttle counts do rise with it. The labels are unaffected, but the models will
separate throttling bands by throttle counts rather than latency; per-band results in M9 should be read
with that in mind.

## Control 2 — Temporal split

| Slots | Partition | Generated |
|---|---|---|
| 1–560 | `train` | first |
| 561–700 | `val` (hyperparameter tuning) | first |
| 701–1050 | `test` | on a later UTC day than any training run |
| 1051 onwards | `stress` (reported separately) | with the test set |

The plan writes "701–1000", but its test set is 350 experiments (§17), so the test set runs to 1050 and the
stress slice is numbered after it. The batch runner accepts any slot above 700 as `test` or `stress`;
`split_of()` pins which is which.

- `split_of(slot, phase=None)` returns the partition, and refuses a registry `phase` that disagrees with the slot.
- `temporal_split(records)` takes registry records or `tier2/experiments.jsonl` lines and returns
  `{experiment_id: partition}` for accepted experiments. It refuses the whole set if any test or stress
  experiment was generated on or before the last training/validation day.

The batch runner already enforces the same ordering when it generates (`refuse_if_out_of_order`);
`temporal_split` checks it again when the data is used, so an evaluation can't run on a split that broke it.

## Control 3 — Simulator diversity protocol (test set only)

Training runs every fault for 5 minutes in the default 60/15/10/10/5 endpoint mix. The test set varies:

- **Endpoint mix:** every test-set experiment uses `TEST_MIX = [40, 25, 15, 10, 10]` (GET /products,
  GET /products/{id}, POST /cart, GET /orders/{id}, POST /orders): less listing, more product and cart
  traffic. The stress slice keeps the default mix, so only its magnitudes differ.
- **Varied durations (20 experiments):** single faults of 2 minutes (120 s) and 12 minutes (720 s), split
  across traffic spike, throttling, Lambda degradation and payment failure. A 12-minute run is 17 minutes,
  inside the simulator's 30-minute cap. Note for M4-4: a 2-minute throttling fault is labeled only from
  `label_from` (~47 s in), so it leaves about one labeled window.
- **Confounders (30 experiments):** two single faults over the same window, labeled with the **first** one's
  type, never `CASCADING_FAILURE`. Pairs: traffic spike + Lambda degradation (the plan's example), traffic
  spike + payment failure, DB throttling + Lambda degradation, Lambda degradation + payment failure.

## Experiment counts (plan §17)

| Class | Train + val (1–700) | Test (701–1050) |
|---|---|---|
| NORMAL | 140 | 60 |
| TRAFFIC_SPIKE | 140 | 60 |
| DATABASE_THROTTLING | 140 | 60 |
| LAMBDA_DEGRADATION | 105 | 45 |
| DEPENDENCY_FAILURE | 105 | 45 |
| CASCADING_FAILURE | 70 | 30 |
| Confounders | — | 30 |
| Varied duration | — | 20 |
| **Total** | **700** | **350** |

Stress slice: 5 each of traffic spike, Lambda degradation, DB throttling and payment failure (20, slots 1051–1070).

These are `COUNTS_TRAIN`, `COUNTS_TEST` and `COUNTS_STRESS`. Spreading each class evenly over its bands
and over train/val slots is the queue generator's job (M4-2, M9-1).

## Cascade templates

Every cascade runs in the M4 run shape (180 s baseline, 300 s cascade, 120 s recovery). A stage lasts until
the cascade ends, so from the second onset on two or more faults are active. Onsets have seeded jitter.

| Template | Stages (onset after the previous stage) |
|---|---|
| `spike-throttle-slowdown` | traffic spike → DB throttling (+60 s) → Order slowdown (+60 s) |
| `spike-payment-retry` | traffic spike → payment failure (+60 s) → retry amplification (+30 s) |
| `throttle-slowdown-payment-retry` | DB throttling → Product slowdown (+60 s) → payment failure (+45 s) → retry amplification (+30 s) |
| `slowdown-payment-retry` | Order slowdown → payment failure (+60 s) → retry amplification (+30 s) |

Training cascades (70) use training bands; test cascades (30) use test bands, spread over the same four
templates. **Retry amplification always overlaps payment failure**, which fails every `POST /orders` at any
band: retries need failures to amplify, and DB throttling's training bands (up to 4.5×) barely fail any
requests (`CASCADE.md`). A test checks this for every template.

Whether retry amplification counts as a fault for "two or more faults active" is M4-4's decision
(`CASCADE.md`, "Open for M4"); these templates have two or more service faults overlapping either way.

## Evaluation Integrity Statement

> Because the training and test datasets are both generated by the same incident simulator, we explicitly
> control for data leakage using three mechanisms. (1) **Interleaved parameter-space partitioning:** every
> incident type's magnitude axis is split into alternating training and test bands, checked automatically,
> so test incidents use magnitudes never seen in training; intensities avoid the bands' shared edges, so no
> experiment's magnitude appears in both sets. (2) **Strict temporal splitting:** experiments 1–700 (train
> and validation) are generated first and the test set (701–1050) on a later day, so it carries the AWS
> environment's day-to-day variation; this is enforced when the data is generated and checked again when it
> is used. (3) **A simulator diversity protocol:** the test set shifts the endpoint mix, varies incident
> duration (2 and 12 minutes against training's 5), and adds confounders, two incident types co-occurring
> without being labeled a cascade. A strict Feature/Label Boundary (plan §3) keeps near-label fields out of
> the model input. These controls let us claim that reported metrics reflect generalization across held-out
> regions of the simulator's parameter space rather than memorization of simulator artifacts. Each
> injector's upper test band lies slightly above its highest training band, so results are reported per
> band, and the extreme-extrapolation stress slice is reported separately because more extreme incidents
> are easier to detect. We do not claim generalization to real-world production incidents; that would
> require real-system telemetry and is out of scope.

## Open points

- **Slot numbers:** the test set is 701–1050 and the stress slice 1051–1070, not "701–1000" as the plan and
  M9 write. The code allows it; M9-1's queue should follow it.
- **Stress upper ends and retry bands** are M4-3's choices (reasons above); revisit if M9 wants a wider slice.
- **Confounder label:** the first fault's type. If M9 prefers to score confounders differently (for example
  accept either type), that is an evaluation choice, not a change to the data.
