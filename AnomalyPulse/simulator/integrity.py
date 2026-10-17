import random

from simulator import dataset, injectors

PHASES = ("train", "val", "test", "stress")


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
        "stress": [(5000, 6000)],
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


def incident_spec(injector: str, phase: str, rng: random.Random, *, duration_s: int = FAULT_S,
                  target: str | None = None) -> dict:
    """One single-fault experiment's `incident`, with a band and intensity drawn for `phase`.

    `noop` (NORMAL) takes no band. A multi-target injector (lambda-degradation)
    gets a target drawn from its targets unless one is given.
    """
    spec = {"injector": injector, "baseline_s": BASELINE_S, "duration_s": duration_s,
            "recovery_s": RECOVERY_S}
    if injector == "noop":
        return spec
    choices = BANDS[injector][band_phase(phase)]
    if not choices:
        raise ValueError(f"{injector}: no {band_phase(phase)} bands")
    spec["band"] = band(injector, phase, rng.randrange(len(choices)))
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
CASCADES = {
    "spike-throttle-slowdown": [
        {"injector": "traffic-spike"},
        {"injector": "db-throttling", "onset_delay_s": 60, "onset_jitter_s": 10},
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


def cascade_spec(template: str, phase: str, rng: random.Random) -> dict:
    """One cascade experiment's `cascade` spec from a template, with bands and intensities for `phase`."""
    stages = []
    for stage in CASCADES[template]:
        injector = stage["injector"]
        choices = BANDS[injector][band_phase(phase)]
        stages.append({**stage, "intensity": _intensity(rng),
                       "band": band(injector, phase, rng.randrange(len(choices)))})
    return {"baseline_s": BASELINE_S, "cascade_s": FAULT_S, "recovery_s": RECOVERY_S, "stages": stages}


def confounder_plan(first: str, second: str, rng: random.Random) -> dict:
    """A test-set confounder's `plan`: two faults over the same window, labeled as the first."""
    faults = []
    for injector in (first, second):
        spec = incident_spec(injector, "test", rng)
        fault = {"injector": injector, "offset_s": BASELINE_S, "duration_s": FAULT_S,
                 "intensity": spec["intensity"], "band": spec["band"]}
        if "target" in spec:
            fault["target"] = spec["target"]
        faults.append(fault)
    return {"incident_type": injectors.get(first).incident_type, "recovery_s": RECOVERY_S,
            "faults": faults}
