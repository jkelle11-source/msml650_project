"""Evaluation-integrity controls (M4-3; project plan section 6b).

The single source of truth for what each experiment may be, by phase:

    Control 1  BANDS: interleaved train and test bands per injector, a stress
               band above both; check_bands() proves the interleaving.
    Control 2  split_of() and temporal_split(): 1-560 train, 561-700 val,
               701-1050 test, 1051 on stress; the test set on a later day.
    Control 3  the diversity protocol, test set only: TEST_MIX, varied
               durations, confounders.

Generators build queue entries with incident_spec(), cascade_spec() and
confounder_plan(), each drawing only from the rng it is given (a band can be
chosen instead, to balance them), and order them in blocks of TRAIN_BLOCK or
TEST_BLOCK. check_entry()
refuses an entry that breaks a control; batch.load_queue() runs it on every
entry of the main dataset's queues. Docs/EVALUATION_INTEGRITY.md has where
each number comes from.
"""
import random

from simulator import dataset, injectors

PHASES = dataset.PHASES


BASELINE_S = 180
FAULT_S = 300
RECOVERY_S = 120

# --- Control 1: interleaved parameter bands ------------------------------------

BANDS = {
    "traffic-spike": {
        "parameter": "multiplier",
        "train": [(2, 4), (6, 8)],
        "test": [(4, 6), (8, 10)],
        "stress": [(10, 12)],
    },
    "lambda-degradation": {
        "parameter": "added_latency_ms",
        "train": [(200, 500), (800, 1100)],
        "test": [(500, 800), (1100, 1400)],
        "stress": [(1500, 2000)],
    },
    "db-throttling": {
        "parameter": "load_ratio",
        "train": [(1.5, 2.5), (3.5, 4.5)],
        "test": [(2.5, 3.5), (4.5, 5.5)],
        "stress": [(6, 8)],
    },
    "payment-failure": {
        "parameter": "timeout_ms",
        "train": [(500, 1500), (2500, 3500)],
        "test": [(1500, 2500), (3500, 4500)],
        # Not up to 6000: Order stops waiting for Payment at 6 s (its read
        # timeout), so a fault near it would fail some requests as Order's
        # timeouts instead of Payment's 503, two failure modes in one band.
        "stress": [(5000, 5500)],
    },
    "retry-amplification": {
        "parameter": "retries_per_failure",
        "train": [(0.5, 1.0), (1.5, 2.0)],
        "test": [(1.0, 1.5), (2.0, 2.5)],
        "stress": [],
    },
}

INTENSITY_RANGE = (0.05, 0.95)


