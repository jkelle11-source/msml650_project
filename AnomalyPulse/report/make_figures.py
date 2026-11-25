"""M9-6: the report figures, drawn only from the result files.

    python3 -m report.make_figures            # from AnomalyPulse/

Reads (never writes) the M9-2 / M9-3 / M9-1 / M9-6 result files, and writes each figure as PNG and SVG to
Docs/images/m9-6/. Nothing is typed in by hand: every number on a figure comes from a result file, so a rerun
after a new evaluation redraws the whole set. Drawn with matplotlib 3.11.2 (report/requirements.txt); another
version can move a label by a pixel.

Style: plain report style (white, large type, high contrast) so the figures read on a slide projector and in the
written report. Colours are the validated categorical slots from the dataviz palette, and keep one meaning across
the set: blue is AnomalyPulse, red a problem (false alarms, the degraded class, its largest confusion), and orange
only CloudWatch's settle in figure 6. Every mark carries its value as a label, so no meaning rests on colour alone.
"""

from __future__ import annotations

import json
import math
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

BLUE, ORANGE, AQUA, VIOLET, GOLD, RED = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#c98500", "#d03b3b"
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
WORDS = {1: "One", 2: "Two", 3: "Three", 4: "All four"}


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
            "svg.hashsalt": "m9-6",     # fixed element ids, so a rerun writes the same SVG
        }
    )


def save(fig, name: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in ("png", "svg"):
        path = out_dir / f"{name}.{ext}"
        # No timestamp in the SVG, so a rerun on the same inputs writes the same bytes.
        fig.savefig(path, dpi=200, bbox_inches="tight", metadata={"Date": None} if ext == "svg" else None)
        paths.append(path)
    plt.close(fig)
    return paths


def rnd(x: float) -> int:
    """Round half up (Python rounds 74.5 to 74)."""
    return int(math.floor(x + 0.5))


def pct(x: float, digits: int = 0) -> str:
    return f"{100 * x:.{digits}f}%"


def footnote(fig, text: str, y: float = -0.02) -> None:
    fig.text(0.01, y, text, ha="left", va="top", fontsize=10.5, color=SECOND, wrap=True)


# --------------------------------------------------------------------------------------------------------- 1

def header(fig, title: str) -> None:
    """A title that states the finding. Nothing else: each figure has to explain itself by its labels."""
    fig.text(0.01, 1.0, title, fontsize=17, fontweight="bold", ha="left", va="bottom")


def cell_text(v: float) -> str:
    """A cell's label: blank for none, "<1%" for a few windows, else the rounded share."""
    if v == 0:
        return ""
    return "<1%" if v < 0.005 else pct(v)


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
            ax.text(b_, a_, cell_text(v), ha="center", va="center", fontsize=14 if v >= 0.005 else 11.5,
                    color="white" if v > 0.6 else (INK if v >= 0.005 else SECOND),
                    fontweight="bold" if a_ == b_ or (a_, b_) == (i, j) else "normal")
    ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, fill=False, edgecolor=RED, lw=4))
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
    few = sorted({matrix[a_][b_] for a_ in range(n) for b_ in range(n) if 0 < share[a_][b_] < 0.005})
    few_text = (f" <1%: {few[0]} to {few[-1]} windows." if len(few) > 1 else f" <1%: {few[0]} window{'s' * (few[0] > 1)}.") if few else ""
    footnote(fig, f"Share of each row's windows. Test set: {cls['windows']:,} windows. Overall accuracy {pct(cls['accuracy'])}.{few_text}",
             y=-0.06)
    return save(fig, FIGURES[0], out_dir)


