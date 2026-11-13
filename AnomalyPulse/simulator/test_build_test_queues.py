"""Tests for the test set's and stress slice's queues (M9-1). No AWS.

Run from AnomalyPulse/:  python -m pytest simulator/test_build_test_queues.py -q
"""
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import batch, build_queues, injectors, integrity

TEST = build_queues.test_entries()
STRESS = build_queues.stress_entries()
BLOCK = build_queues.TEST_BLOCK_SIZE


def _kind(entry):
    """The TEST_BLOCK kind of a test entry."""
    if "cascade" in entry:
        return "CASCADING_FAILURE"
    if "plan" in entry:
        return "confounder"
    if entry["incident"]["duration_s"] != integrity.FAULT_S:
        return "varied_duration"
    return injectors.get(entry["incident"]["injector"]).incident_type


def _band(injector, band, phase="test"):
    """0 for the phase's lower band, 1 for its upper."""
    return [low for low, _ in integrity.BANDS[injector][phase]].index(band["low"])


def _singles(injector, entries=TEST, varied=False):
    return [e["incident"] for e in entries if "incident" in e and e["incident"]["injector"] == injector
            and (e["incident"]["duration_s"] != integrity.FAULT_S) == varied]


# --- slots, phases, counts -------------------------------------------------------------------

def test_slots_701_to_1050_then_the_stress_slice_to_1070():
    assert [e["slot"] for e in TEST] == list(range(701, 1051))
    assert [e["slot"] for e in STRESS] == list(range(1051, 1071))
    assert all(e["phase"] == "test" for e in TEST) and all(e["phase"] == "stress" for e in STRESS)
    assert all(integrity.split_of(e["slot"], e["phase"]) == e["phase"] for e in TEST + STRESS)


def test_counts_match_plan_section_17():
    assert Counter(map(_kind, TEST)) == integrity.COUNTS_TEST
    assert Counter(e["incident"]["injector"] for e in STRESS) == integrity.COUNTS_STRESS


def test_every_block_of_70_holds_exactly_the_test_block():
    for start in range(0, len(TEST), BLOCK):
        assert Counter(map(_kind, TEST[start:start + BLOCK])) == integrity.TEST_BLOCK


def test_blocks_are_shuffled_not_in_a_fixed_order():
    orders = {tuple(map(_kind, TEST[s:s + BLOCK])) for s in range(0, len(TEST), BLOCK)}
    assert len(orders) == len(TEST) // BLOCK


def test_every_entry_passes_the_guard():
    for entry in TEST + STRESS:
        integrity.check_entry(entry)


# --- the diversity protocol ------------------------------------------------------------------

def test_every_test_run_has_the_shifted_mix_and_the_stress_slice_the_default():
    assert all(e["mix"] == integrity.TEST_MIX for e in TEST)
    assert all("mix" not in e for e in STRESS)


def test_confounders_cycle_the_pairs_and_are_labeled_as_their_first_fault():
    plans = [e["plan"] for e in TEST if "plan" in e]
    pairs = [tuple(f["injector"] for f in p["faults"]) for p in plans]
    assert pairs[:4] == list(integrity.CONFOUNDER_PAIRS)
    assert sorted(Counter(pairs).values()) == [7, 7, 8, 8]
    for plan, pair in zip(plans, pairs):
        assert plan["incident_type"] == injectors.get(pair[0]).incident_type != "CASCADING_FAILURE"


def test_each_confounder_pair_goes_through_its_band_combinations():
    combos = {}
    for e in TEST:
        if "plan" in e:
            faults = e["plan"]["faults"]
            pair = tuple(f["injector"] for f in faults)
            combos.setdefault(pair, Counter())[tuple(_band(f["injector"], f["band"]) for f in faults)] += 1
    for pair, seen in combos.items():
        assert set(seen) == {(0, 0), (1, 1), (0, 1), (1, 0)}, pair
        assert max(seen.values()) - min(seen.values()) <= 1, pair


def test_varied_durations_are_5_per_injector_10_per_duration_and_10_per_band():
    varied = [e["incident"] for e in TEST if _kind(e) == "varied_duration"]
    assert Counter(i["injector"] for i in varied) == {
        "traffic-spike": 5, "db-throttling": 5, "lambda-degradation": 5, "payment-failure": 5}
    assert Counter(i["duration_s"] for i in varied) == {120: 10, 720: 10}
    assert Counter(_band(i["injector"], i["band"]) for i in varied) == {0: 10, 1: 10}
    for injector in ("traffic-spike", "db-throttling", "lambda-degradation", "payment-failure"):
        runs = _singles(injector, varied=True)
        assert {(i["duration_s"], _band(injector, i["band"])) for i in runs} == {
            (120, 0), (120, 1), (720, 0), (720, 1)}, injector


# --- bands, targets, templates ---------------------------------------------------------------

@pytest.mark.parametrize("injector", ["traffic-spike", "db-throttling"])
def test_spike_and_throttling_bands_are_half_and_half(injector):
    assert Counter(_band(injector, i["band"]) for i in _singles(injector)) == {0: 30, 1: 30}


