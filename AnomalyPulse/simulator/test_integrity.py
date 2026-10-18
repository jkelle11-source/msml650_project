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


def test_payment_stress_stays_below_orders_read_timeout():
    # Order stops waiting for Payment at 6 s (services/order/handler.py). Past
    # that, requests fail as Order's timeouts, not Payment's 503.
    assert max(high for _, high in BANDS["payment-failure"]["stress"]) <= 5500


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


def test_the_blocks_keep_the_plans_proportions():
    block = integrity.TRAIN_BLOCK
    assert sum(block.values()) == 20
    assert {label: 35 * n for label, n in block.items()} == integrity.COUNTS_TRAIN
    # 28 blocks are training (1-560), 7 validation (561-700).
    assert 28 * 20 == dataset.TRAIN_LAST and 7 * 20 == dataset.VAL_LAST - dataset.TRAIN_LAST
    assert [7 * n for n in block.values()] == [28, 28, 28, 21, 21, 14]
    assert sum(integrity.TEST_BLOCK.values()) == 70
    assert {kind: 5 * n for kind, n in integrity.TEST_BLOCK.items()} == integrity.COUNTS_TEST


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


def test_band_index_picks_the_band():
    for index, (low, high) in enumerate(BANDS["lambda-degradation"]["train"]):
        spec = integrity.incident_spec("lambda-degradation", "val", random.Random(1), band_index=index)
        assert (spec["band"]["low"], spec["band"]["high"]) == (low, high)


@pytest.mark.parametrize("band_index", [2, -1, 1.0, True])
def test_a_bad_band_index_is_refused(band_index):
    with pytest.raises(ValueError, match="band_index"):
        integrity.incident_spec("traffic-spike", "train", random.Random(1), band_index=band_index)


def test_cascades_and_confounders_take_band_indices():
    spec = integrity.cascade_spec("spike-payment-retry", "train", random.Random(1), band_indices=[1, 0, None])
    assert [s["band"] for s in spec["stages"][:2]] == [band("traffic-spike", "train", 1),
                                                      band("payment-failure", "train", 0)]
    with pytest.raises(ValueError, match="needs 3 entries"):
        integrity.cascade_spec("spike-payment-retry", "train", random.Random(1), band_indices=[1, 0])
    plan = integrity.confounder_plan("traffic-spike", "lambda-degradation", random.Random(1),
                                     band_indices=[1, 0])
    assert [f["band"] for f in plan["faults"]] == [band("traffic-spike", "test", 1),
                                                   band("lambda-degradation", "test", 0)]


def test_a_normal_run_has_no_band():
    assert integrity.incident_spec("noop", "train", random.Random(1)) == {
        "injector": "noop", "baseline_s": 180, "duration_s": 300, "recovery_s": 120}


def test_lambda_degradation_gets_a_target():
    spec = integrity.incident_spec("lambda-degradation", "train", random.Random(2))
    assert spec["target"] in injectors.get("lambda-degradation").targets


def test_the_stress_slice_has_no_cascades():
    with pytest.raises(ValueError, match="no cascades"):
        integrity.cascade_spec("spike-payment-retry", "stress", random.Random(1))


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


def test_confounders_are_never_labeled_cascading():
    rng = random.Random(9)
    for first, second in integrity.CONFOUNDER_PAIRS:
        plan = integrity.confounder_plan(first, second, rng)
        assert plan["incident_type"] == injectors.get(first).incident_type != "CASCADING_FAILURE"
        assert {f["offset_s"] for f in plan["faults"]} == {integrity.BASELINE_S}


# --- the queue guard -----------------------------------------------------------

def test_every_builder_entry_passes_the_guard():
    for entry in _entries():
        integrity.check_entry(entry)


def _incident(slot, phase, injector="traffic-spike", mix=None, seed=3, **change):
    spec = {**integrity.incident_spec(injector, phase, random.Random(seed)), **change}
    entry = {"slot": slot, "phase": phase, "seed": 4000 + slot, "incident": spec}
    if mix is not None:
        entry["mix"] = mix
    return entry


def _cascade(slot, phase, mix=None, stage_change=None, **change):
    spec = {**integrity.cascade_spec("spike-payment-retry", phase, random.Random(4)), **change}
    if stage_change:
        spec["stages"][1] = {**spec["stages"][1], **stage_change}
    entry = {"slot": slot, "phase": phase, "seed": 4000 + slot, "cascade": spec}
    if mix is not None:
        entry["mix"] = mix
    return entry


def _confounder(slot, phase="test", pair=("traffic-spike", "lambda-degradation"), mix=integrity.TEST_MIX,
                fault_change=None, **change):
    plan = {**integrity.confounder_plan(*pair, random.Random(5)), **change}
    if fault_change:
        plan["faults"][1] = {**plan["faults"][1], **fault_change}
    return {"slot": slot, "phase": phase, "seed": 4000 + slot, "mix": mix, "plan": plan}