# --------------------------------------------------------------------------------------------------------- 2

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
    card(ax, 8.8, "FALSE ALARMS", "\u00d7", RED, "Healthy windows:", "Wrongly flagged by the\nlive detector", wrong["deployed"], f"{pct(wrong['deployed'], 1)} wrongly flagged")
    ax.text(0.3, 7.0, f"The live detector catches {pct(caught, 1)} of faults and wrongly flags {pct(wrong['deployed'], 1)} of healthy windows",
            fontsize=16, fontweight="bold", va="center")
    h = det["deployed"]["headline"]
    ax.text(0.3, 0.1, f"One dot = one window out of 100. Filled = flagged as a fault. Test set: {h['windows']:,} windows ({h['normal_windows']['healthy']} healthy).",
            fontsize=11.5, color=SECOND, va="center")
    if wrong["iforest"] >= 0.999:
        ax.text(0.3, -0.22, "The live detector flags a window when the forest does and the classifier names a fault. The forest flags every "
                "test window, so here its verdict is the classifier's:\nthe same numbers as the AnomalyPulse classifier in figure 4.",
                fontsize=11.5, color=SECOND, va="top", linespacing=1.3)
    return save(fig, FIGURES[1], out_dir)


# --------------------------------------------------------------------------------------------------------- 3

BAND_UNITS = {
    "db-throttling": "{}\u2013{}\u00d7 offered load",
    "lambda-degradation": "+{}\u2013{} ms latency",
    "traffic-spike": "{}\u2013{}\u00d7 traffic",
    "payment-failure": "{}\u2013{} ms timeout",
}
ROLES = (("interpolation", "Between the training bands"), ("mild extrapolation", "Just above the training range"))


def band_text(injector: str, band: list) -> str:
    return BAND_UNITS[injector].format(*(f"{x:,g}" for x in band))


def per_band(data: dict, out_dir: Path) -> list[Path]:
    rows = [r for r in data["m9_2"]["per_band"]["rows"] if r["family"] == "classification" and r["model"] == "incident_type"]
    order = ["db-throttling", "lambda-degradation", "traffic-spike", "payment-failure"]
    names = {"db-throttling": "Database\nthrottling", "lambda-degradation": "Lambda\ndegradation", "traffic-spike": "Traffic\nspike", "payment-failure": "Dependency\nfailure"}

    fig = plt.figure(figsize=(13, 6.6))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 13), ax.set_ylim(0, 6.6), ax.axis("off")
    w, gap, y0, h = 2.9, 0.28, 1.05, 4.95
    yt = y0 + h
    bad_names, bad_recalls = [], []
    for k, inj in enumerate(order):
        x0 = 0.3 + k * (w + gap)
        mine = {role: next(r for r in rows if r["injector"] == inj and r["role"] == role) for role, _ in ROLES}
        bad = any(r["vs_validation"]["status"] == "degraded" for r in mine.values())
        color = RED if bad else BLUE
        shell(ax, x0, y0, w, h, "DEGRADED" if bad else "HOLDS", "\u00d7" if bad else "\u2713", color)
        for line_no, line in enumerate(names[inj].split("\n")):
            ax.text(x0 + 0.4, yt - 0.95 - 0.29 * line_no, line, fontsize=13.5, fontweight="bold", va="center", zorder=3)
        for j, (role, label) in enumerate(ROLES):
            r = mine[role]
            top = yt - 1.85 - j * 1.3
            ax.text(x0 + 0.4, top, label, fontsize=10, color=SECOND, va="center", zorder=3)
            ax.text(x0 + 0.4, top - 0.28, band_text(inj, r["band"]), fontsize=10, color=SECOND, va="center", zorder=3)
            ax.text(x0 + 0.4, top - 0.72, pct(r["recall"]), fontsize=20, fontweight="bold", color=color, va="center", zorder=3)
        ref = mine["interpolation"]["vs_validation"]["ref_recall"]
        if bad:
            gaps = [100 * r["vs_validation"]["gap"] for r in mine.values()]
            bad_names.append(PRETTY[mine["interpolation"]["label"]].lower())
            bad_recalls += [r["recall"] for r in mine.values()]
            ax.text(x0 + 0.4, y0 + 0.5, f"{min(gaps):.0f}\u2013{max(gaps):.0f} points below\nvalidation ({pct(ref)})", fontsize=10.5, fontweight="bold", color=RED, va="center", zorder=3)
        else:
            ax.text(x0 + 0.4, y0 + 0.5, f"Validation: {pct(ref)}", fontsize=10.5, color=SECOND, va="center", zorder=3)
    good = len(order) - len(bad_names)
    if bad_names:
        lo, hi = pct(min(bad_recalls)), pct(max(bad_recalls))
        title = f"{WORDS[good]} fault types hold up in both held-out bands; {' and '.join(bad_names)} drops to {lo[:-1]}\u2013{hi} in both"
    else:
        title = "All four fault types hold up in both held-out bands"
    ax.text(0.3, 6.3, title, fontsize=14.5, fontweight="bold", va="center")
    ax.text(0.3, 0.62, "Percentages: the share of each fault's test windows the classifier names correctly. Both bands are held out of training: one lies between "
            "the two training bands,", fontsize=10, color=SECOND, va="center")
    ax.text(0.3, 0.36, "the other just above the stronger one. Validation: the same classifier on the validation runs (experiments 561\u2013700), at the "
            "training bands' strengths.", fontsize=10, color=SECOND, va="center")
    ax.text(0.3, 0.1, "DEGRADED: more than 10 points below validation, with the band's 95% interval wholly below validation's.",
            fontsize=10, color=SECOND, va="center")
    return save(fig, FIGURES[2], out_dir)


