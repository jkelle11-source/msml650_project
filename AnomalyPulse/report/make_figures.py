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

GOLD, RED = "#c98500", "#d03b3b"


def shell(ax, x0: float, y0: float, w: float, h: float, tag: str, glyph: str, color: str, shade: str = "white") -> None:
    """The rounded white card with a soft shadow and a small tag (coloured icon + grey label) at its top."""
    from matplotlib.patches import Circle, FancyBboxPatch

    for dx, dy in ((0.05, -0.07), (0.03, -0.04)):
        ax.add_patch(FancyBboxPatch((x0 + dx, y0 + dy), w, h, boxstyle="round,pad=0,rounding_size=0.22", fc="#00000012", ec="none", zorder=0))
    ax.add_patch(FancyBboxPatch((x0, y0), w, h, boxstyle="round,pad=0,rounding_size=0.22", fc=shade, ec="#e4e3df", lw=1, zorder=1))
    ax.add_patch(Circle((x0 + 0.5, y0 + h - 0.5), 0.17, fc=color, ec="none", zorder=2))
    ax.text(x0 + 0.5, y0 + h - 0.51, glyph, color="white", fontsize=11, fontweight="bold", ha="center", va="center", zorder=3)
    ax.text(x0 + 0.8, y0 + h - 0.5, tag, fontsize=12.5, color=SECOND, va="center", zorder=3)


def card(ax, x0: float, tag: str, glyph: str, color: str, lead: str, bold: str, share: float, verdict: str, shade: str = "white") -> None:
    """One card: a tag, a two-line question, a 10 x 10 grid (one dot per window out of 100), and the verdict.

    The dot at the end of the filled run is filled in proportion, so 99.9 of 100 and 2.4 of 100 are drawn exactly."""
    from matplotlib.patches import Circle, Rectangle

    w, y0, h = 3.9, 0.45, 6.1
    shell(ax, x0, y0, w, h, tag, glyph, color, shade)
    ax.text(x0 + 0.4, y0 + h - 1.0, lead, fontsize=15, va="top", zorder=3)
    ax.text(x0 + 0.4, y0 + h - 1.38, bold, fontsize=15, fontweight="bold", va="top", linespacing=1.15, zorder=3)

    filled = share * 100
    for k in range(100):
        r, c = divmod(k, 10)
        cx, cy, rad = x0 + 0.52 + c * 0.32, y0 + h - 2.45 - r * 0.285, 0.115
        ax.add_patch(Circle((cx, cy), rad, fc="white", ec=GRID, lw=1.6, zorder=2))
        frac = min(1.0, max(0.0, filled - k))
        if frac > 0:
            dot = Circle((cx, cy), rad, fc=color, ec=color, lw=1.6, zorder=3)
            ax.add_patch(dot)
            if frac < 1:
                clip = Rectangle((cx - rad - 0.02, cy - rad - 0.02), 2 * rad * frac + 0.02, 2 * rad + 0.04, transform=ax.transData)
                dot.set_clip_path(clip)
    ax.text(x0 + 0.4, y0 + 0.45, verdict, fontsize=14.5, fontweight="bold", color=color, va="center", zorder=3)


