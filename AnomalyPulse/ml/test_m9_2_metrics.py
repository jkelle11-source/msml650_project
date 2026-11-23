"""Tests for the M9-2 metrics script, on a synthetic dataset and tiny models. No AWS.

Run from AnomalyPulse/:  python3 -m pytest ml/test_m9_2_metrics.py -q

The synthetic `v1` has the real layout: tier2/experiments.jsonl, tier1/features and tier2/labels per
run, and queue files holding the entries the script reads (incident, cascade, plan). A window's
features copy the signature of the class it "looks like", so a test decides what the models will say
by choosing `looks`; the models are fitted on separate synthetic windows, never on the dataset.
"""
import hashlib
import json
import zlib
from pathlib import Path

import joblib
import numpy as np
import pytest

pytest.importorskip("sklearn")
from sklearn.ensemble import IsolationForest, RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402

from ml import compare, m9_2_metrics as m  # noqa: E402

SIG = {"NORMAL": (0.0, 0.0), "TRAFFIC_SPIKE": (10.0, -3.0), "LAMBDA_DEGRADATION": (20.0, -6.0),
       "DATABASE_THROTTLING": (30.0, -9.0), "DEPENDENCY_FAILURE": (40.0, -12.0), "CASCADING_FAILURE": (50.0, -15.0)}
CLASSES = sorted(SIG)
PARAM = {"traffic-spike": "multiplier", "lambda-degradation": "added_latency_ms",
         "db-throttling": "load_ratio", "payment-failure": "timeout_ms"}
BANDS = {"traffic-spike": [(4, 6), (8, 10)], "lambda-degradation": [(500, 800), (1100, 1400)],
         "db-throttling": [(2.5, 3.5), (4.5, 5.5)], "payment-failure": [(1500, 2500), (3500, 4500)]}
STRESS = {"traffic-spike": (10, 12), "lambda-degradation": (1500, 2000),
          "db-throttling": (6, 8), "payment-failure": (5000, 5500)}
PAIRS = [("traffic-spike", "lambda-degradation"), ("traffic-spike", "payment-failure"),
         ("db-throttling", "lambda-degradation"), ("lambda-degradation", "payment-failure")]
LABEL = m.INJECTOR_LABEL
NORMAL_RUN = {"incident": {"injector": "noop", "baseline_s": 180, "duration_s": 300, "recovery_s": 120}}
UPPER_SPIKE_MISSED = lambda t: "NORMAL" if (t["injector"], t["band"]) == ("traffic-spike", (8, 10)) else None  # noqa: E731


def feats(looks, k):
    f1, f2 = SIG[looks]
    return {"f1": f1 + 0.1 * (k % 3), "f2": f2 - 0.1 * (k % 2)}


class World:
    """Writes a dataset one run at a time, and keeps the registry and queue entries to match."""

    def __init__(self, root):
        self.root, self.registry, self.n = Path(root) / "v1", [], 0
        self.queues = {name: [] for name in m.QUEUES}
        self.slots = {"train": 1, "val": 561, "test": 701, "stress": 1051}

    def run(self, phase, label_type, entry, windows, date="2026-11-13"):
        """windows: [(label or None, looks, fault_ms, multi_fault_ms)] in minute order."""
        self.n += 1
        slot = self.slots[phase]
        self.slots[phase] += 1
        eid = f"exp-20261113T{self.n:06d}Z-{self.n:04x}"
        features, labels = [], []
        for i, (label, looks, fault_ms, multi_ms) in enumerate(windows):
            ws = f"2026-11-13T{self.n // 60 % 24:02d}:{i:02d}:00.000Z"
            features.append({"window_start": ws, **feats(looks, self.n + i)})
            labels.append({"window_start": ws, "label": label, "included": label is not None,
                           "excluded_reason": None if label else "startup", "fault_ms": fault_ms, "multi_fault_ms": multi_ms})
        for tree, name, rows in (("tier1/features", "features.jsonl", features), ("tier2/labels", "labels.jsonl", labels)):
            folder = self.root / tree / f"experiment_id={eid}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.registry.append({"slot": slot, "phase": phase, "incident_type": label_type, "experiment_id": eid,
                              "generation_date": date, "generated_at": "x", "ended_at": "x", "batch_id": "b", "seed": 1})
        if entry is not None:
            self.queues["v1-stress" if phase == "stress" else "v1-test-1"].append(
                {"slot": slot, "phase": phase, "seed": 1, **entry})
        return eid

    def finish(self):
        (self.root / "tier2").mkdir(parents=True, exist_ok=True)
        (self.root / "tier2" / "experiments.jsonl").write_text("".join(json.dumps(r) + "\n" for r in self.registry))
        qdir = self.root.parent / "queues"
        qdir.mkdir(exist_ok=True)
        for name, entries in self.queues.items():
            (qdir / f"{name}.json").write_text(json.dumps({"dataset": "v1", "experiments": entries}))
        return qdir


