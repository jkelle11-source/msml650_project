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
integrity.check_entry(entry)                              # raises if a queue entry breaks a control
```

Every builder draws only from the `rng` it is given, so a queue is reproducible from its generator's seed.
`test_integrity.py` builds one queue entry of every kind and passes it through `batch.load_queue`, the batch
runner's own check, so a spec that would be refused fails the tests first.

## The queue guard

The builders make correct entries, but nothing made a queue use them: a hand-edited or buggy queue could
put a test band in training, and M4-2's "no test-band parameters used" had no automatic check.
`check_entry()` refuses an entry unless it is what the builders make for its phase:

- **The phase its slot says** (`split_of`): 1–560 train, 561–700 val, 701–1050 test, 1051 on stress.
- **Its phase's bands** (val uses training bands), and every intensity from 0.05 to 0.95 (see "Shared edges").
- **The M4 run shape:** 180 s baseline, a 300 s fault or cascade, 120 s recovery.
- **The endpoint mix:** the default in train, val and stress; `TEST_MIX` in every test experiment.
- **Test-set kinds in the test set only:** 2- and 12-minute faults, and confounders (a `plan` is only ever a
  confounder: one of `CONFOUNDER_PAIRS`, labeled as its first fault, both faults over the same window).
- **Cascades from the templates:** stage injectors, targets and onsets as in `CASCADES`; none in stress.

`batch.load_queue()` runs it on every entry of a queue for the main dataset, `v1`, so `run-batch` (and
`--dry-run`) refuses a bad queue before anything runs. Other datasets are not guarded: a validation queue
probes band edges on purpose (M4-1's ran 1.5× and 4.5× exactly). A test feeds the guard one bad entry per
rule and checks each is refused.

---

## Control 1 — Interleaved parameter bands

Test magnitudes sit in the gaps between training magnitudes, so the test set asks the model to interpolate
into values it never saw.

| Injector (label) | Parameter | Train + val bands | Test bands | Stress slice |
|---|---|---|---|---|
| `traffic-spike` (TRAFFIC_SPIKE) | `multiplier` | 2–4×, 6–8× | 4–6×, 8–10× | 10–12× |
| `lambda-degradation` (LAMBDA_DEGRADATION) | `added_latency_ms` | 200–500, 800–1100 | 500–800, 1100–1400 | 1500–2000 |
| `db-throttling` (DATABASE_THROTTLING) | `load_ratio` | 1.5–2.5×, 3.5–4.5× | 2.5–3.5×, 4.5–5.5× | 6–8× |
| `payment-failure` (DEPENDENCY_FAILURE) | `timeout_ms` | 500–1500, 2500–3500 | 1500–2500, 3500–4500 | 5000–5500 |
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
  - 2000 ms latency: well under the services' 10 s Lambda timeout, so a run measures a slow service, not a
    timeout of the caller.
  - 5500 ms payment failure: Payment sleeps `timeout_ms` and answers 503, and Order stops waiting for it at
    6 s (`read_timeout=6`, `services/order/handler.py`). Up to 6000 ms, as first written, the top of the
    band would fail some requests as Order's read timeouts instead of Payment's 503, two failure modes in
    one band. At most 5475 ms is drawn, which leaves room for the invoke and Payment's cold starts. A test
    pins it.
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

### Throttling bands and latency

M4-1 found that Product's p95 under throttling sits on the app's retry-ladder steps (1.59 s or 3.19 s)
regardless of ratio, while throttle counts do rise with it (`ReadThrottleEvents` in a labeled minute: about
1,400 at 1.5×, 2,100 at 2.0×, 4,500 at 4.0×, 5,000 at 4.5×; `DATASET.md`, "Validation"). **Closed here
without further runs (decided 2026-10-17):** the bands are defined on the ratio, and the Tier-1 signal that
measures the ratio, the throttle count, tracks it, so the throttling test bands are still interpolation in
what the model sees. The labels are unaffected. The models will separate throttling bands by throttle counts
rather than latency; per-band results in M9 should be read with that in mind.

## Control 2 — Temporal split

| Slots | Partition | Generated |
|---|---|---|
| 1–560 | `train` | first |
| 561–700 | `val` (hyperparameter tuning) | first |
| 701–1050 | `test` | on a later UTC day than any training run |
| 1051 onwards | `stress` (reported separately) | with the test set |

The plan first wrote "701–1000", but its test set is 350 experiments (§17), so the test set runs to 1050 and
the stress slice is numbered after it; the plan, M04, M09 and `DATASET.md` now say so (decided
2026-10-17). The batch runner accepts any slot above 700 as `test` or `stress`; `split_of()` pins which is
which, and the queue guard holds every `v1` slot to it.

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
  across traffic spike, throttling, Lambda degradation and payment failure. A 12-minute run lasts 17
  minutes (180 + 720 + 120 s). Note for M4-4: a 2-minute throttling fault is labeled only from
  `label_from` (~47 s in), so it leaves about one labeled window.
- **Confounders (30 experiments):** two single faults over the same window, labeled with the **first** one's
  type, never `CASCADING_FAILURE`. Pairs: traffic spike + Lambda degradation (the plan's example), traffic
  spike + payment failure, DB throttling + Lambda degradation, Lambda degradation + payment failure.
  M4-4's labeler gives the windows where both are active the manifest's `incident_type` (PR #73).

### Confounders: a test-only probe, and how M9 scores it

A confounder is two **unrelated** faults that happen to co-occur; a cascade is one fault leading to another.
The project wants the model to tell them apart, but two things limit what this test set can show, and the
report should say so (decided 2026-10-17: keep confounders a test-only probe, as the plan has them):

- **The model never sees a confounder in training.** Every training example with two faults active is a
  cascade, labeled `CASCADING_FAILURE`. Every pair of the four single faults co-occurs in at least one
  training template (spike + payment failure in `spike-payment-retry`, for example), so a model that learned
  cascades will likely call a confounder's overlap `CASCADING_FAILURE`.
- **No cascade stage is caused by the one before it.** Every stage starts on a timer: the spike doesn't
  cause the throttling (the injector reads at its ratio either way). The one real causal link is retry
  amplification, where failures trigger retries that add traffic. So what tells a cascade from a confounder
  in the telemetry is the retry feedback and the staggered onsets, not causation.

M9 therefore reports, over each confounder's windows with both faults active: the share predicted as
**either** fault's type, the share predicted as the **labeled** (first) type, and the share predicted
`CASCADING_FAILURE`. A high `CASCADING_FAILURE` share is a finding about what the model learned (two faults
at once means a cascade), not a data error. The labels stay as they are.

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

These are `COUNTS_TRAIN`, `COUNTS_TEST` and `COUNTS_STRESS`.

## Building a queue (M4-2, M9-1)

Agreed 2026-10-17 (Josh's proposal for M4-2, refined). The generator is M4-2's and M9-1's; these are the
rules it follows, so that nothing but the incident varies with the slot.

1. **Blocks.** Every 20 consecutive training or validation slots hold exactly `TRAIN_BLOCK`: 4 NORMAL,
   4 traffic spike, 4 DB throttling, 3 Lambda degradation, 3 payment failure, 2 cascades, in an order
   shuffled with the generator's fixed seed. Training is 28 blocks (1–560) and validation 7 (561–700:
   28/28/28/21/21/14), so both keep §17's proportions. A plain shuffle of a whole split can still bunch a
   class into one day, and then time of day or that day's AWS state goes with the label; a block of
   20 runs (~3.3 h) can't. The test set is 5 blocks of 70 (`TEST_BLOCK`), the stress slice one of 20.
2. **Bands half and half within each split**, spread over the blocks with `band_index` (`band_indices` for
   cascades and confounders) instead of drawn. A block's 4 spikes and 4 throttling runs are 2 and 2; its
   3 Lambda and 3 payment runs alternate 2/1 and 1/2 by block. Training comes out 56/56 for spikes and
   throttling and 42/42 for Lambda and payment; validation 14/14, and 11/10 for Lambda and payment, with
   the extra run in opposite bands (Lambda 53/52 overall, payment 52/53).
3. **Targets and templates in turn.** Lambda degradation's four targets round-robin (21 each in training);
   cascades cycle the four templates (14 each in training, 4/4/3/3 in validation), and each stage's band
   alternates across a template's runs. Intensities stay drawn by the builders.
4. **Slots are numbered after shuffling:** slot *i* is the *i*-th run, so slot order is generation order.
5. **Training and validation are separate queues, training first.** A rejected run is retried at the end
   of its own queue, so training's reruns finish before validation begins.
6. **Training in four daily queues of 140 slots** (7 blocks, ~23.5 h each), then validation's 140. Each
   batch exports when it ends, so M4-4 and M4-5 get each day's runs and a problem shows within a day;
   someone starts the next queue.
7. **Commit the generator, its seed and the queue files**, and check each queue with `run-batch --dry-run`:
   the guard runs on every `v1` entry.

At 600 s a run and ~0.3 s between runs (M4-1's batch), 700 runs take 116.7 h, plus 10 minutes per rerun
and about 15 minutes per batch for CloudWatch, the shipper and the export: about 5 days nonstop, 5.5–6
with daily batches.

The test set follows the same rules in blocks of 70, with the confounder pairs and the varied-duration
faults in turn as well (M9-1).

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

M4-4 (PR #73) doesn't count retry amplification as a fault for "two or more faults active"; these templates
have two or more service faults overlapping either way.

**The ratio DynamoDB sees in `spike-throttle-slowdown`.** The throttling injector reads at `load_ratio` ×
capacity on top of the app's own reads, and the spike multiplies those: about 7.5 RCU/s at 20 rps, about
60 at a 7.9× spike, 0.3 of the 200 RCU capacity. So a 4.4× training stage offers DynamoDB about 4.7×,
inside the 4.5–5.5× test band, and the manifest's `offered_ratio` understates it (it averages the app's
reads over the 3 minutes before the stage, partly before the spike; `CASCADE.md`). Those windows are
labeled `CASCADING_FAILURE`, so DB throttling's per-band results are unaffected; the band names the
injector's own ratio.

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

- **Stress upper ends and retry bands** are M4-3's choices (reasons above); revisit if M9 wants a wider slice.
- **The test-set kinds haven't run on the runner:** 12-minute faults, confounders, throttling at 6–8× (the
  runner has run up to 4.5× since the 16-client fix; M3-3 ran 8× from a laptop) and the 12× spike (200 rps
  validated). M9-1 should validate them in a separate dataset before generating the test set.