def band_phase(phase: str) -> str:
    """Which BANDS entry an experiment phase uses: val experiments use training bands."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}, got {phase!r}")
    return "train" if phase == "val" else phase


def band(injector: str, phase: str, index: int) -> dict:
    """One band in the queue's format.

    band("traffic-spike", "train", 0) -> {"parameter": "multiplier", "low": 2, "high": 4}
    """
    spec = BANDS[injector]
    low, high = spec[band_phase(phase)][index]
    return {"parameter": spec["parameter"], "low": low, "high": high}


def _overlaps(a, b) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def check_bands(bands: dict = BANDS) -> None:
    """Raise ValueError unless, for every injector, train and test bands alternate
    without overlapping and every stress band lies above them both."""
    problems = []
    for name, spec in bands.items():
        try:
            parameter = injectors.get(name).parameter
        except LookupError:
            problems.append(f"{name}: no such injector")
            continue
        if spec["parameter"] != parameter:
            problems.append(f"{name}: parameter is {parameter!r}, not {spec['parameter']!r}")

        for phase in ("train", "test", "stress"):
            for low, high in spec[phase]:
                if not low < high:
                    problems.append(f"{name}: {phase} band ({low}, {high}) is empty")
        if not spec["train"] or not spec["test"]:
            problems.append(f"{name}: needs both train and test bands")
            continue

        for t in spec["train"]:
            for s in spec["test"]:
                if _overlaps(t, s):
                    problems.append(f"{name}: train band {t} overlaps test band {s}")

        ordered = sorted([(b, "train") for b in spec["train"]] + [(b, "test") for b in spec["test"]])
        for (first, phase_a), (second, phase_b) in zip(ordered, ordered[1:]):
            if phase_a == phase_b:
                problems.append(f"{name}: {phase_a} bands {first} and {second} are adjacent; "
                                f"train and test must alternate")
            if _overlaps(first, second):
                problems.append(f"{name}: bands {first} and {second} overlap")

        top = max(high for _, high in spec["train"] + spec["test"])
        for low, high in spec["stress"]:
            if low < top:
                problems.append(f"{name}: stress band ({low}, {high}) starts below the "
                                f"train/test range's top ({top})")
    if problems:
        raise ValueError("bands are not interleaved:\n  " + "\n  ".join(problems))


# --- experiment counts (plan section 17) --------------------------------------

# Training + validation, slots 1-700 (Phase 1).
COUNTS_TRAIN = {
    "NORMAL": 140,
    "TRAFFIC_SPIKE": 140,
    "DATABASE_THROTTLING": 140,
    "LAMBDA_DEGRADATION": 105,
    "DEPENDENCY_FAILURE": 105,
    "CASCADING_FAILURE": 70,
}
# The test set (Phase 2), generated on a later day.
COUNTS_TEST = {
    "NORMAL": 60,
    "TRAFFIC_SPIKE": 60,
    "DATABASE_THROTTLING": 60,
    "LAMBDA_DEGRADATION": 45,
    "DEPENDENCY_FAILURE": 45,
    "CASCADING_FAILURE": 30,
    "confounder": 30,
    "varied_duration": 20,
}
COUNTS_STRESS = {"traffic-spike": 5, "lambda-degradation": 5, "db-throttling": 5, "payment-failure": 5}

# Run order. A queue runs in blocks of consecutive slots, each holding exactly
# this mix in a seeded shuffle, so no stretch of generation (a time of day, a
# day's AWS state, a batch cut short) leans toward one class. Training is 28
# blocks of 20 (slots 1-560) and validation 7 (561-700), so both keep
# COUNTS_TRAIN's proportions; the test set is 5 blocks of 70.
TRAIN_BLOCK = {label: count // 35 for label, count in COUNTS_TRAIN.items()}     # 4/4/4/3/3/2
TEST_BLOCK = {kind: count // 5 for kind, count in COUNTS_TEST.items()}          # 12/12/12/9/9/6/6/4

INJECTOR_FOR = {
    "NORMAL": "noop",
    "TRAFFIC_SPIKE": "traffic-spike",
    "DATABASE_THROTTLING": "db-throttling",
    "LAMBDA_DEGRADATION": "lambda-degradation",
    "DEPENDENCY_FAILURE": "payment-failure",
}

# --- Control 2: the temporal split ---------------------------------------------

TEST_FIRST = dataset.VAL_LAST + 1
TEST_LAST = dataset.VAL_LAST + sum(COUNTS_TEST.values())
STRESS_FIRST = TEST_LAST + 1


def split_of(slot: int, phase: str | None = None) -> str:
    """The partition experiment number `slot` belongs to: train, val, test or stress.

    1-560 train, 561-700 val, 701-1050 test, 1051 on stress. A registry record's
    `phase` is passed as `phase` and must agree with the slot.
    """
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
        raise ValueError(f"slot must be a whole number, 1 or more, got {slot!r}")
    if slot <= dataset.TRAIN_LAST:
        expected = "train"
    elif slot <= dataset.VAL_LAST:
        expected = "val"
    elif slot <= TEST_LAST:
        expected = "test"
    else:
        expected = "stress"
    if phase is not None and phase != expected:
        raise ValueError(f"slot {slot} is {expected}, but its record says {phase!r}")
    return expected


def temporal_split(records) -> dict:
    """{experiment_id: partition} for the accepted experiments in `records`.

    `records` are registry records or tier2/experiments.jsonl lines (each with
    slot, phase, experiment_id and generation_date). Records without an
    experiment_id (not accepted) are skipped. Raises ValueError if a record's
    phase disagrees with its slot, or if any test or stress experiment was
    generated on or before the last training/validation day: the test set must
    come from a later day (plan section 6b, Control 2).
    """
    split, trained, tested = {}, [], []
    for record in records:
        experiment_id = record.get("experiment_id")
        if not experiment_id:
            continue
        part = split_of(record["slot"], record.get("phase"))
        date = record.get("generation_date")
        if not date:
            raise ValueError(f"{experiment_id}: accepted but has no generation_date")
        (trained if part in ("train", "val") else tested).append((date, experiment_id))
        split[experiment_id] = part

    if trained and tested:
        last_train_day = max(trained)[0]
        early = sorted(e for d, e in tested if d <= last_train_day)
        if early:
            raise ValueError(f"test/stress experiments generated on or before the last training day "
                             f"({last_train_day}): {', '.join(early)}")
    return split


# --- Control 3: the simulator diversity protocol (test set only) ---------------

# Shifted endpoint mix for every test-set experiment, in workload.ROUTES order:
# GET /products, GET /products/{id}, POST /cart, GET /orders/{id}, POST /orders.
# Training uses the default 60/15/10/10/5.
TEST_MIX = [40, 25, 15, 10, 10]

# Varied-duration test experiments: faults of 2 and 12 minutes instead of 5.
# A 12-minute run is 180 + 720 + 120 s = 17 minutes, inside the 30-minute cap.
VARIED_DURATIONS_S = (120, 720)

# Confounders: two single faults at once, over the same window, labeled with the
# first one's type, never CASCADING_FAILURE. The first pair is the plan's example.
CONFOUNDER_PAIRS = (
    ("traffic-spike", "lambda-degradation"),
    ("traffic-spike", "payment-failure"),
    ("db-throttling", "lambda-degradation"),
    ("lambda-degradation", "payment-failure"),
)

# --- spec builders (pure, seeded) --------------------------------------------


def _intensity(rng: random.Random) -> float:
    return round(rng.uniform(*INTENSITY_RANGE), 3)


def _target(injector: str, rng: random.Random, target: str | None) -> str | None:
    targets = injectors.get(injector).targets
    if target is not None or len(targets) < 2:
        return target
    return rng.choice(targets)


def _band_for(injector: str, phase: str, rng: random.Random, band_index: int | None) -> dict:
    """The band at `band_index` among the phase's bands, or one drawn at random when it is None."""
    choices = BANDS[injector][band_phase(phase)]
    if not choices:
        raise ValueError(f"{injector}: no {band_phase(phase)} bands")
    if band_index is None:
        band_index = rng.randrange(len(choices))
    elif not isinstance(band_index, int) or isinstance(band_index, bool) or not 0 <= band_index < len(choices):
        raise ValueError(f"{injector}: band_index must be 0 to {len(choices) - 1} for its "
                         f"{band_phase(phase)} bands, got {band_index!r}")
    return band(injector, phase, band_index)