def fault_windows(label, injectors, n=5, looks=None, multi=0, first_overlap=None, first_looks=None):
    """startup, one healthy minute, n fault minutes (the first has 40 s of fault), a recovery minute."""
    out = [(None, "NORMAL", {}, 0), ("NORMAL", "NORMAL", {}, 0)]
    for i in range(n):
        overlap = (first_overlap if i == 0 and first_overlap is not None else multi) if multi else 0
        out.append((label, (first_looks if i == 0 and first_looks else looks) or label,
                    {inj: 40_000 if i == 0 else 60_000 for inj in injectors}, overlap))
    return out + [(None, "NORMAL", {}, 0)]


def incident(injector, band, duration=300):
    return {"incident": {"injector": injector, "baseline_s": 180, "duration_s": duration, "recovery_s": 120,
                         "band": {"parameter": PARAM[injector], "low": band[0], "high": band[1]}, "intensity": 0.5}}


def build(tmp_path, looks=None, pair_looks=None, date="2026-11-13"):
    """26 test runs of every kind the script tells apart (2 normal, 16 band, a 12- and a 2-minute fault,
    2 cascades, 4 confounders), 4 stress runs and 8 validation runs. `looks(tag)` overrides what a
    single-fault run's fault windows look like; `pair_looks[i]` is "first", "second" or a class name."""
    w, looks, pair_looks = World(tmp_path), looks or (lambda tag: None), pair_looks or {}
    for _ in range(2):
        w.run("test", "NORMAL", NORMAL_RUN, [(None, "NORMAL", {}, 0)] + [("NORMAL", "NORMAL", {}, 0)] * 6 + [(None, "NORMAL", {}, 0)], date)
    for injector, bands in BANDS.items():
        for band in bands:
            for _ in range(2):
                w.run("test", LABEL[injector], incident(injector, band),
                      fault_windows(LABEL[injector], [injector], looks=looks({"injector": injector, "band": band})), date)
    w.run("test", "TRAFFIC_SPIKE", incident("traffic-spike", (4, 6), 720), fault_windows("TRAFFIC_SPIKE", ["traffic-spike"], n=12), date)
    w.run("test", "LAMBDA_DEGRADATION", incident("lambda-degradation", (500, 800), 120),
          fault_windows("LAMBDA_DEGRADATION", ["lambda-degradation"], n=1), date)
    for stages in (["db-throttling", "traffic-spike", "lambda-degradation"], ["traffic-spike", "payment-failure", "retry-amplification"]):
        windows = fault_windows(LABEL[stages[0]], stages[:1])[:3]
        windows += [("CASCADING_FAILURE", "CASCADING_FAILURE", {s: 60_000 for s in stages[:2]}, 60_000)] * 4 + [(None, "NORMAL", {}, 0)]
        w.run("test", "CASCADING_FAILURE", {"cascade": {"baseline_s": 180, "cascade_s": 300, "recovery_s": 120,
                                                          "stages": [{"injector": s} for s in stages]}}, windows, date)
    for i, (a, b) in enumerate(PAIRS):
        shown = {"first": LABEL[a], "second": LABEL[b]}.get(pair_looks.get(i, "first"), pair_looks.get(i))
        plan = {"incident_type": LABEL[a], "recovery_s": 120, "faults": [{"injector": a, "offset_s": 180}, {"injector": b, "offset_s": 180}]}
        w.run("test", LABEL[a], {"plan": plan},
              fault_windows(LABEL[a], [a, b], looks=shown, multi=60_000, first_overlap=20_000, first_looks="NORMAL"), date)
    for injector, band in STRESS.items():
        w.run("stress", LABEL[injector], incident(injector, band), fault_windows(LABEL[injector], [injector]), date)
    for injector in BANDS:
        for _ in range(2):
            w.run("val", LABEL[injector], None, fault_windows(LABEL[injector], [injector]), "2026-10-24")
    return w, w.finish()


def package(directory, model, meta):
    directory.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, directory / "model.joblib")
    (directory / "model.json").write_text(json.dumps({"format": 1, "columns": ["f1", "f2"], **meta}))
    return directory


def train_packages(tmp_path):
    """A six-class forest, an Isolation Forest (cut-off between its normal and anomalous windows) and a
    supervised binary detector, fitted on synthetic windows."""
    X = np.asarray([[feats(c, k)["f1"], feats(c, k)["f2"]] for c in CLASSES for k in range(30)])
    y = np.asarray([c for c in CLASSES for _ in range(30)])
    root = tmp_path / "pkgs"
    clf = RandomForestClassifier(n_estimators=20, random_state=0).fit(X, y)
    package(root / "clf", clf, {"task": "incident_type", "estimator": "random_forest", "classes": [str(c) for c in clf.classes_]})
    # The forest sees a spread of normal windows (not the six distinct points the dataset repeats), so
    # far-away windows score clearly lower; the cut-off sits between the two groups.
    calm = np.random.default_rng(0).normal(0.0, 0.5, size=(300, 2))
    forest = IsolationForest(n_estimators=100, random_state=0).fit(calm)
    threshold = (forest.score_samples(calm).min() + forest.score_samples(X[y != "NORMAL"]).max()) / 2
    package(root / "iforest", forest, {"kind": "detector", "task": "anomaly_iforest", "estimator": "isolation_forest",
                                       "threshold": float(threshold)})
    binary = LogisticRegression(max_iter=500).fit(X, np.where(y == "NORMAL", "NORMAL", "ANOMALY"))
    package(root / "binary", binary, {"task": "anomaly_binary", "estimator": "logistic_regression",
                                      "classes": [str(c) for c in binary.classes_]})
    return {"clf": root / "clf", "iforest": root / "iforest", "binary": root / "binary"}


