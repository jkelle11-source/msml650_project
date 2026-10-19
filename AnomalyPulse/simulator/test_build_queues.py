import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import batch, build_queues, injectors, integrity

ENTRIES = build_queues.entries()


def _label(entry):
    if "cascade" in entry:
        return "CASCADING_FAILURE"
    return injectors.get(entry["incident"]["injector"]).incident_type


def _split(phase):
    return [e for e in ENTRIES if e["phase"] == phase]


def _band_counts(entries, injector):
    """Counter of band index (0 low, 1 high) over the single-fault runs of `injector`."""
    lows = [low for low, _ in integrity.BANDS[injector]["train"]]
    return Counter(lows.index(e["incident"]["band"]["low"]) for e in entries
                   if "incident" in e and e["incident"]["injector"] == injector)


# --- slots, phases, class balance ------------------------------------------------------------

def test_slots_1_to_700_in_order_with_the_train_val_cut():
    assert [e["slot"] for e in ENTRIES] == list(range(1, 701))
    assert all(e["phase"] == ("train" if e["slot"] <= 560 else "val") for e in ENTRIES)
    assert all(integrity.split_of(e["slot"], e["phase"]) == e["phase"] for e in ENTRIES)


def test_class_counts_match_plan_section_17():
    assert Counter(map(_label, ENTRIES)) == integrity.COUNTS_TRAIN


def test_validation_keeps_the_training_proportions():
    assert Counter(map(_label, _split("val"))) == {
        "NORMAL": 28, "TRAFFIC_SPIKE": 28, "DATABASE_THROTTLING": 28,
        "LAMBDA_DEGRADATION": 21, "DEPENDENCY_FAILURE": 21, "CASCADING_FAILURE": 14}


def test_every_block_of_20_holds_exactly_the_block_mix():
    for start in range(0, 700, 20):
        block = ENTRIES[start:start + 20]
        assert Counter(map(_label, block)) == integrity.TRAIN_BLOCK, f"block at slot {start + 1}"


def test_blocks_are_shuffled_not_in_a_fixed_order():
    orders = {tuple(map(_label, ENTRIES[s:s + 20])) for s in range(0, 700, 20)}
    assert len(orders) > 30


# --- bands -----------------------------------------------------------------------------------

def test_spike_and_throttling_bands_are_half_and_half_in_each_split():
    for injector in ("traffic-spike", "db-throttling"):
        assert _band_counts(_split("train"), injector) == {0: 56, 1: 56}
        assert _band_counts(_split("val"), injector) == {0: 14, 1: 14}
        for start in range(0, 700, 20):
            assert _band_counts(ENTRIES[start:start + 20], injector) == {0: 2, 1: 2}


def test_lambda_and_payment_alternate_and_lean_opposite_ways():
    assert _band_counts(_split("train"), "lambda-degradation") == {0: 42, 1: 42}
    assert _band_counts(_split("train"), "payment-failure") == {0: 42, 1: 42}
    assert _band_counts(_split("val"), "lambda-degradation") == {0: 11, 1: 10}
    assert _band_counts(_split("val"), "payment-failure") == {0: 10, 1: 11}
    assert _band_counts(ENTRIES, "lambda-degradation") == {0: 53, 1: 52}
    assert _band_counts(ENTRIES, "payment-failure") == {0: 52, 1: 53}


def test_only_training_bands_and_safe_intensities_are_used():
    # Training and validation both use the training bands; no test or stress band appears.
    def magnitudes(entry):
        if "incident" in entry:
            return [] if entry["incident"]["injector"] == "noop" else [entry["incident"]]
        return entry["cascade"]["stages"]

    for entry in ENTRIES:
        for m in magnitudes(entry):
            spec = integrity.BANDS[m["injector"]]
            assert (m["band"]["low"], m["band"]["high"]) in spec["train"]
            assert (m["band"]["low"], m["band"]["high"]) not in spec["test"] + spec["stress"]
            assert integrity.INTENSITY_RANGE[0] <= m["intensity"] <= integrity.INTENSITY_RANGE[1]


# --- targets and cascade templates -----------------------------------------------------------

