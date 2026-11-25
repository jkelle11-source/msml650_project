"""The figure script: every figure is written, and what is drawn matches the result files."""

import json
import re

import pytest

from report import make_figures as mf


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("figs")
    mf.main(d)
    return d


def svg_text(out, name):
    return " ".join(re.findall(r">([^<>]+)<", (out / f"{name}.svg").read_text()))


def test_every_figure_is_written_as_png_and_svg(out):
    for name in mf.FIGURES:
        for ext in ("png", "svg"):
            assert (out / f"{name}.{ext}").stat().st_size > 5_000, name


def test_rounding_is_half_up():
    assert mf.rnd(74.5) == 75 and mf.rnd(52.2) == 52 and mf.rnd(169.4) == 169


def test_latency_figure_shows_each_steps_median_and_the_chain_totals(out):
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    text = svg_text(out, "fig6_latency_breakdown")
    for key in ("window", "pipeline_wait", "diagnosis"):
        assert f"{mf.rnd(steps[key]['median'])} s" in text, key
    assert f"End to end: {mf.rnd(steps['end_to_end']['median'])} s" in text
    assert f"within {mf.rnd(steps['end_to_end']['p90'])} s" in text


def test_detection_latency_is_the_window_plus_the_wait_per_run(out):
    det = mf.detection_latency(mf.load())
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    assert det["n"] == steps["end_to_end"]["n"] == 281
    assert f"Detection: {mf.rnd(det['median'])} s" in svg_text(out, "fig6_latency_breakdown")
    assert steps["window"]["median"] + steps["pipeline_wait"]["median"] - 2 < det["median"] < steps["end_to_end"]["median"]


def test_latency_headline_calls_the_wait_the_main_delay(out):
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    assert max(("window", "pipeline_wait", "diagnosis"), key=lambda k: steps[k]["median"]) == "pipeline_wait"
    assert "waiting for CloudWatch" in svg_text(out, "fig6_latency_breakdown")


def test_isolation_forest_is_never_drawn_as_a_success(out):
    text = svg_text(out, "fig2_detection")
    # It appears once, as the healthy-window false-alarm grid; its recall and F1 are never drawn.
    assert text.count("Isolation") == 1 and "100% flagged as faults" in text
    assert "1.000" not in text and "0.747" not in text
    assert "caught" in text.split("Isolation")[0]  # the "caught" grid belongs to the live detector


def test_confusion_title_names_the_largest_real_confusion(out):
    c = json.loads(mf.INPUTS["m9_2"].read_text())["classification"]["incident_type"]["headline"]
    text = svg_text(out, "fig1_confusion_matrix")
    row = c["confusion"][2]
    assert f"Dependency failure is mistaken for cascading failure {mf.pct(row[0] / sum(row))} of the time" in text


def test_baseline_margins_come_from_the_paired_results(out):
    b = json.loads(mf.INPUTS["m9_3"].read_text())
    det = b["detection"]["headline"]
    text = svg_text(out, "fig4_baseline_margins")
    gain = b["margins"]["headline"]["model0_earlier_mean_3"]["difference"]["recall"]
    assert f"+{100 * gain:.0f} points more faults caught" in text
    assert mf.pct(det["model0_earlier_mean_3"]["metrics"]["recall"], 1) in text
    assert mf.pct(det["classifier_as_detector"]["metrics"]["fpr_healthy"], 1) in text


def test_cost_figure_ends_on_the_last_projection_row(out):
    last = json.loads(mf.INPUTS["cost"].read_text())["rows"][-1]
    text = svg_text(out, "fig5_cost_curve")
    assert f"${last['app']:,.0f}" in text and f"${last['anomalypulse']:,.0f}" in text


def test_per_band_figure_shows_every_recall_and_the_dependency_gap(out):
    m = json.loads(mf.INPUTS["m9_2"].read_text())
    rows = [r for r in m["per_band"]["rows"] if r["family"] == "classification" and r["model"] == "incident_type"]
    text = svg_text(out, "fig3_per_band")
    for r in rows:
        assert mf.pct(r["recall"]) in text
    gaps = [100 * r["vs_validation"]["gap"] for r in rows if r["vs_validation"]["status"] == "degraded"]
    assert f"{min(gaps):.0f}\u2013{max(gaps):.0f} points below" in text