# --------------------------------------------------------------------------------------------------------- 4

def points(diff: float) -> str:
    """A margin in percentage points: '+53', '+9.3', '0'. One decimal below 10 points, where a point is a large share."""
    v = 100 * diff
    if round(v, 1) == 0:
        return "0"
    text = f"{v:.1f}" if abs(v) < 10 else f"{rnd(v)}"
    return f"+{text}" if v > 0 else text


def margin_text(diff: float, ci: list) -> str:
    """'+53 [48, 58]': the margin and its 95% interval, the bounds as precise as the margin."""
    digits = 1 if 0 < abs(round(100 * diff, 1)) < 10 else 0

    def bound(x: float) -> str:
        return f"{100 * x:.1f}" if digits else f"{rnd(100 * x)}"
    return f"{points(diff)} [{bound(ci[0])}, {bound(ci[1])}]"


def share_text(x: float) -> str:
    """A share flagged: one decimal under 10% and between 99% and 100%, so 1.5% and 99.9% don't read as 2% and 100%."""
    return pct(x, 1 if x < 0.1 or 0.99 < x < 1 else 0)


def margin_rows(data: dict) -> list[dict]:
    """Figure 4's rows, top to bottom: all faults, each fault type from the largest margin, then the healthy
    windows' false alarms. Margins are the classifier minus Model 0 (the 3-window rule), paired by run."""
    b = data["m9_3"]
    rule = "model0_earlier_mean_3"
    det = b["detection"]["headline"]
    overall = b["margins"]["headline"][rule]
    rows = [{"label": "All faults", "diff": overall["difference"]["recall"], "ci": overall["ci95"]["recall"],
             "model0": det[rule]["metrics"]["recall"], "ml": det["classifier_as_detector"]["metrics"]["recall"], "kind": "gain"}]
    shares = b["share_margins"]["headline"][rule]
    types = sorted((t for t in shares if t not in ("NORMAL", "healthy")), key=lambda t: -shares[t]["difference"])
    rows += [{"label": PRETTY[t], "diff": shares[t]["difference"], "ci": shares[t]["ci95"], "model0": shares[t]["baseline"],
              "ml": shares[t]["ml"], "kind": "gain"} for t in types]
    h = shares["healthy"]
    rows.append({"label": "False alarms on\nhealthy windows", "diff": h["difference"], "ci": h["ci95"], "model0": h["baseline"],
                 "ml": h["ml"], "kind": "price"})
    return rows


