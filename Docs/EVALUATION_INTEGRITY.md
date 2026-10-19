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
  `label_from` (~47 s in), so it leaves one labeled window, or none when `label_from` falls in a minute's
  first 18 s, about 3 runs in 10; `run-batch` reruns such a run (`Docs/LABELING.md`, "Windows per run").
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
3. **Targets and templates in turn.** Lambda degradation's four targets take turns within each band, the
   high band starting halfway round: 21 each in training, 10 or 11 in each band (in turn over all Lambda
   runs, which M4-2's review replaced, cart-service went 12/9 in training and 5/0 in validation);
   cascades cycle the four templates (14 each in training, 4/4/3/3 in validation), and a template's runs
   go through every combination of its stages' bands, in complementary pairs (all low and all high, then
   the rest), so each stage is 7/7 in training. A three-stage template has all 8 combinations in
   training; the four-stage one has 14 of its 16 there and the other two in validation. (Decided in
   M4-2's review: stages that alternate together, or in opposite bands, give a template only two of its
   combinations, so a heavy stage would always come with the same band of the others.) Intensities stay
   drawn by the builders.
4. **Slots are numbered after shuffling:** slot *i* is the *i*-th run, so slot order is generation order.
5. **Training and validation are separate queues, training first.** A rejected run is retried at the end
   of its own queue, so training's reruns finish before validation begins. `run-batch` enforces the order
   (M4-2's review): a `v1` train/val queue waits until every slot before its first is accepted or
   exhausted.
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
| `throttle-spike-slowdown` | DB throttling → traffic spike (+75 s) → Order slowdown (+60 s) |
| `spike-payment-retry` | traffic spike → payment failure (+60 s) → retry amplification (+30 s) |
| `throttle-slowdown-payment-retry` | DB throttling → Product slowdown (+60 s) → payment failure (+45 s) → retry amplification (+30 s) |
| `slowdown-payment-retry` | Order slowdown → payment failure (+60 s) → retry amplification (+30 s) |

Training cascades (70) use training bands; test cascades (30) use test bands, spread over the same four
templates. **Retry amplification always overlaps payment failure**, which fails every `POST /orders` at any
band: retries need failures to amplify, and DB throttling's training bands (up to 4.5×) barely fail any
requests (`CASCADE.md`). A test checks this for every template.

M4-4 (PR #73) doesn't count retry amplification as a fault for "two or more faults active"; these templates
have two or more service faults overlapping either way.

**DB throttling never starts during a spike** (a test checks every template). The template was first
written spike first, as the plan's example cascade runs, and the validation below rejected it three times
out of three: when the throttling surge spends the burst bank, nearly every Product read climbs the SDK's
retry ladder to ~3.2 s for a few seconds, and at a 7.9× spike that is more requests in flight than the
client's 256, so it drops some. Throttling now comes first, its surge at 20 rps like every single-fault
run, and the spike arrives 75 ± 10 s in, after `label_from` (~48 s), into steady throttling. The stages
were never causal (see "Confounders"), so the order changes no claim.

**The ratio DynamoDB sees in `throttle-spike-slowdown`.** The throttling injector reads at `load_ratio` ×
capacity on top of the app's own reads: about 7.5 RCU/s at 20 rps (the validation's `app_reads_per_s`),
so roughly 60 once a 7.9× spike arrives, 0.3 of the 200 RCU capacity. So a 4.4× training stage offers
DynamoDB about 4.7× once the spike is on, inside the 4.5–5.5× test band, and the injector's steady throttle
rate rises above `1 − 1/offered_ratio`, which is measured before the stage (0.48 where 0.39 was expected,
below). The batch's steady-rate check is one-sided for that reason: burst capacity only ever lowers the
rate. Those windows are labeled `CASCADING_FAILURE`, so DB throttling's per-band results are unaffected;
the band names the injector's own ratio.

## Validation on the runner (2026-10-18)

Three batches into dataset `m4-3-validation` (never `v1`), on the runner host with the stack as M4-1 left
it, from `simulator/queues/m4-3-validation.json`, `-2.json` and `-3.json`. Each put the cascades at their
heaviest corners.

**Batch 1** (`batch-20261018T021430Z-f2ca`, commit `48d555a`): the four templates in training bands, the
first one still spike first.

| Slot | Cascade | Result |
|---|---|---|
| 1 | spike 7.9× → throttling 1.55× → Order +350 ms | **Rejected 3 times out of 3:** the client dropped 55, 58 and 73 requests; `EXHAUSTED` |
| 2 | spike 2.1× → throttling 4.45× → Order +950 ms | Accepted at the first attempt; steady throttle rate 0.788 (0.778 expected) |
| 3 | throttling 2.0× → Product +350 ms → payment failure 1,000 ms → retries 1.75 | Accepted; steady 0.514 (0.509); 282 retries |
| 4 | spike 7.9× → payment failure 3,450 ms → retries 1.975 | Accepted; 56,947 requests, none dropped; 3,329 retries |
| 5 | Order +950 ms → payment failure 1,000 ms → retries 0.75 | Accepted; 137 retries |

Every `label_from` came 47–48 s into its throttling stage. Slot 1's drops came as the surge spent the
burst bank (slot 6 below, the same magnitudes reordered, dropped none): in the 10 s ending at
`label_from` the services' mean latency was 983 ms and about 164 requests were open at once on average
(latency × rate), against about 1 at 7.9× before the surge. Its steady throttle rate, 0.48 against an
expected 0.39 in all three attempts, came within 0.003–0.005 of the old two-sided limit.

**Batch 2** (`batch-20261018T033322Z-ff92`, commit `a6d9554`): the template reordered, throttling first.

| Slot | Cascade | Result |
|---|---|---|
| 6 | throttling 1.55× → spike 7.9× (+77 s) → Order +350 ms (slot 1's magnitudes) | Accepted at the first attempt, none dropped; steady 0.467 where 0.369 was expected (+0.098: the one-sided check was needed) |
| 7 | throttling 4.45× → spike 7.9× (+69 s) → Order +950 ms | First attempt dropped 7 of 44,003 requests and was rejected; the second dropped none and was accepted; steady 0.815 (0.777) |

The surge's end now came at 20 rps, with about 20 requests open. Under steady 4.45× throttling the 7.9×
spike kept 100–150 open on average over each 10 s, and short bursts, when DynamoDB refuses a second or two
of reads together, went past the client's 256. So the batch's client now keeps 512 open
(`batch.MAX_IN_FLIGHT`), and no higher: an open `POST /orders` keeps an Order and a Payment Lambda running,
and the account runs 1,000 at once across every function, so a bigger client could make Lambda refuse
requests, a failure no injector caused.

**Batch 3** (`batch-20261018T042307Z-035a`, commit `086f7a9`): the client at 512, at the heaviest mixes.
Slots 9 and 10 take the test set's top magnitudes and shifted mix; they are `train` entries only because
this dataset already held training runs from that day, and the batch refuses test runs on a training day.

| Slot | Cascade | Result |
|---|---|---|
| 8 | throttling 4.45× → spike 7.9× → Order +950 ms (slot 7's mix) | Accepted at the first attempt, none dropped; steady 0.816 (0.777) |
| 9 | throttling 5.45× → spike 9.9× → Order +1,385 ms, test mix | Accepted at the first attempt, none dropped; steady 0.849 (0.818) |
| 10 | spike 9.9× → payment failure 4,450 ms → retries 2.475, test mix | Accepted at the first attempt, none dropped; 74,432 requests, 9,628 of them retries |

- **The client:** the batch process's thread count topped out at 251, 351 and 371 in the three runs. That
  is one thread per open request at the run's peak, plus about 18 of the process's own and, in slots 8
  and 9, up to 64 of the throttling injector's. So slot 10 kept about 350 requests open and slot 9 at
  least 270: the old 256 would have dropped some in both.
- **Lambda:** at most 244, 347 and 716 functions ran at once across the account (slot 10: Order 359 and
  Payment 353, since each open `POST /orders` holds both), against the account's 1,000. Lambda throttled
  nothing, in any function.
- **The runner:** the batch process used at most 32% of one of its two cores while runs were going, and
  held at most 381 open files, against a limit of 65,535.

Every intensity in slots 9 and 10 is 0.95, the top of the drawn range, so these are the test set's heaviest
cascades of the two templates with a spike. The other two templates have no spike, and the heaviest
confounder (a 9.9× spike with a 4,450 ms payment failure and no retries) needs about 90 open requests by
the same arithmetic.

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
- **Most test-set kinds haven't run on the runner:** 12-minute faults, confounders, throttling at 6–8× on
  its own (M3-3 ran 8× from a laptop) and the 12× spike (200 rps validated). The test set's heaviest
  cascades have (batch 3 above). M9-1 should validate the rest in a separate dataset before generating the
  test set.
