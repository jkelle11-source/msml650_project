"""The figure script: every figure is written, and what is drawn matches the result files."""

import copy
import html
import json
import re

import pytest

from report import latency
from report import make_figures as mf


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("figs")
    mf.main(d)
    return d


def svg_texts(out, name):
    """The figure's text elements, in drawing order."""
    return [html.unescape(t) for t in re.findall(r">([^<>]+)<", (out / f"{name}.svg").read_text()) if t.strip()]


def svg_text(out, name):
    return " ".join(svg_texts(out, name))


def in_order(texts, expected):
    """True when `expected` appears in `texts` as a contiguous run."""
    return any(texts[k:k + len(expected)] == expected for k in range(len(texts)))


def test_every_figure_is_written_as_png_and_svg(out):
    for name in mf.FIGURES:
        for ext in ("png", "svg"):
            assert (out / f"{name}.{ext}").stat().st_size > 5_000, name


def test_a_rerun_writes_the_same_bytes(out, tmp_path):
    mf.main(tmp_path)
    for name in mf.FIGURES:
        for ext in ("png", "svg"):
            assert (tmp_path / f"{name}.{ext}").read_bytes() == (out / f"{name}.{ext}").read_bytes(), (name, ext)


def test_rounding_is_half_up():
    assert mf.rnd(74.5) == 75 and mf.rnd(52.2) == 52 and mf.rnd(169.4) == 169 and mf.rnd(249.5) == 250


# --- figure 1 ------------------------------------------------------------------------------------------------

def test_confusion_title_names_the_largest_real_confusion(out):
    c = json.loads(mf.INPUTS["m9_2"].read_text())["classification"]["incident_type"]["headline"]
    text = svg_text(out, "fig1_confusion_matrix")
    row = c["confusion"][2]
    assert f"Dependency failure is mistaken for cascading failure {mf.pct(row[0] / sum(row))} of the time" in text


def test_confusion_cells_are_shares_of_their_row(out):
    c = json.loads(mf.INPUTS["m9_2"].read_text())["classification"]["incident_type"]["headline"]
    texts = svg_texts(out, "fig1_confusion_matrix")
    for i, row in enumerate(c["confusion"]):
        assert mf.pct(row[i] / sum(row)) in texts, c["classes"][i]
    assert "54%" in texts      # dependency failure: 143 of 265 named correctly (row), not 143 of 145 (column)


def test_cells_under_one_percent_are_labelled_and_counted():
    assert mf.cell_text(0) == "" and mf.cell_text(0.0038) == "<1%" and mf.cell_text(0.0076) == "1%"


def test_confusion_footnote_counts_the_small_cells(out):
    # Rows: Lambda -> Normal 1 of 262; Normal -> cascading 1, dependency 2, Lambda 2 of 845.
    assert "<1%: 1 to 2 windows." in svg_text(out, "fig1_confusion_matrix")


# --- figure 2 ------------------------------------------------------------------------------------------------

def test_isolation_forest_is_never_drawn_as_a_success(out):
    text = svg_text(out, "fig2_detection")
    # It appears once, as the healthy-window false-alarm grid; its recall and F1 are never drawn.
    assert text.count("Isolation") == 1 and "100% flagged as faults" in text
    assert "1.000" not in text and "0.747" not in text
    assert "caught" in text.split("Isolation")[0]  # the "caught" grid belongs to the live detector


def test_detection_cards_show_the_live_detector_s_numbers(out):
    det = json.loads(mf.INPUTS["m9_2"].read_text())["detection"]["deployed"]["headline"]["metrics"]
    texts = svg_texts(out, "fig2_detection")
    assert f"{mf.pct(det['recall'], 1)} caught" in texts
    assert f"{mf.pct(det['fpr_healthy'], 1)} wrongly flagged" in texts
    assert not any("Only" in t or "only" in t for t in texts)


def test_the_live_detector_is_the_classifier_on_the_test_set(out):
    m9_2 = json.loads(mf.INPUTS["m9_2"].read_text())["detection"]
    m9_3 = json.loads(mf.INPUTS["m9_3"].read_text())["detection"]["headline"]["classifier_as_detector"]["metrics"]
    live = m9_2["deployed"]["headline"]["metrics"]
    assert m9_2["iforest"]["headline"]["metrics"]["fpr_healthy"] == 1.0
    assert live["recall"] == pytest.approx(m9_3["recall"]) and live["fpr_healthy"] == pytest.approx(m9_3["fpr_healthy"])
    assert "the same numbers as the AnomalyPulse classifier in figure 4." in svg_texts(out, "fig2_detection")


# --- figure 3 ------------------------------------------------------------------------------------------------

def band_rows():
    m = json.loads(mf.INPUTS["m9_2"].read_text())
    return [r for r in m["per_band"]["rows"] if r["family"] == "classification" and r["model"] == "incident_type"]


