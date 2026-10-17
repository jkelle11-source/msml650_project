"""Tests for the evaluation-integrity controls (M4-3). No test reaches AWS."""
import copy
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import batch, dataset, injectors, integrity, workload
from simulator.integrity import BANDS, band, check_bands, split_of, temporal_split


# --- Control 1: bands ------------------------------------------------------------

def test_the_bands_are_interleaved():
    check_bands()


def test_band_is_in_the_queue_format():
    assert band("traffic-spike", "train", 0) == {"parameter": "multiplier", "low": 2, "high": 4}
    assert band("db-throttling", "test", 1) == {"parameter": "load_ratio", "low": 4.5, "high": 5.5}


def test_validation_experiments_use_the_training_bands():
    assert band("lambda-degradation", "val", 1) == band("lambda-degradation", "train", 1)


def test_the_plan_table_is_unchanged():
    # Plan section 6b. If this fails, the plan and the code disagree.
    assert BANDS["traffic-spike"]["train"] == [(2, 4), (6, 8)]
    assert BANDS["traffic-spike"]["test"] == [(4, 6), (8, 10)]
    assert BANDS["lambda-degradation"]["train"] == [(200, 500), (800, 1100)]
    assert BANDS["lambda-degradation"]["test"] == [(500, 800), (1100, 1400)]
    assert BANDS["db-throttling"]["train"] == [(1.5, 2.5), (3.5, 4.5)]
    assert BANDS["db-throttling"]["test"] == [(2.5, 3.5), (4.5, 5.5)]
    assert BANDS["payment-failure"]["train"] == [(500, 1500), (2500, 3500)]
    assert BANDS["payment-failure"]["test"] == [(1500, 2500), (3500, 4500)]


def test_stress_starts_at_the_plans_thresholds():
    assert BANDS["traffic-spike"]["stress"][0][0] == 10
    assert BANDS["lambda-degradation"]["stress"][0][0] == 1500
    assert BANDS["db-throttling"]["stress"][0][0] == 6
    assert BANDS["payment-failure"]["stress"][0][0] == 5000


def _broken(**change):
    bands = copy.deepcopy(BANDS)
    bands["traffic-spike"].update(change)
    return bands


@pytest.mark.parametrize("change, problem", [
    ({"test": [(3, 5), (8, 10)]}, "overlaps"),                   # a test band inside a training one
    ({"train": [(2, 4), (4, 6)], "test": [(6, 8), (8, 10)]}, "alternate"),   # side by side, not interleaved
    ({"stress": [(9, 12)]}, "stress"),                            # stress that isn't extrapolation
    ({"parameter": "load_ratio"}, "parameter"),                   # wrong parameter for the injector
    ({"train": [(4, 2), (6, 8)]}, "empty"),                       # a band upside down
])
def test_check_bands_catches(change, problem):
    with pytest.raises(ValueError, match=problem):
        check_bands(_broken(**change))


def test_check_bands_catches_an_unknown_injector():
    bands = copy.deepcopy(BANDS)
    bands["no-such-thing"] = bands.pop("traffic-spike")
    with pytest.raises(ValueError, match="no such injector"):
        check_bands(bands)


def test_no_intensity_reaches_a_shared_edge():
    # Train 2-4x and test 4-6x share 4.0. The drawn intensities never reach it.
    low, high = integrity.INTENSITY_RANGE
    for name, spec in BANDS.items():
        cls = injectors.get(name)
        for phase in ("train", "test"):
            for lo, hi in spec[phase]:
                b = {"parameter": spec["parameter"], "low": lo, "high": hi}
                assert lo < cls.native_value(low, b) and cls.native_value(high, b) < hi


# --- counts and slots ------------------------------------------------------

def test_counts_match_the_plan():
    assert sum(integrity.COUNTS_TRAIN.values()) == dataset.VAL_LAST == 700
    assert sum(integrity.COUNTS_TEST.values()) == 350
    assert (integrity.TEST_FIRST, integrity.TEST_LAST, integrity.STRESS_FIRST) == (701, 1050, 1051)


# --- Control 2: the temporal split -------------------------------------------

@pytest.mark.parametrize("slot, expected", [
    (1, "train"), (560, "train"), (561, "val"), (700, "val"),
    (701, "test"), (1050, "test"), (1051, "stress"), (1070, "stress"),
])
def test_split_of(slot, expected):
    assert split_of(slot) == expected
    assert split_of(slot, expected) == expected


def test_split_of_agrees_with_the_batch_runner():
    # The batch accepts a phase only if dataset.phases_for allows it.
    for slot in (1, 560, 561, 700, 701, 1050, 1051):
        assert split_of(slot) in dataset.phases_for(slot)


@pytest.mark.parametrize("slot, phase", [(10, "test"), (600, "train"), (705, "stress"), (1060, "test")])
def test_split_of_refuses_a_phase_that_disagrees_with_the_slot(slot, phase):
    with pytest.raises(ValueError, match=f"slot {slot}"):
        split_of(slot, phase)


@pytest.mark.parametrize("slot", [0, -3, 1.5, True, "7"])
def test_split_of_refuses_a_bad_slot(slot):
    with pytest.raises(ValueError):
        split_of(slot)


def _record(slot, experiment_id, date, phase=None):
    return {"slot": slot, "phase": phase or split_of(slot), "experiment_id": experiment_id,
            "generation_date": date}