def test_lambda_targets_round_robin():
    targets = [e["incident"]["target"] for e in ENTRIES
               if "incident" in e and e["incident"]["injector"] == "lambda-degradation"]
    assert targets[:8] == list(injectors.get("lambda-degradation").targets) * 2
    train = targets[:84]
    assert Counter(train) == {t: 21 for t in injectors.get("lambda-degradation").targets}


def _template(entry):
    shape = [{k: v for k, v in s.items() if k not in ("intensity", "band")}
             for s in entry["cascade"]["stages"]]
    return next(name for name, stages in integrity.CASCADES.items() if stages == shape)


def test_cascades_cycle_the_templates():
    train = Counter(_template(e) for e in _split("train") if "cascade" in e)
    val = Counter(_template(e) for e in _split("val") if "cascade" in e)
    assert train == {name: 14 for name in integrity.CASCADES}
    assert sorted(val.values()) == [3, 3, 4, 4]


def _stage_bands(entry):
    """A cascade's band index (0 low, 1 high) per stage, in stage order."""
    return tuple(integrity.BANDS[s["injector"]]["train"].index((s["band"]["low"], s["band"]["high"]))
                 for s in entry["cascade"]["stages"])


def test_each_template_goes_through_every_combination_of_stage_bands():
    for name, stages in integrity.CASCADES.items():
        train = [_stage_bands(e) for e in _split("train") if "cascade" in e and _template(e) == name]
        val = [_stage_bands(e) for e in _split("val") if "cascade" in e and _template(e) == name]
        for j in range(len(stages)):
            assert Counter(r[j] for r in train) == {0: 7, 1: 7}, f"{name} stage {j}"
        # Three stages: all 8 combinations in training. Four: 14 of 16, and validation has the other two.
        assert len(set(train)) == min(len(train), 2 ** len(stages)), name
        assert len(set(train + val)) == 2 ** len(stages), name


def test_validation_cascade_stages_lean_at_most_one_run_either_way():
    for injector in integrity.BANDS:
        counts = Counter(b for e in _split("val") if "cascade" in e
                         for s, b in zip(e["cascade"]["stages"], _stage_bands(e)) if s["injector"] == injector)
        assert abs(counts[0] - counts[1]) <= 1, injector


# --- seeds, reproducibility, files -----------------------------------------------------------

def test_seeds_are_unique_and_valid():
    seeds = [e["seed"] for e in ENTRIES]
    assert len(set(seeds)) == 700
    assert all(0 <= s < 2**32 for s in seeds)


def test_same_seed_rebuilds_the_same_queues():
    assert build_queues.entries() == ENTRIES
    assert build_queues.entries(seed=1) != ENTRIES


def test_four_daily_training_queues_then_validation():
    queues = build_queues.queues()
    assert list(queues) == ["v1-train-1.json", "v1-train-2.json", "v1-train-3.json",
                            "v1-train-4.json", "v1-val.json"]
    assert all(q["dataset"] == "v1" and len(q["experiments"]) == 140 for q in queues.values())
    assert [e["slot"] for q in queues.values() for e in q["experiments"]] == list(range(1, 701))
    assert {e["phase"] for e in queues["v1-val.json"]["experiments"]} == {"val"}


@pytest.mark.parametrize("name", list(build_queues.queues()))
def test_committed_queue_files_match_a_rebuild(name):
    # The queue files are the record of what was asked for; they must be what the seed builds.
    path = build_queues.QUEUE_DIR / name
    assert path.read_text() == build_queues._text(build_queues.queues()[name])


@pytest.mark.parametrize("name", list(build_queues.queues()))
def test_committed_queue_files_pass_the_batch_runners_check(name):
    # load_queue resolves every entry as its run would, and runs integrity's guard on v1.
    queue = batch.load_queue(build_queues.QUEUE_DIR / name)
    assert queue.dataset == "v1" and len(queue.entries) == 140


def test_check_mode_reports_a_stale_file(tmp_path, capsys):
    assert build_queues.main(["--out", str(tmp_path)]) == 0
    assert build_queues.main(["--check", "--out", str(tmp_path)]) == 0
    (tmp_path / "v1-val.json").write_text("{}")
    assert build_queues.main(["--check", "--out", str(tmp_path)]) == 1
    assert "v1-val.json" in capsys.readouterr().err
