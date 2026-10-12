"""Cascading failure orchestration (M3-5).

A cascade is not an injector that wraps other injectors. It is one run with
several activations whose windows start one after another and overlap, which
the runner already schedules, records and tears down. This module adds what a
cascade needs on top of that:

    build_plan            stages with onset delays -> a runner.Plan
    load_spec             the same, from a JSON file (simulator/cascades/)
    concurrency_timeline  which faults were active together, over time
    multi_fault_windows   the stretches with two or more faults active

The shape of a cascade. Stages are listed in the order they begin. Each begins
`onset_delay_s` after the one before it (plus a seeded jitter, so repeated runs
do not line up exactly), and they all stay active until the cascade ends, so
the later ones start while the earlier ones are still going:

    traffic-spike          |================================|
    db-throttling              |============================|
    lambda-degradation               |======================|
    payment-failure                       |=================|
    retry-amplification                        |============|
              baseline ----^                                 ^---- recovery

The early part of such a run has only one fault active. That is intended:
M4 labels those windows by the single fault (project plan, window labeling
policy) and CASCADING_FAILURE only where concurrency_timeline() shows two or
more. The manifest's `activations` already hold the windows, so nothing
extra is stored: the timeline is derived from them.

Magnitudes come from each stage's band, never from this file.
"""
import json
import random
from dataclasses import dataclass
from pathlib import Path

from simulator import injectors, manifest
from simulator.runner import Fault, Plan

# Cost bound. A run is baseline + cascade + recovery, and the workload sends
# traffic the whole time, so its length is its cost. build_plan() refuses a
# longer one unless the caller raises the limit on purpose.
MAX_RUN_S = 30 * 60

SPEC_DIR = Path(__file__).parent / "cascades"


@dataclass(frozen=True)
class Stage:
    """One fault of the cascade. Times are in seconds."""
    injector: str                  # registry name
    intensity: float | None = None
    band: dict | None = None
    target: str | None = None
    # After the previous stage begins (for the first stage: after the baseline).
    onset_delay_s: float = 0
    # The onset moves by a seeded amount from -jitter to +jitter. 0 is exact.
    onset_jitter_s: float = 0
    # None: stays active until the cascade ends. Otherwise it ends this long
    # after its own onset, which must not be past the end of the cascade.
    duration_s: float | None = None


def _onsets(stages, seed) -> list[float]:
    """Each stage's onset in seconds from the start of the cascade, in stage order."""
    onsets, previous = [], 0.0
    for index, stage in enumerate(stages):
        # Its own RNG, apart from the runner's per-activation ones (which are
        # seeded "<seed>:<index>"), so jitter never shifts a resolved param.
        rng = random.Random(f"{seed}:cascade-onset:{index}")
        jitter = rng.uniform(-stage.onset_jitter_s, stage.onset_jitter_s) if stage.onset_jitter_s else 0.0
        # A stage never begins before the one listed ahead of it.
        onset = max(previous, previous + stage.onset_delay_s + jitter)
        onsets.append(round(onset, 3))
        previous = onset
    return onsets


def build_plan(stages, *, baseline_s: float, cascade_s: float, recovery_s: float, seed: int,
               created_by: str, max_run_s: float = MAX_RUN_S) -> Plan:
    """The runner.Plan for a cascade.

    `cascade_s` runs from the first stage beginning to the last window closing.
    Raises ValueError, before anything is touched, for a cascade that cannot
    overlap its stages, a stage that is not a fault, or a run past `max_run_s`.
    """
    stages = tuple(stages)
    if len(stages) < 2:
        raise ValueError("a cascade needs at least two stages")
    if min(baseline_s, cascade_s, recovery_s) < 0 or cascade_s == 0:
        raise ValueError("baseline and recovery cannot be negative, and the cascade needs a length")
    total_s = baseline_s + cascade_s + recovery_s
    if total_s > max_run_s:
        raise ValueError(f"this run would last {total_s:g} s, past the limit of {max_run_s:g} s; "
                         f"shorten it, or raise max_run_s knowingly (the workload sends traffic "
                         f"the whole time)")

    for stage in stages:
        if stage.onset_delay_s < 0 or stage.onset_jitter_s < 0:
            raise ValueError(f"{stage.injector}: onset delay and jitter cannot be negative")
        if injectors.get(stage.injector).incident_type == "NORMAL":
            raise ValueError(f"{stage.injector}: injects nothing, so it is not a cascade stage")

    onsets = _onsets(stages, seed)
    faults = []
    for stage, onset in zip(stages, onsets):
        if onset >= cascade_s:
            raise ValueError(f"{stage.injector}: it would begin {onset:g} s into a cascade that "
                             f"lasts {cascade_s:g} s; shorten the delays or lengthen the cascade")
        end = cascade_s if stage.duration_s is None else onset + stage.duration_s
        if end > cascade_s:
            raise ValueError(f"{stage.injector}: it would end {end:g} s into a cascade that "
                             f"lasts {cascade_s:g} s")
        faults.append(Fault(stage.injector, offset_s=round(baseline_s + onset, 3),
                            duration_s=round(end - onset, 3), intensity=stage.intensity,
                            band=stage.band, target=stage.target))

    # The stages must really be active together, or this is not a cascade.
    if not any(a.offset_s + a.duration_s > b.offset_s for a, b in zip(faults, faults[1:])):
        raise ValueError("no two stages overlap; lengthen a stage or shorten the delays")

    return Plan(incident_type="CASCADING_FAILURE", faults=tuple(faults), recovery_s=recovery_s,
                seed=seed, created_by=created_by)


