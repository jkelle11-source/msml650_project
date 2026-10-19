"""Labels for every kind of run M4-2 and M9-1 generate (M4-4). No AWS.

The manifests come from integrity's spec builders through batch.plan_for() and
runner.preview(), the path run-batch takes, so a change to a cascade template,
a confounder pair or a run shape is tested here as soon as it is made. Each
run starts at a different second of the minute, as real runs do.

Each labeled window is checked against what was active in it, worked out here
from the activations' own times (db-throttling from label_from, retries left
out), not through cascade.multi_fault_windows() as the labeler does: so a
CASCADING_FAILURE window really has two faults for half of it, and a window
that does is never labeled anything else.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labels import policy
from simulator import batch, injectors, integrity, runner, workload, manifest as mf

BASE_MS = mf.to_epoch_ms("2026-10-16T22:38:00.000Z")
LABEL_FROM_S = 48       # label_from came 47-48 s into every throttling stage on the runner (M4-3)
LOAD = workload.Workload(rps=20, seed=7, log=lambda *a: None)    # preview() never starts it
SEEDS = range(20)
SINGLE = [i for i in integrity.INJECTOR_FOR.values() if i != "noop"]


def _kinds():
    """(name, entry builder) for each kind of queue entry, the builder taking an rng."""
    kinds = [("normal", lambda rng: {"incident": integrity.incident_spec("noop", "train", rng)})]
    for injector in SINGLE:
        for phase in ("train", "test"):
            kinds.append((f"{injector} {phase}", lambda rng, i=injector, p=phase:
                          {"incident": integrity.incident_spec(i, p, rng)}))
        for duration_s in integrity.VARIED_DURATIONS_S:
            kinds.append((f"{injector} {duration_s} s", lambda rng, i=injector, d=duration_s:
                          {"incident": integrity.incident_spec(i, "test", rng, duration_s=d)}))
    for injector in integrity.COUNTS_STRESS:
        kinds.append((f"{injector} stress", lambda rng, i=injector:
                      {"incident": integrity.incident_spec(i, "stress", rng)}))
    for template in integrity.CASCADES:
        for phase in ("train", "test"):
            kinds.append((f"cascade {template} {phase}", lambda rng, t=template, p=phase:
                          {"cascade": integrity.cascade_spec(t, p, rng)}))
    for first, second in integrity.CONFOUNDER_PAIRS:
        kinds.append((f"confounder {first} + {second}", lambda rng, a=first, b=second:
                      {"plan": integrity.confounder_plan(a, b, rng)}))
    return kinds


KINDS = _kinds()


def completed(entry: dict, seed: int, start_ms: int | None = None) -> dict:
    """The manifest run-batch would write for `entry`, had every fault run as planned."""
    plan = batch.plan_for({"slot": 1, "seed": seed, **entry}, "test")
    if start_ms is None:
        start_ms = BASE_MS + random.Random(f"start:{seed}").randrange(60_000)
    m = runner.preview(plan, workload=LOAD, now_ms=start_ms)
    m["status"] = "COMPLETED"
    m["ended_at"] = mf.to_iso(start_ms + round(batch.run_seconds(plan) * 1000))
    for a in m["activations"]:
        if a["injector"] == "db-throttling":
            label_from = mf.to_epoch_ms(a["active_from"]) + LABEL_FROM_S * 1000
            a["observed"] = {**(a.get("observed") or {}), "label_from": mf.to_iso(label_from)}
    return m


def _active(m: dict) -> list[tuple[int, int, str]]:
    """(from, until, incident type) of each counted fault, from the activations directly."""
    spans = []
    for a in m["activations"]:
        if a["injector"] in policy.UNCOUNTED_INJECTORS:
            continue
        start = a["observed"]["label_from"] if a["injector"] == "db-throttling" else a["active_from"]
        spans.append((mf.to_epoch_ms(start), mf.to_epoch_ms(a["active_until"]),
                      injectors.get(a["injector"]).incident_type))
    return spans


def _two_or_more_ms(spans, w0: int, w1: int) -> int:
    """Milliseconds of [w0, w1) in which two or more of `spans` are active."""
    cuts = sorted({w0, w1, *(t for s, e, _ in spans for t in (s, e) if w0 < t < w1)})
    return sum(b - a for a, b in zip(cuts, cuts[1:])
               if sum(s <= a and b <= e for s, e, _ in spans) >= 2)


def check_run(m: dict, windows: list) -> None:
    """Each window's outcome against what was active in it. Raises AssertionError."""
    spans = _active(m)
    onsets = [(mf.to_epoch_ms(a["active_from"]), mf.to_epoch_ms(a["observed"]["label_from"]))
              for a in m["activations"] if a["injector"] == "db-throttling"]
    cascading_run = m["incident_type"] == "CASCADING_FAILURE"
    half = policy.HALF_WINDOW_MS
    for w in windows:
        w0 = mf.to_epoch_ms(w.window_start)
        w1 = w0 + policy.MINUTE_MS
        where = f"{m['incident_type']} {w.window_start} {w.label or w.excluded}"
        multi = _two_or_more_ms(spans, w0, w1)
        by_type: dict = {}
        for s, e, kind in spans:
            by_type[kind] = by_type.get(kind, 0) + max(0, min(w1, e) - max(w0, s))
        if w.label is None:
            # Partial, startup and onset windows go whatever was active; the others only
            # when no fault fills half of them.
            if w.excluded in ("recovery", "uncounted_fault"):
                assert multi < half and all(ms < half for ms in by_type.values()), \
                    f"{where}: excluded with a fault active half of it"
            continue
        assert not any(min(w1, e) > max(w0, s) for s, e in onsets), f"{where}: overlaps a surge"
        if multi >= half:
            assert w.label == ("CASCADING_FAILURE" if cascading_run else m["incident_type"]), where
        elif w.label == "NORMAL":
            assert all(ms < half for ms in by_type.values()), f"{where}: a fault is active half of it"
        else:
            assert w.label != "CASCADING_FAILURE", f"{where}: two faults for only {multi} ms"
            assert by_type.get(w.label, 0) >= half, f"{where}: its fault is active {by_type.get(w.label)} ms"


