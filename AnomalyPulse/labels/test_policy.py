"""Tests for the Window Labeling Policy (M4-4). No AWS: manifests are built here.

Runs start at hh:mm:59.412, as the M4-1 batches' runs do (each starts as the last
one's export settles), so a run's first window is a 0.6 s sliver.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labels import policy
from simulator import manifest as mf

EID = "exp-20261016T223859Z-0550"
S = mf.to_epoch_ms("2026-10-16T22:38:59.412Z")


def t(seconds):
    return mf.to_iso(S + round(seconds * 1000))


def activation(n, injector, start_s, end_s, observed=None, target=None):
    return {"fault_id": f"{EID}#{n}", "injector": injector, "target": target, "intensity": 0.5,
            "band": {"parameter": "x", "low": 0, "high": 1}, "params": {},
            "observed": observed or {}, "active_from": t(start_s), "active_until": t(end_s)}


def run(incident_type, activations, length_s=600, status="COMPLETED"):
    return {"schema_version": "1.0", "experiment_id": EID, "status": status,
            "incident_type": incident_type, "severity": None if incident_type == "NORMAL" else "MEDIUM",
            "seed": 1, "created_by": "test", "started_at": t(0), "ended_at": t(length_s),
            "workload": {}, "activations": activations,
            "telemetry": {"bucket": "b", "prefixes": ["raw/"]}}


def outcomes(windows):
    return [(w.window_start[11:16], w.label or f"x:{w.excluded}") for w in windows]


def test_normal_run_drops_partial_and_startup_and_keeps_the_rest():
    got = outcomes(policy.label_experiment(run("NORMAL", [])))
    assert got[0] == ("22:38", "x:partial_window")
    assert got[1] == ("22:39", "x:startup")
    assert got[-1] == ("22:48", "x:partial_window")
    assert {label for _, label in got[2:-1]} == {"NORMAL"}
    assert len(got) == 11


def test_single_fault_follows_the_activity_threshold_and_excludes_recovery():
    got = outcomes(policy.label_experiment(run("TRAFFIC_SPIKE", [activation(0, "traffic-spike", 180, 480)])))
    assert got == [
        ("22:38", "x:partial_window"), ("22:39", "x:startup"),
        ("22:40", "NORMAL"),
        ("22:41", "NORMAL"),             # the spike begins 0.6 s before this window ends: under 50%
        ("22:42", "TRAFFIC_SPIKE"), ("22:43", "TRAFFIC_SPIKE"), ("22:44", "TRAFFIC_SPIKE"),
        ("22:45", "TRAFFIC_SPIKE"),
        ("22:46", "TRAFFIC_SPIKE"),      # 59.4 s of spike, then recovery: the threshold wins
        ("22:47", "x:recovery"),
        ("22:48", "x:partial_window"),
    ]


def test_threshold_is_exactly_half_the_window():
    # Onset 30 s into window 22:41 -> exactly 30 s active: labeled. 30.001 s in -> NORMAL.
    at_half = (mf.to_epoch_ms("2026-10-16T22:41:30.000Z") - S) / 1000
    w = {x[0]: x[1] for x in outcomes(policy.label_experiment(
        run("TRAFFIC_SPIKE", [activation(0, "traffic-spike", at_half, 480)])))}
    assert w["22:41"] == "TRAFFIC_SPIKE"
    w = {x[0]: x[1] for x in outcomes(policy.label_experiment(
        run("TRAFFIC_SPIKE", [activation(0, "traffic-spike", at_half + 0.001, 480)])))}
    assert w["22:41"] == "NORMAL"


def test_trailing_window_under_half_active_is_recovery_not_normal():
    # Fault ends 20 s into 22:46: 20 s active, 40 s recovery.
    end = (mf.to_epoch_ms("2026-10-16T22:46:20.000Z") - S) / 1000
    w = dict(outcomes(policy.label_experiment(run("LAMBDA_DEGRADATION", [
        activation(0, "lambda-degradation", 180, end, target="order-service")]))))
    assert w["22:46"] == "x:recovery"


def test_db_throttling_counts_from_label_from_and_excludes_the_onset():
    act = activation(0, "db-throttling", 180, 480, observed={"label_from": t(180 + 48.1)},
                     target="product-service")
    got = outcomes(policy.label_experiment(run("DATABASE_THROTTLING", [act])))
    assert got[2:10] == [
        ("22:40", "NORMAL"),
        ("22:41", "x:db_throttling_onset"),   # holds the surge's first 0.6 s
        ("22:42", "x:db_throttling_onset"),   # surge, bank spent, label_from at 22:42:47.5
        ("22:43", "DATABASE_THROTTLING"), ("22:44", "DATABASE_THROTTLING"),
        ("22:45", "DATABASE_THROTTLING"), ("22:46", "DATABASE_THROTTLING"),
        ("22:47", "x:recovery"),
    ]


def test_db_throttling_without_label_from_is_refused():
    act = activation(0, "db-throttling", 180, 480, observed={"label_from": None})
    with pytest.raises(policy.LabelingError, match="label_from"):
        policy.label_experiment(run("DATABASE_THROTTLING", [act]))


def test_only_completed_runs():
    with pytest.raises(policy.LabelingError, match="COMPLETED"):
        policy.label_experiment(run("NORMAL", [], status="FAILED"))


def slot11(retry_start_s=300):
    # M4-1's slot 11 shape: spike from 180 s, payment failure ~60 s later, retries ~60 s after that.
    return run("CASCADING_FAILURE", [
        activation(0, "traffic-spike", 180, 480),
        activation(1, "payment-failure", 245, 480, target="payment-service"),
        activation(2, "retry-amplification", retry_start_s, 480),
    ])


def test_cascade_starts_single_fault_then_cascading_once_two_overlap_for_half_a_window():
    got = dict(outcomes(policy.label_experiment(slot11())))
    assert got["22:42"] == "TRAFFIC_SPIKE"       # spike alone (payment begins 22:43:04.4)
    assert got["22:43"] == "CASCADING_FAILURE"   # 55.6 s of spike + payment
    assert got["22:44"] == got["22:45"] == got["22:46"] == "CASCADING_FAILURE"
    assert got["22:47"] == "x:recovery"


def test_payment_onset_late_in_window_is_not_premature_cascade():
    m = slot11()
    m["activations"][1]["active_from"] = "2026-10-16T22:43:45.000Z"   # 15 s of overlap in 22:43
    got = dict(outcomes(policy.label_experiment(m)))
    assert got["22:43"] == "TRAFFIC_SPIKE"
    assert got["22:44"] == "CASCADING_FAILURE"


def test_retry_amplification_is_not_counted_as_a_fault():
    # Payment failure + retries only: one counted fault, so DEPENDENCY_FAILURE, never cascading.
    m = run("CASCADING_FAILURE", [
        activation(0, "payment-failure", 180, 480, target="payment-service"),
        activation(1, "retry-amplification", 240, 480),
    ])
    labels = {w.label for w in policy.label_experiment(m) if w.label}
    assert labels == {"NORMAL", "DEPENDENCY_FAILURE"}


def test_window_with_only_retry_amplification_is_excluded():
    m = run("CASCADING_FAILURE", [
        activation(0, "traffic-spike", 180, 300),
        activation(1, "payment-failure", 200, 300, target="payment-service"),
        activation(2, "retry-amplification", 220, 480),      # outlives the faults it amplifies
    ])
    got = dict(outcomes(policy.label_experiment(m)))
    assert got["22:45"] == "x:uncounted_fault"


def test_cascade_with_db_throttling_uses_label_from_for_concurrency():
    # Spike at 180 s, throttling at 240 s labeled from 288 s: a cascade only from 288 s.
    m = run("CASCADING_FAILURE", [
        activation(0, "traffic-spike", 180, 480),
        activation(1, "db-throttling", 240, 480, observed={"label_from": t(288)}),
    ])
    got = dict(outcomes(policy.label_experiment(m)))
    assert got["22:42"] == "x:db_throttling_onset"     # spike alone, but the surge's first 0.6 s
    assert got["22:43"] == "x:db_throttling_onset"     # surge until 22:43:47.4
    assert got["22:44"] == "CASCADING_FAILURE"


def test_confounder_multi_fault_windows_take_the_manifest_type():
    m = run("TRAFFIC_SPIKE", [
        activation(0, "traffic-spike", 180, 480),
        activation(1, "lambda-degradation", 180, 480, target="order-service"),
    ])
    labels = {w.label for w in policy.label_experiment(m) if w.label}
    assert labels == {"NORMAL", "TRAFFIC_SPIKE"}


def test_every_window_has_exactly_one_outcome_and_labels_are_deterministic():
    m = slot11()
    first = [w.to_record() for w in policy.label_experiment(m)]
    second = [w.to_record() for w in policy.label_experiment(m)]
    assert first == second
    for r in first:
        assert (r["label"] is None) != (r["excluded_reason"] is None)
        assert r["included"] == (r["label"] is not None)


def test_window_keys_match_the_feature_matrix_format():
    w = policy.label_experiment(run("NORMAL", []))
    assert w[2].window_start == "2026-10-16T22:40:00.000Z"


def test_summary_logs_every_exclusion():
    m = slot11()
    windows = policy.label_experiment(m)
    s = policy.summarize(EID, m, windows)
    assert s["labeled"] + s["excluded"] == len(windows)
    assert s["excluded_reasons"] == {"partial_window": 2, "startup": 1, "recovery": 1}
    assert len(s["excluded_windows"]) == s["excluded"]