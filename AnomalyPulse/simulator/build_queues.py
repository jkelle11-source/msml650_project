"""Queue generator for dataset v1's training and validation sets (M4-2).

Builds slots 1-700 from integrity.py's bands, builders and block mix, following
the rules in Docs/EVALUATION_INTEGRITY.md ("Building a queue"):

1. Blocks of 20 consecutive slots, each holding exactly integrity.TRAIN_BLOCK
   (4 NORMAL, 4 spike, 4 throttling, 3 Lambda, 3 payment, 2 cascades), shuffled
   with the generator's seed. Training is blocks 0-27, validation 28-34.
2. Bands half and half: a block's 4 spikes and 4 throttling runs are 2 + 2; its
   3 Lambda and 3 payment runs alternate 2/1 and 1/2 by block, in opposite
   bands to each other.
3. Lambda degradation's targets take turns within each band (the high band
   starting halfway round), so each target is half and half too. Cascades
   cycle the templates, and a template's runs go through every combination of
   its stages' bands, in complementary pairs, so each stage is half and half
   (_cascade_bands).
4. Slots are numbered after shuffling: slot i is the i-th run.
5./6. Four daily training queues of 140 slots, then one validation queue of 140.

Intensities are drawn by the builders, from a per-slot RNG.

    python -m simulator.build_queues            # write the five queue files
    python -m simulator.build_queues --check    # refuse if the files differ from a rebuild
"""
import argparse
import json
import random
import sys
from pathlib import Path

from simulator import dataset, injectors, integrity

# The generator's seed. Changing it changes every queue: don't, once generation starts.
SEED = 20261018

DATASET = dataset.DEFAULT_DATASET
BLOCK_SIZE = sum(integrity.TRAIN_BLOCK.values())                      # 20
TRAIN_BLOCKS = dataset.TRAIN_LAST // BLOCK_SIZE                       # 28
VAL_BLOCKS = (dataset.VAL_LAST - dataset.TRAIN_LAST) // BLOCK_SIZE    # 7
QUEUE_SLOTS = 140                                                     # one day's batch (~23.5 h)
QUEUE_DIR = Path(__file__).parent / "queues"

# Which band an odd count's extra run goes to on even blocks; odd blocks take the
# other. Lambda and payment start in opposite bands, so neither split leans low.
_EXTRA_BAND = {"LAMBDA_DEGRADATION": 0, "DEPENDENCY_FAILURE": 1}


