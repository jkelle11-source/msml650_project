"""M9-3: the threshold baseline (Model 0) beside every model's test-set results.

    cd AnomalyPulse
    python -m ml.m9_3_report --dataset ~/v1 \\
        --classifier incident_type=~/pkgs/incident_type --classifier xgboost=~/pkgs/xgb ... \\
        --detector iforest=~/pkgs/iforest --detector binary=~/pkgs/binary \\
        --job incident_type=<training job> --job xgboost=<training job> ... \\
        --m9-2-results ml/m9_2_results/metrics_m9_2.json --out ~/m9-3-results

The first --classifier is the live one, the one M9-2 scored; the others are M5-3's other classifiers,
scored here because M5 said M9 reports every classifier on the test set. The live choice is not
revisited on these numbers. --detector packages (M9-2's Isolation Forest and binary detector) are
scored again here so their margins over Model 0 are paired; with --m9-2-results the report checks that
they, and the live classifier, reproduce M9-2's published numbers.

Model 0 is scored with M9-2's own code (ml/m9_2_metrics.py) on its runs and windows. Margins are paired
(ml/m9_3_baseline.py). Nothing here fits or tunes anything. Model 0 is ml/model0.py as reviewed in M5-3,
fixed before the test set existed and not tuned; every result is reported whichever way it falls.
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
SLICE_KEY = {"headline": "headline", "all_test": "all-test"}   # M9-2's bootstrap keys, so its numbers reproduce
DEPENDENCY, CASCADE = "DEPENDENCY_FAILURE", m.CASCADE
MATCH_TOLERANCE = 1e-9


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


def share_margins(scored, eids, key, ml=b.CLASSIFIER_AS_DETECTOR):
    """{floor: {label or "healthy": paired share margin}}: `ml` minus each Model 0 floor."""
    return {base: b.paired_share_margins(scored, eids, ml, base, f"{key}/share_margin/{ml}/{base}")
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


def _forest(detectors):
    forests = [p.name for p in detectors if p.role == "iforest"]
    return forests[0] if len(forests) == 1 else None


def prepare(scored, classifiers, detectors):
    """Model 0, every classifier read as a detector, and the deployed rule, added to `scored`. Returns
    (flag_only, ml_names): the flag-only names, and every ML detector but the live classifier, in order."""
    live, others = classifiers[0].name, [p.name for p in classifiers[1:]]
    b.add_deployed(scored, _forest(detectors), live)
    flag_only = b.add_baselines(scored, live, others)
    names = [b.as_detector(n, live) for n in others] + [p.name for p in detectors]
    if m.DEPLOYED in scored.flags:
        names.append(m.DEPLOYED)
    return flag_only, names


def other_detection(scored, slices, names, flag_only):
    """Each ML detector but the live classifier: detection by M9-2's own call (so M9-2's detectors reproduce
    its published numbers), paired margins over both floors, and per-class and healthy share margins."""
    out = {"detection": {}, "margins": {}, "share_margins": {}}
    for s in ("headline", "all_test"):
        eids = slices[s]
        out["detection"][s], out["margins"][s] = {}, {}
        for name in names:
            block, _ = m.detection_block(scored, name, eids, SLICE_KEY[s])
            out["detection"][s][name] = _strip_ranking(block) if name in flag_only else block
            out["margins"][s][name] = {base: b.paired_margin(scored, eids, name, base, f"{s}/margin/{name}/{base}")
                                       for base in b.NAMES}
    out["share_margins"]["headline"] = {name: share_margins(scored, slices["headline"], "headline", name)
                                        for name in names}
    return out


def validation_scored(dataset, registry, classifiers, detectors=()):
    """Validation's runs scored the same way, with Model 0 added: each detector's own shift to the test set."""
    val = {r["experiment_id"]: {"eid": r["experiment_id"], "phase": "val", "label_type": r.get("incident_type")}
           for r in registry if r["phase"] == "val"}
    if not val:
        raise m.MetricsError("the registry has no validation runs")
    scored = m.score_runs(dataset, val, list(detectors), classifiers)
    flag_only, names = prepare(scored, classifiers, detectors)
    return scored, val, flag_only, names


def reference_cells(scored, val):
    """{(family, model, label_type): {recall, ci95, ...}} on validation's single-fault runs, for every detector
    and classifier: the per-band table's 'against validation' column."""
    out = {}
    for label_type in m.SINGLE_LABELS:
        eids = [e for e, t in val.items() if t["label_type"] == label_type]
        if not eids:
            continue
        for family, model in [("detection", n) for n in scored.flags] + [("classification", n) for n in scored.preds]:
            cell = m._cell(scored, family, model, eids, label_type, f"validation/{label_type}/{family}/{model}")
            out[(family, model, label_type)] = {k: cell[k] for k in ("recall", "ci95", "runs", "windows")}
    return out


