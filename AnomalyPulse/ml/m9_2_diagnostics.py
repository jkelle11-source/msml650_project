"""M9-2 diagnostics: why the Isolation Forest and DEPENDENCY_FAILURE results look as they do.

    cd AnomalyPulse
    python -m ml.m9_2_diagnostics --dataset ~/v1 --classifier ~/pkgs/incident_type \\
        --detector ~/pkgs/iforest --job classifier=<training job> --job detector=<training job> \\
        --out ~/m9-2-results

These explanations were worked out AFTER the test set was scored. No number here is a result, and
none feeds ml/m9_2_metrics.py. Nothing is fitted, no cut-off is chosen and the models are unchanged.

A swap test replaces a few columns of some windows with values from validation windows and asks the
same model again. It shows which columns a result depends on. It is offline evidence, not a
controlled live experiment.
"""
import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ml import m9_2_metrics as m
from ml.evaluate import EvalError
from ml.m9_2_metrics import MetricsError, TrainError, detection

SEED = 20261123
OUT_JSON, OUT_MD = "diagnostics_m9_2.json", "diagnostics_m9_2.md"
DEPENDENCY, CASCADE = "DEPENDENCY_FAILURE", m.CASCADE
ORDER_P50 = "order_p50_latency_ms"
SLOW_MS = 100.0         # Order's median in a healthy minute is about 3-10 ms, a slow POST's is seconds
# GET /products, GET /products/{id}, POST /cart, GET /orders/{id}, POST /orders
MIXES = {"default": (60, 15, 10, 10, 5), "shifted": (40, 25, 15, 10, 10)}
BASELINE_COLUMNS = ("product_request_rate", "cart_request_rate", "order_request_rate", "payment_request_rate",
                    "order_db_consumed_write_capacity", ORDER_P50)


@dataclass
class Windows:
    """One entry per labeled window, across validation, test and stress."""
    X: np.ndarray
    columns: list
    phase: np.ndarray
    label: np.ndarray
    healthy: np.ndarray     # NORMAL with no fault active
    kind: np.ndarray        # single, cascade, confounder, normal, validation
    date: np.ndarray
    flagged: np.ndarray     # the forest, at its packaged cut-off
    score: np.ndarray       # score_samples: lower is more anomalous
    pred: np.ndarray        # the classifier


def post_share(mix):
    """POST /orders' share of the Order service's traffic."""
    return mix[4] / (mix[3] + mix[4])


def rate_columns(columns):
    """The columns that move directly with how much traffic each service gets."""
    return [i for i, c in enumerate(columns) if "_request_rate" in c or "_consumed_" in c]


def swap_columns(target, source, columns, rng):
    """`target` with `columns` replaced by the values of randomly drawn `source` rows."""
    out = target.copy()
    out[:, columns] = source[rng.integers(0, len(source), len(target))][:, columns]
    return out


def class_names(raw, classes):
    return np.asarray([classes[int(v)] if isinstance(v, (int, np.integer)) and not isinstance(v, bool) else str(v)
                       for v in raw])


def load_windows(dataset, queues, forest, classifier):
    if forest.meta["columns"] != classifier.meta["columns"]:
        raise MetricsError("the forest and the classifier read different columns")
    registry = m.load_registry(dataset)
    m.check_split(registry)
    meta = m.tag_runs(registry, m.load_specs(queues))
    meta.update({r["experiment_id"]: {"eid": r["experiment_id"], "slot": r["slot"], "phase": "val", "kind": "validation"}
                 for r in registry if r["phase"] == "val"})
    scored = m.score_runs(dataset, meta, [forest], [classifier])
    dates = {r["experiment_id"]: r["generation_date"] for r in registry}
    eids = [eid for eid, _ in scored.rows]
    return Windows(X=m.feature_matrix(scored.rows, forest.meta, forest.name), columns=list(forest.meta["columns"]),
                   phase=np.asarray([meta[e]["phase"] for e in eids]), label=np.asarray(scored.labels),
                   healthy=np.asarray(detection.healthy(scored.runs, scored.rows), bool),
                   kind=np.asarray([meta[e]["kind"] for e in eids]), date=np.asarray([dates[e] for e in eids]),
                   flagged=np.asarray(scored.flags[forest.name], bool), score=-scored.anomaly_scores[forest.name],
                   pred=np.asarray(scored.preds[classifier.name]))