@pytest.mark.parametrize("name,build", KINDS, ids=[name for name, _ in KINDS])
def test_every_run_kind_labels_by_what_was_active(name, build):
    for seed in SEEDS:
        m = completed(build(random.Random(seed)), seed)
        windows = policy.label_experiment(m)
        check_run(m, windows)
        labels = {w.label for w in windows if w.label}
        if m["incident_type"] == "NORMAL":
            assert labels == {"NORMAL"}
        elif m["incident_type"] == "CASCADING_FAILURE":
            assert "CASCADING_FAILURE" in labels, f"{name} seed {seed}: never two faults at once"
        else:
            # A single fault or a confounder: its own type and NORMAL only. check_run has
            # already held every window a fault fills half of to that type, so a run with
            # none (a 2-minute throttling fault, below) has none to label.
            assert labels <= {"NORMAL", m["incident_type"]}, f"{name} seed {seed}: {labels}"
            if not name.startswith("db-throttling 120"):
                assert m["incident_type"] in labels, f"{name} seed {seed}: {labels}"


def test_a_two_minute_throttling_fault_can_leave_no_labeled_window():
    # Labeling starts at label_from, ~48 s in, so 72 s of the 120 s are labeled. The window
    # holding label_from is excluded as onset; when label_from falls in a minute's first
    # 18 s, under half a window is left after it, and that window is recovery.
    entry = {"incident": integrity.incident_spec("db-throttling", "test", random.Random(1),
                                                  duration_s=120)}
    at_23 = policy.label_experiment(completed(entry, 1, start_ms=BASE_MS + 23_000))
    assert [w.label for w in at_23 if w.label] == ["NORMAL"]          # label_from at :11
    at_59 = policy.label_experiment(completed(entry, 1, start_ms=BASE_MS + 59_412))
    assert [w.label for w in at_59 if w.label] == ["NORMAL", "DATABASE_THROTTLING"]


def test_check_run_catches_a_premature_cascade():
    # The check is independent of the labeler: a cascade label one window early fails it.
    name, build = next(k for k in KINDS if k[0].startswith("cascade spike-payment-retry"))
    m = completed(build(random.Random(0)), 0)
    windows = policy.label_experiment(m)
    first = next(i for i, w in enumerate(windows) if w.label == "CASCADING_FAILURE")
    assert windows[first - 1].label == "TRAFFIC_SPIKE"
    windows[first - 1].label = "CASCADING_FAILURE"
    with pytest.raises(AssertionError, match="two faults for only"):
        check_run(m, windows)