def models(pk, detectors=("iforest", "binary")):
    return ([m.load_package(n, pk[n], "detector") for n in detectors], [m.load_package("incident_type", pk["clf"], "classifier")])


def report_for(tmp_path, with_val=False, **kw):
    world, qdir = build(tmp_path, **kw)
    pk = train_packages(tmp_path)
    detectors, classifiers = models(pk)
    return m.build_report(world.root, qdir, detectors, classifiers, resamples=200, with_validation=with_val,
                          allow_partial=True, repo_root=tmp_path), world, pk, qdir


def rows_of(report, family, model, injector):
    return sorted((r for r in report["per_band"]["rows"] if (r["family"], r["model"], r["injector"]) == (family, model, injector)),
                  key=lambda r: r["band"])


# --- the split and the tags ------------

def test_a_test_run_generated_on_a_training_day_is_refused(tmp_path):
    world, _ = build(tmp_path, date="2026-10-25")
    with pytest.raises(m.MetricsError, match="not after 2026-10-25"):
        m.check_split(m.load_registry(world.root), allow_partial=True)


def test_missing_test_slots_are_refused_unless_partial_is_allowed(tmp_path):
    world, _ = build(tmp_path)
    records = m.load_registry(world.root)
    with pytest.raises(m.MetricsError, match="test slot"):
        m.check_split(records)
    split = m.check_split(records, allow_partial=True)
    assert split["test_runs"] == 26 and split["stress_runs"] == 4 and split["missing_test_slots"] == 324


def test_a_phase_that_disagrees_with_its_slot_is_refused(tmp_path):
    world, _ = build(tmp_path)
    records = m.load_registry(world.root)
    records[0]["phase"] = "stress"
    with pytest.raises(m.MetricsError, match="its record says 'stress'"):
        m.check_split(records, allow_partial=True)


def test_duplicate_experiment_ids_are_refused(tmp_path):
    world, _ = build(tmp_path)
    world.registry[1]["experiment_id"] = world.registry[0]["experiment_id"]
    world.finish()
    with pytest.raises(m.MetricsError, match="duplicate experiment_id"):
        m.load_registry(world.root)


def test_a_registry_run_with_no_queue_entry_is_refused(tmp_path):
    world, qdir = build(tmp_path)
    queue = json.loads((qdir / "v1-test-1.json").read_text())
    queue["experiments"].pop()
    (qdir / "v1-test-1.json").write_text(json.dumps(queue))
    with pytest.raises(m.MetricsError, match="in no queue file"):
        m.tag_runs(m.load_registry(world.root), m.load_specs(qdir))


def test_runs_are_tagged_from_their_queue_entries(tmp_path):
    world, qdir = build(tmp_path)
    meta = m.tag_runs(m.load_registry(world.root), m.load_specs(qdir))
    kinds = {}
    for t in meta.values():
        kinds.setdefault(t["kind"], []).append(t)
    assert {k: len(v) for k, v in kinds.items()} == {"normal": 2, "single": 22, "cascade": 2, "confounder": 4}
    spike = [t for t in kinds["single"] if t["injector"] == "traffic-spike" and t["phase"] == "test" and t["duration_s"] == 300]
    assert sorted(t["band"] for t in spike) == [(4, 6), (4, 6), (8, 10), (8, 10)]
    pair = [t for t in kinds["confounder"] if t["pair"] == "TRAFFIC_SPIKE+LAMBDA_DEGRADATION"][0]
    assert pair["first_type"] == "TRAFFIC_SPIKE" and pair["fault_types"] == ["TRAFFIC_SPIKE", "LAMBDA_DEGRADATION"]
    assert {t["duration_s"] for t in kinds["single"]} == {120, 300, 720}
    assert sum(t["phase"] == "stress" for t in meta.values()) == 4


def test_the_injector_labels_match_the_simulator_when_it_imports():
    try:
        from simulator import integrity
    except ImportError:
        pytest.skip("the simulator package is not importable here")
    assert m.INJECTOR_LABEL == {inj: label for label, inj in integrity.INJECTOR_FOR.items() if inj != "noop"}


# --- headline, and what stays out of it ---

