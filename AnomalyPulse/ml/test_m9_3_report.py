"""Tests for M9-3's report, on M9-2's synthetic world. No real data, no AWS.

Run from AnomalyPulse/, in the training image's versions:
    python -m pytest ml/test_m9_3_report.py ml/test_m9_3_baseline.py ml/test_m9_2_metrics.py -q
"""
import json

import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier

from ml import evaluate
from ml import m9_2_metrics as m
from ml import m9_3_baseline as b
from ml import m9_3_report as r
from ml import test_m9_2_metrics as tm
from ml.test_m9_3_baseline import feats_with_model0_columns


def classifier_package(tmp_path):
    """A six-class forest over the synthetic columns, including the ones Model 0 reads."""
    cols = sorted(feats_with_model0_columns("NORMAL", 0))
    X = np.asarray([[feats_with_model0_columns(c, k)[col] for col in cols] for c in tm.CLASSES for k in range(30)])
    y = np.asarray([c for c in tm.CLASSES for _ in range(30)])
    clf = RandomForestClassifier(n_estimators=20, random_state=0).fit(X, y)
    return tm.package(tmp_path / "pkgs" / "clf", clf, {"task": "incident_type", "estimator": "random_forest",
                                                       "columns": cols, "classes": [str(c) for c in clf.classes_]})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "feats", feats_with_model0_columns)
    w, qdir = tm.build(tmp_path)
    return w, qdir, classifier_package(tmp_path)


def build(world, **kw):
    w, qdir, pkg = world
    classifiers = [m.load_package("incident_type", pkg, "classifier")]
    report = r.build_report(w.root, qdir, classifiers, resamples=200, allow_partial=True,
                            repo_root=w.root.parent, **kw)
    return report, classifiers


def test_the_report_has_every_slice_and_model(world):
    report, _ = build(world)
    assert set(report["detection"]) == {"headline", "all_test", "stress"}
    assert set(report["detection"]["headline"]) == set(r.MODELS)
    assert set(report["margins"]["headline"]) == set(b.NAMES)
    assert report["validation"]["runs"] == 8
    assert report["extreme_slice"]["runs"] == 4


def test_ranking_metrics_are_n_a_for_every_flag_only_detector(world):
    report, _ = build(world)
    for section in (report["detection"]["headline"], report["validation"]["detection"]):
        for name in r.MODELS:
            for key in r.RANKING:
                assert section[name]["metrics"][key] is None


def test_the_classifier_numbers_are_m9_2s_own(world):
    report, classifiers = build(world)
    w, qdir, _ = world
    theirs = m.build_report(w.root, qdir, [], classifiers, resamples=200, allow_partial=True, repo_root=w.root.parent)
    ours, ref = report["classification"]["headline"], theirs["classification"]["incident_type"]["headline"]
    assert ours["accuracy"] == ref["accuracy"] and ours["macro_f1"] == ref["macro_f1"] and ours["ci95"] == ref["ci95"]


def test_model0_floors_behave_as_designed_on_the_synthetic_world(world):
    report, _ = build(world)
    previous = report["detection"]["headline"][b.PREVIOUS]["metrics"]
    earlier = report["detection"]["headline"][b.EARLIER]["metrics"]
    assert previous["recall"] == 1.0 and previous["fpr_healthy"] == 0.0
    assert earlier["recall"] < previous["recall"] and earlier["fpr_healthy"] == 0.0


def test_the_classifier_beats_the_stronger_floor_with_a_margin(world):
    report, _ = build(world)
    margin = report["margins"]["headline"][b.EARLIER]
    assert margin["difference"]["recall"] > 0
    assert margin["ci95"]["recall"][0] <= margin["difference"]["recall"] <= margin["ci95"]["recall"][1]


def test_per_band_rows_cover_model0_with_a_validation_reference(world):
    report, _ = build(world)
    rows = [row for row in report["per_band"] if row["model"] == b.EARLIER]
    assert rows and all(row["family"] == "detection" for row in rows)
    assert any(row["vs_validation"] for row in rows) and any(row["vs_other_band"] for row in rows)