def forest_section(w, score, threshold, seed=SEED):
    """Where the forest flags healthy windows, and whether traffic-volume columns explain it.
    `score` is the forest's score_samples."""
    healthy = {p: w.healthy & (w.phase == p) for p in ("val", "test", "stress")}
    by_phase = {p: {"windows": int(h.sum()), "flagged": int(w.flagged[h].sum()),
                    "score_median": float(np.median(w.score[h])), "score_min": float(w.score[h].min()),
                    "score_max": float(w.score[h].max())} for p, h in healthy.items() if h.any()}
    shared = sorted(set(w.date[healthy["test"]]) & set(w.date[healthy["stress"]]))
    same_day = None
    if shared:
        day = shared[-1]
        same_day = {"date": day}
        for name, p in (("shifted_mix_test", "test"), ("default_mix_stress", "stress")):
            h = healthy[p] & (w.date == day)
            same_day[name] = {"windows": int(h.sum()), "flagged": int(w.flagged[h].sum())}
    baseline = {c: {"validation": float(w.X[healthy["val"], w.columns.index(c)].mean()),
                    "test": float(w.X[healthy["test"], w.columns.index(c)].mean())}
                for c in BASELINE_COLUMNS if c in w.columns}
    columns = rate_columns(w.columns)
    target, source = w.X[healthy["test"]], w.X[healthy["val"]]
    swapped = swap_columns(target, source, columns, np.random.default_rng(seed))
    return {"threshold": threshold, "healthy_by_phase": by_phase, "same_day": same_day, "healthy_baseline_means": baseline,
            "swap_traffic_columns": {"columns": len(columns), "windows": len(target),
                                     "flagged_share_before": float(np.mean(score(target) < threshold)),
                                     "flagged_share_after": float(np.mean(score(swapped) < threshold))}}


def dependency_section(w, predict, seed=SEED):
    """Whether Order's median latency explains single-fault dependency windows called cascades.
    `predict` maps a feature matrix to class names."""
    column = w.columns.index(ORDER_P50)
    slow = w.X[:, column] > SLOW_MS
    dep = w.label == DEPENDENCY
    groups = {"validation": dep & (w.phase == "val"), "test": dep & (w.phase == "test") & (w.kind == "single"),
              "stress": dep & (w.phase == "stress")}
    by_group = {name: {"windows": int(g.sum()), "recall": float(np.mean(w.pred[g] == DEPENDENCY)),
                       "share_order_p50_slow": float(slow[g].mean())} for name, g in groups.items() if g.any()}
    test = groups["test"]
    split = {name: {"windows": int((test & s).sum()), "called_cascade": int((test & s & (w.pred == CASCADE)).sum())}
             for name, s in (("order_p50_slow", slow), ("order_p50_fast", ~slow))}
    rng = np.random.default_rng(seed)
    source = w.X[groups["validation"]]
    fixed = swap_columns(w.X[test], source, [column], rng)
    cascades = (w.label == CASCADE) & (w.phase == "test")
    control = swap_columns(w.X[cascades], source, [column], rng)
    return {"post_orders_share_of_order_traffic": {name: post_share(mix) for name, mix in MIXES.items()},
            "slow_ms": SLOW_MS, "by_group": by_group, "test_windows_by_order_p50": split,
            "swap_order_p50": {"dependency_recall_before": by_group["test"]["recall"],
                               "dependency_recall_after": float(np.mean(predict(fixed) == DEPENDENCY)),
                               "control_cascade_windows": int(cascades.sum()),
                               "control_cascade_recall_before": float(np.mean(w.pred[cascades] == CASCADE)),
                               "control_cascade_recall_after": float(np.mean(predict(control) == CASCADE))}}


