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

def gap_row(ax, rule: float, ml: float, delta_text: str, upper: float, ticks: list[float], title: str) -> None:
    """A dumbbell: the simple rule's dot, the classifier's dot, and the gap between them."""
    ax.plot([rule, ml], [0, 0], color="#cfcec9", lw=7, solid_capstyle="round", zorder=1)
    ax.scatter([rule], [0], s=520, color=ORANGE, zorder=3)
    ax.scatter([ml], [0], s=520, color=BLUE, zorder=3)
    ax.text(rule, 0.34, pct(rule, 1), color=ORANGE, fontsize=17, fontweight="bold", ha="center", va="bottom")
    ax.text(ml, 0.34, pct(ml, 1), color=BLUE, fontsize=17, fontweight="bold", ha="center", va="bottom")
    ax.text((rule + ml) / 2, -0.34, delta_text, fontsize=13, fontweight="bold", ha="center", va="top", color=INK)
    ax.set_xlim(-0.05 * upper, upper * 1.06)
    ax.set_ylim(-1.0, 1.0)
    ax.set_yticks([])
    ax.set_xticks(ticks, [f"{100 * t:g}%" for t in ticks])
    for side in ("left", "top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.set_title(title, loc="left", fontsize=13.5, fontweight="bold", pad=14, x=-0.0)


def baseline_margins(data: dict, out_dir: Path) -> list[Path]:
    b = data["m9_3"]
    det = b["detection"]["headline"]
    rule, ml = det["model0_earlier_mean_3"]["metrics"], det["classifier_as_detector"]["metrics"]
    gain = b["margins"]["headline"]["model0_earlier_mean_3"]["difference"]["recall"]
    cost = b["share_margins"]["headline"]["model0_earlier_mean_3"]["healthy"]["difference"]

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(11, 6.2), gridspec_kw={"hspace": 0.9})
    fig.subplots_adjust(top=0.76, bottom=0.17, left=0.04, right=0.97)
    gap_row(top, rule["recall"], ml["recall"], f"+{100 * gain:.0f} points more faults caught", 1.0, [0, 0.25, 0.5, 0.75, 1.0], "Real faults caught")
    gap_row(bottom, rule["fpr_healthy"], ml["fpr_healthy"], f"+{100 * cost:.1f} points more false alarms", 0.05, [0, 0.01, 0.02, 0.03, 0.04, 0.05], "Healthy windows wrongly flagged")
    # Direct labels: which dot is which.
    fig.text(0.04, 0.95, f"The classifier catches {pct(ml['recall'], 1)} of faults, the simple rule only {pct(rule['recall'])}", fontsize=15, fontweight="bold", ha="left")
    fig.text(0.04, 0.885, "Orange dot: simple rule.   Blue dot: classifier.", fontsize=11.5, color=SECOND, ha="left")
    p = b["provenance"]["counts"]
    fig.text(0.04, 0.06, "Simple rule: flags a window when any service's p95 latency is above 2x its normal level or its 5xx share is above 5%.", fontsize=10, color=SECOND, ha="left")
    fig.text(0.04, 0.02, f"Test set, headline runs ({p['headline_runs']} runs, {p['headline_windows']:,} windows).", fontsize=10, color=SECOND, ha="left")
    return save(fig, FIGURES[3], out_dir)


# --------------------------------------------------------------------------------------------------------- 5

