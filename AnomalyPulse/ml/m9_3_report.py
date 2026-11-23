"""M9-3: the threshold baseline (Model 0) beside M9-2's test-set results.

    cd AnomalyPulse
    python -m ml.m9_3_report --dataset ~/v1 --classifier incident_type=~/pkgs/logreg \\
        --m9-2-results ml/m9_2_results/metrics_m9_2.json --out ~/m9-3-results

Model 0 is scored with M9-2's own code (ml/m9_2_metrics.py) on its runs and windows. Margins are paired
(ml/m9_3_baseline.py). Nothing here fits or tunes anything. Frozen before Model 0 touched the test set:
ml/model0.py as reviewed in M5-3, no tuning, everything reported whichever way it falls.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ml import evaluate, model0
from ml import m9_2_metrics as m
from ml import m9_3_baseline as b

OUT_JSON, OUT_MD = "baseline_m9_3.json", "baseline_m9_3.md"
MODELS = (b.PREVIOUS, b.EARLIER, b.CLASSIFIER_AS_DETECTOR)
RANKING = ("roc_auc", "average_precision", "recall_at_healthy_fpr_1")
NAME = {b.PREVIOUS: "Model 0 (previous window)", b.EARLIER: "Model 0 (earlier mean, the stronger floor)",
        b.CLASSIFIER_AS_DETECTOR: "classifier, read as a detector"}


def _strip_ranking(block):
    """A rule has no score, so its ranking metrics (computed from 0/1 flags) are not results."""
    for key in RANKING:
        block["metrics"][key] = None
    return block


def detection_for(scored, eids, key, flag_only):
    out = {}
    for name in MODELS:
        block, _ = m.detection_block(scored, name, eids, f"{key}/{name}")
        out[name] = _strip_ranking(block) if name in flag_only else block
    return out


def margins(scored, eids, key):
    return {base: b.paired_margin(scored, eids, b.CLASSIFIER_AS_DETECTOR, base, f"{key}/margin/{base}")
            for base in b.NAMES}


def share_flagged(scored, eids, key):
    """Per label, the share of its windows each detector flags, with a run-bootstrap interval. For NORMAL it
    is the false-alarm rate; for CASCADING_FAILURE, whether the rule notices a cascade at all."""
    sub = m.subset(scored, eids)
    labels = np.asarray(sub.labels)
    out = {}
    for label in sorted(set(sub.labels)):
        mask = labels == label
        out[label] = {"windows": int(mask.sum())}
        for name in MODELS:
            per_run = m.per_run_ratio(sub, scored.flags[name][sub.idx], mask)
            hits, windows = per_run.sum(axis=0)
            out[label][name] = {"share": float(hits / windows) if windows else None,
                                "ci95": m.bootstrap(per_run, m.ratio_stats, f"{key}/share/{label}/{name}").get("ratio")}
    return m._clean(out)


def validation_scored(dataset, registry, classifiers):
    """Validation's runs scored the same way, with Model 0 added: the baseline's own shift to the test set."""
    val = {r["experiment_id"]: {"eid": r["experiment_id"], "phase": "val", "label_type": r.get("incident_type")}
           for r in registry if r["phase"] == "val"}
    if not val:
        raise m.MetricsError("the registry has no validation runs")
    scored = m.score_runs(dataset, val, [], classifiers)
    return scored, val, b.add_baselines(scored, classifiers[0].name)


def reference_cells(scored, val, classifier):
    """{(family, model, label_type): {recall, ci95, ...}} on validation's single-fault runs, for the per-band
    table's 'against validation' column."""
    out = {}
    for label_type in m.SINGLE_LABELS:
        eids = [e for e, t in val.items() if t["label_type"] == label_type]
        if not eids:
            continue
        for family, model in [("detection", n) for n in MODELS] + [("classification", classifier)]:
            cell = m._cell(scored, family, model, eids, label_type, f"validation/{label_type}/{family}/{model}")
            out[(family, model, label_type)] = {k: cell[k] for k in ("recall", "ci95", "runs", "windows")}
    return out


