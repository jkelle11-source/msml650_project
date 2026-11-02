"""Tests for the evaluation script (M5-3), on synthetic job folders. No AWS.

Run from AnomalyPulse/:  python3 -m pytest ml/test_evaluate.py -q
"""
import json

import pytest

from ml import evaluate
from ml.test_train_incident_rf import CLASSES, SMALL, make_root
import train_incident_rf as tr


def trained_root(tmp_path):
    root = make_root(tmp_path)
    tr.run(root)
    return root


def write_full_run(root, channel, eid):
    """Like the job-script tests' runs, plus the columns Model 0 reads and multi_fault_ms.

    Non-NORMAL windows get a 50% 5xx rate, so Model 0 flags exactly those. Cascade windows
    carry a full minute of multi-fault time."""
    features, labels = [], []
    n = 0
    for label in CLASSES:
        index = CLASSES.index(label)
        for k in range(2):
            w = f"2026-10-16T03:{n:02d}:00.000Z"
            n += 1
            row = {"window_start": w, "x1": float(index * 10 + k), "x2": float(-3 * index)}
            row.update({column: 0.0 for column in evaluate.model0_columns()})
            row["order_error_rate_5xx"] = 0.0 if label == "NORMAL" else 0.5
            features.append(row)
            labels.append({"window_start": w, "label": label, "included": True,
                           "excluded_reason": None,
                           "multi_fault_ms": 60000 if label == "CASCADING_FAILURE" else 0})
    base = root / "input" / "data" / channel
    for tree, file, rows in (("tier1/features", "features.jsonl", features),
                             ("tier2/labels", "labels.jsonl", labels)):
        folder = base / tree / f"experiment_id={eid}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text("".join(json.dumps(r) + "\n" for r in rows))


def make_full_root(tmp_path):
    for channel, eid in (("train", "a"), ("train", "b"), ("validation", "c")):
        write_full_run(tmp_path, channel, eid)
    config = tmp_path / "input" / "config"
    config.mkdir(parents=True)
    (config / "hyperparameters.json").write_text(json.dumps(SMALL))
    return tmp_path


def lab(ws, label=None, included=True, reason=None, multi=0):
    return {"window_start": ws, "label": label, "included": included,
            "excluded_reason": reason, "multi_fault_ms": multi}


def w(hhmm):
    return f"2026-10-24T{hhmm}:00.000Z"


def test_a_trained_package_is_scored_against_model0(tmp_path):
    root = make_full_root(tmp_path)
    tr.run(root)
    report, classes = evaluate.build_report(root, {"random_forest": root / "model"})
    assert report["classifiers"]["random_forest"]["accuracy"] > 0.9
    assert set(report["model0"]) == {"previous_window", "earlier_mean_3"}
    assert "CASCADING_FAILURE" in classes


def test_the_cascade_margin_is_reported(tmp_path):
    root = make_full_root(tmp_path)
    tr.run(root)
    report, _ = evaluate.build_report(root, {"random_forest": root / "model"})
    assert report["cascade"]["windows"] == 2
    assert "random_forest" in report["cascade"]["margin"]


def test_xgboost_numbers_are_mapped_back_to_class_names(tmp_path):
    pytest.importorskip("xgboost")
    import train_incident_xgb as tx
    root = make_full_root(tmp_path)
    tx.run(root)
    report, _ = evaluate.build_report(root, {"xgboost": root / "model"})
    assert report["classifiers"]["xgboost"]["accuracy"] > 0.9


def test_a_package_whose_columns_differ_is_refused(tmp_path):
    root = trained_root(tmp_path)
    model, meta = evaluate.load_package(root / "model")
    meta["columns"] = ["x1", "zzz"]
    with pytest.raises(evaluate.EvalError, match="columns differ"):
        evaluate.predict(model, meta, evaluate.load_validation(root))


def test_a_detector_package_is_refused(tmp_path):
    root = trained_root(tmp_path)
    model, meta = evaluate.load_package(root / "model")
    meta["kind"] = "detector"
    with pytest.raises(evaluate.EvalError, match="detector"):
        evaluate.predict(model, meta, evaluate.load_validation(root))


def test_model0_reads_twelve_columns():
    columns = evaluate.model0_columns()
    assert len(columns) == len(set(columns)) == 12
    assert "order_p95_latency_ms_change" in columns and "payment_error_rate_5xx" in columns


def test_main_refuses_features_that_lack_model0_columns(tmp_path, capsys):
    root = trained_root(tmp_path)
    code = evaluate.main(["--root", str(root), "--package", f"rf={root / 'model'}"])
    assert code == 1
    assert "Model 0 reads columns the features lack" in capsys.readouterr().err


def test_a_malformed_package_argument_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        evaluate.main(["--root", str(tmp_path), "--package", "no-equals-sign"])