def render_markdown(report):
    f, d = report["forest"], report["dependency"]
    prov = report["provenance"]
    L = ["# M9-2 diagnostics (after scoring)", "",
         "These explain the scored results. They are not results themselves: nothing was fitted or tuned, and a swap test is "
         "offline evidence about which columns a result depends on, not a controlled live experiment.", "",
         f"Code: commit `{prov['git']['commit']}`{' (uncommitted changes present)' if prov['git']['dirty'] else ''}; seed {prov['seed']}; "
         f"generated {prov['generated_at']}.", "", "## The Isolation Forest on healthy windows", "",
         f"Cut-off: {f['threshold']:.4f} (scores below it are flagged).", "",
         "| Phase | Healthy windows | Flagged | Median score | Highest (most normal) score |", "|---|---|---|---|---|"]
    for phase, b in f["healthy_by_phase"].items():
        L.append(f"| {phase} | {b['windows']} | {b['flagged']} | {b['score_median']:.3f} | {b['score_max']:.3f} |")
    if f["same_day"]:
        s = f["same_day"]
        L += ["", f"Same day ({s['date']}): the shifted mix's test runs had {s['shifted_mix_test']['flagged']} of "
                  f"{s['shifted_mix_test']['windows']} healthy windows flagged, the default mix's stress runs "
                  f"{s['default_mix_stress']['flagged']} of {s['default_mix_stress']['windows']}."]
    L += ["", "Healthy-window means:", "", "| Column | Validation | Test |", "|---|---|---|"]
    L += [f"| {c} | {v['validation']:.2f} | {v['test']:.2f} |" for c, v in f["healthy_baseline_means"].items()]
    swap = f["swap_traffic_columns"]
    L += ["", f"Swap test: with the {swap['columns']} request-rate and database-capacity columns of the {swap['windows']} healthy test "
              f"windows replaced by validation values, the flagged share goes from {swap['flagged_share_before']:.1%} to "
              f"{swap['flagged_share_after']:.1%}.", "", "## DEPENDENCY_FAILURE called CASCADING_FAILURE", "",
          "`POST /orders` is " + " and ".join(f"{v:.1%} of Order's traffic in the {k} mix"
                                              for k, v in d["post_orders_share_of_order_traffic"].items()) + ".", "",
          f"| Runs | Windows | Dependency recall | Share with Order p50 above {d['slow_ms']:.0f} ms |", "|---|---|---|---|"]
    L += [f"| {k} | {v['windows']} | {v['recall']:.3f} | {v['share_order_p50_slow']:.3f} |" for k, v in d["by_group"].items()]
    split, swap = d["test_windows_by_order_p50"], d["swap_order_p50"]
    L += ["", f"Test windows with a slow Order p50: {split['order_p50_slow']['called_cascade']} of {split['order_p50_slow']['windows']} "
              f"called a cascade. With a fast one: {split['order_p50_fast']['called_cascade']} of {split['order_p50_fast']['windows']}.", "",
          f"Swap test: replacing only `{ORDER_P50}` with validation dependency-failure values takes dependency recall from "
          f"{swap['dependency_recall_before']:.3f} to {swap['dependency_recall_after']:.3f}. Control: the same swap on the "
          f"{swap['control_cascade_windows']} real cascade windows leaves their recall at {swap['control_cascade_recall_after']:.3f} "
          f"(was {swap['control_cascade_recall_before']:.3f}).", ""]
    return "\n".join(L)


def build_report(dataset, queues, forest, classifier, seed=SEED):
    w = load_windows(dataset, queues, forest, classifier)
    classes = classifier.meta["classes"]
    return {"provenance": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seed": seed,
                           "git": m.git_state(Path(__file__).resolve().parents[2]), "environment": m.library_versions(),
                           "packages": {p.name: {"job": p.job, "sha256": p.sha256} for p in (forest, classifier)}},
            "forest": forest_section(w, forest.model.score_samples, forest.meta["threshold"], seed),
            "dependency": dependency_section(w, lambda X: class_names(classifier.model.predict(X), classes), seed)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ml.m9_2_diagnostics", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True, help="a local copy of datasets/v1/")
    parser.add_argument("--queues", default=str(Path(__file__).resolve().parents[1] / "simulator" / "queues"))
    parser.add_argument("--classifier", required=True, help="the six-class package folder")
    parser.add_argument("--detector", required=True, help="the Isolation Forest package folder")
    parser.add_argument("--job", action="append", default=[], metavar="classifier|detector=JOB")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    jobs = m._pairs(parser, args.job, "--job", as_path=False)
    if set(jobs) - {"classifier", "detector"}:
        parser.error("--job takes classifier=JOB or detector=JOB")
    try:
        forest = m.load_package("iforest", Path(args.detector).expanduser(), "detector", jobs.get("detector"))
        if forest.role != "iforest":
            raise MetricsError(f"{args.detector} is not an Isolation Forest package")
        classifier = m.load_package("incident_type", Path(args.classifier).expanduser(), "classifier", jobs.get("classifier"))
        report = build_report(Path(args.dataset).expanduser(), Path(args.queues).expanduser(), forest, classifier, args.seed)
    except (MetricsError, EvalError, TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / OUT_JSON).write_text(json.dumps(m._clean(report), indent=2, allow_nan=False) + "\n")
    (out / OUT_MD).write_text(render_markdown(report))
    print(out / OUT_JSON, out / OUT_MD, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