def load_m9_2_reference(path):
    data = json.loads(Path(path).expanduser().read_text())
    out = {}
    for name, blocks in data.get("detection", {}).items():
        block = (blocks or {}).get("headline") or {}
        out[name] = {"metrics": block.get("metrics"), "ci95": block.get("ci95"), "runs": block.get("runs")}
    return out


def build_report(dataset, queues, classifiers, seed=m.SEED, resamples=m.BOOTSTRAP, allow_partial=False,
                 repo_root=None, m9_2_results=None, commit=None):
    if len(classifiers) != 1:
        raise m.MetricsError("give exactly one classifier (the live one M9-2 scored)")
    m.CONFIG.update(seed=seed, resamples=resamples)
    package = classifiers[0]
    classifier = package.name
    registry = m.load_registry(dataset)
    split = m.check_split(registry, allow_partial)
    meta = m.tag_runs(registry, m.load_specs(queues))
    scored = m.score_runs(dataset, meta, [], classifiers)
    flag_only = b.add_baselines(scored, classifier)

    test = [e for e, t in meta.items() if t["phase"] == "test"]
    headline = [e for e in test if meta[e]["kind"] not in m.HEADLINE_EXCLUDES]
    stress = [e for e, t in meta.items() if t["phase"] == "stress"]
    slices = {"headline": headline, "all_test": test}
    if stress:
        slices["stress"] = stress

    val_scored, val_meta, val_flag_only = validation_scored(dataset, registry, classifiers)
    val_eids = sorted(val_meta)
    reference = reference_cells(val_scored, val_meta, classifier)
    environment = m.library_versions()
    headline_set = set(headline)
    report = {
        "provenance": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": seed, "bootstrap_resamples": resamples,
            "bootstrap_unit": "run (the windows of a run are resampled together; a margin resamples the same runs for both detectors)",
            "split": {"test_slots": [m.TEST_FIRST, m.TEST_LAST], "stress_first_slot": m.STRESS_FIRST, **split},
            "counts": {"test_runs": len(test), "headline_runs": len(headline), "stress_runs": len(stress),
                       "headline_windows": sum(1 for e, _ in scored.rows if e in headline_set),
                       "validation_runs": len(val_eids), "validation_windows": len(val_scored.rows)},
            "classifier": m.package_summary(package, environment),
            "git": {**m.git_state(repo_root or Path(__file__).resolve().parents[2]), **({"commit": commit} if commit else {})},
            "environment": environment,
            "model0": {"p95_ratio": model0.RATIO, "error_limit": model0.ERROR_LIMIT,
                       "earlier_windows": model0.EARLIER, "tuned": False,
                       "floors": [b.PREVIOUS, b.EARLIER]},
        },
        "detection": {s: detection_for(scored, eids, s, flag_only) for s, eids in slices.items()},
        "margins": {s: margins(scored, eids, s) for s, eids in slices.items()},
        "share_flagged": {s: share_flagged(scored, eids, s) for s, eids in slices.items()},
        "classification": {"headline": m.classification_block(scored, classifier, headline, "headline"),
                           "all_test": m.classification_block(scored, classifier, test, "all-test")},
        "validation": {"runs": len(val_eids), "windows": len(val_scored.rows),
                       "detection": detection_for(val_scored, val_eids, "validation", val_flag_only),
                       "classification": m.classification_block(val_scored, classifier, val_eids, "validation")},
        "per_band": m.band_table(scored, reference),
        "extreme_slice": {"runs": len(stress), "by_injector": m._per_injector(scored, stress, "stress")} if stress else None,
        "varied_duration": m.varied_duration_block(scored),
        "classifier_name": classifier,
    }
    if m9_2_results:
        report["m9_2_reference"] = load_m9_2_reference(m9_2_results)
    return m._clean(report)


# --- markdown ---------