def classifier_blocks(scored, val_scored, classifiers, slices, val_eids, cascades):
    """Six-class results for every classifier but the live one (whose blocks are the report's own
    `classification` and `validation.classification`), and every classifier's time to recognize a cascade."""
    other, ttr = {}, {}
    for i, p in enumerate(classifiers):
        ttr[p.name] = m._clean(m.time_to_recognize(scored, p.name, cascades))
        if i == 0:
            continue
        full = m.classification_block(scored, p.name, slices["all_test"], "all-test")
        other[p.name] = {"headline": m.classification_block(scored, p.name, slices["headline"], "headline"),
                         "all_test": {k: full[k] for k in ("runs", "windows", "accuracy", "macro_f1", "ci95")},
                         "validation": m.classification_block(val_scored, p.name, val_eids, "validation")}
    return other, ttr


def _differences(ours, theirs, path=""):
    """Paths where two JSON values differ by more than MATCH_TOLERANCE (numbers) or at all (anything else)."""
    if isinstance(ours, dict) and isinstance(theirs, dict):
        return [d for k in sorted(set(ours) | set(theirs))
                for d in (_differences(ours.get(k), theirs.get(k), f"{path}/{k}") if k in ours and k in theirs
                          else [f"{path}/{k}"])]
    if isinstance(ours, list) and isinstance(theirs, list):
        if len(ours) != len(theirs):
            return [path]
        return [d for i, (x, y) in enumerate(zip(ours, theirs)) for d in _differences(x, y, f"{path}[{i}]")]
    if isinstance(ours, (int, float)) and isinstance(theirs, (int, float)) and not isinstance(ours, bool):
        return [] if abs(ours - theirs) <= MATCH_TOLERANCE else [path]
    return [] if ours == theirs else [path]


def check_against_m9_2(report, path):
    """Whether this run reproduces M9-2's published headline numbers: its detectors' detection blocks, the live
    classifier's classification block and confounder shares. A name this run did not score is None."""
    data = json.loads(Path(path).expanduser().read_text())
    live = report["classifier_name"]
    ours_det = report["other_models"]["detection"]["headline"]
    out = {"detection": {}, "classification": None, "confounders": None}
    for name, blocks in (data.get("detection") or {}).items():
        mine = ours_det.get(name)
        out["detection"][name] = None if mine is None else _differences(
            {k: mine[k] for k in ("counts", "metrics", "ci95", "latency")},
            {k: blocks["headline"][k] for k in ("counts", "metrics", "ci95", "latency")})
    theirs = (data.get("classification") or {}).get(live)
    if theirs:
        keys = ("accuracy", "macro_f1", "ci95", "per_class", "confusion")
        out["classification"] = _differences({k: report["classification"]["headline"][k] for k in keys},
                                             {k: theirs["headline"][k] for k in keys})
    theirs = ((data.get("confounders") or {}).get("by_classifier") or {}).get(live)
    if theirs and report.get("confounders"):
        out["confounders"] = _differences(report["confounders"]["by_classifier"][live]["both_active"],
                                          theirs["both_active"])
    return out