def incident_spec(injector: str, phase: str, rng: random.Random, *, duration_s: int = FAULT_S,
                  target: str | None = None, band_index: int | None = None) -> dict:
    """One single-fault experiment's `incident`, with a band and intensity drawn for `phase`.

    `noop` (NORMAL) takes no band. A multi-target injector (lambda-degradation)
    gets a target drawn from its targets unless one is given. `band_index`
    picks one of the phase's bands (0 is the lowest), so a generator can
    balance them; by default it is drawn.
    """
    spec = {"injector": injector, "baseline_s": BASELINE_S, "duration_s": duration_s,
            "recovery_s": RECOVERY_S}
    if injector == "noop":
        return spec
    spec["band"] = _band_for(injector, phase, rng, band_index)
    spec["intensity"] = _intensity(rng)
    target = _target(injector, rng, target)
    if target is not None:
        spec["target"] = target
    return spec


# Cascade templates (CASCADE.md spec format), for the 70 training and 30 test
# cascades. Each runs in the M4 run shape: 180 s baseline, a 300 s cascade,
# 120 s recovery. A stage lasts until the cascade ends unless it says otherwise,
# so from the second onset on two or more faults are active. Any template with
# retry-amplification overlaps it with payment-failure, which fails requests at
# every band; DB throttling's training bands (up to 4.5x) barely fail any
# (CASCADE.md, "It needs failures to amplify").
#
# DB throttling never starts during a spike: when its surge spends the burst
# bank, nearly every Product read climbs the SDK's retry ladder to ~3.2 s for a
# few seconds, and at a 7.9x spike that is more requests in flight than the
# client's 256, so it drops some and the run is rejected (M4-3's validation,
# three times out of three). So throttling comes first and the spike arrives
# 75 +/- 10 s in, after label_from (~48 s): under steady throttling a 7.9x
# spike held ~50-85 requests in flight.
CASCADES = {
    "throttle-spike-slowdown": [
        {"injector": "db-throttling"},
        {"injector": "traffic-spike", "onset_delay_s": 75, "onset_jitter_s": 10},
        {"injector": "lambda-degradation", "target": "order-service",
         "onset_delay_s": 60, "onset_jitter_s": 10},
    ],
    "spike-payment-retry": [
        {"injector": "traffic-spike"},
        {"injector": "payment-failure", "onset_delay_s": 60, "onset_jitter_s": 10},
        {"injector": "retry-amplification", "onset_delay_s": 30, "onset_jitter_s": 10},
    ],
    "throttle-slowdown-payment-retry": [
        {"injector": "db-throttling"},
        {"injector": "lambda-degradation", "target": "product-service",
         "onset_delay_s": 60, "onset_jitter_s": 10},
        {"injector": "payment-failure", "onset_delay_s": 45, "onset_jitter_s": 10},
        {"injector": "retry-amplification", "onset_delay_s": 30, "onset_jitter_s": 10},
    ],
    "slowdown-payment-retry": [
        {"injector": "lambda-degradation", "target": "order-service"},
        {"injector": "payment-failure", "onset_delay_s": 60, "onset_jitter_s": 10},
        {"injector": "retry-amplification", "onset_delay_s": 30, "onset_jitter_s": 10},
    ],
}