def _n(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _ci(value, ci, digits=3):
    if value is None:
        return "n/a"
    if not ci:
        return f"{value:.{digits}f}"
    return f"{value:.{digits}f} [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def _table(header, rows):
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


def _det_table(blocks):
    rows = []
    for label, key in (("Precision", "precision"), ("Recall", "recall"), ("F1", "f1"),
                       ("False-positive rate, against the labels", "fpr")):
        rows.append([label] + [_ci(blocks[n]["metrics"].get(key), blocks[n]["ci95"].get(key)) for n in MODELS])
    for label, key in (("False-positive rate, healthy windows only", "fpr_healthy"),
                       ("Fault-start windows flagged", "fault_start_flagged")):
        rows.append([label] + [_n(blocks[n]["metrics"].get(key)) for n in MODELS])
    rows.append(["Median detection latency, s [95% CI]"] + [
        _ci(blocks[n]["latency"].get("median_s"), blocks[n]["latency"].get("median_ci95"), 1) for n in MODELS])
    rows.append(["Incident runs never flagged"] + [
        f"{blocks[n]['latency'].get('missed_runs')} of {blocks[n]['latency'].get('incident_runs')}" for n in MODELS])
    return _table(["Metric"] + [NAME[n] for n in MODELS], rows)


def _margin_table(mg):
    rows = []
    for label, key in (("Precision", "precision"), ("Recall", "recall"), ("F1", "f1"),
                       ("False-positive rate (lower is better)", "fpr")):
        rows.append([label] + [_ci(mg[base]["difference"][key], mg[base]["ci95"][key]) for base in (b.EARLIER, b.PREVIOUS)])
    return _table(["Classifier minus Model 0", "against the earlier-mean floor", "against the previous-window floor"], rows)


def _cells_table(by_injector, classifier):
    rows = []
    for injector, cells in by_injector.items():
        row = [injector, str(cells["runs"])]
        row += [_ci(cells[f"detection:{n}"]["recall"], cells[f"detection:{n}"]["ci95"]) for n in MODELS]
        row.append(_ci(cells[f"classification:{classifier}"]["recall"], cells[f"classification:{classifier}"]["ci95"]))
        rows.append(row)
    return _table(["Fault", "Runs"] + [NAME[n] for n in MODELS] + ["classifier names the class"], rows)


def render_markdown(report):
    p, c = report["provenance"], report["classifier_name"]
    head, mg = report["detection"]["headline"], report["margins"]["headline"]
    cls = report["classification"]["headline"]
    L = ["# M9-3 The threshold baseline beside the test-set results", "", "## Read this first", "",
         "- **Model 0 is a rule.** It says anomaly or not, with no class and no score, so accuracy, macro-F1, ROC AUC, "
         "average precision and recall at a fixed false-alarm rate are n/a for it. It is compared on the anomaly view "
         "(anything but NORMAL is an anomaly). The classifier is read the same way (`classifier, read as a detector`) "
         "for a like-for-like margin.",
         f"- **Frozen before it touched the test set.** Model 0 is `ml/model0.py` as reviewed in M5-3: the rule trips on "
         f"p95 above {model0.RATIO}x its baseline or a 5xx share above {model0.ERROR_LIMIT:.0%}, against the previous "
         f"window or the mean of the last {model0.EARLIER} earlier windows. Not tuned. M9-2 had already scored and seen "
         "the test set, so every result is reported whichever way it falls.",
         "- **Only the live classifier was scored on the test set** (M9-2). The XGBoost and Random Forest of M5-3 are not here.",
         "- **Margins are paired:** both detectors are resampled on the same runs, so a margin's interval is narrower "
         "than two separate intervals suggest.",
         "- **The test set changes the workload on purpose,** so a change from validation to test mixes the held-out "
         "bands with the shifted traffic mix. M9-2's diagnostics attribute dependency failure's drop to the mix "
         "(`Docs/M9-2_REPORT.md`). Nothing here says which cause moved Model 0.",
         "- **No claim about real production incidents:** these results support generalization across held-out regions "
         "of the simulator's parameter space only.", "",
         "## Scope", "",
         f"- **Headline:** {p['counts']['headline_runs']} test runs ({p['counts']['headline_windows']} windows), the test "
         f"set without its confounders. Stress slice: {p['counts']['stress_runs']} runs, reported apart. Validation: "
         f"{p['counts']['validation_runs']} runs ({p['counts']['validation_windows']} windows).",
         f"- **Intervals:** 95% percentile bootstrap, {p['bootstrap_resamples']} resamples of whole runs, seed {p['seed']}.",
         f"- **Classifier:** {p['classifier'].get('estimator')} ({c}), job `{p['classifier'].get('job')}`.",
         f"- **Code:** commit `{p['git'].get('commit')}` (dirty: {p['git'].get('dirty')}); generated {p['generated_at']}.", "",
         "## 1. Detection beside the baseline (headline)", ""]
    L += _det_table(head)
    L += ["", "The latency median covers only the incident runs a detector flagged at all; the last row counts the runs it "
              "never flagged, which a median would hide.", "",
          f"Classification (the classifier alone; Model 0 cannot name a class): accuracy "
              f"{_ci(cls['accuracy'], cls['ci95'].get('accuracy'))}, macro-F1 {_ci(cls['macro_f1'], cls['ci95'].get('macro_f1'))}; "
              "Model 0: n/a.", "", "### The margin", ""]
    L += _margin_table(mg)
    L += ["", "A positive recall difference means the classifier finds anomalous windows Model 0 misses. A positive "
              "false-positive-rate difference means the classifier raises more false alarms.", "",
          "## 2. Share of each class flagged (headline)", ""]
    shares = report["share_flagged"]["headline"]
    rows = []
    for label, cell in shares.items():
        names_it = cls["per_class"].get(label, {})
        rows.append([label + (" (= false-alarm rate)" if label == "NORMAL" else ""), str(cell["windows"])]
                    + [_ci(cell[n]["share"], cell[n]["ci95"]) for n in MODELS]
                    + [_n(names_it.get("recall")), _n(names_it.get("precision"))])
    L += _table(["Label", "Windows"] + [NAME[n] for n in MODELS] + ["classifier recall (names it)", "classifier precision"], rows)
    L += ["", "For `CASCADING_FAILURE` the margin is not the classifier's recall of 1.000 alone: M9-2 shows its precision is "
              "low because dependency failures are called cascades.", "",
          "## 3. Validation to test: the shift in each detector", ""]
    rows = []
    for n in MODELS:
        v, t = report["validation"]["detection"][n], head[n]
        rows.append([NAME[n], _ci(v["metrics"]["recall"], v["ci95"].get("recall")), _ci(t["metrics"]["recall"], t["ci95"].get("recall")),
                     _ci(v["metrics"]["fpr"], v["ci95"].get("fpr")), _ci(t["metrics"]["fpr"], t["ci95"].get("fpr"))])
    L += _table(["Detector", "Recall, validation", "Recall, test", "FPR, validation", "FPR, test"], rows)
    val = report["validation"]["classification"]
    L += ["", f"Classifier, six-class: accuracy {_ci(val['accuracy'], val['ci95'].get('accuracy'))} on validation and "
              f"{_ci(cls['accuracy'], cls['ci95'].get('accuracy'))} on test; macro-F1 "
              f"{_ci(val['macro_f1'], val['ci95'].get('macro_f1'))} and {_ci(cls['macro_f1'], cls['ci95'].get('macro_f1'))}. "
              "Validation tuned the packages, so it is the optimistic side of any gap.", "",
          "## 4. Per held-out band", ""]
    rows = []
    for r in report["per_band"]:
        if r["model"] not in (*MODELS, c):
            continue
        def gap(x):
            return "n/a" if not x else f"{_n(x['gap'])} ({x['status']})"
        rows.append([r["injector"], f"{r['band'][0]}-{r['band'][1]}", r["role"],
                     "classifier (names the class)" if r["family"] == "classification" else NAME[r["model"]],
                     _ci(r["recall"], r["ci95"]), gap(r["vs_other_band"]), gap(r["vs_validation"])])
    L += _table(["Fault", "Band", "Role", "Model", "Recall", "Gap to the other test band", "Gap to validation"], rows)
    if report["extreme_slice"]:
        L += ["", "## 5. Extreme slice (reported apart)", ""] + _cells_table(report["extreme_slice"]["by_injector"], c)
    L += ["", "## 6. Varied durations", ""]
    for duration, block in report["varied_duration"].items():
        L += [f"**{duration} s faults ({block['runs']} runs)**", ""] + _cells_table(block["by_injector"], c) + [""]
    L += ["## What Model 0 cannot be compared on", "",
          "- **Confounders (30 runs).** M9-2 scores them by which class the classifier names (either fault's type, the "
          "labeled first type, or `CASCADING_FAILURE`). A rule names no class, so there is nothing to put beside them. The "
          "headline leaves them out; the JSON's `all_test` slice includes them in the anomaly view only.",
          "- **Time to recognize a cascade, accuracy, macro-F1 and per-class precision** all need a class name, so they are "
          "n/a for Model 0.",
          "- **ROC AUC, average precision and recall at a fixed false-alarm rate** need a score, and a rule has none.",
          "- **XGBoost and Random Forest** were not scored on the test set (M9-2 scored only the live classifier).", ""]
    if report.get("m9_2_reference"):
        L += ["## 7. M9-2's detectors, for reference (headline)", ""]
        rows = []
        for name, ref in report["m9_2_reference"].items():
            mt, ci = ref.get("metrics") or {}, ref.get("ci95") or {}
            rows.append([name] + [_ci(mt.get(k), ci.get(k)) for k in ("precision", "recall", "f1", "fpr")])
        L += _table(["Detector", "Precision", "Recall", "F1", "FPR"], rows)
        L += ["", "From `ml/m9_2_results/metrics_m9_2.json`, not rescored here. The Isolation Forest flags every test window, "
                  "so its recall and latency are not evidence that it detects faults (`Docs/M9-2_REPORT.md`)."]
    return "\n".join(L) + "\n"


# --- command line ------

def write_outputs(report, out):
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    paths = (out / OUT_JSON, out / OUT_MD)
    paths[0].write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    paths[1].write_text(render_markdown(report))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ml.m9_3_report", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True, help="a local copy of datasets/v1/ (tier1/features, tier2/labels, tier2/experiments.jsonl)")
    parser.add_argument("--queues", default=str(Path(__file__).resolve().parents[1] / "simulator" / "queues"))
    parser.add_argument("--classifier", required=True, metavar="NAME=DIR", help="the six-class package M9-2 scored")
    parser.add_argument("--m9-2-results", default=None, help="M9-2's metrics_m9_2.json, to show its detectors beside Model 0")
    parser.add_argument("--out", required=True, help=f"write {OUT_JSON} and {OUT_MD} here")
    parser.add_argument("--seed", type=int, default=m.SEED)
    parser.add_argument("--bootstrap", type=int, default=m.BOOTSTRAP)
    parser.add_argument("--job", default=None, help="the training job the classifier came from")
    parser.add_argument("--commit", default=None, help="the commit this ran from (git rev-parse HEAD; the container has no git)")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite an earlier result")
    args = parser.parse_args(argv)
    name, sep, directory = args.classifier.partition("=")
    if not sep or not name or not directory:
        parser.error(f"--classifier takes NAME=DIR, got {args.classifier!r}")
    out = Path(args.out).expanduser()
    if (out / OUT_JSON).exists() and not args.force:
        print(f"error: {out} already holds a result (--force to overwrite)", file=sys.stderr)
        return 1
    try:
        package = m.load_package(name, Path(directory).expanduser(), "classifier", job=args.job)
        report = build_report(args.dataset, args.queues, [package], seed=args.seed, resamples=args.bootstrap,
                              allow_partial=args.allow_partial, m9_2_results=args.m9_2_results, commit=args.commit)
    except (m.MetricsError, evaluate.EvalError, m.TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    for path in write_outputs(report, out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())