def build_report(dataset, queues, classifiers, detectors=(), seed=m.SEED, resamples=m.BOOTSTRAP, allow_partial=False,
                 repo_root=None, m9_2_results=None, commit=None):
    if not classifiers:
        raise m.MetricsError("give at least one classifier: the live one (the one M9-2 scored) first")
    names = [p.name for p in [*classifiers, *detectors]]
    if len(set(names)) != len(names) or m.DEPLOYED in names or any(n.endswith(b.AS_DETECTOR) for n in names):
        raise m.MetricsError(f"package names must be distinct, not {m.DEPLOYED!r} and not end in {b.AS_DETECTOR!r}")
    m.CONFIG.update(seed=seed, resamples=resamples)
    detectors = list(detectors)
    package = classifiers[0]
    classifier = package.name
    registry = m.load_registry(dataset)
    split = m.check_split(registry, allow_partial)
    meta = m.tag_runs(registry, m.load_specs(queues))
    scored = m.score_runs(dataset, meta, detectors, classifiers)
    flag_only, ml_names = prepare(scored, classifiers, detectors)

    test = [e for e, t in meta.items() if t["phase"] == "test"]
    headline = [e for e in test if meta[e]["kind"] not in m.HEADLINE_EXCLUDES]
    stress = [e for e, t in meta.items() if t["phase"] == "stress"]
    slices = {"headline": headline, "all_test": test}
    if stress:
        slices["stress"] = stress
    cascades = [e for e in headline if meta[e]["kind"] == "cascade"]

    val_scored, val_meta, val_flag_only, val_names = validation_scored(dataset, registry, classifiers, detectors)
    val_eids = sorted(val_meta)
    reference = reference_cells(val_scored, val_meta)
    other_classifiers, ttr = classifier_blocks(scored, val_scored, classifiers, slices, val_eids, cascades)
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
            "packages": {p.name: m.package_summary(p, environment) for p in [*classifiers[1:], *detectors]},
            "git": {**m.git_state(repo_root or Path(__file__).resolve().parents[2]), **({"commit": commit} if commit else {})},
            "environment": environment,
            "model0": {"p95_ratio": model0.RATIO, "error_limit": model0.ERROR_LIMIT,
                       "earlier_windows": model0.EARLIER, "tuned": False,
                       "floors": [b.PREVIOUS, b.EARLIER]},
        },
        "detection": {s: detection_for(scored, eids, s, flag_only) for s, eids in slices.items()},
        "margins": {s: margins(scored, eids, s) for s, eids in slices.items()},
        "share_flagged": {s: share_flagged(scored, eids, s) for s, eids in slices.items()},
        "share_margins": {s: share_margins(scored, slices[s], s) for s in ("headline", "all_test")},
        "classification": {"headline": m.classification_block(scored, classifier, headline, "headline"),
                           "all_test": m.classification_block(scored, classifier, test, "all-test")},
        "validation": {"runs": len(val_eids), "windows": len(val_scored.rows),
                       "detection": detection_for(val_scored, val_eids, "validation", val_flag_only),
                       "classification": m.classification_block(val_scored, classifier, val_eids, "validation"),
                       "other_models": validation_other(val_scored, val_eids, val_names, val_flag_only)},
        "other_models": {**other_detection(scored, slices, ml_names, flag_only), "names": ml_names,
                         "classification": other_classifiers},
        "time_to_recognize_cascade": ttr,
        "confounders": m.confounder_block(scored),
        "per_band": m.band_table(scored, reference),
        "extreme_slice": {"runs": len(stress), "by_injector": m._per_injector(scored, stress, "stress")} if stress else None,
        "varied_duration": m.varied_duration_block(scored),
        "classifier_name": classifier,
        "classifier_names": [p.name for p in classifiers],
        "detector_names": [p.name for p in detectors],
    }
    # M9-2's rule: a detector flagging nearly every healthy window can't tell them from faults.
    report["uninformative_detectors"] = {
        n: blk["metrics"]["fpr_healthy"] for n, blk in report["other_models"]["detection"]["headline"].items()
        if (blk["metrics"]["fpr_healthy"] or 0) >= m.UNINFORMATIVE_SHARE}
    if m9_2_results:
        report["m9_2_check"] = check_against_m9_2(report, m9_2_results)
    return m._clean(report)


def validation_other(val_scored, val_eids, names, flag_only):
    """Validation detection for each ML detector but the live classifier."""
    out = {}
    for name in names:
        block, _ = m.detection_block(val_scored, name, val_eids, "validation")
        out[name] = _strip_ranking(block) if name in flag_only else block
    return out


# --- markdown ---------