def _indices(band_indices, count: int, what: str) -> list:
    if band_indices is None:
        return [None] * count
    if len(band_indices) != count:
        raise ValueError(f"{what}: band_indices needs {count} entries (an index or None each), "
                         f"got {len(band_indices)}")
    return list(band_indices)


def cascade_spec(template: str, phase: str, rng: random.Random, *, band_indices=None) -> dict:
    """One cascade experiment's `cascade` spec from a template, with bands and intensities for `phase`.

    `band_indices` gives each stage's band, in stage order (None draws that one).
    """
    if phase == "stress":
        raise ValueError("the stress slice has no cascades: it extends each injector's own axis")
    stages = []
    indices = _indices(band_indices, len(CASCADES[template]), template)
    for stage, band_index in zip(CASCADES[template], indices):
        intensity = _intensity(rng)
        stages.append({**stage, "intensity": intensity,
                       "band": _band_for(stage["injector"], phase, rng, band_index)})
    return {"baseline_s": BASELINE_S, "cascade_s": FAULT_S, "recovery_s": RECOVERY_S, "stages": stages}


def confounder_plan(first: str, second: str, rng: random.Random, *, band_indices=None) -> dict:
    """A test-set confounder's `plan`: two faults over the same window, labeled as the first.

    `band_indices` gives each fault's band, first then second (None draws that one).
    """
    faults = []
    indices = _indices(band_indices, 2, f"{first} + {second}")
    for injector, band_index in zip((first, second), indices):
        spec = incident_spec(injector, "test", rng, band_index=band_index)
        fault = {"injector": injector, "offset_s": BASELINE_S, "duration_s": FAULT_S,
                 "intensity": spec["intensity"], "band": spec["band"]}
        if "target" in spec:
            fault["target"] = spec["target"]
        faults.append(fault)
    return {"incident_type": injectors.get(first).incident_type, "recovery_s": RECOVERY_S,
            "faults": faults}


# --- the queue guard -------------------------------------------------------------

# The dataset the controls are claimed for: batch.load_queue() runs check_entry()
# on every entry of its queues. A validation dataset is exempt, so it can probe a
# band's edge on purpose (M4-1's ran 1.5x and 4.5x exactly).
GUARDED_DATASET = dataset.DEFAULT_DATASET


def _check_magnitude(injector: str, intensity, band_: dict | None, phase: str) -> None:
    if injector not in BANDS:
        raise ValueError(f"{injector}: has no bands")
    spec, bands_for = BANDS[injector], band_phase(phase)
    allowed = [{"parameter": spec["parameter"], "low": low, "high": high} for low, high in spec[bands_for]]
    if band_ not in allowed:
        names = ", ".join(f"{low:g}-{high:g}" for low, high in spec[bands_for]) or "none"
        raise ValueError(f"{injector}: band {band_} is not one of its {bands_for} bands ({names})")
    low, high = INTENSITY_RANGE
    if not isinstance(intensity, (int, float)) or isinstance(intensity, bool) or not low <= intensity <= high:
        raise ValueError(f"{injector}: intensity must be from {low} to {high}, so the magnitude never "
                         f"reaches an edge a train band shares with a test band; got {intensity!r}")