def test_the_headline_excludes_confounders_and_the_extreme_slice(tmp_path):
    report, *_ = report_for(tmp_path)
    counts = report["provenance"]["counts"]
    assert (counts["test_runs"], counts["headline_runs"], counts["stress_runs"]) == (26, 22, 4)
    assert report["detection"]["iforest"]["headline"]["runs"] == 22
    assert report["detection"]["iforest"]["with_confounders"]["runs"] == 26
    assert report["classification"]["incident_type"]["headline"]["runs"] == 22
    assert report["extreme_slice"]["runs"] == 4 and report["extreme_slice"]["detection"]["iforest"]["runs"] == 4


def test_a_broken_extreme_slice_does_not_move_the_headline(tmp_path):
    clean, *_ = report_for(tmp_path / "a")
    world, qdir = build(tmp_path / "b")
    for r in world.registry:                      # the stress runs' windows all look NORMAL
        if r["phase"] == "stress":
            path = world.root / "tier1/features" / f"experiment_id={r['experiment_id']}" / "features.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            path.write_text("".join(json.dumps({**row, "f1": 0.0, "f2": 0.0}) + "\n" for row in rows))
    detectors, classifiers = models(train_packages(tmp_path / "b"))
    broken = m.build_report(world.root, qdir, detectors, classifiers, resamples=200, allow_partial=True, repo_root=tmp_path)
    assert broken["extreme_slice"]["detection"]["iforest"]["metrics"]["recall"] == 0.0
    assert broken["detection"]["iforest"]["headline"]["metrics"] == clean["detection"]["iforest"]["headline"]["metrics"]
    assert broken["classification"]["incident_type"]["headline"]["accuracy"] == clean["classification"]["incident_type"]["headline"]["accuracy"]


def test_perfectly_separated_windows_score_perfectly(tmp_path):
    report, *_ = report_for(tmp_path)
    for name in ("iforest", "binary"):
        h = report["detection"][name]["headline"]
        assert (h["metrics"]["recall"], h["metrics"]["fpr"], h["metrics"]["precision"]) == (1.0, 0.0, 1.0)
        assert h["latency"]["missed_runs"] == 0
    c = report["classification"]["incident_type"]["headline"]
    assert c["accuracy"] == 1.0 and c["macro_f1"] == 1.0
    assert sum(sum(row) for row in c["confusion"]) == c["windows"]


def test_the_headline_numbers_are_the_expected_counts(tmp_path):
    # Upper-band spikes look NORMAL: 2 runs x 5 windows missed, of 103 fault windows in the headline.
    report, *_ = report_for(tmp_path, looks=UPPER_SPIKE_MISSED)
    h = report["detection"]["iforest"]["headline"]
    assert (h["counts"]["tp"], h["counts"]["fn"], h["counts"]["fp"]) == (93, 10, 0)
    assert abs(h["metrics"]["recall"] - 93 / 103) < 1e-12
    assert h["latency"]["missed_runs"] == 2 and h["latency"]["incident_runs"] == 20
    spike = report["classification"]["incident_type"]["headline"]["per_class"]["TRAFFIC_SPIKE"]
    assert abs(spike["recall"] - (spike["support"] - 10) / spike["support"]) < 1e-12


# --- bands ------

def test_the_upper_band_is_flagged_when_it_falls_off(tmp_path):
    report, *_ = report_for(tmp_path, looks=UPPER_SPIKE_MISSED)
    for family, model in (("detection", "iforest"), ("detection", "binary"), ("classification", "incident_type")):
        low, high = rows_of(report, family, model, "traffic-spike")
        assert (low["role"], high["role"]) == ("interpolation", "mild extrapolation")
        assert (low["recall"], high["recall"]) == (1.0, 0.0)
        assert (low["vs_other_band"]["status"], high["vs_other_band"]["status"]) == ("ok", "degraded")
        assert abs(high["vs_other_band"]["gap"] - 1.0) < 1e-12
    flagged = {(f["model"], f["injector"], tuple(f["band"])) for f in report["per_band"]["flagged"]}
    assert ("incident_type", "traffic-spike", (8, 10)) in flagged
    assert {inj for _, inj, _ in flagged} == {"traffic-spike"}
    assert rows_of(report, "classification", "incident_type", "traffic-spike")[1]["top_errors"] == [["NORMAL", 10]]
    assert "Flagged bands:" in m.render_markdown(report)


def test_a_healthy_set_flags_no_band(tmp_path):
    report, *_ = report_for(tmp_path)
    assert report["per_band"]["flagged"] == []
    assert len(report["per_band"]["rows"]) == 4 * 2 * 4         # 4 injectors x 2 bands x (2 detectors + deployed + 1 classifier)
    assert "No band is flagged" in m.render_markdown(report)


def test_band_runs_leave_out_other_durations(tmp_path):
    report, *_ = report_for(tmp_path)
    assert sorted(r["runs"] for r in rows_of(report, "classification", "incident_type", "traffic-spike")) == [2, 2]
    assert set(report["varied_duration"]) == {"120", "720"}