def _band_indices(label: str, count: int, block: int) -> list:
    """`count` band indices for one class in one block, half and half over its two bands."""
    injector = integrity.INJECTOR_FOR[label]
    bands = len(integrity.BANDS[injector]["train"])
    if bands != 2:
        raise ValueError(f"{injector}: the generator splits two training bands, it has {bands}")
    indices = [0] * (count // 2) + [1] * (count // 2)
    if count % 2:
        indices.append((_EXTRA_BAND.get(label, 0) + block) % 2)
    return indices


def _cascade_bands(stages: int, run: int, flip: int) -> list:
    """Each stage's band index for a template's `run`-th run (0 is its first).

    The runs go through every combination of the stages' two bands, in
    complementary pairs: all low and all high first, then the rest with the
    first stage low and its complement. Every two runs hold each stage once in
    each band, so 14 training runs are 7/7 per stage and hold all 8 combinations
    of three stages, or 14 of four stages' 16 (the template's 15th and 16th
    runs, in validation, are the other two). `flip` swaps the order within each
    pair, so templates with an odd number of validation runs lean opposite ways.
    """
    pair = (run // 2) % 2 ** (stages - 1)
    first = [0] + [(pair >> (stages - 1 - j)) & 1 for j in range(1, stages)]
    second = (run + flip) % 2
    return [b ^ second for b in first]


def _block(block: int, rng: random.Random) -> list:
    """One block's 20 (label, band_index) pairs, in run order."""
    kinds = []
    for label, count in integrity.TRAIN_BLOCK.items():
        if label in ("NORMAL", "CASCADING_FAILURE"):
            kinds += [(label, None)] * count     # no band / bands set per template run
        else:
            kinds += [(label, i) for i in _band_indices(label, count, block)]
    rng.shuffle(kinds)
    return kinds


def entries(seed: int = SEED) -> list[dict]:
    """Every queue entry for slots 1-700, in slot (= run) order. Pure."""
    order = random.Random(f"{seed}:order")
    targets = injectors.get("lambda-degradation").targets
    templates = list(integrity.CASCADES)
    # Targets take turns within each band, not over all Lambda runs: in turn over
    # all of them, cart-service's runs split 12/9 by band in training and 5/0 in
    # validation. The high band starts halfway round, so every target has 21 in
    # training, 10 or 11 in each band.
    lambda_runs = [0, 0]
    cascade_runs = 0
    template_runs = dict.fromkeys(templates, 0)

    out, slot = [], 1
    for block in range(TRAIN_BLOCKS + VAL_BLOCKS):
        phase = "train" if block < TRAIN_BLOCKS else "val"
        for label, band_index in _block(block, order):
            spec_rng = random.Random(f"{seed}:spec:{slot}")
            # Unique per slot; a rerun of the slot reuses it (batch.py).
            entry = {"slot": slot, "phase": phase, "seed": (seed * 10_000 + slot) % 2**32}
            if label == "CASCADING_FAILURE":
                t = cascade_runs % len(templates)
                template = templates[t]
                bands = _cascade_bands(len(integrity.CASCADES[template]), template_runs[template], flip=t % 2)
                entry["cascade"] = integrity.cascade_spec(template, phase, spec_rng, band_indices=bands)
                cascade_runs += 1
                template_runs[template] += 1
            elif label == "LAMBDA_DEGRADATION":
                turn = lambda_runs[band_index] + band_index * len(targets) // 2
                entry["incident"] = integrity.incident_spec(
                    "lambda-degradation", phase, spec_rng,
                    target=targets[turn % len(targets)], band_index=band_index)
                lambda_runs[band_index] += 1
            else:
                entry["incident"] = integrity.incident_spec(
                    integrity.INJECTOR_FOR[label], phase, spec_rng, band_index=band_index)
            out.append(entry)
            slot += 1
    return out


def queues(seed: int = SEED) -> dict[str, dict]:
    """{file name: queue}: v1-train-1..4 (140 slots each), then v1-val."""
    all_entries = entries(seed)
    train = [e for e in all_entries if e["phase"] == "train"]
    val = [e for e in all_entries if e["phase"] == "val"]
    out = {}
    for day, start in enumerate(range(0, len(train), QUEUE_SLOTS), start=1):
        out[f"{DATASET}-train-{day}.json"] = {"dataset": DATASET,
                                              "experiments": train[start:start + QUEUE_SLOTS]}
    out[f"{DATASET}-val.json"] = {"dataset": DATASET, "experiments": val}
    return out


def _text(queue: dict) -> str:
    # One experiment per line: a diff of a queue file shows which slots changed.
    lines = ",\n".join("    " + json.dumps(e) for e in queue["experiments"])
    return f'{{\n  "dataset": {json.dumps(queue["dataset"])},\n  "experiments": [\n{lines}\n  ]\n}}\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulator.build_queues",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="don't write; exit 1 if the committed files differ from a rebuild")
    parser.add_argument("--out", type=Path, default=QUEUE_DIR, help=f"directory (default: {QUEUE_DIR})")
    args = parser.parse_args(argv)

    built = {name: _text(q) for name, q in queues().items()}
    if args.check:
        stale = [name for name, text in built.items()
                 if not (args.out / name).exists() or (args.out / name).read_text() != text]
        if stale:
            print(f"differ from a rebuild with seed {SEED}: {', '.join(stale)}", file=sys.stderr)
            return 1
        print(f"all {len(built)} queue files match a rebuild with seed {SEED}")
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in built.items():
        (args.out / name).write_text(text)
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