def test_lambda_and_payment_bands_lean_one_run_opposite_ways():
    lam = Counter(_band("lambda-degradation", i["band"]) for i in _singles("lambda-degradation"))
    pay = Counter(_band("payment-failure", i["band"]) for i in _singles("payment-failure"))
    assert lam == {0: 23, 1: 22} and pay == {0: 22, 1: 23}


def test_only_test_bands_and_safe_intensities_are_used():
    low, high = integrity.INTENSITY_RANGE
    for e in TEST:
        faults = (e["cascade"]["stages"] if "cascade" in e else e["plan"]["faults"] if "plan" in e
                  else [e["incident"]] if e["incident"]["injector"] != "noop" else [])
        for f in faults:
            assert f["band"]["low"] in [b for b, _ in integrity.BANDS[f["injector"]]["test"]]
            assert low <= f["intensity"] <= high


def test_lambda_targets_take_turns_within_each_band():
    runs = _singles("lambda-degradation")
    counts = Counter((i["target"], _band("lambda-degradation", i["band"])) for i in runs)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert set(Counter(i["target"] for i in runs).values()) <= {11, 12}


def test_cascades_cycle_the_templates_through_their_band_combinations():
    cascades = [e["cascade"] for e in TEST if "cascade" in e]
    shapes = [[{k: v for k, v in s.items() if k not in ("intensity", "band")} for s in c["stages"]]
              for c in cascades]
    names = [next(n for n, t in integrity.CASCADES.items() if t == shape) for shape in shapes]
    assert names[:4] == list(integrity.CASCADES)
    assert sorted(Counter(names).values()) == [7, 7, 8, 8]
    for name in integrity.CASCADES:
        runs = [c for c, n in zip(cascades, names) if n == name]
        for j in range(len(integrity.CASCADES[name])):
            stage = Counter(_band(r["stages"][j]["injector"], r["stages"][j]["band"]) for r in runs)
            assert abs(stage[0] - stage[1]) <= 1, (name, j)


def test_the_stress_slice_uses_each_injectors_stress_band():
    for e in STRESS:
        i = e["incident"]
        assert i["duration_s"] == integrity.FAULT_S
        low, high = integrity.BANDS[i["injector"]]["stress"][0]
        assert (i["band"]["low"], i["band"]["high"]) == (low, high)
    targets = Counter(e["incident"]["target"] for e in STRESS if "target" in e["incident"])
    assert sum(targets.values()) == 5 and len(targets) == 4


# --- seeds and files -------------------------------------------------------------------------

def test_seeds_are_unique_across_the_whole_dataset_and_valid():
    seeds = [e["seed"] for e in build_queues.entries() + TEST + STRESS]
    assert len(set(seeds)) == len(seeds) == 1070
    assert all(0 <= s < 2**32 for s in seeds)


def test_the_test_seed_rebuilds_the_same_entries_and_leaves_training_alone():
    assert build_queues.test_entries() == TEST and build_queues.stress_entries() == STRESS
    assert build_queues.test_entries(seed=1) != TEST
    assert build_queues.TEST_SEED != build_queues.SEED
    assert build_queues.queues(test_seed=1)["v1-train-1.json"] == build_queues.queues()["v1-train-1.json"]


def test_three_test_queues_then_the_stress_slice():
    queues = build_queues.queues()
    names = ["v1-test-1.json", "v1-test-2.json", "v1-test-3.json", "v1-stress.json"]
    assert list(queues)[5:] == names
    assert [len(queues[n]["experiments"]) for n in names] == [140, 140, 70, 20]
    assert [e["slot"] for n in names for e in queues[n]["experiments"]] == list(range(701, 1071))


def test_the_test_queues_run_in_about_a_day_each():
    hours = {n: sum(batch.run_seconds(batch.plan_for(e, "check")) for e in q["experiments"]) / 3600
             for n, q in build_queues.queues().items() if "test" in n}
    assert all(h < 24 for h in hours.values()), hours


# --- the validation queue (M9-1, before the test set) ----------------------------------------

def test_the_validation_queue_tries_each_untested_kind_at_its_heaviest_outside_v1():
    queue = batch.load_queue(build_queues.QUEUE_DIR / "m9-1-validation.json")
    assert queue.dataset == "m9-1-validation" != integrity.GUARDED_DATASET
    entries = list(queue.entries)
    for entry in entries:
        integrity.check_entry(entry)      # the test set's rules too, so it tries what the test set runs
    singles = {(e["phase"], e["incident"]["injector"], e["incident"]["duration_s"])
               for e in entries if "incident" in e}
    assert singles == {("test", injector, duration)
                       for injector in ("traffic-spike", "db-throttling", "lambda-degradation", "payment-failure")
                       for duration in (720,)} | {("test", "db-throttling", 120)} | {
                       ("stress", injector, integrity.FAULT_S) for injector in integrity.COUNTS_STRESS}
    assert {tuple(f["injector"] for f in e["plan"]["faults"]) for e in entries if "plan" in e} == set(
        integrity.CONFOUNDER_PAIRS)
    for e in entries:
        for f in e["plan"]["faults"] if "plan" in e else [e["incident"]]:
            assert f["intensity"] == integrity.INTENSITY_RANGE[1]
            if e["phase"] == "test":
                assert _band(f["injector"], f["band"]) == 1      # the upper test band