def test_band_status_rule():
    assert m.band_status(0.95, [0.9, 1.0], 1.0, [1.0, 1.0]) == "ok"            # gap under 10 points
    assert m.band_status(0.70, [0.5, 0.9], 1.0, [0.9, 1.0]) == "possible"      # big gap, intervals overlap
    assert m.band_status(0.50, [0.4, 0.6], 1.0, [0.9, 1.0]) == "degraded"      # big gap, no overlap
    assert m.band_status(None, None, 1.0, None) == "n/a"
    assert m.band_status(1.0, [1.0, 1.0], 0.5, [0.4, 0.6]) == "ok"             # better than its reference


def test_the_validation_reference_is_an_optional_second_comparison(tmp_path):
    plain, *_ = report_for(tmp_path / "a")
    assert all(r["vs_validation"] is None for r in plain["per_band"]["rows"])
    report, *_ = report_for(tmp_path / "b", with_val=True, looks=UPPER_SPIKE_MISSED)
    high = rows_of(report, "classification", "incident_type", "traffic-spike")[1]
    assert high["vs_validation"]["ref_recall"] == 1.0 and high["vs_validation"]["status"] == "degraded"
    assert all(r["vs_validation"] is not None for r in report["per_band"]["rows"])


# --- confounders -----

def test_confounder_shares_over_windows_with_both_faults_active(tmp_path):
    # Pair 0 looks like a cascade, pairs 1 and 3 like their first fault, pair 2 like its second.
    report, *_ = report_for(tmp_path, pair_looks={0: "CASCADING_FAILURE", 1: "first", 2: "second", 3: "first"})
    cf = report["confounders"]
    assert (cf["runs"], cf["windows_both_active"], cf["windows_all_fault_labeled"]) == (4, 16, 20)
    entry = cf["by_classifier"]["incident_type"]
    b = entry["both_active"]
    assert (b["windows"], b["runs"]) == (16, 4)
    assert b["either_fault_type"]["share"] == 0.75 and b["labeled_first_type"]["share"] == 0.5
    assert b["cascading_failure"]["share"] == 0.25 and b["neither"]["share"] == 0.0
    assert sum(b["predicted"].values()) == 16 and b["predicted"]["CASCADING_FAILURE"] == 4
    # Each run's fault-start window overlaps for 20 s only: scored in the sensitivity alone, where the
    # models call it NORMAL, i.e. "neither".
    s = entry["all_fault_labeled"]
    assert (s["either_fault_type"]["share"], s["neither"]["share"], s["cascading_failure"]["share"]) == (0.6, 0.2, 0.2)
    pairs = entry["by_pair"]
    assert pairs["TRAFFIC_SPIKE+LAMBDA_DEGRADATION"]["cascading_failure"]["share"] == 1.0
    second = pairs["DATABASE_THROTTLING+LAMBDA_DEGRADATION"]
    assert (second["labeled_first_type"]["share"], second["either_fault_type"]["share"]) == (0.0, 1.0)
    assert cf["detection"]["iforest"]["flagged_share"] == 1.0


def test_the_labeled_type_is_one_of_the_two_so_the_shares_overlap(tmp_path):
    report, *_ = report_for(tmp_path)
    b = report["confounders"]["by_classifier"]["incident_type"]["both_active"]
    assert b["labeled_first_type"]["share"] <= b["either_fault_type"]["share"]
    total = b["either_fault_type"]["share"] + b["cascading_failure"]["share"] + b["neither"]["share"]
    assert abs(total - 1.0) < 1e-12
    assert "do not sum to 1" in m.render_markdown(report)


def test_a_confounder_run_with_no_overlap_evidence_is_reported(tmp_path):
    world, qdir = build(tmp_path)
    meta = m.tag_runs(m.load_registry(world.root), m.load_specs(qdir))
    victim = next(e for e, t in meta.items() if t["kind"] == "confounder")
    path = world.root / "tier2/labels" / f"experiment_id={victim}" / "labels.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    path.write_text("".join(json.dumps({**r, "multi_fault_ms": 0}) + "\n" for r in rows))
    _, classifiers = models(train_packages(tmp_path))
    report = m.build_report(world.root, qdir, [], classifiers, resamples=200, allow_partial=True, repo_root=tmp_path)
    assert report["confounders"]["runs_with_no_both_active_window"] == [victim]
    assert report["confounders"]["windows_both_active"] == 12


# --- extreme slice, cascades, durations ----

def test_the_extreme_slice_is_its_own_section_with_per_injector_cells(tmp_path):
    report, *_ = report_for(tmp_path)
    ex = report["extreme_slice"]
    assert set(ex["by_injector"]) == set(m.INJECTOR_LABEL)
    assert ex["by_injector"]["traffic-spike"]["classification:incident_type"]["recall"] == 1.0
    assert "## 4. Extreme-extrapolation slice (reported separately" in m.render_markdown(report)


def test_time_to_recognize_a_cascade(tmp_path):
    report, *_ = report_for(tmp_path)
    ttr = report["classification"]["incident_type"]["time_to_recognize_cascade"]
    assert ttr["summary"]["runs"] == 2 and ttr["summary"]["never"] == 0
    assert all(v is not None and 0 < v <= 120 for v in ttr["per_run"].values())