TEST = integrity.TEST_MIX


@pytest.mark.parametrize("entry, problem", [
    # Control 1: the phase's bands, away from shared edges
    (_incident(5, "train", band={"parameter": "multiplier", "low": 4, "high": 6}), "not one of its train bands"),
    (_incident(600, "val", band={"parameter": "multiplier", "low": 10, "high": 12}), "not one of its train bands"),
    (_incident(750, "test", mix=TEST, band={"parameter": "multiplier", "low": 2, "high": 4}),
     "not one of its test bands"),
    (_incident(5, "train", intensity=1.0), "intensity"),
    (_incident(750, "test", mix=TEST, intensity=0.0), "intensity"),
    (_cascade(5, "train", stage_change={"band": {"parameter": "timeout_ms", "low": 1500, "high": 2500}}),
     "payment-failure: band"),
    # Control 2: the phase the slot says
    (_incident(1060, "test", mix=TEST), "slot 1060 is stress"),
    (_incident(705, "stress"), "slot 705 is test"),
    # Control 3: the diversity protocol in the test set only
    (_incident(750, "test"), "shifted mix"),
    (_incident(5, "train", mix=TEST), "default mix"),
    (_incident(1060, "stress", mix=TEST), "default mix"),
    (_incident(5, "train", duration_s=120), "lasts 300 s"),
    (_incident(750, "test", injector="noop", mix=TEST, duration_s=720), "lasts 300 s"),
    (_confounder(5, "train", mix=None), "only plans are the test set's confounders"),
    (_confounder(750, pair=("lambda-degradation", "traffic-spike")), "not a confounder pair"),
    (_confounder(750, incident_type="CASCADING_FAILURE"), "labeled as its first fault"),
    (_confounder(750, fault_change={"offset_s": 240, "duration_s": 240}), "both faults"),
    # the run shape and the cascade templates
    (_incident(5, "train", baseline_s=300), "run shape"),
    (_cascade(5, "train", cascade_s=420), "run shape"),
    (_cascade(5, "train", stage_change={"onset_delay_s": 90}), "not one of the templates"),
])
def test_the_guard_refuses(entry, problem):
    with pytest.raises(ValueError, match=problem):
        integrity.check_entry(entry)


def test_a_stress_cascade_is_refused():
    entry = _cascade(1060, "test")
    entry["phase"] = "stress"
    with pytest.raises(ValueError, match="no cascades"):
        integrity.check_entry(entry)


def test_the_varied_durations_pass_in_the_test_set_only():
    for duration_s in integrity.VARIED_DURATIONS_S:
        integrity.check_entry(_incident(750, "test", mix=TEST, duration_s=duration_s))
        with pytest.raises(ValueError, match="lasts 300 s"):
            integrity.check_entry(_incident(1060, "stress", duration_s=duration_s))


def _write(tmp_path, name, entries):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"dataset": name, "experiments": entries}))
    return path


def test_load_queue_guards_the_main_dataset(tmp_path):
    bad = [_incident(5, "train", band={"parameter": "multiplier", "low": 4, "high": 6})]
    with pytest.raises(ValueError, match="slot 5: traffic-spike: band"):
        batch.load_queue(_write(tmp_path, integrity.GUARDED_DATASET, bad))
    # A validation dataset may probe edges and test bands on purpose.
    batch.load_queue(_write(tmp_path, "some-validation", bad))
    batch.load_queue(_write(tmp_path, integrity.GUARDED_DATASET, _entries()))


def test_the_m4_3_validation_queues_cover_every_template():
    # Slots 1-2 ran the spike before the throttling, the order that validation retired
    # (the client dropped requests); m4-3-validation-2.json runs it throttling first.
    retired = ["traffic-spike", "db-throttling", "lambda-degradation"]
    templates = set()
    for name in ("m4-3-validation.json", "m4-3-validation-2.json"):
        queue = batch.load_queue(Path(batch.__file__).parent / "queues" / name)
        assert queue.dataset == "m4-3-validation"
        for entry in queue.entries:
            stages = [s["injector"] for s in entry["cascade"]["stages"]]
            if stages == retired:
                assert entry["slot"] in (1, 2)
                continue
            integrity.check_entry(entry)      # it would pass in v1 too
            templates |= {t for t, spec in integrity.CASCADES.items() if [s["injector"] for s in spec] == stages}
    assert templates == set(integrity.CASCADES)


def test_db_throttling_never_starts_during_a_spike():
    # Its surge's end would put more requests in flight than the client holds.
    for name, stages in integrity.CASCADES.items():
        injectors_in = [s["injector"] for s in stages]
        if "db-throttling" in injectors_in and "traffic-spike" in injectors_in:
            assert injectors_in.index("db-throttling") < injectors_in.index("traffic-spike"), name