def _n(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _ci(value, ci, digits=3):
    if value is None:
        return "n/a"
    if not ci:
        return f"{value:.{digits}f}"
    return f"{value:.{digits}f} [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


def _signed(value, ci, digits=3):
    if value is None:
        return "n/a"
    text = f"{value:+.{digits}f}"
    return text + (f" [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]" if ci else "")


def _table(header, rows):
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


def label_of(name, report):
    """A detector's name for the tables."""
    if name in NAME:
        return NAME[name]
    if name.endswith(b.AS_DETECTOR):
        clf = name[: -len(b.AS_DETECTOR)]
        est = report["provenance"]["packages"].get(clf, {}).get("estimator")
        return f"{clf} ({est}), read as a detector" if est else f"{clf}, read as a detector"
    if name == m.DEPLOYED:
        return "deployed rule (forest AND live classifier)"
    est = report["provenance"]["packages"].get(name, {}).get("estimator")
    return f"{name} ({est})" if est else name


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


def _margin_table(mg, sm):
    rows = []
    for label, key in (("Precision", "precision"), ("Recall", "recall"), ("F1", "f1"),
                       ("False-positive rate, against the labels (lower is better)", "fpr")):
        rows.append([label] + [_signed(mg[base]["difference"][key], mg[base]["ci95"][key]) for base in (b.EARLIER, b.PREVIOUS)])
    rows.append(["Healthy windows flagged (lower is better)"] + [
        _signed(sm[base][b.HEALTHY]["difference"], sm[base][b.HEALTHY]["ci95"]) for base in (b.EARLIER, b.PREVIOUS)])
    return _table(["Classifier minus Model 0", "against the earlier-mean floor", "against the previous-window floor"], rows)


def _cells_table(by_injector, classifier):
    rows = []
    for injector, cells in by_injector.items():
        row = [injector, str(cells["runs"])]
        row += [_ci(cells[f"detection:{n}"]["recall"], cells[f"detection:{n}"]["ci95"]) for n in MODELS]
        row.append(_ci(cells[f"classification:{classifier}"]["recall"], cells[f"classification:{classifier}"]["ci95"]))
        rows.append(row)
    return _table(["Fault", "Runs"] + [NAME[n] for n in MODELS] + ["classifier names the class"], rows)


def _ttr(entry):
    s = (entry or {}).get("summary") or {}
    if not s.get("runs"):
        return "n/a"
    return f"{_n(s.get('median'), 1)} s, max {_n(s.get('max'), 1)} s, never {s.get('never')} of {s.get('runs')}"


def _shares(conf, name):
    c = ((conf or {}).get("by_classifier") or {}).get(name, {}).get("both_active")
    if not c:
        return "n/a"
    return " / ".join(_n(c[k]["share"]) for k in ("either_fault_type", "labeled_first_type", "cascading_failure"))


def _classifier_rows(report):
    live = report["classifier_name"]
    rows = []
    for name in report["classifier_names"]:
        if name == live:
            val, test = report["validation"]["classification"], report["classification"]["headline"]
            est = report["provenance"]["classifier"].get("estimator")
        else:
            blocks = report["other_models"]["classification"][name]
            val, test = blocks["validation"], blocks["headline"]
            est = report["provenance"]["packages"][name].get("estimator")
        rows.append([f"{name} ({est}){', live' if name == live else ''}",
                     _n(val["accuracy"]), _ci(test["accuracy"], test["ci95"].get("accuracy")),
                     _n(val["macro_f1"]), _ci(test["macro_f1"], test["ci95"].get("macro_f1")),
                     _n(test["per_class"].get(DEPENDENCY, {}).get("recall")),
                     _n(test["per_class"].get(CASCADE, {}).get("precision")),
                     _ttr(report["time_to_recognize_cascade"].get(name)), _shares(report.get("confounders"), name)])
    rows.append(["Model 0 (either floor)"] + ["n/a"] * 6 + ["n/a: it cannot name a cascade", "n/a: it names no class"])
    return rows


def _every_detector_rows(report):
    head = report["detection"]["headline"]
    mg, sm = report["margins"]["headline"], report["share_margins"]["headline"]
    entries = [(b.CLASSIFIER_AS_DETECTOR, head[b.CLASSIFIER_AS_DETECTOR], mg[b.EARLIER], sm[b.EARLIER])]
    other = report["other_models"]
    for name in other["names"]:
        entries.append((name, other["detection"]["headline"][name], other["margins"]["headline"][name][b.EARLIER],
                        other["share_margins"]["headline"][name][b.EARLIER]))
    rows = []
    for name, blk, margin, share in entries:
        mt, ci = blk["metrics"], blk["ci95"]
        rows.append([label_of(name, report) + (" (flags every healthy window)" if name in report["uninformative_detectors"] else ""),
                     _ci(mt["precision"], ci.get("precision")), _ci(mt["recall"], ci.get("recall")),
                     _ci(mt["fpr"], ci.get("fpr")), _n(mt["fpr_healthy"]),
                     _signed(margin["difference"]["recall"], margin["ci95"]["recall"]),
                     _signed(margin["difference"]["fpr"], margin["ci95"]["fpr"]),
                     _signed(share[b.HEALTHY]["difference"], share[b.HEALTHY]["ci95"])])
    em = head[b.EARLIER]["metrics"]
    rows.append([NAME[b.EARLIER], _ci(em["precision"], head[b.EARLIER]["ci95"].get("precision")),
                 _ci(em["recall"], head[b.EARLIER]["ci95"].get("recall")), _ci(em["fpr"], head[b.EARLIER]["ci95"].get("fpr")),
                 _n(em["fpr_healthy"]), "(the floor)", "(the floor)", "(the floor)"])
    return rows


def render_markdown(report):
    p, c = report["provenance"], report["classifier_name"]
    head, mg = report["detection"]["headline"], report["margins"]["headline"]
    sm = report["share_margins"]["headline"]
    cls = report["classification"]["headline"]
    others = [n for n in report["classifier_names"] if n != c]
    conf = report.get("confounders") or {}
    L = ["# M9-3 The threshold baseline beside the test-set results", "", "## Read this first", "",
         "- **Model 0 is a rule.** It says anomaly or not, with no class and no score, so accuracy, macro-F1, ROC AUC, "
         "average precision and recall at a fixed false-alarm rate are n/a for it. It is compared on the anomaly view "
         "(anything but NORMAL is an anomaly). Each classifier is read the same way (`read as a detector`) "
         "for a like-for-like margin.",
         f"- **Fixed before the test set existed.** Model 0 is `ml/model0.py` as reviewed in M5-3: the rule trips on "
         f"p95 above {model0.RATIO}x its baseline or a 5xx share above {model0.ERROR_LIMIT:.0%}, against the previous "
         f"window or the mean of the last {model0.EARLIER} earlier windows. Not tuned. M9-2 had already scored and seen "
         "the test set, so every result is reported whichever way it falls.",
         f"- **Every classifier M5-3 trained is scored.** The live one (`{c}`) first"
         + (f", then {', '.join(f'`{n}`' for n in others)}" if others else "")
         + ". The live one was chosen on validation (M5-3); that choice is not revisited on these numbers.",
         "- **M9-2's detectors are scored again here**"
         + (f" ({', '.join(f'`{n}`' for n in report['detector_names'])}, and the deployed rule)" if report["detector_names"] else
            " only when given (none were)")
         + ", so their margins over Model 0 are paired. Section 10 checks them against M9-2's published numbers.",
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
         f"- **Live classifier:** {p['classifier'].get('estimator')} ({c}), job `{p['classifier'].get('job')}`.",
         *[f"- **`{n}`:** {q.get('estimator')}, job `{q.get('job')}`." for n, q in p["packages"].items()],
         f"- **Code:** commit `{p['git'].get('commit')}` (dirty: {p['git'].get('dirty')}); generated {p['generated_at']}.", "",
         "## 1. Detection beside the baseline (headline)", ""]
    L += _det_table(head)
    L += ["", "The latency median covers only the incident runs a detector flagged at all; the last row counts the runs it "
              "never flagged, which a median would hide.", "",
          f"Classification (the live classifier alone; Model 0 cannot name a class): accuracy "
              f"{_ci(cls['accuracy'], cls['ci95'].get('accuracy'))}, macro-F1 {_ci(cls['macro_f1'], cls['ci95'].get('macro_f1'))}; "
              "Model 0: n/a.", "", "### The margin", ""]
    L += _margin_table(mg, sm)
    L += ["", "A positive recall difference means the classifier finds anomalous windows Model 0 misses. A positive "
              "false-positive-rate or healthy-window difference means the classifier raises more false alarms. Healthy "
              "windows are the NORMAL windows with no fault active: the false alarms an operator would see.", "",
          "## 2. Share of each class flagged (headline)", ""]
    shares = report["share_flagged"]["headline"]
    rows = []
    for label, cell in shares.items():
        names_it = cls["per_class"].get(label, {})
        margin = sm[b.EARLIER][label]
        rows.append([label + (" (= false-alarm rate)" if label == "NORMAL" else ""), str(cell["windows"])]
                    + [_ci(cell[n]["share"], cell[n]["ci95"]) for n in MODELS]
                    + [_signed(margin["difference"], margin["ci95"]), _n(names_it.get("recall")), _n(names_it.get("precision"))])
    healthy = sm[b.EARLIER][b.HEALTHY]
    rows.append(["healthy windows (NORMAL, no fault active)", str(healthy["windows"]),
                 _n(sm[b.PREVIOUS][b.HEALTHY]["baseline"]), _n(healthy["baseline"]), _n(healthy["ml"]),
                 _signed(healthy["difference"], healthy["ci95"]), "", ""])
    L += _table(["Label", "Windows"] + [NAME[n] for n in MODELS]
                + ["paired margin over the earlier-mean floor", "classifier recall (names it)", "classifier precision"], rows)
    L += ["", "The margin column is the classifier's share minus the earlier-mean floor's, both resampled on the same "
              "runs. For `CASCADING_FAILURE` it is the cascade margin. The classifier's precision for that class is low "
              "because dependency failures are called cascades (M9-2).", "",
          "## 3. Every ML model beside Model 0 (headline)", ""]
    L += _table(["Model", "Precision", "Recall", "False-positive rate", "Healthy windows flagged",
                 "Recall margin over the earlier-mean floor", "FPR margin", "Healthy-window margin"], _every_detector_rows(report))
    L += ["", "Margins are the model minus the earlier-mean floor, paired. A detector that flags every healthy window "
              "has recall and latency that follow from flagging everything, not from detecting faults. The deployed "
              "rule equals the live classifier read as a detector wherever the forest flags every window.", "",
          "## 4. Every classifier, six classes (headline)", ""]
    L += _table(["Classifier", "Accuracy, validation", "Accuracy, test", "Macro-F1, validation", "Macro-F1, test",
                 "Dependency-failure recall", "Cascade precision", "Time to recognize a cascade",
                 "Confounders: either type / labeled type / cascade"], _classifier_rows(report))
    L += ["", "Time to recognize: seconds from the moment two faults are first active together to the end of the first "
              f"labeled window called `CASCADING_FAILURE` (ml/evaluate.py), over the headline's cascade runs. Confounders: "
              f"over the {conf.get('windows_both_active', 'n/a')} windows of the {conf.get('runs', 'n/a')} confounder runs "
              "with both faults active (M9-2's definition).", "",
          "## 5. Validation to test: the shift in each detector", ""]
    rows = []
    for n in MODELS:
        v, t = report["validation"]["detection"][n], head[n]
        rows.append([NAME[n], _ci(v["metrics"]["recall"], v["ci95"].get("recall")), _ci(t["metrics"]["recall"], t["ci95"].get("recall")),
                     _ci(v["metrics"]["fpr"], v["ci95"].get("fpr")), _ci(t["metrics"]["fpr"], t["ci95"].get("fpr"))])
    for n in report["other_models"]["names"]:
        v, t = report["validation"]["other_models"][n], report["other_models"]["detection"]["headline"][n]
        rows.append([label_of(n, report), _ci(v["metrics"]["recall"], v["ci95"].get("recall")),
                     _ci(t["metrics"]["recall"], t["ci95"].get("recall")),
                     _ci(v["metrics"]["fpr"], v["ci95"].get("fpr")), _ci(t["metrics"]["fpr"], t["ci95"].get("fpr"))])
    L += _table(["Detector", "Recall, validation", "Recall, test", "FPR, validation", "FPR, test"], rows)
    val = report["validation"]["classification"]
    L += ["", f"Live classifier, six-class: accuracy {_ci(val['accuracy'], val['ci95'].get('accuracy'))} on validation and "
              f"{_ci(cls['accuracy'], cls['ci95'].get('accuracy'))} on test; macro-F1 "
              f"{_ci(val['macro_f1'], val['ci95'].get('macro_f1'))} and {_ci(cls['macro_f1'], cls['ci95'].get('macro_f1'))}. "
              "Validation tuned the packages, so it is the optimistic side of any gap.", "",
          "## 6. Per held-out band", ""]
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
    L += ["", "Model 0 and the live classifier only; the JSON's `per_band` has every model."]
    if report["extreme_slice"]:
        L += ["", "## 7. Extreme slice (reported apart)", ""] + _cells_table(report["extreme_slice"]["by_injector"], c)
    L += ["", "## 8. Varied durations", ""]
    for duration, block in report["varied_duration"].items():
        L += [f"**{duration} s faults ({block['runs']} runs)**", ""] + _cells_table(block["by_injector"], c) + [""]
    L += ["## 9. Confounders, read as detection", ""]
    if conf:
        rows = [[label_of(n, report), _ci(d["flagged_share"], d["ci95"])] for n, d in conf["detection"].items()]
        L += [f"Share of the {conf['windows_both_active']} both-faults-active windows ({conf['runs']} runs) each detector "
              "flags. A rule names no class, so the naming shares in section 4 have no Model 0 counterpart.", ""]
        L += _table(["Detector", "Flagged share"], rows)
    else:
        L += ["No confounder runs were scored."]
    L += ["", "## What Model 0 cannot be compared on", "",
          "- **Confounder naming shares** (either fault's type, the labeled first type, `CASCADING_FAILURE`): a rule "
          "names no class. The headline leaves confounders out; the JSON's `all_test` slice includes them.",
          "- **Accuracy, macro-F1, per-class precision and time to recognize a cascade** all need a class name, so they "
          "are n/a for Model 0. The classifiers' values are in section 4.",
          "- **ROC AUC, average precision and recall at a fixed false-alarm rate** need a score, and a rule has none.", ""]
    check = report.get("m9_2_check")
    L += ["## 10. Against M9-2's published numbers", ""]
    if check:
        rows = [[f"`{n}` detection, headline", "not scored here" if d is None else ("matches" if not d else f"differs: {', '.join(d[:5])}")]
                for n, d in check["detection"].items()]
        for what, key in ((f"`{c}` classification, headline", "classification"), (f"`{c}` confounder shares", "confounders")):
            d = check[key]
            rows.append([what, "not checked" if d is None else ("matches" if not d else f"differs: {', '.join(d[:5])}")])
        L += _table(["M9-2 result", f"This run (tolerance {MATCH_TOLERANCE:g})"], rows)
    else:
        L += ["Not checked (no `--m9-2-results`)."]
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
    parser.add_argument("--classifier", action="append", default=[], metavar="NAME=DIR",
                        help="a six-class package; the first is the live one (the one M9-2 scored)")
    parser.add_argument("--detector", action="append", default=[], metavar="NAME=DIR",
                        help="an Isolation Forest or binary package M9-2 scored, scored again for paired margins")
    parser.add_argument("--job", action="append", default=[], metavar="NAME=JOB", help="the training job a package came from")
    parser.add_argument("--m9-2-results", default=None, help="M9-2's metrics_m9_2.json, to check its numbers reproduce")
    parser.add_argument("--out", required=True, help=f"write {OUT_JSON} and {OUT_MD} here")
    parser.add_argument("--seed", type=int, default=m.SEED)
    parser.add_argument("--bootstrap", type=int, default=m.BOOTSTRAP)
    parser.add_argument("--commit", default=None, help="the commit this ran from, where git is not available (a container)")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite an earlier result")
    args = parser.parse_args(argv)
    classifiers = m._pairs(parser, args.classifier, "--classifier")
    detectors = m._pairs(parser, args.detector, "--detector")
    if not classifiers:
        parser.error("give at least one --classifier (the live one first)")
    jobs = m._pairs(parser, args.job, "--job", as_path=False)
    unknown = sorted(set(jobs) - set(classifiers) - set(detectors))
    if unknown:
        parser.error(f"--job names no package: {unknown}")
    out = Path(args.out).expanduser()
    if (out / OUT_JSON).exists() and not args.force:
        print(f"error: {out} already holds a result (--force to overwrite)", file=sys.stderr)
        return 1
    try:
        report = build_report(
            args.dataset, args.queues,
            [m.load_package(n, d, "classifier", job=jobs.get(n)) for n, d in classifiers.items()],
            [m.load_package(n, d, "detector", job=jobs.get(n)) for n, d in detectors.items()],
            seed=args.seed, resamples=args.bootstrap, allow_partial=args.allow_partial,
            m9_2_results=args.m9_2_results, commit=args.commit)
    except (m.MetricsError, evaluate.EvalError, m.TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    for path in write_outputs(report, out):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