def test_latency_is_reported_for_each_detector(tmp_path):
    report, *_ = report_for(tmp_path)
    lat = report["detection"]["iforest"]["headline"]["latency"]
    assert lat["detected_runs"] == lat["incident_runs"] == 20 and lat["median_s"] is not None
    assert len(report["latency_runs"]["iforest"]) == 20


# --- statistics ---------------------------------------------------------------------------------

def test_bootstrap_stats_agree_with_the_validation_definitions():
    rng = np.random.default_rng(1)
    truth, pred = list(rng.choice(["NORMAL", "A", "B"], 200)), list(rng.choice(["NORMAL", "A", "B"], 200))
    classes = ["A", "B", "NORMAL"]
    sub = m.Sub(eids=["x"], idx=np.arange(200), runs=[], rows=[], labels=truth, group=np.zeros(200, int))
    expected = compare.evaluate_classifier(truth, pred, classes)
    stats = m.clf_stats(m.per_run_confusion(sub, truth, pred, classes).sum(axis=0))
    assert abs(float(stats["accuracy"]) - expected["accuracy"]) < 1e-12
    assert abs(float(stats["macro_f1"]) - expected["macro_f1"]) < 1e-12
    flagged = rng.random(200) < 0.5
    want = m.detection.rates(m.detection.confusion([t != "NORMAL" for t in truth], flagged))
    got = m.det_stats(m.per_run_detection(sub, flagged).sum(axis=0))
    for key in ("precision", "recall", "f1", "fpr"):
        assert abs(float(got[key]) - want[key]) < 1e-12


def test_the_bootstrap_resamples_runs_not_windows():
    # Six runs of 100 windows, half all-hit and half all-miss. Resampling the 600 windows would give
    # about 0.5 +/- 0.04; resampling the six runs gives roughly 0.17 to 0.83.
    per_run = np.array([[100, 100]] * 3 + [[0, 100]] * 3, float)
    lo, hi = m.bootstrap(per_run, m.ratio_stats, "k")["ratio"]
    assert lo <= 0.34 and hi >= 0.66


def test_the_bootstrap_is_reproducible_and_keyed():
    rng = np.random.default_rng(3)
    per_run = np.stack([rng.integers(0, 40, 30), np.full(30, 40)], axis=1).astype(float)
    try:
        m.CONFIG.update(seed=1, resamples=300)
        a = m.bootstrap(per_run, m.ratio_stats, "k")
        assert m.bootstrap(per_run, m.ratio_stats, "k") == a
        assert m.bootstrap(per_run, m.ratio_stats, "other") != a
        m.CONFIG.update(seed=2)
        assert m.bootstrap(per_run, m.ratio_stats, "k") != a
    finally:
        m.CONFIG.update(seed=m.SEED, resamples=m.BOOTSTRAP)


def test_identical_runs_give_a_collapsed_interval_and_one_run_gives_none():
    assert m.bootstrap(np.array([[4, 5]] * 6, float), m.ratio_stats, "k")["ratio"] == [0.8, 0.8]
    assert m.bootstrap(np.array([[4, 5]], float), m.ratio_stats, "k") == {}


# --- command line and input checks ------

def cli_args(tmp_path, out, extra=()):
    world, qdir = build(tmp_path)
    pk = train_packages(tmp_path)
    return ["--dataset", str(world.root), "--queues", str(qdir), "--classifier", f"incident_type={pk['clf']}",
            "--detector", f"iforest={pk['iforest']}", "--detector", f"binary={pk['binary']}",
            "--out", str(out), "--bootstrap", "200", "--allow-partial", *extra]


def test_main_writes_json_and_markdown_and_refuses_to_overwrite(tmp_path):
    out = tmp_path / "results"
    argv = cli_args(tmp_path, out)
    assert m.main(argv) == 0
    text = (out / "metrics_m9_2.md").read_text()
    assert json.loads((out / "metrics_m9_2.json").read_text())["provenance"]["counts"]["test_runs"] == 26
    for heading in ("## Scope and provenance", "## 1. Detection (headline)", "## 2. Classification (headline)",
                    "## 3. Per-band breakdown", "## 4. Extreme-extrapolation slice", "## 5. Confounders", "## 6. Varied-duration"):
        assert heading in text
    first = (out / "metrics_m9_2.json").read_text()
    assert m.main(argv) == 1                                   # the test set is scored once: a rerun needs --force
    assert (out / "metrics_m9_2.json").read_text() == first
    assert m.main(argv + ["--force"]) == 0


def test_the_json_has_no_nan(tmp_path):
    out = tmp_path / "results"
    assert m.main(cli_args(tmp_path, out)) == 0
    def refuse(constant):
        raise ValueError(constant)
    json.loads((out / "metrics_m9_2.json").read_text(), parse_constant=refuse)


