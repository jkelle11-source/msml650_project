"""M9-6: the report figures, drawn only from the result files.

    python3 -m report.make_figures            # from AnomalyPulse/

Reads (never writes) the M9-2 / M9-3 / M9-1 / M9-6 result files, and writes each figure as PNG and SVG to
Docs/images/m9-6/. Nothing is typed in by hand: every number on a figure comes from a result file, so a rerun
after a new evaluation redraws the whole set.

Style: plain report style (white, large type, high contrast) so the figures read on a slide projector and in the
written report. Colours are the validated blue / orange / aqua from the dataviz palette; every bar carries its
value as a label, so no meaning rests on colour alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent

INPUTS = {
    "m9_2": ROOT / "ml/m9_2_results/metrics_m9_2.json",
    "m9_3": ROOT / "ml/m9_3_results/baseline_m9_3.json",
    "cost": ROOT / "cost/results/projections.json",
    "latency": ROOT / "report/results/latency_m9_6.json",
}
OUT_DIR = REPO / "Docs/images/m9-6"

FIGURES = (
    "fig1_confusion_matrix",
    "fig2_detection",
    "fig3_per_band",
    "fig4_baseline_margins",
    "fig5_cost_curve",
    "fig6_latency_breakdown",
)

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, SECOND, GRID, MUTED = "#0b0b0b", "#52514e", "#dcdbd7", "#8a8985"

PRETTY = {
    "CASCADING_FAILURE": "Cascading failure",
    "DATABASE_THROTTLING": "Database throttling",
    "DEPENDENCY_FAILURE": "Dependency failure",
    "LAMBDA_DEGRADATION": "Lambda degradation",
    "NORMAL": "Normal",
    "TRAFFIC_SPIKE": "Traffic spike",
    "db-throttling": "Database throttling",
    "lambda-degradation": "Lambda degradation",
    "payment-failure": "Dependency failure\n(payment failure)",
    "traffic-spike": "Traffic spike",
}


def load() -> dict:
    return {name: json.loads(path.read_text()) for name, path in INPUTS.items()}


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "font.family": "DejaVu Sans",
            "font.size": 13,
            "axes.titlesize": 15,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.labelsize": 13,
            "axes.edgecolor": SECOND,
            "axes.labelcolor": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "text.color": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": False,
            "legend.frameon": False,
            "svg.fonttype": "none",
        }
    )


def save(fig, name: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "svg"):
        path = out_dir / f"{name}.{ext}"
        fig.savefig(path, dpi=200, bbox_inches="tight")
        paths.append(path)
    plt.close(fig)
    return paths


def rnd(x: float) -> int:
    """Round half up (Python rounds 74.5 to 74)."""
    return int(x + 0.5)


def pct(x: float, digits: int = 0) -> str:
    return f"{100 * x:.{digits}f}%"


def footnote(fig, text: str, y: float = -0.02) -> None:
    fig.text(0.01, y, text, ha="left", va="top", fontsize=10.5, color=SECOND, wrap=True)


# --------------------------------------------------------------------------------------------------------- 1

def header(fig, title: str) -> None:
    """A title that states the finding. Nothing else: each figure has to explain itself by its labels."""
    fig.text(0.01, 1.0, title, fontsize=17, fontweight="bold", ha="left", va="bottom")


def confusion_matrix(data: dict, out_dir: Path) -> list[Path]:
    cls = data["m9_2"]["classification"]["incident_type"]["headline"]
    classes, matrix = cls["classes"], cls["confusion"]
    n = len(classes)
    share = [[matrix[i][j] / sum(matrix[i]) for j in range(n)] for i in range(n)]
    s, i, j = max((share[i][j], i, j) for i in range(n) for j in range(n) if i != j)

    cmap = LinearSegmentedColormap.from_list("blue", ["#ffffff", "#9ec5f4", "#256abf", "#0d366b"])
    fig, ax = plt.subplots(figsize=(11, 6.6))
    ax.imshow(share, cmap=cmap, vmin=0, vmax=1)
    for a_ in range(n):
        for b_ in range(n):
            v = share[a_][b_]
            if v < 0.01:
                continue
            ax.text(b_, a_, pct(v), ha="center", va="center", fontsize=14,
                    color="white" if v > 0.4 else INK, fontweight="bold" if a_ == b_ else "normal")
    ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False, edgecolor=ORANGE, lw=4))
    ax.set_xticks(range(n), [PRETTY[c].replace(" ", "\n", 1) if " " in PRETTY[c] else PRETTY[c] for c in classes], fontsize=10.5, va="top")
    ax.set_yticks(range(n), [PRETTY[c] for c in classes], fontsize=12)
    ax.set_xlabel("What the classifier said", labelpad=10)
    ax.set_ylabel("What really happened")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.tick_params(length=0)
    header(
        fig,
        f"{PRETTY[classes[i]]} is mistaken for {PRETTY[classes[j]].lower()} {pct(s)} of the time",
    )
    footnote(fig, f"Test set: {cls['windows']:,} windows. Overall accuracy {pct(cls['accuracy'])}.", y=-0.06)
    return save(fig, FIGURES[0], out_dir)


# --------------------------------------------------------------------------------------------------------- 2

def detection(data: dict, out_dir: Path) -> list[Path]:
    det = data["m9_2"]["detection"]
    ci_deployed = data["m9_3"]["share_margins"]["headline"]["model0_previous_window"]["healthy"]["ci95"]
    caught = {k: det[k]["headline"]["metrics"]["recall"] for k in ("deployed", "binary")}
    wrong = {k: det[k]["headline"]["metrics"]["fpr_healthy"] for k in ("iforest", "binary", "deployed")}
    fig, (a, b) = plt.subplots(1, 2, figsize=(11, 5.2), gridspec_kw={"width_ratios": [1, 1.25], "wspace": 0.35})

    for x, (key, label, color) in enumerate([("deployed", "Deployed\ndetector", BLUE), ("binary", "Binary\ndetector", ORANGE)]):
        a.bar(x, caught[key], 0.6, color=color)
        a.text(x, caught[key] + 0.03, pct(caught[key], 1), ha="center", fontsize=14, fontweight="bold")
    a.set_xticks([0, 1], ["Deployed\ndetector", "Binary\ndetector"])
    a.set_ylim(0, 1.15)
    a.set_yticks([0, 0.5, 1.0], ["0%", "50%", "100%"])
    a.set_title("Faults caught", loc="left", fontsize=14)

    for x, (key, color) in enumerate([("iforest", MUTED), ("binary", ORANGE), ("deployed", BLUE)]):
        v = wrong[key]
        b.bar(x, v, 0.6, color=color, hatch="//" if key == "iforest" else None, edgecolor="white" if key == "iforest" else color)
        b.text(x, v + 0.03, pct(v, 1), ha="center", fontsize=14, fontweight="bold")
    b.set_xticks(range(3), ["Isolation\nForest", "Binary\ndetector", "Deployed\ndetector"])
    b.set_ylim(0, 1.15)
    b.set_yticks([0, 0.5, 1.0], ["0%", "50%", "100%"])
    b.set_title("Healthy windows wrongly flagged", loc="left", fontsize=14)
    b.text(0.5, 0.55, "Flags everything, so it\ndetects nothing", ha="left", fontsize=12, color=SECOND)

    header(fig, f"The deployed detector catches {pct(caught['deployed'], 1)} of faults and wrongly flags {pct(wrong['deployed'], 1)} of healthy windows")
    h = det["deployed"]["headline"]
    footnote(fig, f"Test set: {h['windows']:,} windows ({h['normal_windows']['healthy']} healthy).", y=-0.02)
    return save(fig, FIGURES[1], out_dir)


# --------------------------------------------------------------------------------------------------------- 3

def per_band(data: dict, out_dir: Path) -> list[Path]:
    rows = [r for r in data["m9_2"]["per_band"]["rows"] if r["family"] == "classification" and r["model"] == "incident_type"]
    injectors = []
    for r in rows:
        if r["injector"] not in injectors:
            injectors.append(r["injector"])
    roles = [("interpolation", BLUE, "Inside the trained range (interpolation)"), ("mild extrapolation", ORANGE, "Just beyond it (mild extrapolation)")]

    def fmt(x: float) -> str:
        return f"{x:,.4g}" if x < 100 else f"{x:,.0f}"

    fig, ax = plt.subplots(figsize=(13, 6.2))
    width = 0.36
    gaps: dict[str, list[float]] = {}
    for k, (role, color, label) in enumerate(roles):
        xs, vals, errs, refs, ticks = [], [], [[], []], [], {}
        for g, inj in enumerate(injectors):
            r = next(x for x in rows if x["injector"] == inj and x["role"] == role)
            x = g + (k - 0.5) * width
            xs.append(x), vals.append(r["recall"])
            errs[0].append(r["recall"] - r["ci95"][0]), errs[1].append(r["ci95"][1] - r["recall"])
            refs.append(r["vs_validation"]["ref_recall"])
            ticks[x] = f"{fmt(r['band'][0])}–{fmt(r['band'][1])}"
            ax.text(x, r["recall"] + 0.03 if r["recall"] > 0.9 else r["ci95"][1] + 0.03, pct(r["recall"], 1), ha="center", fontsize=12, fontweight="bold")
            lo_hi = ticks[x].split("–")
            ax.text(x, 0.02, ticks[x] if len(ticks[x]) <= 9 else f"{lo_hi[0]}–\n{lo_hi[1]}", ha="center", va="bottom", fontsize=10, color="white" if r["recall"] > 0.12 else INK)
            if r["vs_validation"]["status"] == "degraded":
                gaps.setdefault(inj, []).append(100 * r["vs_validation"]["gap"])
        ax.bar(xs, vals, width * 0.92, color=color, label=label, yerr=errs, error_kw={"ecolor": INK, "capsize": 4, "lw": 1.4})
        ax.plot(xs, refs, linestyle="none", marker="_", markersize=30, markeredgewidth=2.6, color=INK, label="Recall on the validation set" if k == 0 else None)
    for g, inj in enumerate(injectors):
        if inj in gaps:
            lo, hi = min(gaps[inj]), max(gaps[inj])
            span = f"{lo:.0f}" if round(lo) == round(hi) else f"{lo:.0f}–{hi:.0f}"
            ax.text(g, 0.83, f"{span} points below\nvalidation recall", ha="center", fontsize=11.5, fontweight="bold")
    ax.set_xticks(range(len(injectors)), [PRETTY[i] for i in injectors])
    ax.set_ylim(0, 1.3)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"])
    ax.set_ylabel("Recall of the incident-type classifier")
    ax.set_title("Fault intensity barely matters, except for dependency failures")
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.12), ncol=3, fontsize=11)
    footnote(
        fig,
        "Test set, standard-length (300 s) single-fault runs; recall on the fault's windows. Numbers inside bars are the injected intensity band "
        "for that fault.\nError bars: 95% CI, resampled by run. A band is flagged when recall is more than "
        "10 points below validation.",
        y=-0.13,
    )
    return save(fig, FIGURES[2], out_dir)


# --------------------------------------------------------------------------------------------------------- 4

def baseline_margins(data: dict, out_dir: Path) -> list[Path]:
    b = data["m9_3"]
    base = [("model0_previous_window", "Rule vs previous window", ORANGE), ("model0_earlier_mean_3", "Rule vs mean of 3 earlier windows\n(the stronger rule)", MUTED)]
    metrics = [("recall", "Recall"), ("precision", "Precision"), ("f1", "F1")]
    fig, (a, c) = plt.subplots(1, 2, figsize=(13.5, 5.6), gridspec_kw={"width_ratios": [1.6, 1]})

    for k, (key, label, color) in enumerate(base):
        ys = [i + (0.17 if k == 0 else -0.17) for i in range(3)][::-1]
        for y, (m, _) in zip(ys, metrics):
            d, ci = b["margins"]["headline"][key]["difference"][m], b["margins"]["headline"][key]["ci95"][m]
            a.errorbar(100 * d, y, xerr=[[100 * (d - ci[0])], [100 * (ci[1] - d)]], fmt="o", color=color, ecolor=color, elinewidth=2.4, capsize=5, markersize=9,
                       label=label if m == "recall" else None)
            a.text(100 * ci[1] + 1.5, y, f"+{100 * d:.0f} pts", va="center", fontsize=12, fontweight="bold")
    a.axvline(0, color=INK, lw=1.4)
    a.set_yticks(range(3), [m[1] for m in metrics][::-1])
    a.set_xlim(-5, 85)
    a.set_xlabel("Classifier minus rule, percentage points (paired by run, 95% CI)")
    a.set_title("The classifier catches far more faults than the rule")
    a.legend(loc="center right", fontsize=11.5)

    det = b["detection"]["headline"]
    share = b["share_margins"]["headline"]["model0_previous_window"]["healthy"]
    bars = [
        ("Rule:\nprevious\nwindow", det["model0_previous_window"]["metrics"]["fpr_healthy"], None, ORANGE),
        ("Rule:\nearlier\nmean", det["model0_earlier_mean_3"]["metrics"]["fpr_healthy"], None, MUTED),
        ("Live\nclassifier", det["classifier_as_detector"]["metrics"]["fpr_healthy"], share["ci95"], BLUE),
    ]
    for x, (label, v, ci, color) in enumerate(bars):
        c.bar(x, v, 0.6, color=color, yerr=None if ci is None else [[v - ci[0]], [ci[1] - v]], error_kw={"ecolor": INK, "capsize": 4, "lw": 1.4})
        c.text(x, (ci[1] if ci else v) + 0.004, pct(v, 1), ha="center", fontsize=12.5, fontweight="bold")
    c.set_xticks(range(3), [x[0] for x in bars])
    c.set_ylim(0, 0.06)
    c.set_yticks([0, 0.02, 0.04, 0.06], ["0%", "2%", "4%", "6%"])
    c.set_ylabel("Healthy windows flagged")
    c.set_title("The price: a few false alarms")
    p = b["provenance"]["counts"]
    footnote(
        fig,
        f"Test set, headline runs ({p['headline_runs']} runs, {p['headline_windows']} windows). The rule (Model 0) flags a window when any service's p95 is above 2x its baseline or its 5xx share above 5%.\n"
        "Margins are paired: both detectors are scored on the same resampled runs. Healthy windows = windows with no fault active.",
        y=-0.1,
    )
    return save(fig, FIGURES[3], out_dir)


# --------------------------------------------------------------------------------------------------------- 5

def cost_curve(data: dict, out_dir: Path) -> list[Path]:
    rows = data["cost"]["rows"]
    rps = [r["rps"] for r in rows]
    fig, ax = plt.subplots(figsize=(11, 6.2))
    ax.plot(rps, [r["app"] for r in rows], "-o", color=ORANGE, lw=3, markersize=8)
    ax.plot(rps, [r["anomalypulse"] for r in rows], "-o", color=BLUE, lw=3, markersize=8)
    ax.set_xscale("log"), ax.set_yscale("log")
    ax.set_xticks(rps, [str(r) for r in rps])
    ax.minorticks_off()
    ax.set_yticks([10, 30, 100, 300, 1000], ["$10", "$30", "$100", "$300", "$1,000"])
    ax.set_xlabel("Request rate of the monitored store (requests per second)")
    ax.set_ylabel("Monthly AWS cost, list price")
    ax.grid(axis="y", color=GRID, lw=0.9)
    ax.set_axisbelow(True)
    last = rows[-1]
    ax.text(rps[-1] * 1.12, last["app"], f"The store\n${last['app']:,.0f}", color=ORANGE, va="center", fontsize=12.5, fontweight="bold")
    ax.text(rps[-1] * 1.12, last["anomalypulse"], f"AnomalyPulse\n${last['anomalypulse']:,.0f}", color=BLUE, va="center", fontsize=12.5, fontweight="bold")
    ax.set_xlim(rps[0] * 0.8, rps[-1] * 3.2)
    first = rows[0]
    ax.set_title(f"Monitoring is {pct(first['anomalypulse_share'])} of the bill at 1 request/s and {pct(last['anomalypulse_share'])} at {last['rps']} requests/s")
    footnote(
        fig,
        f"Projection from M9-1's measured unit costs, {data['cost']['incidents_per_month']} incidents a month, {data['cost']['hours_per_month']} hours a month. "
        "Both axes logarithmic. List price: the always-free allowance is not subtracted.\nOrange: the monitored store alone. Blue: what AnomalyPulse adds. The cost has a fixed part, "
        "so it grows more slowly than traffic.",
    )
    return save(fig, FIGURES[4], out_dir)


# --------------------------------------------------------------------------------------------------------- 6

def latency_breakdown(data: dict, out_dir: Path) -> list[Path]:
    t = data["latency"]["summary"]["test"]
    steps = t["steps_s"]
    parts = [("window", "Window\n(1-minute bucket)", BLUE), ("pipeline_wait", "CloudWatch settle wait", ORANGE), ("diagnosis", "Diagnosis\n(Bedrock + store)", AQUA)]
    share = t["share_of_mean"]
    fig, (a, c) = plt.subplots(2, 1, figsize=(12.5, 7.8), gridspec_kw={"height_ratios": [1, 1.5], "hspace": 0.55})

    left = 0
    for key, label, color in parts:
        w = steps[key]["mean"]
        a.barh(0, w, left=left, color=color, edgecolor="white", linewidth=2.5, height=0.7)
        a.text(left + w / 2, 0, f"{pct(share[key])}\n{w:.0f} s", ha="center", va="center", color="white" if key != "diagnosis" else INK, fontsize=13, fontweight="bold")
        a.text(left + w / 2, 0.55, label, ha="center", va="bottom", fontsize=11.5)
        left += w
    a.set_xlim(0, left * 1.01)
    a.set_ylim(-0.5, 1.2)
    a.set_yticks([])
    a.spines["left"].set_visible(False)
    a.set_xlabel("Seconds from fault start to diagnosis stored (mean of the timed runs)")
    big = max(parts, key=lambda p: share[p[0]])
    a.set_title(f"The CloudWatch settle wait is {pct(share[big[0]])} of the time from fault to diagnosis")

    rows = [(k, lbl.replace("\n", " "), col) for k, lbl, col in parts] + [("end_to_end", "End to end", INK)]
    for y, (key, label, color) in enumerate(rows[::-1]):
        s = steps[key]
        c.plot([s["min"], s["max"]], [y, y], color=GRID, lw=6, solid_capstyle="round", zorder=1)
        c.plot([s["median"], s["p90"]], [y, y], color=color, lw=2.5, zorder=2)
        c.plot(s["median"], y, "o", color=color, markersize=13, zorder=3)
        c.plot(s["p90"], y, "D", color=color, markersize=10, zorder=3)
        c.text(s["max"] + 6, y, f"median {rnd(s['median'])} s    90th percentile {rnd(s['p90'])} s", va="center", fontsize=11.5)
    c.set_yticks(range(len(rows)), [r[1] for r in rows][::-1])
    c.set_xlim(0, 480)
    c.set_xlabel("Seconds (grey bar: fastest to slowest run;  circle: median;  diamond: 90th percentile)")
    c.set_title("Each step's spread")
    n = steps["end_to_end"]["n"]
    st = data["latency"]["summary"]["stress"]["steps_s"]["end_to_end"]
    footnote(
        fig,
        f"Test set: {n} timed detections of {t['fault_runs']} fault runs ({len(t['early'])} opened before the fault's recorded start and are not timed; {len(t['missed'])} were missed). "
        f"Runbooks off.\nClock starts at the fault's real start from the experiment manifest. Stress slice (n={st['n']}): median end to end {st['median']:.0f} s. "
        "The 15-second-window experiment was not run.",
    )
    return save(fig, FIGURES[5], out_dir)


# ------------------------------------------------------------------------------------------------------- main

def main(out_dir: Path = OUT_DIR) -> list[Path]:
    style()
    data = load()
    paths: list[Path] = []
    for draw in (confusion_matrix, detection, per_band, baseline_margins, cost_curve, latency_breakdown):
        paths += draw(data, out_dir)
    return paths


if __name__ == "__main__":
    for p in main():
        print(p.relative_to(REPO))
