"""Tests for M9-3's baseline scoring, on M9-2's synthetic world. No real data, no AWS.

Run from AnomalyPulse/, in the training image's versions:
    python -m pytest ml/test_m9_3_baseline.py ml/test_m9_2_metrics.py -q
"""
import numpy as np
import pytest

from ml import m9_2_metrics as m
from ml import m9_3_baseline as b
from ml import model0
from ml import test_m9_2_metrics as tm

ORIGINAL_FEATS = tm.feats


def feats_with_model0_columns(looks, k):
    """M9-2's synthetic features plus the 12 columns Model 0 reads: a fault makes Order's p95 5x its
    healthy 100 ms, and the previous window's p95 was always 100 ms."""
    out = dict(ORIGINAL_FEATS(looks, k))
    for svc in model0.SERVICES:
        p95 = 500.0 if looks != "NORMAL" and svc == "order" else 100.0
        out[f"{svc}_p95_latency_ms"] = p95
        out[f"{svc}_p95_latency_ms_change"] = p95 - 100.0
        out[f"{svc}_error_rate_5xx"] = 0.0
    return out


def make(tmp_path, monkeypatch, with_columns=True):
    if with_columns:
        monkeypatch.setattr(tm, "feats", feats_with_model0_columns)
    world, qdir = tm.build(tmp_path)
    meta = m.tag_runs(m.load_registry(world.root), m.load_specs(qdir))
    return m.score_runs(world.root, meta, [], [])


@pytest.fixture
def scored(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch)


def by_run(scored, flags):
    out = {}
    for (eid, row), flagged in zip(scored.rows, flags):
        out.setdefault(eid, []).append((row["label"], bool(flagged)))
    return out


def headline(scored):
    return [e for e, t in scored.meta.items() if t["phase"] == "test" and t["kind"] not in m.HEADLINE_EXCLUDES]


def test_flags_line_up_with_the_labeled_windows(scored):
    flags = b.model0_flags(scored)
    assert set(flags) == set(b.NAMES)
    for f in flags.values():
        assert f.dtype == bool and len(f) == len(scored.rows)


def test_healthy_runs_are_never_flagged(scored):
    for name, flags in b.model0_flags(scored).items():
        for eid, windows in by_run(scored, flags).items():
            if scored.meta[eid]["kind"] == "normal":
                assert not any(flagged for _, flagged in windows), name


def test_previous_window_flags_every_fault_window_of_a_single_fault_run(scored):
    flags = by_run(scored, b.model0_flags(scored)[b.PREVIOUS])
    singles = [eid for eid, t in scored.meta.items() if t["kind"] == "single"]
    assert singles
    for eid in singles:
        for label, flagged in flags[eid]:
            assert flagged == (label != m.NORMAL)


def test_earlier_mean_flags_only_the_first_two_minutes_of_a_sustained_fault(scored):
    flags = by_run(scored, b.model0_flags(scored)[b.EARLIER])
    singles = [eid for eid, t in scored.meta.items() if t["kind"] == "single"]
    assert singles
    for eid in singles:
        windows = flags[eid]
        assert not any(flagged for label, flagged in windows if label == m.NORMAL), eid
        fault = [flagged for label, flagged in windows if label != m.NORMAL]
        expected = min(2, len(fault))
        assert fault[:expected] == [True] * expected and sum(fault) == expected, eid


def test_a_run_without_model0_columns_is_refused(tmp_path, monkeypatch):
    plain = make(tmp_path, monkeypatch, with_columns=False)
    with pytest.raises(m.MetricsError, match="Model 0 reads columns"):
        b.model0_flags(plain)


def test_add_baselines_puts_the_flags_in_m9_2s_scored_object(scored):
    scored.preds["clf"] = [row["label"] for _, row in scored.rows]
    flag_only = b.add_baselines(scored, "clf")
    assert set(flag_only) == {*b.NAMES, b.CLASSIFIER_AS_DETECTOR}
    for name in flag_only:
        assert name in scored.flags and len(scored.anomaly_scores[name]) == len(scored.rows)
    truth = [row["label"] != m.NORMAL for _, row in scored.rows]
    assert list(scored.flags[b.CLASSIFIER_AS_DETECTOR]) == truth


@pytest.fixture
def margin_world(scored, monkeypatch):
    monkeypatch.setitem(m.CONFIG, "resamples", 200)
    truth = np.asarray([row["label"] != m.NORMAL for _, row in scored.rows], bool)
    scored.flags["perfect"] = truth
    scored.flags["silent"] = np.zeros(len(truth), bool)
    scored.flags["same"] = truth.copy()
    return scored


def test_a_perfect_detector_beats_a_silent_one_by_the_full_recall(margin_world):
    r = b.paired_margin(margin_world, headline(margin_world), "perfect", "silent", "t")
    assert r["difference"]["recall"] == 1.0 and r["ci95"]["recall"] == [1.0, 1.0]
    assert r["difference"]["precision"] is None
    assert r["difference"]["fpr"] == 0.0


def test_a_detector_has_no_margin_over_itself(margin_world):
    r = b.paired_margin(margin_world, headline(margin_world), "perfect", "same", "t")
    for s in b.STATS:
        assert r["difference"][s] == 0.0 and r["ci95"][s] == [0.0, 0.0]


def test_the_margin_is_reproducible(margin_world):
    eids = headline(margin_world)
    first = b.paired_margin(margin_world, eids, "perfect", "silent", "k")
    assert first == b.paired_margin(margin_world, eids, "perfect", "silent", "k")