"""Window Labeling Policy (M4-4; project plan §3).

One label per one-minute window of one experiment, derived from the manifest
alone (Tier-2). A window is named by its start, the UTC minute, in telemetry's
timestamp format: the same key M4-5's feature matrix uses (`window_start`), so
features and labels join on (experiment_id, window_start).

Every window of the run gets a record. A window either carries exactly one
label or is excluded with exactly one reason; excluded windows stay in the
labels file (and the log) so the exclusion is visible, but are not training
rows. The rules, applied in this order (the first that matches decides):

  1. partial_window    The window is not wholly inside [started_at, ended_at).
                       Its counts cover only part of a minute.
  2. startup           The window overlaps the run's first minute
                       [started_at, started_at + 60 s): startup 404s and cold
                       starts after the idle gap between runs.
  3. db_throttling_onset
                       The window overlaps a db-throttling activation's
                       [active_from, observed.label_from): the surge that spends
                       the burst bank and its aftermath. Any overlap excludes
                       it, so the surge never reaches a labeled window's
                       features (Docs/DB_THROTTLING_INJECTOR.md).
  4. CASCADING_FAILURE Two or more counted faults are active together for at
                       least half the window (cascade.multi_fault_windows on the
                       effective activations). In a run whose manifest is not
                       CASCADING_FAILURE (a test-set confounder: co-occurring
                       faults deliberately not labeled cascading) the window
                       takes the manifest's incident_type instead.
  5. single fault      A counted fault is active for at least half the window:
                       its injector's incident_type. If faults of different
                       types each pass while their overlap does not, the type
                       with the most active time wins; ties go to the fault
                       listed first in the manifest.
  6. recovery          No fault reaches half the window and the window overlaps
                       recovery (after the last activation ends).
  7. uncounted_fault   Only an uncounted fault (retry-amplification) is active
                       for half the window or more. It cannot be labeled by
                       itself and is not NORMAL.
  8. NORMAL            Anything else, including an onset window in which the
                       fault is active for under half the window.

Effective activity. A fault counts as active over [active_from, active_until),
except db-throttling, which counts from observed.label_from. A db-throttling
activation with no label_from never saw the bank spent; M4-1's batch reruns
such a run, so meeting one here is an error, not a label.

retry-amplification is not counted as a fault (decision recorded in
Docs/LABELING.md): it fails nothing; it re-sends requests another fault
already failed, so a window in which it overlaps one failing fault shows that
one fault plus more of its traffic. It is passed to multi_fault_windows as
exclude_injectors.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from simulator import cascade, injectors, manifest as mf

MINUTE_MS = 60_000
HALF_WINDOW_MS = MINUTE_MS // 2          # the activity threshold: >= 50% of the window
STARTUP_MS = MINUTE_MS                   # the run's first minute is dropped
UNCOUNTED_INJECTORS = ("retry-amplification",)
DB_THROTTLING = "db-throttling"

POLICY_VERSION = "m4-4/1"

LABELS = ("NORMAL", "TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION",
          "DEPENDENCY_FAILURE", "CASCADING_FAILURE")
EXCLUSION_REASONS = ("partial_window", "startup", "db_throttling_onset", "recovery",
                     "uncounted_fault")


class LabelingError(ValueError):
    """The manifest cannot be labeled under the policy (and the run should not be in the dataset)."""


@dataclass
class WindowLabel:
    window_start: str
    label: str | None                    # None when excluded
    excluded: str | None                 # the exclusion reason, None when labeled
    # Evidence, all Tier-2: milliseconds of the window each thing covers.
    fault_ms: dict = field(default_factory=dict)     # fault_id -> effective active ms (counted faults)
    multi_fault_ms: int = 0
    recovery_ms: int = 0

    def to_record(self) -> dict:
        return {"window_start": self.window_start, "label": self.label,
                "included": self.label is not None, "excluded_reason": self.excluded,
                "fault_ms": self.fault_ms, "multi_fault_ms": self.multi_fault_ms,
                "recovery_ms": self.recovery_ms}


def _floor_minute(ms: int) -> int:
    return ms - ms % MINUTE_MS


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def _effective(manifest_: dict) -> tuple[dict, list[tuple[int, int]]]:
    """The manifest with each db-throttling activation starting at label_from, and the onset stretches cut off."""
    activations, onsets = [], []
    for a in manifest_["activations"]:
        if a["injector"] == DB_THROTTLING:
            label_from = (a.get("observed") or {}).get("label_from")
            if not label_from:
                raise LabelingError(f"{a['fault_id']}: db-throttling with no observed.label_from "
                                    f"(the bank was never seen spent); M4-1 reruns such a run")
            start, until = mf.to_epoch_ms(a["active_from"]), mf.to_epoch_ms(a["active_until"])
            cut = mf.to_epoch_ms(label_from)
            if not start <= cut <= until:
                raise LabelingError(f"{a['fault_id']}: label_from {label_from} is outside its window")
            onsets.append((start, cut))
            a = {**a, "active_from": label_from}
        activations.append(a)
    return {**manifest_, "activations": activations}, onsets


def label_experiment(manifest_: dict) -> list[WindowLabel]:
    """Every one-minute window of a COMPLETED run, in time order, each labeled or excluded."""
    if manifest_.get("status") != "COMPLETED":
        raise LabelingError(f"{manifest_.get('experiment_id')}: status {manifest_.get('status')}, "
                            f"only COMPLETED runs are labeled")
    started, ended = mf.to_epoch_ms(manifest_["started_at"]), mf.to_epoch_ms(manifest_["ended_at"])
    effective, onsets = _effective(manifest_)

    counted = [a for a in effective["activations"] if a["injector"] not in UNCOUNTED_INJECTORS]
    uncounted = [a for a in effective["activations"] if a["injector"] in UNCOUNTED_INJECTORS]
    spans = [(mf.to_epoch_ms(a["active_from"]), mf.to_epoch_ms(a["active_until"]), a) for a in counted]
    uncounted_spans = [(mf.to_epoch_ms(a["active_from"]), mf.to_epoch_ms(a["active_until"]))
                       for a in uncounted]
    multi = [(mf.to_epoch_ms(f), mf.to_epoch_ms(u))
             for f, u in cascade.multi_fault_windows(effective, exclude_injectors=UNCOUNTED_INJECTORS)]
    # Recovery: from the last activation's end (planned or not) to the end of the run.
    # A NORMAL run has no activations and so no recovery: all of it is baseline.
    last_end = max((mf.to_epoch_ms(a["active_until"]) for a in manifest_["activations"]), default=None)
    recovery = (last_end, ended) if last_end is not None and last_end < ended else None
    cascading_run = manifest_["incident_type"] == "CASCADING_FAILURE"

    out = []
    for w0 in range(_floor_minute(started), ended, MINUTE_MS):
        w1 = w0 + MINUTE_MS
        fault_ms = {a["fault_id"]: _overlap(w0, w1, s, e) for s, e, a in spans}
        fault_ms = {k: v for k, v in fault_ms.items() if v}
        multi_ms = sum(_overlap(w0, w1, s, e) for s, e in multi)
        recovery_ms = _overlap(w0, w1, *recovery) if recovery else 0
        record = WindowLabel(mf.to_iso(w0), None, None, fault_ms, multi_ms, recovery_ms)

        if w0 < started or w1 > ended:
            record.excluded = "partial_window"
        elif _overlap(w0, w1, started, started + STARTUP_MS):
            record.excluded = "startup"
        elif any(_overlap(w0, w1, s, e) for s, e in onsets):
            record.excluded = "db_throttling_onset"
        elif multi_ms >= HALF_WINDOW_MS:
            record.label = "CASCADING_FAILURE" if cascading_run else manifest_["incident_type"]
        else:
            by_type: dict[str, int] = {}
            for s, e, a in spans:          # manifest order, so a tie keeps the earlier fault's type
                ms = _overlap(w0, w1, s, e)
                if ms >= HALF_WINDOW_MS:
                    kind = injectors.get(a["injector"]).incident_type
                    by_type[kind] = by_type.get(kind, 0) + ms
            if by_type:
                record.label = max(by_type, key=by_type.get)    # max keeps the first of equals
            elif recovery_ms:
                record.excluded = "recovery"
            elif any(_overlap(w0, w1, s, e) >= HALF_WINDOW_MS for s, e in uncounted_spans):
                record.excluded = "uncounted_fault"
            else:
                record.label = "NORMAL"
        out.append(record)
    _verify(manifest_, out)
    return out


def _verify(manifest_: dict, windows: list[WindowLabel]) -> None:
    """Checks on every experiment: one outcome per window, and a guard on the cascade rule.

    The guard holds each CASCADING_FAILURE window to M3's own concurrency timeline.
    That timeline starts db-throttling at active_from, no later than the labeler
    does, so the guard catches a change to the rule above (a lower threshold, a
    missed exclusion), not a timing error. test_run_kinds.py checks the timing
    against the activations directly, on every kind of run M4 generates."""
    for w in windows:
        if (w.label is None) == (w.excluded is None):
            raise AssertionError(f"{w.window_start}: needs exactly one of a label or an exclusion")
        if w.label is not None and w.label not in LABELS:
            raise AssertionError(f"{w.window_start}: unknown label {w.label}")
        if w.excluded is not None and w.excluded not in EXCLUSION_REASONS:
            raise AssertionError(f"{w.window_start}: unknown exclusion {w.excluded}")
    raw_multi = [(mf.to_epoch_ms(f), mf.to_epoch_ms(u)) for f, u in
                 cascade.multi_fault_windows(manifest_, exclude_injectors=UNCOUNTED_INJECTORS)]
    for w in windows:
        if w.label == "CASCADING_FAILURE":
            w0 = mf.to_epoch_ms(w.window_start)
            seen = sum(_overlap(w0, w0 + MINUTE_MS, s, e) for s, e in raw_multi)
            if seen < HALF_WINDOW_MS:
                raise AssertionError(f"{w.window_start}: CASCADING_FAILURE before the M3 timeline "
                                     f"shows two faults for half the window")


def summarize(experiment_id: str, manifest_: dict, windows: list[WindowLabel]) -> dict:
    """One log line per experiment: how many windows got each label or exclusion."""
    labels: dict[str, int] = {}
    excluded: dict[str, int] = {}
    for w in windows:
        if w.label:
            labels[w.label] = labels.get(w.label, 0) + 1
        else:
            excluded[w.excluded] = excluded.get(w.excluded, 0) + 1
    return {"experiment_id": experiment_id, "incident_type": manifest_["incident_type"],
            "policy": POLICY_VERSION, "windows": len(windows),
            "labeled": sum(labels.values()), "labels": labels,
            "excluded": sum(excluded.values()), "excluded_reasons": excluded,
            "excluded_windows": [{"window_start": w.window_start, "reason": w.excluded}
                                 for w in windows if w.excluded]}