def baseline_margins(data: dict, out_dir: Path) -> list[Path]:
    b = data["m9_3"]
    rows = margin_rows(data)
    weaker = b["margins"]["headline"]["model0_previous_window"]
    n = len(rows)
    ys = [n - k + (0.0 if k < n - 1 else -0.5) for k in range(n)]   # a half-row gap before the false alarms

    fig, ax = plt.subplots(figsize=(13, 6.9))
    fig.subplots_adjust(left=0.19, right=0.64, top=0.8, bottom=0.2)
    ax.axvline(0, color=SECOND, lw=1.2, zorder=1)
    for x in (25, 50, 75, 100):
        ax.axvline(x, color=GRID, lw=0.9, zorder=0)
    for y, r in zip(ys, rows):
        color = RED if r["kind"] == "price" else BLUE
        lo, hi = 100 * r["ci"][0], 100 * r["ci"][1]
        ax.plot([lo, hi], [y, y], color=color, lw=2.4, solid_capstyle="butt", zorder=2)
        if hi - lo > 3:     # caps only where the interval is wider than the dot
            for x in (lo, hi):
                ax.plot([x, x], [y - 0.13, y + 0.13], color=color, lw=2.0, zorder=2)
        ax.plot(100 * r["diff"], y, "o", ms=10, color=color, mec="white", mew=2, zorder=3)
        weight = "bold" if r["label"] == "All faults" else "normal"
        ax.text(1.03, y, margin_text(r["diff"], r["ci"]), transform=ax.get_yaxis_transform(), fontsize=13, fontweight="bold",
                color=INK, va="center")
        ax.text(1.36, y, f"{share_text(r['model0'])} \u2192 {share_text(r['ml'])}", transform=ax.get_yaxis_transform(),
                fontsize=12, color=SECOND, va="center")
        ax.text(-0.02, y, r["label"], transform=ax.get_yaxis_transform(), fontsize=12.5, fontweight=weight, ha="right", va="center",
                linespacing=1.15)
    top = max(ys) + 0.75
    ax.text(1.03, top, "Margin [95% interval]", transform=ax.get_yaxis_transform(), fontsize=11, color=SECOND, va="center")
    ax.text(1.36, top, "Model 0 \u2192 classifier", transform=ax.get_yaxis_transform(), fontsize=11, color=SECOND, va="center")
    ax.set_ylim(min(ys) - 0.6, top + 0.35)
    ax.set_xlim(-5, 105)
    ax.set_xticks([0, 25, 50, 75, 100], ["0", "+25", "+50", "+75", "+100"])
    ax.set_yticks([])
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Classifier minus Model 0, percentage points of windows flagged")
    gains = rows[1:-1]
    best, worst = gains[0], gains[-1]
    fig.text(0.01, 0.95, f"The classifier's margin over Model 0 runs from {points(best['diff'])} points on "
             f"{best['label'].lower()}s to {points(worst['diff'])} on {worst['label'].lower()}",
             fontsize=15, fontweight="bold", ha="left")
    fig.text(0.01, 0.895, "Blue: faults caught (higher is better). Red: false alarms on healthy windows, the price of the gain (higher is worse).",
             fontsize=11.5, color=SECOND, ha="left")
    p = b["provenance"]["counts"]
    fig.text(0.01, 0.075, "Model 0, the simple threshold rule, flags a window when any service's p95 latency is above 2x normal or its 5xx share is "
             "above 5%, against the mean of the 3 earlier windows.", fontsize=10, color=SECOND, ha="left")
    fig.text(0.01, 0.04, f"Against the weaker version (the previous window only), the margin on all faults is "
             f"{margin_text(weaker['difference']['recall'], weaker['ci95']['recall'])} points. Intervals: 1,000 bootstrap resamples of whole runs.",
             fontsize=10, color=SECOND, ha="left")
    fig.text(0.01, 0.005, f"Test set, headline runs ({p['headline_runs']} runs, {p['headline_windows']:,} windows). Source: M9-3.",
             fontsize=10, color=SECOND, ha="left")
    return save(fig, FIGURES[3], out_dir)


