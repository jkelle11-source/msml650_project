"""Tests for M9-3's baseline scoring, on M9-2's synthetic world. No real data, no AWS.

Run from AnomalyPulse/, in the training image's versions:
    python -m pytest ml/test_m9_3_baseline.py ml/test_m9_2_metrics.py -q
"""
import zlib

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


def test_an_imperfect_detector_has_no_margin_over_its_copy_only_if_the_runs_are_paired(margin_world):
    """Model 0's earlier-mean floor misses most fault windows, so its recall moves from resample to resample:
    its own interval is wide, and only a paired resample makes the difference with its copy exactly 0."""
    scored, eids = margin_world, headline(margin_world)
    scored.flags["copy"] = b.model0_flags(scored)[b.EARLIER].copy()
    scored.flags[b.EARLIER] = scored.flags["copy"].copy()
    alone = m.bootstrap(m.per_run_detection(m.subset(scored, eids), scored.flags["copy"][m.subset(scored, eids).idx]),
                        m.det_stats, "alone")
    assert alone["recall"][1] - alone["recall"][0] > 0.05
    r = b.paired_margin(scored, eids, "copy", b.EARLIER, "t")
    for s in b.STATS:
        assert r["difference"][s] == 0.0 and r["ci95"][s] == [0.0, 0.0], s


def test_the_margin_interval_matches_a_loop_over_the_same_resamples(margin_world):
    """An independent computation: count each run's windows in a plain loop, sum them over each resample's
    runs (the documented scheme: one draw of run indices per (seed, key), shared by both detectors), and
    take the 2.5th and 97.5th percentiles of the recall and fpr differences."""
    scored, eids = margin_world, headline(margin_world)
    scored.flags["earlier"] = b.model0_flags(scored)[b.EARLIER]
    runs = sorted(set(eids))
    counts = {e: {"perfect": [0, 0, 0, 0], "earlier": [0, 0, 0, 0]} for e in runs}   # tp, fp, fn, tn
    for i, (eid, row) in enumerate(scored.rows):
        if eid not in counts:
            continue
        truth = row["label"] != m.NORMAL
        for name in ("perfect", "earlier"):
            hit = bool(scored.flags[name][i])
            counts[eid][name][(0 if hit else 2) if truth else (1 if hit else 3)] += 1
    rng = np.random.default_rng([m.CONFIG["seed"], zlib.crc32(b"loop")])
    draws = rng.integers(0, len(runs), size=(m.CONFIG["resamples"], len(runs)))
    recall, fpr = [], []
    for pick in draws:
        tot = {n: [sum(counts[runs[j]][n][k] for j in pick) for k in range(4)] for n in ("perfect", "earlier")}
        rec = {n: t[0] / (t[0] + t[2]) for n, t in tot.items()}
        fp = {n: t[1] / (t[1] + t[3]) for n, t in tot.items()}
        recall.append(rec["perfect"] - rec["earlier"])
        fpr.append(fp["perfect"] - fp["earlier"])
    r = b.paired_margin(scored, eids, "perfect", "earlier", "loop")
    assert r["ci95"]["recall"] == pytest.approx(list(np.quantile(recall, [0.025, 0.975])))
    assert r["ci95"]["fpr"] == pytest.approx(list(np.quantile(fpr, [0.025, 0.975])))
    assert r["ci95"]["recall"][0] < r["difference"]["recall"] < r["ci95"]["recall"][1]


# --- the earlier-mean floor's lookback, on one hand-built run ---

def _row(ws, p95, change, label=None):
    row = {"window_start": ws}
    for svc in model0.SERVICES:
        row[f"{svc}_p95_latency_ms"] = p95 if svc == "order" else 100.0
        row[f"{svc}_p95_latency_ms_change"] = change if svc == "order" else 0.0
        row[f"{svc}_error_rate_5xx"] = 0.0
    return row if label is None else {**row, "label": label}


def test_the_earlier_mean_skips_an_excluded_onset_window():
    """startup 100 ms, a healthy minute 100 ms, a DB-throttling onset minute 1000 ms (excluded, never a
    baseline), then a fault minute at 250 ms. Its earlier mean is (100 + 100) / 2 = 100, and 250 > 2 x 100,
    so it is flagged. Reading the onset minute too would give (100 + 100 + 1000) / 3 = 400, and no flag."""
    ws = [f"2026-11-13T00:0{i}:00.000Z" for i in range(4)]
    features = [_row(ws[0], 100.0, 0.0), _row(ws[1], 100.0, 0.0), _row(ws[2], 1000.0, 900.0), _row(ws[3], 250.0, -750.0)]
    labels = [{"window_start": ws[0], "label": None, "included": False, "excluded_reason": "startup", "fault_ms": {}},
              {"window_start": ws[1], "label": "NORMAL", "included": True, "excluded_reason": None, "fault_ms": {}},
              {"window_start": ws[2], "label": None, "included": False, "excluded_reason": "db_throttling_onset",
               "fault_ms": {"f": 20_000}},
              {"window_start": ws[3], "label": "DATABASE_THROTTLING", "included": True, "excluded_reason": None,
               "fault_ms": {"f": 60_000}}]
    rows = [("e", {**features[1], "label": "NORMAL"}), ("e", {**features[3], "label": "DATABASE_THROTTLING"})]
    scored = m.Scored(runs=[("e", features, labels)], rows=rows, meta={}, evidence={}, classes=[],
                      labels=[r["label"] for _, r in rows])
    flags = b.model0_flags(scored)
    assert list(flags[b.EARLIER]) == [False, True]
    assert list(flags[b.PREVIOUS]) == [False, False]     # 250 against the onset minute's 1000