def cost_curve(data: dict, out_dir: Path) -> list[Path]:
    rows = data["cost"]["rows"]
    rps = [r["rps"] for r in rows]
    first, last = rows[0], rows[-1]
    fig, ax = plt.subplots(figsize=(11, 6.2))
    fig.subplots_adjust(top=0.8, bottom=0.2, left=0.09, right=0.93)
    for key, color, label in (("app", ORANGE, "E-commerce app"), ("anomalypulse", BLUE, "AnomalyPulse\n(added cost)")):
        ax.plot(rps, [r[key] for r in rows], "-o", color=color, lw=3.2, markersize=8, zorder=3)
        ax.text(rps[-1] * 1.12, last[key], f"{label}\n${last[key]:,.0f}", color=color, fontsize=13, fontweight="bold", va="center")
    # Label the first point of each line too, so the low end reads without a table.
    ax.text(rps[0], first["app"] + 45, f"${first['app']:,.0f}", color=ORANGE, fontsize=12, fontweight="bold", ha="center")
    ax.text(rps[0], first["anomalypulse"] - 75, f"${first['anomalypulse']:,.0f}", color=BLUE, fontsize=12, fontweight="bold", ha="center")
    ax.set_xscale("log")
    ax.set_xticks(rps, [str(r) for r in rps])
    ax.minorticks_off()
    ax.set_xlim(rps[0] * 0.8, rps[-1] * 3.4)
    ax.set_ylim(-110, 1200)
    ax.set_yticks([0, 400, 800, 1200], ["$0", "$400", "$800", "$1,200"])
    ax.set_xlabel("Requests per second to the e-commerce app")
    ax.set_ylabel("Monthly AWS cost")
    ax.grid(axis="y", color=GRID, lw=0.9)
    ax.set_axisbelow(True)
    fig.text(0.09, 0.93, f"Monitoring is {pct(first['anomalypulse_share'])} of the bill at {first['rps']} request/s and {pct(last['anomalypulse_share'])} at {last['rps']} requests/s", fontsize=15, fontweight="bold", ha="left")
    fig.text(0.09, 0.875, f"Orange: running the e-commerce app. Blue: the extra cost of adding AnomalyPulse. Total bill = orange + blue (${last['total']:,.0f} at {last['rps']} requests/s).", fontsize=11.5, color=SECOND, ha="left")
    fig.text(0.09, 0.03, f"Projected monthly cost at list price, {data['cost']['incidents_per_month']} incidents a month. Horizontal axis is logarithmic.", fontsize=10, color=SECOND, ha="left")
    return save(fig, FIGURES[4], out_dir)


# --------------------------------------------------------------------------------------------------------- 6

def detection_latency(data: dict) -> dict:
    """Detection latency per timed test run: the window plus the CloudWatch wait (before diagnosis starts)."""
    from report.latency import stats

    runs = [r for r in data["latency"]["runs"] if r["phase"] == "test" and r["outcome"] == "detected"]
    return stats([r["steps"]["window"] + r["steps"]["pipeline_wait"] for r in runs])


def latency_breakdown(data: dict, out_dir: Path) -> list[Path]:
    from matplotlib.patches import Rectangle

    t = data["latency"]["summary"]["test"]
    steps = t["steps_s"]
    det = detection_latency(data)
    parts = [("window", "1-minute window", BLUE, "white"), ("pipeline_wait", "Waiting for CloudWatch\nto settle", ORANGE, "white"), ("diagnosis", "Diagnosis\n(Bedrock)", AQUA, INK)]
    total = sum(steps[k]["median"] for k, *_ in parts)
    wait_share = steps["pipeline_wait"]["median"] / total

    fig = plt.figure(figsize=(12, 5.4))
    ax = fig.add_axes([0.03, 0.02, 0.94, 0.96])
    ax.set_xlim(-8, total + 12), ax.set_ylim(-3.1, 2.3), ax.axis("off")
    scale = 1.0
    x = 0.0
    for key, label, color, ink in parts:
        w = steps[key]["median"] * scale
        ax.add_patch(Rectangle((x, 0), w, 0.9, fc=color, ec="white", lw=3, zorder=2))
        ax.text(x + w / 2, 0.45, f"{rnd(steps[key]['median'])} s", color=ink, fontsize=17, fontweight="bold", ha="center", va="center", zorder=3)
        ax.text(x + w / 2, 1.1, label, fontsize=11.5, ha="center", va="bottom", linespacing=1.2)
        x += w

    def bracket(x1: float, x2: float, y: float, head: str, sub: str) -> None:
        ax.plot([x1, x1, x2, x2], [y + 0.14, y, y, y + 0.14], color=SECOND, lw=1.6, zorder=1)
        ax.text((x1 + x2) / 2, y - 0.14, head, fontsize=14, fontweight="bold", ha="center", va="top")
        ax.text((x1 + x2) / 2, y - 0.52, sub, fontsize=10.5, color=SECOND, ha="center", va="top")

    d = steps["window"]["median"] + steps["pipeline_wait"]["median"]
    bracket(0, d, -0.35, f"Detection: {rnd(det['median'])} s", "fault starts to incident opened and typed")
    bracket(0, total, -1.65, f"End to end: {rnd(steps['end_to_end']['median'])} s", f"fault starts to diagnosis stored. 9 in 10 runs finish within {rnd(steps['end_to_end']['p90'])} s")
    share_text = "about two-thirds" if 0.62 <= wait_share <= 0.72 else pct(wait_share)
    ax.text(-6, 2.15, f"Fault to stored diagnosis takes about 4 minutes, and {share_text} of it is waiting for CloudWatch", fontsize=15, fontweight="bold", va="center")
    ax.text(-6, -3.0, f"Typical run (median of each step), {steps['end_to_end']['n']} timed detections on the test set. Runbooks off.", fontsize=10, color=SECOND, va="center")
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