def test_per_band_figure_shows_every_recall_and_the_dependency_gap(out):
    rows = band_rows()
    text = svg_text(out, "fig3_per_band")
    for r in rows:
        assert mf.pct(r["recall"]) in text
    gaps = [100 * r["vs_validation"]["gap"] for r in rows if r["vs_validation"]["status"] == "degraded"]
    assert f"{min(gaps):.0f}–{max(gaps):.0f} points below" in text


def test_each_band_row_names_its_role_its_range_and_its_recall(out):
    texts = svg_texts(out, "fig3_per_band")
    for r in band_rows():
        label = dict(mf.ROLES)[r["role"]]
        assert in_order(texts, [label, mf.band_text(r["injector"], r["band"]), mf.pct(r["recall"])]), (r["injector"], r["role"])
    assert "Same strength as in training" not in texts
    # The lower band lies between the training bands, the upper just above them (plan, Control 1).
    assert dict(mf.ROLES) == {"interpolation": "Between the training bands", "mild extrapolation": "Just above the training range"}
    assert in_order(texts, ["Between the training bands", "1,500–2,500 ms timeout", "55%",
                            "Just above the training range", "3,500–4,500 ms timeout", "57%"])
    assert mf.band_text("payment-failure", [1500, 2500]) == "1,500–2,500 ms timeout"
    assert mf.band_text("db-throttling", [4.5, 5.5]) == "4.5–5.5× offered load"


def test_per_band_title_and_tags_follow_the_degraded_status(out):
    texts = svg_texts(out, "fig3_per_band")
    assert texts.count("DEGRADED") == 1 and texts.count("HOLDS") == 3
    assert "Three fault types hold up in both held-out bands; dependency failure drops to 55–57% in both" in texts
    assert "Validation: 97%" in texts   # traffic spike's reference


def test_one_degraded_band_is_enough_to_flag_the_fault(tmp_path):
    style_data = mf.load()
    data = copy.deepcopy(style_data)
    for r in data["m9_2"]["per_band"]["rows"]:
        if r["injector"] == "payment-failure" and r["role"] == "mild extrapolation" and r["model"] == "incident_type":
            r["vs_validation"]["status"] = "ok"
    mf.style()
    mf.per_band(data, tmp_path)
    texts = svg_texts(tmp_path, "fig3_per_band")
    assert texts.count("DEGRADED") == 1


# --- figure 4 ------------------------------------------------------------------------------------------------

def test_margin_text_keeps_one_decimal_only_below_ten_points():
    assert mf.margin_text(0.5277, [0.4786, 0.5757]) == "+53 [48, 58]"
    assert mf.margin_text(0.0932, [0.0352, 0.1560]) == "+9.3 [3.5, 15.6]"
    assert mf.margin_text(0.0, [0.0, 0.0]) == "0 [0, 0]"
    assert mf.points(0.985) == "+99" and mf.points(-0.012) == "-1.2"
    assert mf.share_text(0.0148) == "1.5%" and mf.share_text(0.9992) == "99.9%" and mf.share_text(1.0) == "100%"


def test_baseline_rows_are_the_paired_margins_against_the_three_window_rule(out):
    b = json.loads(mf.INPUTS["m9_3"].read_text())
    rule = "model0_earlier_mean_3"
    texts = svg_texts(out, "fig4_baseline_margins")
    overall = b["margins"]["headline"][rule]
    det = b["detection"]["headline"]
    assert in_order(texts, [mf.margin_text(overall["difference"]["recall"], overall["ci95"]["recall"]),
                            f"{mf.share_text(det[rule]['metrics']['recall'])} → "
                            f"{mf.share_text(det['classifier_as_detector']['metrics']['recall'])}", "All faults"])
    shares = b["share_margins"]["headline"][rule]
    for t in ("TRAFFIC_SPIKE", "LAMBDA_DEGRADATION", "DATABASE_THROTTLING", "CASCADING_FAILURE", "DEPENDENCY_FAILURE"):
        s = shares[t]
        assert in_order(texts, [mf.margin_text(s["difference"], s["ci95"]),
                                f"{mf.share_text(s['baseline'])} → {mf.share_text(s['ml'])}", mf.PRETTY[t]]), t
    h = shares["healthy"]
    assert in_order(texts, [mf.margin_text(h["difference"], h["ci95"]), "0.0% → 2.4%", "False alarms on"])
    weaker = b["margins"]["headline"]["model0_previous_window"]
    assert any(mf.margin_text(weaker["difference"]["recall"], weaker["ci95"]["recall"]) in t and "weaker" in t for t in texts)


def test_baseline_rows_run_from_the_largest_margin_and_end_on_the_price():
    rows = mf.margin_rows(mf.load())
    assert rows[0]["label"] == "All faults" and rows[-1]["kind"] == "price"
    gains = [r["diff"] for r in rows[1:-1]]
    assert gains == sorted(gains, reverse=True) and len(gains) == 5
    assert [r["label"] for r in rows[1:-1]][-2:] == ["Cascading failure", "Dependency failure"]