def test_main_scores_and_writes_the_report_end_to_end(tmp_path, capsys):
    root = make_full_root(tmp_path)
    tr.run(root)
    out = tmp_path / "report.md"
    code = evaluate.main(["--root", str(root), "--package", f"random_forest={root / 'model'}",
                          "--out", str(out)])
    assert code == 0
    text = out.read_text()
    assert capsys.readouterr().out.strip() == text.strip()
    for needle in ("Model 0 (earlier_mean_3)", "Model 0 (previous_window)", "random_forest",
                   "Confusion matrix", "Cascade margin over Model 0",
                   "Model 0: share of each anomaly class flagged", "Time to recognize a cascade",
                   "Wrong windows"):
        assert needle in text


def test_model0_alone_can_be_scored_with_no_package(tmp_path, capsys):
    root = make_full_root(tmp_path)
    assert evaluate.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Model 0 (earlier_mean_3)" in out and "No trained model was scored" in out


def test_model0_flags_exactly_the_anomalous_windows_in_the_synthetic_data(tmp_path):
    root = make_full_root(tmp_path)
    report, _ = evaluate.build_report(root, {})
    for variant in ("previous_window", "earlier_mean_3"):
        assert report["model0"][variant]["recall"] == 1.0
        assert report["model0"][variant]["fpr"] == 0.0


def test_main_reports_a_missing_validation_channel(tmp_path, capsys):
    assert evaluate.main(["--root", str(tmp_path)]) == 1
    assert "no runs" in capsys.readouterr().err


def test_lookback_is_the_labeled_windows_plus_the_startup_minute():
    labels = [lab(w("11:29"), None, False, "partial_window"), lab(w("11:30"), None, False, "startup"),
              lab(w("11:31"), "NORMAL"), lab(w("11:32"), None, False, "db_throttling_onset"),
              lab(w("11:33"), "CASCADING_FAILURE"), lab(w("11:37"), None, False, "recovery")]
    features = [{"window_start": r["window_start"]} for r in labels[1:] + labels[:1]]
    assert [r["window_start"] for r in evaluate.lookback_rows(features, labels)] == [
        w("11:30"), w("11:31"), w("11:33")]


def test_time_to_recognize_when_the_cascade_starts_in_a_labeled_window():
    # Jake's hand check, exp-20261024T112908Z-a9bf: the start is 11:33:16.4
    labels = [lab(w("11:31"), "NORMAL"), lab(w("11:32"), None, False, "db_throttling_onset"),
              lab(w("11:33"), "CASCADING_FAILURE", multi=43600),
              lab(w("11:34"), "CASCADING_FAILURE", multi=60000)]
    called = {w("11:31"): "NORMAL", w("11:33"): "CASCADING_FAILURE", w("11:34"): "CASCADING_FAILURE"}
    assert evaluate.time_to_recognize(labels, called) == pytest.approx(43.6)


def test_time_to_recognize_when_the_cascade_starts_in_an_onset_minute():
    # Jake's hand check, exp-20261024T145915Z-2a18: the start is 15:03:12.0, in a minute that is not scored
    labels = [lab(w("15:01"), "NORMAL"), lab(w("15:03"), None, False, "db_throttling_onset", multi=48000),
              lab(w("15:04"), "CASCADING_FAILURE", multi=60000)]
    called = {w("15:01"): "NORMAL", w("15:04"): "CASCADING_FAILURE"}
    assert evaluate.time_to_recognize(labels, called) == pytest.approx(108.0)


def test_an_onset_minute_is_never_scored_even_if_the_model_calls_it_a_cascade():
    labels = [lab(w("15:03"), None, False, "db_throttling_onset", multi=48000),
              lab(w("15:04"), "CASCADING_FAILURE", multi=60000),
              lab(w("15:05"), "CASCADING_FAILURE", multi=60000),
              lab(w("15:06"), "CASCADING_FAILURE", multi=60000)]
    called = {w("15:03"): "CASCADING_FAILURE", w("15:04"): "NORMAL", w("15:05"): "NORMAL",
              w("15:06"): "CASCADING_FAILURE"}
    assert evaluate.time_to_recognize(labels, called) == pytest.approx(228.0)


def test_a_cascade_the_model_never_calls_has_no_time():
    labels = [lab(w("11:33"), "CASCADING_FAILURE", multi=60000)]
    assert evaluate.time_to_recognize(labels, {w("11:33"): "NORMAL"}) is None


def test_which_runs_are_cascade_runs():
    assert evaluate.is_cascade_run([lab(w("11:33"), "CASCADING_FAILURE")])
    assert not evaluate.is_cascade_run([lab(w("11:33"), "NORMAL"), lab(w("11:34"), None, False, "recovery")])


def test_summary_counts_median_max_and_never():
    s = evaluate.summarize_ttr([43.6, 108.0, None])
    assert s["runs"] == 3 and s["never"] == 1 and s["max"] == 108.0
    assert s["median"] == pytest.approx(75.8)


