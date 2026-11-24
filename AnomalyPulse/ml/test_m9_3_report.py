"""Tests for M9-3's report, on M9-2's synthetic world. No real data, no AWS.

Run from AnomalyPulse/, in the training image's versions:
    python -m pytest ml/test_m9_3_report.py ml/test_m9_3_baseline.py ml/test_m9_2_metrics.py -q

The world (ml/test_m9_2_metrics.build): 26 test runs (22 in the headline: 2 normal, 16 band, a 12- and a
2-minute fault, 2 cascades; plus 4 confounders), 4 stress runs and 8 validation runs. This file adds one
training run, which no M9-3 number may read.
"""
import json

import numpy as np
import pytest
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from ml import evaluate
from ml import m9_2_metrics as m
from ml import m9_3_baseline as b
from ml import m9_3_report as r
from ml import test_m9_2_metrics as tm
from ml.test_m9_3_baseline import feats_with_model0_columns

HEADLINE_RUNS, HEADLINE_INCIDENT_RUNS, STRESS_RUNS, VALIDATION_RUNS = 22, 20, 4, 8


def packages(tmp_path):
    """Over the synthetic columns, Model 0's included: the live classifier (a forest), a second classifier
    (logistic regression), an Isolation Forest with its cut-off between normal and fault windows, and a
    binary detector."""
    cols = sorted(feats_with_model0_columns("NORMAL", 0))
    X = np.asarray([[feats_with_model0_columns(c, k)[col] for col in cols] for c in tm.CLASSES for k in range(30)])
    y = np.asarray([c for c in tm.CLASSES for _ in range(30)])
    root = tmp_path / "pkgs"
    live = RandomForestClassifier(n_estimators=20, random_state=0).fit(X, y)
    other = LogisticRegression(max_iter=2000).fit(X, y)
    calm = X[y == "NORMAL"] + np.random.default_rng(0).normal(0.0, 0.5, size=X[y == "NORMAL"].shape)
    forest = IsolationForest(n_estimators=100, random_state=0).fit(calm)
    cut = (forest.score_samples(calm).min() + forest.score_samples(X[y != "NORMAL"]).max()) / 2
    binary = LogisticRegression(max_iter=2000).fit(X, np.where(y == "NORMAL", "NORMAL", "ANOMALY"))
    return {
        "live": tm.package(root / "live", live, {"task": "incident_type", "estimator": "random_forest", "columns": cols,
                                                 "classes": [str(c) for c in live.classes_]}),
        "other": tm.package(root / "other", other, {"task": "incident_type", "estimator": "logistic_regression",
                                                    "columns": cols, "classes": [str(c) for c in other.classes_]}),
        "iforest": tm.package(root / "iforest", forest, {"kind": "detector", "task": "anomaly_iforest", "columns": cols,
                                                         "estimator": "isolation_forest", "threshold": float(cut)}),
        "binary": tm.package(root / "binary", binary, {"task": "anomaly_binary", "estimator": "logistic_regression",
                                                       "columns": cols, "classes": [str(c) for c in binary.classes_]}),
    }