def test_a_package_of_the_wrong_kind_is_refused(tmp_path):
    pk = train_packages(tmp_path)
    with pytest.raises(m.MetricsError, match="not a detector"):
        m.load_package("x", pk["clf"], "detector")
    for name in ("iforest", "binary"):
        with pytest.raises(m.MetricsError, match="not a six-class classifier"):
            m.load_package("x", pk[name], "classifier")


def test_a_package_whose_columns_differ_is_refused(tmp_path):
    world, qdir = build(tmp_path)
    pk = train_packages(tmp_path)
    meta = json.loads((pk["iforest"] / "model.json").read_text())
    (pk["iforest"] / "model.json").write_text(json.dumps({**meta, "columns": ["f1", "zzz"]}))
    with pytest.raises(m.MetricsError, match="columns differ"):
        m.build_report(world.root, qdir, [m.load_package("iforest", pk["iforest"], "detector")], [], resamples=200, allow_partial=True)


def test_a_run_with_missing_files_is_refused(tmp_path):
    world, qdir = build(tmp_path)
    victim = world.registry[3]["experiment_id"]
    (world.root / "tier2/labels" / f"experiment_id={victim}" / "labels.jsonl").unlink()
    _, classifiers = models(train_packages(tmp_path))
    with pytest.raises(m.MetricsError, match="not found"):
        m.build_report(world.root, qdir, [], classifiers, resamples=200, allow_partial=True)


def test_only_the_test_stress_and_reference_runs_are_read(tmp_path):
    world, qdir = build(tmp_path)
    world.registry.append({"slot": 5, "phase": "train", "incident_type": "NORMAL", "experiment_id": "exp-20261001T000000Z-dead",
                           "generation_date": "2026-10-01"})
    world.finish()
    folder = world.root / "tier1/features/experiment_id=exp-20261001T000000Z-dead"
    folder.mkdir(parents=True)
    (folder / "features.jsonl").write_text("not json")          # a training run is never opened
    _, classifiers = models(train_packages(tmp_path))
    report = m.build_report(world.root, qdir, [], classifiers, resamples=200, allow_partial=True)
    assert report["provenance"]["counts"]["test_runs"] == 26


# --- ranking, intervals and references ------

def test_detector_scores_rank_faults_above_healthy_windows(tmp_path):
    report, *_ = report_for(tmp_path)
    for name in ("iforest", "binary"):
        assert report["detection"][name]["headline"]["metrics"]["roc_auc"] == 1.0


def test_macro_f1_leaves_out_a_class_nobody_has():
    # A: 3 right, 1 called B. B: 2 right. C never appears. F1(A) = 6/7, F1(B) = 4/5, mean 29/35.
    stats = m.clf_stats(np.array([[3, 1, 0], [0, 2, 0], [0, 0, 0]], float))
    assert abs(float(stats["macro_f1"]) - 29 / 35) < 1e-12


def test_the_intervals_are_95_percent():
    per_run = np.stack([np.arange(20), np.full(20, 40)], axis=1).astype(float)
    values = np.arange(30.0)
    try:
        m.CONFIG.update(seed=5, resamples=400)
        ratio = m.bootstrap(per_run, m.ratio_stats, "k")["ratio"]
        median = m.bootstrap_median(values, "k")
    finally:
        m.CONFIG.update(seed=m.SEED, resamples=m.BOOTSTRAP)
    rng, draws = np.random.default_rng([5, zlib.crc32(b"k")]), []
    for start in range(0, 400, m.CHUNK):
        sums = per_run[rng.integers(0, 20, size=(min(m.CHUNK, 400 - start), 20))].sum(axis=1)
        draws.append(sums[:, 0] / sums[:, 1])
    assert ratio == pytest.approx(np.quantile(np.concatenate(draws), [0.025, 0.975]).tolist())
    rng = np.random.default_rng([5, zlib.crc32(b"k")])
    medians = np.median(values[rng.integers(0, 30, size=(400, 30))], axis=1)
    assert median == pytest.approx(np.quantile(medians, [0.025, 0.975]).tolist())


def test_a_band_called_the_wrong_fault_is_a_classification_miss_but_still_detected(tmp_path):
    wrong = lambda t: "LAMBDA_DEGRADATION" if (t["injector"], t["band"]) == ("traffic-spike", (8, 10)) else None  # noqa: E731
    report, *_ = report_for(tmp_path, looks=wrong)
    low, high = rows_of(report, "classification", "incident_type", "traffic-spike")
    assert (low["recall"], high["recall"]) == (1.0, 0.0)
    assert high["top_errors"] == [["LAMBDA_DEGRADATION", 10]]
    assert rows_of(report, "detection", "binary", "traffic-spike")[1]["recall"] == 1.0


def test_the_validation_reference_never_reads_a_training_run(tmp_path):
    world, qdir = build(tmp_path)
    world.registry.append({"slot": 5, "phase": "train", "incident_type": "TRAFFIC_SPIKE",
                           "experiment_id": "exp-20261001T000000Z-dead", "generation_date": "2026-10-01"})
    world.finish()
    folder = world.root / "tier1/features/experiment_id=exp-20261001T000000Z-dead"
    folder.mkdir(parents=True)
    (folder / "features.jsonl").write_text("not json")          # reading this run would fail
    detectors, classifiers = models(train_packages(tmp_path))
    report = m.build_report(world.root, qdir, detectors, classifiers, resamples=200, with_validation=True,
                            allow_partial=True, repo_root=tmp_path)
    assert all(r["vs_validation"] is not None for r in report["per_band"]["rows"])