def _check_incident(spec: dict, phase: str) -> None:
    injector, duration_s = spec["injector"], spec["duration_s"]
    if (spec["baseline_s"], spec["recovery_s"]) != (BASELINE_S, RECOVERY_S):
        raise ValueError(f"every run has a {BASELINE_S} s baseline and {RECOVERY_S} s of recovery "
                         f"(the M4 run shape); got {spec['baseline_s']} and {spec['recovery_s']}")
    varied = phase == "test" and injector != "noop" and duration_s in VARIED_DURATIONS_S
    if duration_s != FAULT_S and not varied:
        also = (f", or {' or '.join(map(str, VARIED_DURATIONS_S))} s for a test-set fault"
                if injector != "noop" else "")
        raise ValueError(f"{injector}: the window lasts {FAULT_S} s{also}; got {duration_s} in {phase}")
    if injector != "noop":
        _check_magnitude(injector, spec.get("intensity"), spec.get("band"), phase)


def _check_cascade(spec: dict, phase: str) -> None:
    if phase == "stress":
        raise ValueError("the stress slice has no cascades")
    if (spec["baseline_s"], spec["cascade_s"], spec["recovery_s"]) != (BASELINE_S, FAULT_S, RECOVERY_S):
        raise ValueError(f"a cascade runs {BASELINE_S} s baseline, {FAULT_S} s cascade, {RECOVERY_S} s "
                         f"recovery (the M4 run shape)")
    shape = [{k: v for k, v in stage.items() if k not in ("intensity", "band")} for stage in spec["stages"]]
    if shape not in CASCADES.values():
        raise ValueError(f"its stages (injectors, targets, onsets) are not one of the templates: "
                         f"{', '.join(CASCADES)}")
    for stage in spec["stages"]:
        _check_magnitude(stage["injector"], stage.get("intensity"), stage.get("band"), phase)


def _check_confounder(plan: dict, phase: str) -> None:
    if phase != "test":
        raise ValueError(f"a {phase} experiment can't be a plan: the only plans are the test set's "
                         f"confounders")
    pair = tuple(f["injector"] for f in plan["faults"])
    if pair not in CONFOUNDER_PAIRS:
        raise ValueError(f"faults {' + '.join(pair)} are not a confounder pair: "
                         + "; ".join(" + ".join(p) for p in CONFOUNDER_PAIRS))
    first_type = injectors.get(pair[0]).incident_type
    if plan["incident_type"] != first_type:
        raise ValueError(f"a confounder is labeled as its first fault, {first_type}, "
                         f"not {plan['incident_type']}")
    if plan["recovery_s"] != RECOVERY_S or any(
            (f["offset_s"], f["duration_s"]) != (BASELINE_S, FAULT_S) for f in plan["faults"]):
        raise ValueError(f"both faults of a confounder start after the {BASELINE_S} s baseline and last "
                         f"{FAULT_S} s, then {RECOVERY_S} s of recovery")
    for fault in plan["faults"]:
        _check_magnitude(fault["injector"], fault.get("intensity"), fault.get("band"), phase)


def check_entry(entry: dict) -> None:
    """Raise ValueError if a queue entry breaks a control.

    `entry` is already a valid queue entry (batch.load_queue resolves it first).
    It must also be what this module's builders make for its phase: the phase
    agrees with the slot (split_of); every band is one of the phase's bands and
    every intensity is inside INTENSITY_RANGE; the M4 run shape; the default
    endpoint mix, except TEST_MIX in the test set; varied durations and
    confounders in the test set only; cascades from CASCADES, never in stress.
    """
    phase = split_of(entry["slot"], entry["phase"])
    mix = entry.get("mix")
    if phase == "test" and mix != TEST_MIX:
        raise ValueError(f"the test set runs in the shifted mix {TEST_MIX}, got {mix}")
    if phase != "test" and mix is not None:
        raise ValueError(f"a {phase} experiment runs in the default mix; only the test set shifts it")
    if "incident" in entry:
        _check_incident(entry["incident"], phase)
    elif "cascade" in entry:
        _check_cascade(entry["cascade"], phase)
    else:
        _check_confounder(entry["plan"], phase)