def make_world(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(tm, "feats", feats_with_model0_columns)
    real_finish = tm.World.finish

    def finish(self):
        # one training run, labeled like a validation run, which validation must not read
        self.run("train", "TRAFFIC_SPIKE", None, tm.fault_windows("TRAFFIC_SPIKE", ["traffic-spike"]), "2026-10-20")
        return real_finish(self)

    monkeypatch.setattr(tm.World, "finish", finish)
    w, qdir = tm.build(tmp_path, **kw)
    return w, qdir, packages(tmp_path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return make_world(tmp_path, monkeypatch)


def load(pk, names=("live", "other"), detectors=("iforest", "binary")):
    return ([m.load_package("incident_type" if n == "live" else n, pk[n], "classifier", job=f"job-{n}") for n in names],
            [m.load_package(n, pk[n], "detector") for n in detectors])


def build(world, names=("live", "other"), detectors=("iforest", "binary"), **kw):
    w, qdir, pk = world
    classifiers, dets = load(pk, names, detectors)
    report = r.build_report(w.root, qdir, classifiers, dets, resamples=200, allow_partial=True,
                            repo_root=w.root.parent, **kw)
    return report, classifiers, dets


@pytest.fixture
def report(world):
    return build(world)[0]


def table(markdown, first_header):
    """(header, {row label: cells}) of the markdown table whose header starts with `first_header`."""
    lines = markdown.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {first_header} |"))
    header = [c.strip() for c in lines[start].strip("|").split("|")]
    rows = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = cells[1:]
    return header, rows


# --- what is scored ---

def test_the_report_has_every_slice_and_model(report):
    assert set(report["detection"]) == {"headline", "all_test", "stress"}
    assert set(report["detection"]["headline"]) == set(r.MODELS)
    assert set(report["margins"]["headline"]) == set(b.NAMES)
    assert report["classifier_names"] == ["incident_type", "other"]
    assert report["other_models"]["names"] == ["other_as_detector", "iforest", "binary", m.DEPLOYED]
    assert set(report["other_models"]["classification"]) == {"other"}
    assert set(report["time_to_recognize_cascade"]) == {"incident_type", "other"}
    assert report["extreme_slice"]["runs"] == STRESS_RUNS


def test_the_headline_holds_exactly_the_test_runs_without_confounders(report):
    head = report["detection"]["headline"]
    for name in r.MODELS:
        assert head[name]["runs"] == HEADLINE_RUNS and head[name]["latency"]["incident_runs"] == HEADLINE_INCIDENT_RUNS
    assert report["detection"]["all_test"][b.EARLIER]["runs"] == HEADLINE_RUNS + 4
    assert report["provenance"]["counts"]["headline_runs"] == HEADLINE_RUNS
    for name in report["other_models"]["names"]:
        assert report["other_models"]["detection"]["headline"][name]["runs"] == HEADLINE_RUNS


def test_validation_reads_validation_runs_only(report):
    assert report["validation"]["runs"] == VALIDATION_RUNS
    assert report["provenance"]["counts"]["validation_runs"] == VALIDATION_RUNS
    assert report["other_models"]["classification"]["other"]["validation"]["runs"] == VALIDATION_RUNS
    assert all(report["validation"]["other_models"][n]["runs"] == VALIDATION_RUNS for n in report["other_models"]["names"])


def test_every_models_margin_is_that_model_minus_the_floor(report):
    head, other = report["detection"]["headline"], report["other_models"]
    for name in other["names"]:
        for floor in b.NAMES:
            margin = other["margins"]["headline"][name][floor]["difference"]
            mine, base = other["detection"]["headline"][name]["metrics"], head[floor]["metrics"]
            assert margin["recall"] == pytest.approx(mine["recall"] - base["recall"]), (name, floor)
            assert margin["fpr"] == pytest.approx(mine["fpr"] - base["fpr"]), (name, floor)
            healthy = other["share_margins"]["headline"][name][floor][b.HEALTHY]
            assert healthy["ml"] == pytest.approx(mine["fpr_healthy"]) and healthy["baseline"] == base["fpr_healthy"]


def test_ranking_metrics_are_n_a_for_flag_only_detectors_only(report):
    for section in (report["detection"]["headline"], report["validation"]["detection"]):
        for name in r.MODELS:
            assert all(section[name]["metrics"][k] is None for k in r.RANKING)
    other = report["other_models"]["detection"]["headline"]
    assert all(other["other_as_detector"]["metrics"][k] is None for k in r.RANKING)
    assert other[m.DEPLOYED]["metrics"]["roc_auc"] is None              # a yes/no rule
    assert other["iforest"]["metrics"]["roc_auc"] is not None and other["binary"]["metrics"]["roc_auc"] is not None


def test_the_live_classifier_numbers_are_m9_2s_own(world):
    report, classifiers, _ = build(world)
    w, qdir, _ = world
    theirs = m.build_report(w.root, qdir, [], classifiers[:1], resamples=200, allow_partial=True, repo_root=w.root.parent)
    ours, ref = report["classification"]["headline"], theirs["classification"]["incident_type"]["headline"]
    assert ours["accuracy"] == ref["accuracy"] and ours["macro_f1"] == ref["macro_f1"] and ours["ci95"] == ref["ci95"]


def test_model0_floors_behave_as_designed_on_the_synthetic_world(report):
    previous = report["detection"]["headline"][b.PREVIOUS]["metrics"]
    earlier = report["detection"]["headline"][b.EARLIER]["metrics"]
    assert previous["recall"] == 1.0 and previous["fpr_healthy"] == 0.0
    assert earlier["recall"] < previous["recall"] and earlier["fpr_healthy"] == 0.0


def test_shares_are_each_detectors_own(report):
    """Counted in test_m9_3_baseline.test_share_margins_by_hand: 33 spike windows, the earlier-mean floor
    flags 11; the previous-window floor flags every fault window (500 ms against 100)."""
    spike = report["share_flagged"]["headline"]["TRAFFIC_SPIKE"]
    assert spike["windows"] == 33
    assert spike[b.PREVIOUS]["share"] == 1.0 and spike[b.EARLIER]["share"] == pytest.approx(11 / 33)
    assert report["share_flagged"]["headline"]["NORMAL"][b.PREVIOUS]["share"] == 0.0


def test_the_classifier_beats_the_stronger_floor_with_a_margin(report):
    margin = report["margins"]["headline"][b.EARLIER]
    assert margin["difference"]["recall"] > 0
    assert margin["ci95"]["recall"][0] <= margin["difference"]["recall"] <= margin["ci95"]["recall"][1]
    cascade = report["share_margins"]["headline"][b.EARLIER]["CASCADING_FAILURE"]
    assert cascade["windows"] == 8 and cascade["baseline"] == 0.25


def test_per_band_reference_is_validations_not_the_test_sets(tmp_path, monkeypatch):
    """The upper traffic band's test runs look NORMAL, so every model misses them. Validation's spikes
    look like spikes: its reference recall is 1.0, and the band is flagged against it. A reference taken
    from the test runs would be 0.5 (one band of two)."""
    report = build(make_world(tmp_path, monkeypatch, looks=tm.UPPER_SPIKE_MISSED))[0]
    rows = [row for row in report["per_band"] if row["injector"] == "traffic-spike" and row["band"] == [8, 10]
            and row["model"] in (b.EARLIER, "incident_type")]
    assert len(rows) == 2
    for row in rows:
        assert row["recall"] == 0.0
        assert row["vs_validation"]["ref_recall"] == pytest.approx(1.0 if row["family"] == "classification" else 0.4)
        assert row["vs_validation"]["status"] in ("degraded", "possible")
    never = report["detection"]["headline"][b.EARLIER]["latency"]
    assert (never["missed_runs"], never["incident_runs"]) == (2, HEADLINE_INCIDENT_RUNS)


def test_a_forest_that_flags_everything_is_called_out_and_scored_as_itself(tmp_path, monkeypatch):
    """M9-2's test-set failure, in miniature: a cut-off every window scores below. The forest then flags
    every healthy window, it is listed as uninformative, its own share margins say so, and the deployed
    rule reduces to the live classifier read as a detector."""
    w, qdir, pk = make_world(tmp_path, monkeypatch)
    path = pk["iforest"] / "model.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "threshold": 1.0}))     # scores are all below 0
    report = build((w, qdir, pk))[0]
    other = report["other_models"]
    assert other["detection"]["headline"]["iforest"]["metrics"]["fpr_healthy"] == 1.0
    assert report["uninformative_detectors"] == {"iforest": 1.0}
    healthy = other["share_margins"]["headline"]["iforest"][b.EARLIER][b.HEALTHY]
    assert (healthy["ml"], healthy["baseline"], healthy["difference"]) == (1.0, 0.0, 1.0)
    live = report["detection"]["headline"][b.CLASSIFIER_AS_DETECTOR]
    assert other["detection"]["headline"][m.DEPLOYED]["counts"] == live["counts"]


def test_a_classifier_package_with_the_wrong_columns_is_refused(world, tmp_path):
    w, qdir, _ = world
    bad = tm.package(tmp_path / "pkgs" / "bad", RandomForestClassifier(n_estimators=2, random_state=0).fit(
        np.asarray([[0, 0], [1, 1]]), np.asarray(["NORMAL", "TRAFFIC_SPIKE"])),
        {"task": "incident_type", "estimator": "random_forest", "classes": ["NORMAL", "TRAFFIC_SPIKE"]})
    classifiers = [m.load_package("incident_type", bad, "classifier")]
    with pytest.raises(evaluate.EvalError, match="columns differ"):
        r.build_report(w.root, qdir, classifiers, resamples=200, allow_partial=True, repo_root=w.root.parent)


def test_package_names_that_would_collide_are_refused(world):
    w, qdir, pk = world
    classifiers, dets = load(pk)
    clash = [m.load_package("deployed", pk["iforest"], "detector")]
    with pytest.raises(m.MetricsError, match="distinct"):
        r.build_report(w.root, qdir, classifiers, clash, resamples=200, allow_partial=True, repo_root=w.root.parent)


# --- against M9-2's published numbers ---

def test_m9_2s_detectors_and_classifier_reproduce_and_a_changed_number_is_caught(world, tmp_path):
    w, qdir, pk = world
    classifiers, dets = load(pk, names=("live",))
    theirs = m.build_report(w.root, qdir, dets, classifiers, resamples=200, allow_partial=True, repo_root=w.root.parent)
    path = tmp_path / "m92.json"
    path.write_text(json.dumps(m._clean(theirs)))
    report = build(world, m9_2_results=str(path))[0]     # with a second classifier too: the numbers must not move
    check = report["m9_2_check"]
    assert check["detection"] == {"iforest": [], "binary": [], m.DEPLOYED: []}
    assert check["classification"] == [] and check["confounders"] == []
    theirs["detection"]["binary"]["headline"]["metrics"]["recall"] += 0.01
    path.write_text(json.dumps(m._clean(theirs)))
    check = build(world, m9_2_results=str(path))[0]["m9_2_check"]
    assert check["detection"]["binary"] == ["/metrics/recall"]


# --- the markdown ---

def run_main(world, tmp_path, *extra, out="out"):
    w, qdir, pk = world
    out = tmp_path / out
    code = r.main(["--dataset", str(w.root), "--queues", str(qdir), "--classifier", f"incident_type={pk['live']}",
                   "--classifier", f"other={pk['other']}", "--detector", f"iforest={pk['iforest']}",
                   "--detector", f"binary={pk['binary']}", "--out", str(out), "--bootstrap", "200",
                   "--allow-partial", *extra])
    assert code == 0
    return out


@pytest.fixture
def written(world, tmp_path):
    out = run_main(world, tmp_path)
    return json.loads((out / r.OUT_JSON).read_text()), (out / r.OUT_MD).read_text()


def test_main_writes_both_files_and_refuses_to_overwrite(world, tmp_path, capsys):
    out = run_main(world, tmp_path)
    text = (out / r.OUT_JSON).read_text()
    assert "NaN" not in text and json.loads(text)["provenance"]["model0"]["tuned"] is False
    w, qdir, pk = world
    args = ["--dataset", str(w.root), "--queues", str(qdir), "--classifier", f"incident_type={pk['live']}",
            "--out", str(out), "--bootstrap", "200", "--allow-partial"]
    assert r.main(args) == 1
    assert "already holds a result" in capsys.readouterr().err
    assert r.main(args + ["--force"]) == 0


def test_jobs_are_name_value_pairs_and_recorded(world, tmp_path):
    out = run_main(world, tmp_path, "--job", "incident_type=job-1", "--job", "other=job-2", "--commit", "abc123")
    provenance = json.loads((out / r.OUT_JSON).read_text())["provenance"]
    assert provenance["classifier"]["job"] == "job-1" and provenance["packages"]["other"]["job"] == "job-2"
    assert provenance["git"]["commit"] == "abc123"
    text = (out / r.OUT_MD).read_text()
    assert "job `job-1`" in text and "job `job-2`" in text


@pytest.mark.parametrize("job", ["job-1", "nobody=job-1"])
def test_a_bare_job_or_one_naming_no_package_is_refused(world, tmp_path, job):
    with pytest.raises(SystemExit):
        run_main(world, tmp_path, "--job", job)


def test_the_detection_table_columns_follow_its_header(written):
    report, text = written
    header, rows = table(text, "Metric")
    assert header[1:] == [r.NAME[n] for n in r.MODELS]
    head = report["detection"]["headline"]
    for label, key in (("Recall", "recall"), ("False-positive rate, against the labels", "fpr")):
        assert rows[label] == [r._ci(head[n]["metrics"][key], head[n]["ci95"].get(key)) for n in r.MODELS]
    assert rows["False-positive rate, healthy windows only"] == [r._n(head[n]["metrics"]["fpr_healthy"]) for n in r.MODELS]
    assert rows["Incident runs never flagged"] == [
        f"{head[n]['latency']['missed_runs']} of {head[n]['latency']['incident_runs']}" for n in r.MODELS]
    assert rows["Incident runs never flagged"][0] == f"0 of {HEADLINE_INCIDENT_RUNS}"


def test_each_detection_column_shows_its_own_detector(written):
    """On the synthetic world two detectors can tie, so give each a value of its own and render again."""
    report, _ = written
    for i, n in enumerate(r.MODELS):
        for key in ("precision", "recall", "f1", "fpr"):
            report["detection"]["headline"][n]["metrics"][key] = 0.1 * (i + 1)
            report["detection"]["headline"][n]["ci95"][key] = None
    _, rows = table(r.render_markdown(report), "Metric")
    for label in ("Precision", "Recall", "F1", "False-positive rate, against the labels"):
        assert rows[label] == ["0.100", "0.200", "0.300"], label


def test_the_margin_table_columns_follow_its_header(written):
    report, text = written
    header, rows = table(text, "Classifier minus Model 0")
    assert header[1:] == ["against the earlier-mean floor", "against the previous-window floor"]
    mg, sm = report["margins"]["headline"], report["share_margins"]["headline"]
    assert rows["Recall"] == [r._signed(mg[f]["difference"]["recall"], mg[f]["ci95"]["recall"]) for f in (b.EARLIER, b.PREVIOUS)]
    assert rows["Healthy windows flagged (lower is better)"] == [
        r._signed(sm[f][b.HEALTHY]["difference"], sm[f][b.HEALTHY]["ci95"]) for f in (b.EARLIER, b.PREVIOUS)]


def test_every_model_and_every_classifier_have_a_row(written):
    report, text = written
    _, rows = table(text, "Model")
    assert set(rows) == {r.label_of(n, report) for n in [b.CLASSIFIER_AS_DETECTOR, *report["other_models"]["names"]]} | {
        r.NAME[b.EARLIER]}
    live = rows[r.NAME[b.CLASSIFIER_AS_DETECTOR]]
    mg = report["margins"]["headline"][b.EARLIER]
    assert live[1] == r._ci(report["detection"]["headline"][b.CLASSIFIER_AS_DETECTOR]["metrics"]["recall"],
                            report["detection"]["headline"][b.CLASSIFIER_AS_DETECTOR]["ci95"]["recall"])
    assert live[4] == r._signed(mg["difference"]["recall"], mg["ci95"]["recall"])
    _, rows = table(text, "Classifier")
    assert [k.split(" ")[0] for k in rows] == ["incident_type", "other", "Model"]
    other = report["other_models"]["classification"]["other"]["headline"]
    assert rows["other (logistic_regression)"][1] == r._ci(other["accuracy"], other["ci95"]["accuracy"])
    assert all(cell.startswith("n/a") for cell in rows["Model 0 (either floor)"])


def test_the_markdown_says_what_a_rule_cannot_do_and_who_was_scored(written):
    _, text = written
    for needle in ("Read this first", "Model 0 is a rule", "Fixed before the test set existed", "n/a",
                   "Model 0 (earlier mean, the stronger floor)", "Gap to validation",
                   "no claim about real production incidents", "Every classifier M5-3 trained is scored",
                   "`other`", "M9-2's detectors are scored again here", "Median detection latency",
                   "Incident runs never flagged", "which a median would hide", "What Model 0 cannot be compared on",
                   "Time to recognize a cascade", "## 9. Confounders, read as detection"):
        assert needle.lower() in text.lower(), needle
    assert "were not scored" not in text and "30 runs" not in text