# --- the deployed rule and uninformative detectors ------

def flag_everything(pk):
    path = pk["iforest"] / "model.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "threshold": 1.0}))     # scores are all below 0


def test_the_deployed_rule_needs_both_the_forest_and_the_classifier():
    flags = m.deployed_flags([True, True, False, False], ["NORMAL", "TRAFFIC_SPIKE", "NORMAL", "TRAFFIC_SPIKE"])
    assert flags.tolist() == [False, True, False, False]


def test_the_deployed_column_has_the_expected_counts(tmp_path):
    # Upper-band spikes look NORMAL to both models: 2 runs x 5 windows missed, of 103 fault windows.
    report, *_ = report_for(tmp_path, looks=UPPER_SPIKE_MISSED)
    d = report["detection"][m.DEPLOYED]["headline"]
    assert (d["counts"]["tp"], d["counts"]["fn"], d["counts"]["fp"]) == (93, 10, 0)
    assert d["metrics"]["roc_auc"] is None and d["metrics"]["average_precision"] is None
    assert f"`{m.DEPLOYED}` is the live path's rule" in m.render_markdown(report)


def test_a_detector_that_flags_everything_is_called_out(tmp_path):
    world, qdir = build(tmp_path, looks=UPPER_SPIKE_MISSED)
    pk = train_packages(tmp_path)
    flag_everything(pk)
    detectors, classifiers = models(pk)
    report = m.build_report(world.root, qdir, detectors, classifiers, resamples=200, allow_partial=True, repo_root=tmp_path)
    u = report["uninformative_detectors"]
    assert set(u) == {"iforest"} and u["iforest"]["healthy_flagged_share"] == 1.0
    assert u["iforest"]["windows_flagged"] == u["iforest"]["windows"] == report["provenance"]["counts"]["test_windows"]
    assert report["detection"]["iforest"]["headline"]["counts"]["tn"] == 0
    text = m.render_markdown(report)
    assert "## Read this first" in text and text.count('(see "Read this first")') == 4     # sections 1, 3, 5, 6
    # With the forest passing everything, the classifier alone decides: it still misses the 10 upper-band windows.
    d = report["detection"][m.DEPLOYED]["headline"]["counts"]
    assert (d["tp"], d["fn"], d["fp"]) == (93, 10, 0)


def test_a_working_detector_is_not_called_out(tmp_path):
    report, *_ = report_for(tmp_path)
    assert report["uninformative_detectors"] == {}
    assert "Read this first" not in m.render_markdown(report)


def test_no_metric_picks_its_cut_off_from_the_test_windows(tmp_path):
    report, *_ = report_for(tmp_path)
    assert "recall_at_healthy_fpr_1" not in json.dumps(report)


def test_the_markdown_carries_the_all_test_lines_and_the_stress_caveat(tmp_path):
    report, *_ = report_for(tmp_path)
    text = m.render_markdown(report)
    assert "**All test runs** (26 runs" in text and "All test runs, confounders included: accuracy" in text
    assert "does not isolate the effect of severity" in text
    assert "more than 10 percentage points" in text and "10% or more" not in text


# --- provenance ------

def test_the_report_records_which_model_files_and_libraries_it_used(tmp_path):
    out = tmp_path / "results"
    assert m.main(cli_args(tmp_path, out, ("--job", "iforest=job-123"))) == 0
    prov = json.loads((out / "metrics_m9_2.json").read_text())["provenance"]
    forest = prov["packages"]["iforest"]
    assert forest["job"] == "job-123" and prov["packages"]["binary"]["job"] is None
    assert forest["sha256"] == hashlib.sha256((tmp_path / "pkgs/iforest/model.joblib").read_bytes()).hexdigest()
    assert set(prov["environment"]) == {"python", "numpy", "scikit-learn", "scipy", "joblib"}


def test_libraries_match_compares_the_training_image_with_this_run():
    package = m.Package("x", "classifier", None, {"columns": ["a"], "libraries": {"python": "3.10.22"}})
    assert m.package_summary(package, {"python": "3.10.22"})["libraries_match"] is True
    assert m.package_summary(package, {"python": "3.12.16"})["libraries_match"] is False
    unrecorded = m.Package("x", "classifier", None, {"columns": ["a"]})
    assert m.package_summary(unrecorded, {"python": "3.10.22"})["libraries_match"] is None


@pytest.mark.parametrize("extra", (["--job", "nobody=job-1"], ["--detector", "deployed=x"]))
def test_bad_job_or_reserved_package_names_are_refused(tmp_path, extra):
    with pytest.raises(SystemExit):
        m.main(cli_args(tmp_path, tmp_path / "results", extra))