def test_temporal_split_maps_each_experiment_to_its_partition():
    records = [
        _record(1, "exp-a", "2026-10-18"),
        _record(600, "exp-b", "2026-10-22"),
        _record(701, "exp-c", "2026-10-25"),
        _record(1051, "exp-d", "2026-10-25"),
        {"slot": 2, "phase": "train", "experiment_id": None, "generation_date": None},   # not accepted
    ]
    assert temporal_split(records) == {"exp-a": "train", "exp-b": "val", "exp-c": "test",
                                       "exp-d": "stress"}


def test_temporal_split_refuses_a_test_run_on_a_training_day():
    records = [_record(5, "exp-a", "2026-10-22"), _record(710, "exp-b", "2026-10-22")]
    with pytest.raises(ValueError, match="exp-b"):
        temporal_split(records)


def test_temporal_split_refuses_a_test_run_before_training_ended():
    records = [_record(5, "exp-a", "2026-10-22"), _record(710, "exp-b", "2026-10-20")]
    with pytest.raises(ValueError, match="last training day"):
        temporal_split(records)


def test_temporal_split_refuses_a_phase_that_disagrees_with_the_slot():
    with pytest.raises(ValueError, match="slot 3"):
        temporal_split([_record(3, "exp-a", "2026-10-22", phase="test")])


# --- spec builders ----------------------------------------------------------

def test_incident_spec_is_reproducible():
    a = integrity.incident_spec("traffic-spike", "train", random.Random(5))
    b = integrity.incident_spec("traffic-spike", "train", random.Random(5))
    assert a == b


def test_incident_spec_draws_from_the_phases_bands():
    rng = random.Random(1)
    for _ in range(50):
        for phase in ("train", "val", "test", "stress"):
            spec = integrity.incident_spec("db-throttling", phase, rng)
            bounds = (spec["band"]["low"], spec["band"]["high"])
            assert bounds in BANDS["db-throttling"][integrity.band_phase(phase)]
            low, high = integrity.INTENSITY_RANGE
            assert low <= spec["intensity"] <= high


def test_a_normal_run_has_no_band():
    assert integrity.incident_spec("noop", "train", random.Random(1)) == {
        "injector": "noop", "baseline_s": 180, "duration_s": 300, "recovery_s": 120}


def test_lambda_degradation_gets_a_target():
    spec = integrity.incident_spec("lambda-degradation", "train", random.Random(2))
    assert spec["target"] in injectors.get("lambda-degradation").targets


def test_every_retry_stage_overlaps_payment_failure():
    for name, stages in integrity.CASCADES.items():
        injectors_in = [s["injector"] for s in stages]
        if "retry-amplification" in injectors_in:
            assert "payment-failure" in injectors_in, name
            assert injectors_in.index("payment-failure") < injectors_in.index("retry-amplification"), name


def test_the_test_mix_differs_from_training():
    assert len(integrity.TEST_MIX) == len(workload.ROUTES)
    assert dict(zip(workload.ROUTES, integrity.TEST_MIX)) != workload.DEFAULT_MIX


# --- every spec is a queue entry the batch runner accepts --------------------

def _entries():
    """One queue entry per kind of experiment M4-2, M9-1 and the stress slice will generate."""
    rng = random.Random(2026)
    entries, slot = [], 0

    def add(phase, mix=None, **kind):
        nonlocal slot
        slot += 1
        if phase == "test":
            slot = max(slot, integrity.TEST_FIRST)
        if phase == "stress":
            slot = max(slot, integrity.STRESS_FIRST)
        entry = {"slot": slot, "phase": phase, "seed": 4000 + slot, **kind}
        if mix is not None:
            entry["mix"] = mix
        entries.append(entry)

    for phase in ("train", "test"):
        mix = integrity.TEST_MIX if phase == "test" else None
        for incident_type, injector in integrity.INJECTOR_FOR.items():
            for _ in range(3):
                add(phase, mix, incident=integrity.incident_spec(injector, phase, rng))
        for template in integrity.CASCADES:
            add(phase, mix, cascade=integrity.cascade_spec(template, phase, rng))
    for first, second in integrity.CONFOUNDER_PAIRS:
        add("test", integrity.TEST_MIX, plan=integrity.confounder_plan(first, second, rng))
    for duration_s in integrity.VARIED_DURATIONS_S:
        for injector in ("traffic-spike", "db-throttling", "lambda-degradation", "payment-failure"):
            add("test", integrity.TEST_MIX,
                incident=integrity.incident_spec(injector, "test", rng, duration_s=duration_s))
    for injector in integrity.COUNTS_STRESS:
        add("stress", incident=integrity.incident_spec(injector, "stress", rng))
    return entries


def test_every_spec_passes_the_batch_runners_queue_check(tmp_path):
    entries = _entries()
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"dataset": "integrity-check", "experiments": entries}))
    queue = batch.load_queue(path)                      # resolves every entry, no AWS

    labels = set(queue.incident_types.values())
    assert labels == {"NORMAL", "TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION",
                      "DEPENDENCY_FAILURE", "CASCADING_FAILURE"}


def test_confounders_are_never_labeled_cascading(tmp_path):
    rng = random.Random(9)
    for first, second in integrity.CONFOUNDER_PAIRS:
        plan = integrity.confounder_plan(first, second, rng)
        assert plan["incident_type"] == injectors.get(first).incident_type != "CASCADING_FAILURE"
        assert {f["offset_s"] for f in plan["faults"]} == {integrity.BASELINE_S}