def test_shares_include_the_false_alarm_rate_and_the_cascade(world):
    report, _ = build(world)
    shares = report["share_flagged"]["headline"]
    assert "NORMAL" in shares and "CASCADING_FAILURE" in shares
    assert shares["NORMAL"][b.PREVIOUS]["share"] == 0.0


def test_a_classifier_package_with_the_wrong_columns_is_refused(world, tmp_path):
    w, qdir, _ = world
    bad = tm.package(tmp_path / "pkgs" / "bad", RandomForestClassifier(n_estimators=2, random_state=0).fit(
        np.asarray([[0, 0], [1, 1]]), np.asarray(["NORMAL", "TRAFFIC_SPIKE"])),
        {"task": "incident_type", "estimator": "random_forest", "classes": ["NORMAL", "TRAFFIC_SPIKE"]})
    classifiers = [m.load_package("incident_type", bad, "classifier")]
    with pytest.raises(evaluate.EvalError, match="columns differ"):
        r.build_report(w.root, qdir, classifiers, resamples=200, allow_partial=True, repo_root=w.root.parent)


def test_main_writes_both_files_and_refuses_to_overwrite(world, tmp_path, capsys):
    w, qdir, pkg = world
    out = tmp_path / "out"
    args = ["--dataset", str(w.root), "--queues", str(qdir), "--classifier", f"incident_type={pkg}",
            "--out", str(out), "--bootstrap", "200", "--allow-partial"]
    assert r.main(args) == 0
    text = (out / r.OUT_JSON).read_text()
    assert "NaN" not in text and json.loads(text)["provenance"]["model0"]["tuned"] is False
    assert r.main(args) == 1
    assert "already holds a result" in capsys.readouterr().err
    assert r.main(args + ["--force"]) == 0


def test_the_markdown_says_what_a_rule_cannot_do(world, tmp_path):
    w, qdir, pkg = world
    out = tmp_path / "out"
    assert r.main(["--dataset", str(w.root), "--queues", str(qdir), "--classifier", f"incident_type={pkg}",
                   "--out", str(out), "--bootstrap", "200", "--allow-partial"]) == 0
    text = (out / r.OUT_MD).read_text()
    for needle in ("Read this first", "Model 0 is a rule", "Frozen before it touched the test set", "n/a",
                   "Model 0 (earlier mean, the stronger floor)", "Gap to validation", "no claim about real production incidents"):
        assert needle.lower() in text.lower(), needle


def test_m9_2s_detectors_are_shown_beside_model0_when_given(world, tmp_path):
    ref = tmp_path / "m92.json"
    ref.write_text(json.dumps({"detection": {"iforest": {"headline": {
        "metrics": {"precision": 0.6, "recall": 1.0, "f1": 0.75, "fpr": 1.0}, "ci95": {}, "runs": 320}}}}))
    report, _ = build(world, m9_2_results=str(ref))
    assert report["m9_2_reference"]["iforest"]["metrics"]["recall"] == 1.0

def run_main(world, tmp_path, *extra):
    w, qdir, pkg = world
    out = tmp_path / "out"
    code = r.main(["--dataset", str(w.root), "--queues", str(qdir), "--classifier", f"incident_type={pkg}",
                   "--out", str(out), "--bootstrap", "200", "--allow-partial", *extra])
    assert code == 0
    return out


def test_the_job_and_commit_are_recorded(world, tmp_path):
    out = run_main(world, tmp_path, "--job", "job-1", "--commit", "abc123")
    provenance = json.loads((out / r.OUT_JSON).read_text())["provenance"]
    assert provenance["classifier"]["job"] == "job-1" and provenance["git"]["commit"] == "abc123"
    assert "job `job-1`" in (out / r.OUT_MD).read_text()


def test_the_markdown_shows_latency_and_the_runs_never_flagged(world, tmp_path):
    text = (run_main(world, tmp_path) / r.OUT_MD).read_text()
    assert "Median detection latency" in text and "Incident runs never flagged" in text
    assert "which a median would hide" in text


def test_the_markdown_lists_what_model0_cannot_be_compared_on(world, tmp_path):
    text = (run_main(world, tmp_path) / r.OUT_MD).read_text()
    for needle in ("What Model 0 cannot be compared on", "Confounders (30 runs)", "Time to recognize a cascade",
                   "XGBoost and Random Forest"):
        assert needle in text, needle