# --- share margins, counted by hand on the synthetic world ---

@pytest.fixture
def truth_world(scored):
    """The classifier is the truth, except it also calls every single-fault run's healthy minute a spike."""
    preds = []
    for eid, row in scored.rows:
        healthy_minute = scored.meta[eid]["kind"] == "single" and row["label"] == m.NORMAL
        preds.append("TRAFFIC_SPIKE" if healthy_minute else row["label"])
    scored.preds["clf"] = preds
    b.add_baselines(scored, "clf")
    return scored


def test_share_margins_by_hand(truth_world):
    """Headline (22 runs). A single-fault run is startup, 1 healthy minute, 5 fault minutes; the earlier-mean
    floor flags the first 2 fault minutes (500 ms against means of 100 and 233 ms) and no others.
    TRAFFIC_SPIKE: 4 band runs x 5 + the 12-minute run's 12 + 1 early minute of the spike-first cascade = 33
    windows; the floor flags 4 x 2 + 2 + 1 = 11. CASCADING_FAILURE: 2 runs x 4; the floor flags the first
    (500 ms against (100 + 100 + 500) / 3) of each = 2. Healthy: 2 normal runs x 6 + 18 single runs x 1 + 2
    cascades x 1 = 32; the floor flags none, the classifier the 18 single runs' healthy minutes."""
    r = b.paired_share_margins(truth_world, headline(truth_world), b.CLASSIFIER_AS_DETECTOR, b.EARLIER, "hand")
    spike, cascade, healthy = r["TRAFFIC_SPIKE"], r["CASCADING_FAILURE"], r[b.HEALTHY]
    assert (spike["windows"], spike["ml"], spike["baseline"]) == (33, 1.0, pytest.approx(11 / 33))
    assert spike["difference"] == pytest.approx(22 / 33)
    assert (cascade["windows"], cascade["baseline"], cascade["difference"]) == (8, 0.25, 0.75)
    assert (healthy["windows"], healthy["baseline"]) == (32, 0.0)
    assert healthy["ml"] == healthy["difference"] == pytest.approx(18 / 32)
    assert healthy["ci95"][0] <= healthy["difference"] <= healthy["ci95"][1]
    assert r["NORMAL"]["windows"] == 32 + 0      # every headline NORMAL window here is healthy


def test_a_fault_start_minute_is_not_a_healthy_window():
    """Two runs. Run a: a healthy minute (no fault) and a fault-start minute (NORMAL, but 20 s of fault),
    both flagged by `ml`, neither by `base`. Run b: one healthy minute, flagged by neither. Healthy windows
    are a's first and b's: ml flags 1 of 2. All NORMAL windows: ml flags 2 of 3."""
    ws = ["2026-11-13T00:00:00.000Z", "2026-11-13T00:01:00.000Z"]
    labels_a = [{"window_start": ws[0], "label": "NORMAL", "included": True, "fault_ms": {}},
                {"window_start": ws[1], "label": "NORMAL", "included": True, "fault_ms": {"f": 20_000}}]
    labels_b = [{"window_start": ws[0], "label": "NORMAL", "included": True, "fault_ms": {}}]
    rows = [("a", {"window_start": ws[0], "label": "NORMAL"}), ("a", {"window_start": ws[1], "label": "NORMAL"}),
            ("b", {"window_start": ws[0], "label": "NORMAL"})]
    scored = m.Scored(runs=[("a", [], labels_a), ("b", [], labels_b)], rows=rows, meta={}, evidence={}, classes=[],
                      labels=["NORMAL"] * 3)
    scored.flags["ml"] = np.asarray([True, True, False])
    scored.flags["base"] = np.asarray([False, False, False])
    out = b.paired_share_margins(scored, ["a", "b"], "ml", "base", "k")
    assert (out[b.HEALTHY]["windows"], out[b.HEALTHY]["ml"]) == (2, 0.5)
    assert (out["NORMAL"]["windows"], out["NORMAL"]["ml"]) == (3, pytest.approx(2 / 3))


def test_a_share_margin_against_itself_is_exactly_zero(truth_world):
    r = b.paired_share_margins(truth_world, headline(truth_world), b.EARLIER, b.EARLIER, "self")
    assert all(v["difference"] in (0.0, None) and v["ci95"] in ([0.0, 0.0], None) for v in r.values())


def test_every_classifier_is_read_as_a_detector_and_the_deployed_rule_is_forest_and_live(scored):
    scored.preds["live"] = [row["label"] for _, row in scored.rows]
    scored.preds["other"] = [m.NORMAL for _ in scored.rows]
    flag_only = b.add_baselines(scored, "live", ["other"])
    assert flag_only == [*b.NAMES, b.CLASSIFIER_AS_DETECTOR, "other_as_detector"]
    assert not scored.flags["other_as_detector"].any()
    scored.flags["forest"] = np.asarray([i % 2 == 0 for i in range(len(scored.rows))], bool)
    b.add_deployed(scored, "forest", "live")
    expected = scored.flags["forest"] & scored.flags[b.CLASSIFIER_AS_DETECTOR]
    assert list(scored.flags[m.DEPLOYED]) == list(expected)