# --------------------------------------------------------------------------------------------------------- 5

def log_ticks(lo: float, hi: float) -> list[float]:
    """The 1-2-5 ticks inside [lo, hi]."""
    out = []
    for e in range(math.floor(math.log10(lo)), math.ceil(math.log10(hi)) + 1):
        out += [m * 10 ** e for m in (1, 2, 5) if lo <= m * 10 ** e <= hi]
    return out


def spread(values: dict, ratio: float = 1.3) -> dict:
    """Label heights on a log axis, moved apart from the top down until neighbours are at least `ratio` apart."""
    out, above = {}, None
    for key, v in sorted(values.items(), key=lambda kv: -kv[1]):
        out[key] = v if above is None else min(v, above / ratio)
        above = out[key]
    return out


def cost_curve(data: dict, out_dir: Path) -> list[Path]:
    rows = data["cost"]["rows"]
    rps = [r["rps"] for r in rows]
    first, last = rows[0], rows[-1]
    fig, ax = plt.subplots(figsize=(11, 6.8))
    fig.subplots_adjust(top=0.8, bottom=0.17, left=0.13, right=0.74)
    lines = (("total", INK, "Total bill", 2.4), ("app", MUTED, "E-commerce app", 2.4), ("anomalypulse", BLUE, "AnomalyPulse", 3.2))
    ends = spread({key: last[key] for key, *_ in lines})
    starts = spread({key: first[key] for key, *_ in lines})
    for key, color, label, lw in lines:
        ax.plot(rps, [r[key] for r in rows], "-o", color=color, lw=lw, markersize=7, zorder=3, mec="white", mew=1.5)
        bold = "bold" if key == "anomalypulse" else "normal"
        share = f" ({pct(last['anomalypulse_share'])})" if key == "anomalypulse" else ""
        ax.text(rps[-1] * 1.18, ends[key], f"{label}  ${last[key]:,.0f}{share}", color=INK, fontsize=12, fontweight=bold, va="center")
        share = f" ({pct(first['anomalypulse_share'])})" if key == "anomalypulse" else ""
        ax.text(rps[0] / 1.15, starts[key], f"${first[key]:,.0f}{share}", color=INK, fontsize=11.5, ha="right", va="center", fontweight=bold)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(rps, [str(r) for r in rps])
    lo = min(r["anomalypulse"] for r in rows) / 1.6
    hi = max(r["total"] for r in rows) * 1.6
    ax.set_ylim(lo, hi)
    ax.set_yticks(log_ticks(lo, hi), [f"${t:,.0f}" for t in log_ticks(lo, hi)])
    ax.minorticks_off()
    ax.set_xlim(rps[0] / 2.6, rps[-1] * 1.12)
    ax.set_xlabel("Requests per second to the e-commerce app")
    ax.set_ylabel("Monthly AWS cost")
    ax.grid(axis="y", color=GRID, lw=0.9)
    ax.set_axisbelow(True)
    fig.text(0.01, 0.93, f"Monitoring's share of the bill falls from {pct(first['anomalypulse_share'])} at {first['rps']} request/s to "
             f"{pct(last['anomalypulse_share'])} at {last['rps']} requests/s", fontsize=15, fontweight="bold", ha="left")
    fig.text(0.01, 0.875, "Total bill = the e-commerce app + what adding AnomalyPulse costs. Percentages: AnomalyPulse's share of the total.",
             fontsize=11.5, color=SECOND, ha="left")
    fig.text(0.01, 0.025, f"Projected monthly cost at list price, {data['cost']['incidents_per_month']} incidents a month. Both axes are logarithmic.",
             fontsize=10, color=SECOND, ha="left")
    return save(fig, FIGURES[4], out_dir)


# --------------------------------------------------------------------------------------------------------- 6