def detection(data: dict, out_dir: Path) -> list[Path]:
    det = data["m9_2"]["detection"]
    caught = det["deployed"]["headline"]["metrics"]["recall"]
    wrong = {k: det[k]["headline"]["metrics"]["fpr_healthy"] for k in ("iforest", "deployed")}
    fig = plt.figure(figsize=(13, 7.3))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 13), ax.set_ylim(0, 7.3), ax.axis("off")
    card(ax, 0.3, "FAULTS CAUGHT", "\u2713", BLUE, "Real faults:", "Caught by the\nlive detector", caught, f"{pct(caught, 1)} caught")
    card(ax, 4.55, "FOREST ALONE", "!", GOLD, "Healthy windows:", "Flagged by\nIsolation Forest alone", wrong["iforest"], f"{pct(wrong['iforest'])} flagged as faults", shade="#f4f3f0")
    card(ax, 8.8, "FALSE ALARMS", "\u00d7", RED, "Healthy windows:", "Wrongly flagged by the\nlive detector", wrong["deployed"], f"Only {pct(wrong['deployed'], 1)} wrongly flagged")
    ax.text(0.3, 7.0, f"The live detector catches {pct(caught, 1)} of faults and wrongly flags only {pct(wrong['deployed'], 1)} of healthy windows",
            fontsize=16, fontweight="bold", va="center")
    h = det["deployed"]["headline"]
    ax.text(0.3, 0.1, f"One dot = one window out of 100. Filled = flagged as a fault. Test set: {h['windows']:,} windows ({h['normal_windows']['healthy']} healthy).",
            fontsize=11.5, color=SECOND, va="center")
    return save(fig, FIGURES[1], out_dir)


# --------------------------------------------------------------------------------------------------------- 3

def per_band(data: dict, out_dir: Path) -> list[Path]:
    rows = [r for r in data["m9_2"]["per_band"]["rows"] if r["family"] == "classification" and r["model"] == "incident_type"]
    order = ["db-throttling", "lambda-degradation", "traffic-spike", "payment-failure"]
    names = {"db-throttling": "Database\nthrottling", "lambda-degradation": "Lambda\ndegradation", "traffic-spike": "Traffic\nspike", "payment-failure": "Dependency\nfailure"}
    roles = [("interpolation", "Same strength as in training"), ("mild extrapolation", "Slightly stronger than in training")]

    fig = plt.figure(figsize=(13, 5.9))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 13), ax.set_ylim(0, 5.9), ax.axis("off")
    w, gap, y0, h = 2.9, 0.28, 0.95, 4.4
    yt = y0 + h
    for k, inj in enumerate(order):
        x0 = 0.3 + k * (w + gap)
        mine = {role: next(r for r in rows if r["injector"] == inj and r["role"] == role) for role, _ in roles}
        bad = any(r["vs_validation"]["status"] == "degraded" for r in mine.values())
        color = RED if bad else BLUE
        shell(ax, x0, y0, w, h, "FLAGGED" if bad else "STEADY", "\u00d7" if bad else "\u2713", color)
        for line_no, line in enumerate(names[inj].split("\n")):
            ax.text(x0 + 0.4, yt - 0.95 - 0.29 * line_no, line, fontsize=13.5, fontweight="bold", va="center", zorder=3)
        for j, (role, label) in enumerate(roles):
            r = mine[role]
            top = yt - 1.9 - j * 1.05
            ax.text(x0 + 0.4, top, label, fontsize=10, color=SECOND, va="center", zorder=3)
            ax.text(x0 + 0.4, top - 0.4, pct(r["recall"]), fontsize=20, fontweight="bold", color=color, va="center", zorder=3)
        if bad:
            gaps = [100 * r["vs_validation"]["gap"] for r in mine.values()]
            ax.text(x0 + 0.4, y0 + 0.5, f"{min(gaps):.0f}\u2013{max(gaps):.0f} points below\nvalidation recall", fontsize=10.5, fontweight="bold", color=RED, va="center", zorder=3)
    ax.text(0.3, 5.6, "Fault intensity barely changes accuracy, except for dependency failures", fontsize=14.5, fontweight="bold", va="center")
    ax.text(0.3, 0.5, "Percentages: the share of each fault's windows the classifier names correctly.", fontsize=10, color=SECOND, va="center")
    ax.text(0.3, 0.22, "The second row tests whether the model generalizes: slightly stronger faults than the model was trained on.", fontsize=10, color=SECOND, va="center")
    return save(fig, FIGURES[2], out_dir)


# --------------------------------------------------------------------------------------------------------- 4

