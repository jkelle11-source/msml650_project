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


def test_latency_figure_shows_each_steps_median_and_p90_from_the_results(out):
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    text = svg_text(out, "fig6_latency_breakdown")
    for key in ("window", "pipeline_wait", "diagnosis", "end_to_end"):
        assert f"median {mf.rnd(steps[key]['median'])} s" in text, key
        assert f"90th percentile {mf.rnd(steps[key]['p90'])} s" in text, key


def test_latency_headline_is_the_largest_share(out):
    share = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["share_of_mean"]
    assert f"settle wait is {mf.pct(share['pipeline_wait'])}" in svg_text(out, "fig6_latency_breakdown")
    assert max(share, key=share.get) == "pipeline_wait"


def test_isolation_forest_is_never_drawn_as_a_success(out):
    text = svg_text(out, "fig2_detection")
    # It appears only as the warning panel: its healthy-window false-alarm share, 100%, and its caption.
    assert "Isolation Forest flags everything" in text and "is not detection" in text
    # Its recall (1.000) and F1 are never drawn beside the two real detectors.
    assert "1.000" not in text and "0.747" not in text
    assert "Deployed detector" in text and "Binary detector" in text


def test_confusion_title_names_the_largest_real_confusion(out):
    c = json.loads(mf.INPUTS["m9_2"].read_text())["classification"]["incident_type"]["headline"]
    text = svg_text(out, "fig1_confusion_matrix")
    assert "Dependency failure windows are called cascading failure 46% of the time" in text
    assert str(c["confusion"][2][0]) in text


def test_baseline_margins_come_from_the_paired_results(out):
    m = json.loads(mf.INPUTS["m9_3"].read_text())["margins"]["headline"]["model0_previous_window"]["difference"]
    assert f"+{100 * m['recall']:.0f} pts" in svg_text(out, "fig4_baseline_margins")


def test_cost_figure_ends_on_the_last_projection_row(out):
    last = json.loads(mf.INPUTS["cost"].read_text())["rows"][-1]
    text = svg_text(out, "fig5_cost_curve")
    assert f"${last['app']:,.0f}" in text and f"${last['anomalypulse']:,.0f}" in text