# --- specs -----------------------------------------------------------------

_SPEC_KEYS = {"baseline_s", "cascade_s", "recovery_s", "stages"}
_STAGE_KEYS = {"injector", "intensity", "band", "target", "onset_delay_s", "onset_jitter_s",
               "duration_s"}


def load_spec(source) -> dict:
    """Read a cascade spec (a path, or an already-loaded dict) into build_plan() arguments.

        {"baseline_s": 300, "cascade_s": 600, "recovery_s": 300,
         "stages": [{"injector": "traffic-spike", "intensity": 0.6,
                     "band": {"parameter": "multiplier", "low": 2, "high": 4}},
                    {"injector": "db-throttling", "onset_delay_s": 60, "onset_jitter_s": 10, ...}]}

    Returns {"stages", "baseline_s", "cascade_s", "recovery_s"}. Unknown keys are
    an error, so a misspelled one cannot silently fall back to a default.
    """
    if isinstance(source, (str, Path)):
        source = json.loads(Path(source).read_text())
    spec = source
    if not isinstance(spec, dict) or set(spec) != _SPEC_KEYS:
        raise ValueError(f"a cascade spec has exactly these keys: {', '.join(sorted(_SPEC_KEYS))}")
    if not isinstance(spec["stages"], list):
        raise ValueError("stages must be a list")
    stages = []
    for raw in spec["stages"]:
        unknown = set(raw) - _STAGE_KEYS if isinstance(raw, dict) else _STAGE_KEYS
        if not isinstance(raw, dict) or "injector" not in raw or unknown:
            raise ValueError(f"each stage is an object with an injector and, optionally, "
                             f"{', '.join(sorted(_STAGE_KEYS - {'injector'}))}")
        stages.append(Stage(**raw))
    return {"stages": stages, "baseline_s": spec["baseline_s"], "cascade_s": spec["cascade_s"],
            "recovery_s": spec["recovery_s"]}


# --- what was active together ----------------------------------------------

def concurrency_timeline(manifest_: dict, *, exclude_injectors=()) -> list[dict]:
    """Which faults were active together, over time, from a manifest's activations.

    A list of segments in order, each `{"from", "until", "count", "faults",
    "injectors"}`: a stretch in which exactly that set of faults was active.
    Stretches with nothing active are left out. A COMPLETED manifest holds the
    times the faults really began and ended; any other holds the planned ones.

    `exclude_injectors` leaves some out of the count. Retry amplification adds
    load but is not a failure of any service, so a labeler that wants only the
    service faults can pass ("retry-amplification",).
    """
    spans = [(manifest.to_epoch_ms(a["active_from"]), manifest.to_epoch_ms(a["active_until"]), a)
             for a in manifest_["activations"] if a["injector"] not in exclude_injectors]
    edges = sorted({edge for start, end, _ in spans for edge in (start, end)})

    segments = []
    for start, end in zip(edges, edges[1:]):
        active = [a for s, e, a in spans if s <= start and end <= e]
        if not active:
            continue
        faults = [a["fault_id"] for a in active]
        injector_names = [a["injector"] for a in active]
        if segments and segments[-1]["faults"] == faults and segments[-1]["_end"] == start:
            segments[-1]["_end"] = end            # the same set carries on
            continue
        segments.append({"_start": start, "_end": end, "faults": faults,
                         "injectors": injector_names})
    return [{"from": manifest.to_iso(s.pop("_start")), "until": manifest.to_iso(s.pop("_end")),
             "count": len(s["faults"]), **s} for s in segments]


def multi_fault_windows(manifest_: dict, *, exclude_injectors=()) -> list[tuple[str, str]]:
    """(from, until) of each stretch with two or more faults active. Only these may be
    labeled CASCADING_FAILURE; the stretches before them have one fault and are
    labeled by it."""
    windows = []
    for segment in concurrency_timeline(manifest_, exclude_injectors=exclude_injectors):
        if segment["count"] < 2:
            continue
        if windows and windows[-1][1] == segment["from"]:
            windows[-1] = (windows[-1][0], segment["until"])
        else:
            windows.append((segment["from"], segment["until"]))
    return windows


def describe_timeline(manifest_: dict, *, exclude_injectors=()) -> str:
    """The timeline as text, for the CLI."""
    lines = []
    for segment in concurrency_timeline(manifest_, exclude_injectors=exclude_injectors):
        mark = "  <- multi-fault" if segment["count"] >= 2 else ""
        lines.append(f"{segment['from']} -> {segment['until']}  {segment['count']} active: "
                     f"{', '.join(segment['injectors'])}{mark}")
    return "\n".join(lines) or "no faults were active"