def detection_latency(data: dict) -> dict:
    """Detection latency per timed test run: the window plus the pipeline's wait (before diagnosis starts)."""
    from report.latency import stats

    runs = [r for r in data["latency"]["runs"] if r["phase"] == "test" and r["outcome"] == "detected"]
    return stats([r["steps"]["window"] + r["steps"]["pipeline_wait"] for r in runs])


LATENCY_PARTS = (
    ("window", "1-minute\nwindow", BLUE, "white"),
    ("cloudwatch_settle", "CloudWatch settles the minute\n(fixed 2-minute lag)", ORANGE, "white"),
    ("build_and_classify", "Next window-builder run,\nbuild and classify", VIOLET, "white"),
    ("diagnosis", "Diagnosis\n(Bedrock)", AQUA, INK),
)


def latency_segments(steps: dict) -> list[tuple[str, float, float]]:
    """Figure 6's bar: (step, start, width) for each step, its median, laid end to end from 0."""
    out, x = [], 0.0
    for key, *_ in LATENCY_PARTS:
        out.append((key, x, steps[key]["median"]))
        x += steps[key]["median"]
    return out


def latency_breakdown(data: dict, out_dir: Path) -> list[Path]:
    from matplotlib.patches import Rectangle

    t = data["latency"]["summary"]["test"]
    steps = t["steps_s"]
    det = detection_latency(data)
    segments = latency_segments(steps)
    total = segments[-1][1] + segments[-1][2]
    settle_share = t["share_of_mean"]["cloudwatch_settle"]

    fig = plt.figure(figsize=(12, 5.6))
    ax = fig.add_axes([0.03, 0.02, 0.94, 0.96])
    ax.set_xlim(-8, total + 12), ax.set_ylim(-3.3, 2.4), ax.axis("off")
    for (key, label, color, ink), (_, x, w) in zip(LATENCY_PARTS, segments):
        ax.add_patch(Rectangle((x, 0), w, 0.9, fc=color, ec="white", lw=3, zorder=2))
        ax.text(x + w / 2, 0.45, f"{rnd(w)} s", color=ink, fontsize=17, fontweight="bold", ha="center", va="center", zorder=3)
        ax.text(x + w / 2, 1.1, label, fontsize=11.5, ha="center", va="bottom", linespacing=1.2)

    def bracket(x1: float, x2: float, y: float, head: str, sub: str) -> None:
        ax.plot([x1, x1, x2, x2], [y + 0.14, y, y, y + 0.14], color=SECOND, lw=1.6, zorder=1)
        ax.text((x1 + x2) / 2, y - 0.14, head, fontsize=14, fontweight="bold", ha="center", va="top")
        ax.text((x1 + x2) / 2, y - 0.52, sub, fontsize=10.5, color=SECOND, ha="center", va="top")

    d = segments[-1][1]     # where diagnosis starts: the end of the build-and-classify step
    bracket(0, d, -0.35, f"Detection: {rnd(det['median'])} s", "From the start of the fault to detecting it and naming its type.")
    bracket(0, total, -1.65, f"End to end: {rnd(steps['end_to_end']['median'])} s", f"From the start of the fault to detecting it, diagnosing it and storing the diagnosis.\n9 in 10 runs finish within {rnd(steps['end_to_end']['p90'])} s.")
    minutes = rnd(steps["end_to_end"]["median"] / 60)
    ax.text(-6, 2.25, f"Fault to stored diagnosis takes about {minutes} minutes, and {pct(settle_share)} of it is CloudWatch's 2-minute settle",
            fontsize=15, fontweight="bold", va="center")
    ax.text(-6, -3.0, f"Typical run (median of each step), {steps['end_to_end']['n']} timed detections on the test set. Runbooks off. "
            f"The share is of the mean end-to-end time ({rnd(steps['end_to_end']['mean'])} s).", fontsize=10, color=SECOND, va="center")
    ax.text(-6, -3.25, "The 2-minute lag is set by configuration (METRICS_LAG_MINUTES), so its bar is exactly 120 s; the others are measured.",
            fontsize=10, color=SECOND, va="center")
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