def test_time_to_recognize_end_to_end_on_a_synthetic_cascade(tmp_path):
    root = make_full_root(tmp_path)
    tr.run(root)
    report, _ = evaluate.build_report(root, {"random_forest": root / "model"})
    per_run = report["time_to_recognize"]["random_forest"]
    assert list(per_run) == ["c"]
    assert per_run["c"] == pytest.approx(60.0)


def write_validation_run(root, eid, windows):
    """One validation run from (minute, label, reason, order p95, its change) tuples; a window
    with a reason is excluded. Every other column Model 0 reads is 0."""
    features, labels = [], []
    for minute, label, reason, p95, change in windows:
        row = {column: 0.0 for column in evaluate.model0_columns()}
        row.update(window_start=w(minute), order_p95_latency_ms=p95, order_p95_latency_ms_change=change)
        features.append(row)
        labels.append(lab(w(minute), label, reason is None, reason))
    base = root / "input" / "data" / "validation"
    for tree, file, rows in (("tier1/features", "features.jsonl", features),
                             ("tier2/labels", "labels.jsonl", labels)):
        folder = base / tree / f"experiment_id={eid}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_earlier_mean_floor_leaves_out_the_current_window_and_onset_minutes(tmp_path):
    # Lookback p95s before 03:03 are 100 (startup) and 100 (NORMAL): mean 100, so 250 > 2 x 100
    # flags it. With 03:03 itself in the mean (150), or the excluded onset minute (400), it
    # would not, and 03:01 (100 against 100) must not flag either.
    write_validation_run(tmp_path, "v", [("03:00", None, "startup", 100.0, None),
                                         ("03:01", "NORMAL", None, 100.0, 0.0),
                                         ("03:02", None, "db_throttling_onset", 1000.0, 900.0),
                                         ("03:03", "TRAFFIC_SPIKE", None, 250.0, 150.0)])
    report, _ = evaluate.build_report(tmp_path, {})
    assert report["model0"]["earlier_mean_3"]["recall"] == 1.0
    assert report["model0"]["earlier_mean_3"]["fpr"] == 0.0


def test_build_report_refuses_features_without_model0_columns(tmp_path):
    write_validation_run(tmp_path, "v", [("03:01", "NORMAL", None, 100.0, 0.0)])
    path = tmp_path / "input/data/validation/tier1/features/experiment_id=v/features.jsonl"
    row = json.loads(path.read_text())
    del row["cart_error_rate_5xx"]
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(evaluate.EvalError, match="cart_error_rate_5xx"):
        evaluate.build_report(tmp_path, {})


def test_fault_minutes_tells_healthy_first_and_later_minutes_apart():
    labels = [dict(lab(w("11:31"), "NORMAL"), fault_ms={}),
              dict(lab(w("11:33"), "TRAFFIC_SPIKE"), fault_ms={"f#0": 49526}),
              dict(lab(w("11:32"), "NORMAL"), fault_ms={"f#0": 12000}),
              dict(lab(w("11:34"), "CASCADING_FAILURE"), fault_ms={"f#0": 60000, "f#1": 43600}),
              lab(w("11:35"), None, False, "recovery")]
    assert evaluate.fault_minutes(labels) == {
        w("11:31"): (0.0, "healthy"), w("11:32"): (12.0, "first minute"),
        w("11:33"): (49.526, "later"), w("11:34"): (60.0, "later"), w("11:35"): (0.0, "healthy")}


def test_wrong_windows_lists_only_the_misses_with_their_place():
    labels = [dict(lab(w("11:31"), "NORMAL"), fault_ms={}),
              dict(lab(w("11:32"), "NORMAL"), fault_ms={"f#0": 22300}),
              dict(lab(w("11:33"), "TRAFFIC_SPIKE"), fault_ms={"f#0": 60000})]
    runs = [("r", [], labels)]
    rows = [("r", {"window_start": l["window_start"], "label": l["label"]}) for l in labels]
    wrong = evaluate.wrong_windows(runs, rows, ["TRAFFIC_SPIKE", "TRAFFIC_SPIKE", "TRAFFIC_SPIKE"])
    assert wrong == [{"run": "r", "window": w("11:31"), "true": "NORMAL", "predicted": "TRAFFIC_SPIKE",
                      "fault_s": 0.0, "where": "healthy"},
                     {"run": "r", "window": w("11:32"), "true": "NORMAL", "predicted": "TRAFFIC_SPIKE",
                      "fault_s": 22.3, "where": "first minute"}]


def test_the_wrong_window_summary_counts_each_place():
    wrong = {"m": [{"run": "r", "window": w("11:32"), "true": "NORMAL", "predicted": "TRAFFIC_SPIKE",
                    "fault_s": 22.3, "where": "first minute"}], "perfect": []}
    text = evaluate.render_wrong_windows(wrong)
    assert "| m | 1 | 1 | 0 | 0 |" in text and "| perfect | 0 | 0 | 0 | 0 |" in text
    assert "### m: wrong windows" in text and "### perfect" not in text