def bar_row(ax, bx: float, bw: float, top: float, label: str, value: float, color: str, text: str) -> None:
    from matplotlib.patches import Rectangle

    ax.text(bx, top, label, fontsize=10, color=SECOND, va="center", zorder=3)
    ax.text(bx, top - 0.36, text, fontsize=18, fontweight="bold", color=color, va="center", zorder=3)
    ax.add_patch(Rectangle((bx, top - 0.78), bw, 0.12, fc=GRID, ec="none", zorder=2))
    ax.add_patch(Rectangle((bx, top - 0.78), bw * value, 0.12, fc=color, ec="none", zorder=3))


def baseline_margins(data: dict, out_dir: Path) -> list[Path]:
    b = data["m9_3"]
    det = b["detection"]["headline"]
    rule, ml = det["model0_earlier_mean_3"]["metrics"], det["classifier_as_detector"]["metrics"]
    gain = b["margins"]["headline"]["model0_earlier_mean_3"]["difference"]["recall"]
    gain_prev = b["margins"]["headline"]["model0_previous_window"]["difference"]["recall"]
    cost = b["share_margins"]["headline"]["model0_earlier_mean_3"]["healthy"]["difference"]

    fig = plt.figure(figsize=(13, 6.4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 13), ax.set_ylim(0, 6.4), ax.axis("off")
    w, y0, h = 3.9, 1.15, 4.6
    yt = y0 + h
    for x0, tag, glyph, color, heading, key, digits in (
        (0.3, "FAULTS CAUGHT", "\u2713", BLUE, "Real faults flagged", "recall", 1),
        (4.55, "FALSE ALARMS", "\u00d7", RED, "Healthy windows\nwrongly flagged", "fpr_healthy", 1),
    ):
        shell(ax, x0, y0, w, h, tag, glyph, color)
        for i, line in enumerate(heading.split("\n")):
            ax.text(x0 + 0.4, yt - 1.05 - 0.3 * i, line, fontsize=13.5, fontweight="bold", va="center", zorder=3)
        for j, (label, m, c) in enumerate((("Simple rule", rule, ORANGE), ("Classifier", ml, BLUE))):
            bar_row(ax, x0 + 0.4, w - 0.8, yt - 2.0 - j * 1.25, label, m[key], c, pct(m[key], digits))
    # The gain, in one number.
    x0 = 8.8
    shell(ax, x0, y0, w, h, "THE GAIN", "\u2713", BLUE, shade="#f4f3f0")
    ax.text(x0 + 0.4, yt - 1.05, "More faults caught", fontsize=13.5, fontweight="bold", va="center", zorder=3)
    ax.text(x0 + 0.4, yt - 1.95, f"+{100 * gain:.0f} points", fontsize=30, fontweight="bold", color=BLUE, va="center", zorder=3)
    ax.text(x0 + 0.4, yt - 2.6, "than the simple rule", fontsize=11, color=SECOND, va="center", zorder=3)
    ax.text(x0 + 0.4, yt - 3.25, f"+{100 * gain_prev:.0f} points against the\nweaker version of the rule", fontsize=10.5, color=SECOND, va="center", zorder=3)
    ax.text(x0 + 0.4, y0 + 0.55, f"The price: {100 * cost:.1f} points more\nfalse alarms on healthy traffic", fontsize=10.5, fontweight="bold", color=RED, va="center", zorder=3)
    ax.text(0.3, 6.05, f"The classifier catches {pct(ml['recall'], 1)} of faults, the simple rule only {pct(rule['recall'])}", fontsize=14.5, fontweight="bold", va="center")
    ax.text(0.3, 0.8, "Simple rule: flags a window when any service's p95 latency is above 2x its normal level or its 5xx share is above 5%, compared with the mean of the 3 earlier windows.", fontsize=10, color=SECOND, va="center")
    ax.text(0.3, 0.52, "The weaker version of the rule compares with the previous window only. Differences are paired by run.", fontsize=10, color=SECOND, va="center")
    p = b["provenance"]["counts"]
    ax.text(0.3, 0.2, f"Test set, headline runs ({p['headline_runs']} runs, {p['headline_windows']:,} windows).", fontsize=10, color=SECOND, va="center")
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