def test_baseline_title_names_the_largest_and_smallest_margins(out):
    assert ("The classifier's margin over Model 0 runs from +99 points on traffic spikes to 0 on dependency failure"
            in svg_texts(out, "fig4_baseline_margins"))


# --- figure 5 ------------------------------------------------------------------------------------------------

def test_cost_figure_labels_each_line_at_both_ends(out):
    cost = json.loads(mf.INPUTS["cost"].read_text())
    first, last = cost["rows"][0], cost["rows"][-1]
    texts = svg_texts(out, "fig5_cost_curve")
    assert f"Total bill  ${last['total']:,.0f}" in texts
    assert f"E-commerce app  ${last['app']:,.0f}" in texts
    assert f"AnomalyPulse  ${last['anomalypulse']:,.0f} ({mf.pct(last['anomalypulse_share'])})" in texts
    assert f"${first['anomalypulse']:,.0f} ({mf.pct(first['anomalypulse_share'])})" in texts
    assert f"${first['app']:,.0f}" in texts and f"${first['total']:,.0f}" in texts


def test_cost_title_gives_the_share_at_each_end(out):
    cost = json.loads(mf.INPUTS["cost"].read_text())
    first, last = cost["rows"][0], cost["rows"][-1]
    assert (f"Monitoring's share of the bill falls from {mf.pct(first['anomalypulse_share'])} at 1 request/s to "
            f"{mf.pct(last['anomalypulse_share'])} at 100 requests/s") in svg_texts(out, "fig5_cost_curve")


def test_log_ticks_and_label_spreading():
    assert mf.log_ticks(15.2, 2195) == [20, 50, 100, 200, 500, 1000, 2000]
    spread = mf.spread({"total": 1372.0, "app": 1085.0, "anomalypulse": 287.0}, ratio=1.3)
    assert spread["total"] == 1372.0 and spread["app"] == pytest.approx(1372 / 1.3) and spread["anomalypulse"] == 287.0


# --- figure 6 ------------------------------------------------------------------------------------------------

def test_latency_figure_shows_each_steps_median_and_the_chain_totals(out):
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    text = svg_text(out, "fig6_latency_breakdown")
    for key, *_ in mf.LATENCY_PARTS:
        assert f"{mf.rnd(steps[key]['median'])} s" in text, key
    assert f"End to end: {mf.rnd(steps['end_to_end']['median'])} s" in text
    assert f"within {mf.rnd(steps['end_to_end']['p90'])} s" in text
    assert "120 s" in text and "CloudWatch settles the minute" in text


def test_latency_bars_are_the_medians_laid_end_to_end():
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    segments = mf.latency_segments(steps)
    assert [k for k, _, _ in segments] == ["window", "cloudwatch_settle", "build_and_classify", "diagnosis"]
    x = 0.0
    for key, start, width in segments:
        assert start == pytest.approx(x) and width == steps[key]["median"]
        x += width


def test_detection_latency_is_the_window_plus_the_wait_per_run(out):
    det = mf.detection_latency(mf.load())
    steps = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]["steps_s"]
    assert det["n"] == steps["end_to_end"]["n"] == 284
    assert f"Detection: {mf.rnd(det['median'])} s" in svg_text(out, "fig6_latency_breakdown")


def test_detection_is_the_median_of_each_run_s_sum_not_the_sum_of_medians(tmp_path):
    # Windows 10, 50, 90 and waits 160, 180, 161: medians 50 + 161 = 211; per-run sums 170, 230, 251: median 230.
    rows = []
    for window, wait in ((10, 160), (50, 180), (90, 161)):
        steps = {s: 0.0 for s in latency.STEPS}
        steps.update(window=window, pipeline_wait=wait, cloudwatch_settle=120.0, build_and_classify=wait - 120.0,
                     diagnosis=30.0, end_to_end=window + wait + 30.0)
        rows.append({"phase": "test", "outcome": "detected", "labeled_type": "TRAFFIC_SPIKE", "held_back": False,
                     "tagged_to_another_run": 0, "false_incidents": 0, "steps": steps})
    data = {"latency": {"summary": {"test": latency.summarize(rows)}, "runs": rows}}
    mf.style()
    mf.latency_breakdown(data, tmp_path)
    assert "Detection: 230 s" in svg_texts(tmp_path, "fig6_latency_breakdown")


def test_latency_headline_gives_the_settle_s_share_of_the_mean(out):
    t = json.loads(mf.INPUTS["latency"].read_text())["summary"]["test"]
    title = next(x for x in svg_texts(out, "fig6_latency_breakdown") if x.startswith("Fault to stored diagnosis"))
    assert title == (f"Fault to stored diagnosis takes about 4 minutes, and {mf.pct(t['share_of_mean']['cloudwatch_settle'])} "
                     "of it is CloudWatch's 2-minute settle")
