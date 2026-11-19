"""M9-2: detection + classification metrics on the held-out test set.

    cd AnomalyPulse
    python -m ml.m9_2_metrics --dataset ~/v1 \\
        --classifier incident_type=~/pkgs/incident_type \\
        --detector iforest=~/pkgs/iforest --detector binary=~/pkgs/binary \\
        --out ~/m9-2-results [--with-validation-reference]

--dataset is a local copy of `datasets/v1/` (tier1/features, tier2/labels, tier2/experiments.jsonl).
Each --classifier / --detector is a folder holding model.joblib and model.json, the same packages
ml/evaluate.py scores on validation (run in the training image's library versions).

What it reports (Milestones/M09_Evaluation.md, M9-2). Everything is from the test set, slots 701-1050,
except the extreme slice (slots 1051+, phase `stress`) which is its own section:

    headline      test runs except confounders (HEADLINE_EXCLUDES): detection (precision, recall,
                  F1, FPR, fpr_healthy, fault_start_flagged, latency) and classification (accuracy,
                  macro-F1, per-class precision/recall, confusion matrix), each with a 95% CI from a
                  bootstrap over RUNS (the windows of one run are not independent).
    per band      recall for each single-fault injector in each held-out test band, against the
                  injector's other test band and (optionally) against validation's training bands.
    extreme slice slots 1051+, never in the headline.
    confounders   over windows with both faults active: share predicted as either fault's type, as
                  the labeled (first) type, and as CASCADING_FAILURE.
    varied length 2- and 12-minute faults.

Definitions are the validation ones: ml/job/detection.py (window-level; anything but NORMAL is
ANOMALY; latency from the fault's onset to the end of the first flagged labeled window) and
ml/compare.py (six-class view). Nothing here fits or tunes anything: the Isolation Forest's cut-off
is the package's, set on validation. The test set is scored once: the output files are not
overwritten without --force.
"""
import argparse
import json
import platform
import subprocess
import sys
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "job"))
import detection  # noqa: E402
import train  # noqa: E402
from train import TrainError  # noqa: E402

from ml import compare, evaluate  # noqa: E402
from ml.evaluate import EvalError  # noqa: E402

try:  # the single source of truth, when run from AnomalyPulse/
    from simulator.integrity import INJECTOR_FOR as _INJECTOR_FOR
except ImportError:  # pragma: no cover
    _INJECTOR_FOR = None

NORMAL = detection.NORMAL
CASCADE = compare.CASCADE

SEED = 20261118
BOOTSTRAP = 1000
TEST_FIRST, TEST_LAST, STRESS_FIRST = 701, 1050, 1051
LAST_TRAINING_DAY = "2026-10-25"
STANDARD_DURATION_S = 300
HALF_WINDOW_MS = 30_000             # labels/policy.py: a fault labels a window at >= 30 s of it
HEADLINE_EXCLUDES = ("confounder",)  # a test-only probe with no clean single label; reported apart
DEGRADATION_POINTS = 0.10           # a band this far below its reference is flagged (band_status)
OUT_JSON, OUT_MD = "metrics_m9_2.json", "metrics_m9_2.md"
QUEUES = ("v1-test-1", "v1-test-2", "v1-test-3", "v1-stress")
CHUNK = 200
CONFIG = {"seed": SEED, "resamples": BOOTSTRAP}   # build_report sets these; every bootstrap reads them

# injector -> the label its windows get (integrity.INJECTOR_FOR, reversed, minus noop).
INJECTOR_LABEL = {"traffic-spike": "TRAFFIC_SPIKE", "lambda-degradation": "LAMBDA_DEGRADATION",
                  "db-throttling": "DATABASE_THROTTLING", "payment-failure": "DEPENDENCY_FAILURE"}
if _INJECTOR_FOR is not None:
    INJECTOR_LABEL = {inj: label for label, inj in _INJECTOR_FOR.items() if inj != "noop"}
SINGLE_LABELS = tuple(INJECTOR_LABEL.values())


class MetricsError(Exception):
    """Bad input: the evaluation stops with this message."""


# --- registry, queue specs, run tags ---

def load_registry(dataset):
    path = Path(dataset) / "tier2" / "experiments.jsonl"
    if not path.is_file():
        raise MetricsError(f"{path} not found: sync tier2/experiments.jsonl from datasets/v1/")
    records = train.read_jsonl(path)
    for name in ("slot", "phase", "experiment_id", "generation_date"):
        bad = [r for r in records if not r.get(name)]
        if bad:
            raise MetricsError(f"experiments.jsonl: {len(bad)} record(s) without {name}")
    for name in ("slot", "experiment_id"):
        dup = sorted(k for k, n in Counter(r[name] for r in records).items() if n > 1)
        if dup:
            raise MetricsError(f"experiments.jsonl: duplicate {name}: {dup[:5]}")
    return records


def phase_of(slot):
    if slot <= 560:
        return "train"
    if slot < TEST_FIRST:
        return "val"
    return "test" if slot <= TEST_LAST else "stress"


def check_split(records, allow_partial=False):
    """Raise MetricsError unless the test and stress runs are what M9-1 says: each phase agrees with
    its slot (test = 701-1050, all 350 unless allow_partial; stress = 1051 on) and every test and
    stress run was generated after training's last day."""
    problems = []
    last = LAST_TRAINING_DAY.replace("-", "")
    for r in records:
        if phase_of(r["slot"]) != r["phase"]:
            problems.append(f"slot {r['slot']} is {phase_of(r['slot'])}, its record says {r['phase']!r}")
        if r["phase"] in ("test", "stress") and str(r["generation_date"]).replace("-", "") <= last:
            problems.append(f"{r['experiment_id']} (slot {r['slot']}) was generated {r['generation_date']}, "
                            f"not after {LAST_TRAINING_DAY}")
    test_slots = {r["slot"] for r in records if r["phase"] == "test"}
    missing = sorted(set(range(TEST_FIRST, TEST_LAST + 1)) - test_slots)
    if missing and not allow_partial:
        problems.append(f"{len(missing)} test slot(s) missing from the registry, e.g. {missing[:5]} "
                        f"(--allow-partial to score what is there)")
    if problems:
        raise MetricsError("the registry is not the M9-1 test set:\n  " + "\n  ".join(problems[:12]))
    return {"test_runs": len(test_slots), "stress_runs": sum(r["phase"] == "stress" for r in records),
            "missing_test_slots": len(missing), "first_missing": missing[:5]}


def load_specs(queue_dir):
    """{slot: queue entry} for the test and stress queues."""
    specs = {}
    for name in QUEUES:
        path = Path(queue_dir) / f"{name}.json"
        if not path.is_file():
            raise MetricsError(f"{path} not found (--queues)")
        for entry in json.loads(path.read_text())["experiments"]:
            specs[entry["slot"]] = entry
    return specs


def tag_run(entry, record):
    """What a run is, from its queue entry: kind (normal, single, cascade, confounder), the label
    its fault windows get, and for singles the injector, band and duration."""
    tag = {"eid": record["experiment_id"], "slot": record["slot"], "phase": record["phase"],
           "duration_s": STANDARD_DURATION_S}
    if "plan" in entry:
        plan = entry["plan"]
        types = [INJECTOR_LABEL[f["injector"]] for f in plan["faults"]]
        tag.update(kind="confounder", label_type=plan["incident_type"], first_type=plan["incident_type"],
                   fault_types=types, pair="+".join(types))
    elif "cascade" in entry:
        tag.update(kind="cascade", label_type=CASCADE, stages=[s["injector"] for s in entry["cascade"]["stages"]])
    else:
        incident = entry["incident"]
        tag["duration_s"] = incident["duration_s"]
        if incident["injector"] == "noop":
            tag.update(kind="normal", label_type=NORMAL)
        else:
            band = incident["band"]
            tag.update(kind="single", injector=incident["injector"], label_type=INJECTOR_LABEL[incident["injector"]],
                       band=(band["low"], band["high"]), intensity=incident.get("intensity"),
                       target=incident.get("target"))
    return tag


def tag_runs(records, specs):
    meta = {}
    for r in records:
        if r["phase"] not in ("test", "stress"):
            continue
        entry = specs.get(r["slot"])
        if entry is None:
            raise MetricsError(f"slot {r['slot']} ({r['experiment_id']}) is in the registry but in no queue file")
        if entry.get("phase") != r["phase"]:
            raise MetricsError(f"slot {r['slot']}: the queue says {entry.get('phase')!r}, the registry {r['phase']!r}")
        meta[r["experiment_id"]] = tag_run(entry, r)
    return meta


# --- packages, loading, scoring ---

@dataclass
class Package:
    name: str
    role: str       # "classifier" (six classes), "iforest" or "binary" (the two detectors)
    model: object
    meta: dict


def package_role(meta):
    if meta.get("kind") == "detector":
        return "iforest"
    return "binary" if meta.get("task") == "anomaly_binary" else "classifier"


def load_package(name, directory, wanted):
    """wanted: "classifier" for a six-class package, "detector" for an Isolation Forest or binary one."""
    model, meta = evaluate.load_package(directory)
    role = package_role(meta)
    if (role == "classifier") != (wanted == "classifier"):
        raise MetricsError(f"{name}: {directory} is a {meta.get('task')} package, not a "
                           f"{'six-class classifier' if wanted == 'classifier' else 'detector'}")
    return Package(name, role, model, meta)


def load_runs(dataset, eids):
    """[(experiment_id, feature_rows, label_rows)] for exactly these runs. Reads nothing else."""
    runs = []
    for eid in sorted(eids):
        paths = {part: Path(dataset) / train.TREES[part] / f"experiment_id={eid}" / train.FILES[part]
                 for part in train.TREES}
        missing = [p for p in paths.values() if not p.is_file()]
        if missing:
            raise MetricsError(f"run {eid}: {missing[0]} not found")
        runs.append((eid, train.read_jsonl(paths["features"]), train.read_jsonl(paths["labels"])))
    return runs


def feature_matrix(rows, meta, name):
    columns = meta["columns"]
    expected = set(columns) | {train.KEY, train.LABEL}
    X = []
    for eid, row in rows:
        if set(row) != expected:
            raise MetricsError(f"{name}: run {eid}, window {row[train.KEY]}: the package's columns differ "
                               f"from the features'")
        if any(row[c] is None for c in columns):
            raise MetricsError(f"{name}: run {eid}, window {row[train.KEY]}: empty feature cells")
        X.append([row[c] for c in columns])
    return np.asarray(X, dtype=float)


@dataclass
class Scored:
    runs: list
    rows: list                    # [(experiment_id, row)], labeled windows only
    meta: dict                    # experiment_id -> tag
    evidence: dict                # (experiment_id, window_start) -> label row
    classes: list
    labels: list
    flags: dict = field(default_factory=dict)           # detector -> np.ndarray[bool]
    anomaly_scores: dict = field(default_factory=dict)  # detector -> np.ndarray[float], higher = more anomalous
    preds: dict = field(default_factory=dict)           # classifier -> [class name]


def score_runs(dataset, meta, detectors, classifiers):
    runs = load_runs(dataset, meta)
    rows = train.training_rows(runs)
    if not rows:
        raise MetricsError("the runs have no labeled windows")
    classes = sorted({row[train.LABEL] for _, row in rows}.union(*[set(p.meta["classes"]) for p in classifiers]))
    scored = Scored(runs=runs, rows=rows, meta=meta, classes=classes, labels=[row[train.LABEL] for _, row in rows],
                    evidence={(eid, r["window_start"]): r for eid, _, labels in runs for r in labels})
    for p in detectors:
        X = feature_matrix(rows, p.meta, p.name)
        if p.role == "iforest":
            raw = p.model.score_samples(X)
            scored.flags[p.name] = np.asarray(raw < p.meta["threshold"], bool)
            scored.anomaly_scores[p.name] = -np.asarray(raw, float)
        else:
            names = evaluate.predict(p.model, p.meta, rows)
            scored.flags[p.name] = np.asarray([n == detection.ANOMALY for n in names], bool)
            column = p.meta["classes"].index(detection.ANOMALY)
            scored.anomaly_scores[p.name] = np.asarray(p.model.predict_proba(X)[:, column], float)
    for p in classifiers:
        scored.preds[p.name] = evaluate.predict(p.model, p.meta, rows)
    return scored


# --- slices and the bootstrap over runs ---

@dataclass
class Sub:
    eids: list
    idx: np.ndarray     # positions in scored.rows
    runs: list
    rows: list
    labels: list
    group: np.ndarray   # run position of each row

    @property
    def R(self):
        return len(self.eids)


def subset(scored, eids):
    wanted = sorted(set(eids))
    pos = {e: i for i, e in enumerate(wanted)}
    idx = np.asarray([i for i, (e, _) in enumerate(scored.rows) if e in pos], dtype=int)
    rows = [scored.rows[i] for i in idx]
    return Sub(eids=wanted, idx=idx, runs=[r for r in scored.runs if r[0] in pos], rows=rows,
               labels=[row[train.LABEL] for _, row in rows],
               group=np.asarray([pos[e] for e, _ in rows], dtype=int))


def _div(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return np.divide(a, b, out=np.full(np.broadcast(a, b).shape, np.nan), where=b > 0)


def det_stats(sums):
    """precision, recall, f1, fpr from summed (tp, fp, fn, tn); nan where undefined."""
    tp, fp, fn, tn = (sums[..., i] for i in range(4))
    return {"precision": _div(tp, tp + fp), "recall": _div(tp, tp + fn),
            "f1": _div(2 * tp, 2 * tp + fp + fn), "fpr": _div(fp, fp + tn)}


def clf_stats(sums):
    """accuracy and macro-F1 (over classes in the truth or the predictions) from summed confusions."""
    tp = np.diagonal(sums, axis1=-2, axis2=-1)
    support, predicted = sums.sum(axis=-1), sums.sum(axis=-2)
    present = (support + predicted) > 0
    f1 = np.where(present, _div(2 * tp, support + predicted), 0.0)
    return {"accuracy": _div(tp.sum(axis=-1), sums.sum(axis=(-2, -1))),
            "macro_f1": _div(f1.sum(axis=-1), present.sum(axis=-1))}


def ratio_stats(sums):
    return {"ratio": _div(sums[..., 0], sums[..., 1])}


def bootstrap(per_run, stat_fn, key):
    """{stat: [lo, hi]}: 95% percentile CIs from resampling whole runs. `per_run` has one row per run;
    stat_fn maps resample sums to arrays. The generator is seeded from (seed, key), so a number does
    not depend on what else was computed before it."""
    per_run, seed, n = np.asarray(per_run), CONFIG["seed"], CONFIG["resamples"]
    if per_run.shape[0] < 2:
        return {}
    rng = np.random.default_rng([seed, zlib.crc32(key.encode())])
    draws = defaultdict(list)
    for start in range(0, n, CHUNK):
        idx = rng.integers(0, per_run.shape[0], size=(min(CHUNK, n - start), per_run.shape[0]))
        for name, values in stat_fn(per_run[idx].sum(axis=1)).items():
            draws[name].append(values)
    out = {}
    for name, parts in draws.items():
        values = np.concatenate(parts)
        values = values[np.isfinite(values)]
        out[name] = [float(np.quantile(values, 0.025)), float(np.quantile(values, 0.975))] if len(values) else None
    return out


def bootstrap_median(values, key):
    values, seed, n = np.asarray(values, float), CONFIG["seed"], CONFIG["resamples"]
    if len(values) < 2:
        return None
    rng = np.random.default_rng([seed, zlib.crc32(key.encode())])
    meds = np.median(values[rng.integers(0, len(values), size=(n, len(values)))], axis=1)
    return [float(np.quantile(meds, 0.025)), float(np.quantile(meds, 0.975))]


def per_run_detection(sub, flagged):
    truth = np.asarray([label != NORMAL for label in sub.labels], bool)
    flagged = np.asarray(flagged, bool)
    parts = (truth & flagged, ~truth & flagged, truth & ~flagged, ~truth & ~flagged)
    return np.stack([np.bincount(sub.group, weights=p.astype(float), minlength=sub.R) for p in parts], axis=1)


def per_run_confusion(sub, truth, predicted, classes):
    index, K = {c: i for i, c in enumerate(classes)}, len(classes)
    t = np.asarray([index[x] for x in truth], int)
    p = np.asarray([index[x] for x in predicted], int)
    return np.bincount(sub.group * K * K + t * K + p, minlength=sub.R * K * K).reshape(sub.R, K, K)


def per_run_ratio(sub, hit, mask):
    """(hits, windows) per run over the rows where `mask` holds."""
    hit, mask = np.asarray(hit, bool), np.asarray(mask, bool)
    return np.stack([np.bincount(sub.group, weights=(hit & mask).astype(float), minlength=sub.R),
                     np.bincount(sub.group, weights=mask.astype(float), minlength=sub.R)], axis=1)


def _clean(value):
    """Plain JSON values: tuples to lists, numpy to python, nan/inf to None."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


# --- detection and classification ----
def detection_block(scored, name, eids, key):
    """Window-level detection for one detector over the runs `eids`, by the validation definitions
    (detection.evaluate), with run-bootstrap CIs. Returns (block, per-run latency entries)."""
    sub = subset(scored, eids)
    flagged = scored.flags[name][sub.idx]
    metrics, evaluation = detection.evaluate(sub.runs, sub.rows, sub.labels, flagged, scored.anomaly_scores[name][sub.idx])
    counts = evaluation["confusion"]
    tp, fp, fn, tn = counts["tp"], counts["fp"], counts["fn"], counts["tn"]
    defined = {"precision": tp + fp > 0, "recall": tp + fn > 0, "f1": tp + fp + fn > 0, "fpr": fp + tn > 0}
    reported = {k: (metrics[k] if defined.get(k, True) else None) for k in
                ("precision", "recall", "f1", "fpr", "fpr_healthy", "fault_start_flagged", "roc_auc",
                 "average_precision", "recall_at_healthy_fpr_1")}
    runs = evaluation["latency"]["runs"]
    latency = {k: v for k, v in evaluation["latency"].items() if k != "runs"}
    latency["median_ci95"] = bootstrap_median([e["latency_s"] for e in runs if e["detected"]], f"{key}/latency/{name}")
    block = {"runs": sub.R, "windows": len(sub.rows), "counts": counts, "metrics": reported,
             "ci95": bootstrap(per_run_detection(sub, flagged), det_stats, f"{key}/detection/{name}"),
             "normal_windows": evaluation["normal_windows"], "recall_by_label": evaluation["recall_by_label"],
             "latency": latency}
    return _clean(block), runs


def classification_block(scored, name, eids, key):
    sub = subset(scored, eids)
    predicted = [scored.preds[name][i] for i in sub.idx]
    result = compare.evaluate_classifier(sub.labels, predicted, scored.classes)
    per_class = {c: {"precision": v["precision"] if v["predicted"] else None,
                     "recall": v["recall"] if v["support"] else None,
                     "f1": v["f1"] if v["support"] or v["predicted"] else None,
                     "support": v["support"], "predicted": v["predicted"]}
                 for c, v in result["per_class"].items()}
    block = {"runs": sub.R, "windows": len(sub.rows), "accuracy": result["accuracy"], "macro_f1": result["macro_f1"],
             "ci95": bootstrap(per_run_confusion(sub, sub.labels, predicted, scored.classes), clf_stats,
                               f"{key}/classification/{name}"),
             "classes": scored.classes, "per_class": per_class, "confusion": result["confusion"],
             "as_detector": {k: result["anomaly"][k] for k in ("precision", "recall", "f1", "fpr")}}
    return _clean(block)


def time_to_recognize(scored, name, eids):
    """Seconds from a cascade's start to the end of the first labeled window the classifier calls
    CASCADING_FAILURE (ml/evaluate.py's definition), per cascade run among `eids`."""
    called = defaultdict(dict)
    for (eid, row), guess in zip(scored.rows, scored.preds[name]):
        called[eid][row["window_start"]] = guess
    wanted = set(eids)
    per_run = {eid: evaluate.time_to_recognize(labels, called[eid])
               for eid, _, labels in scored.runs if eid in wanted and evaluate.is_cascade_run(labels)}
    return {"summary": evaluate.summarize_ttr(list(per_run.values())), "per_run": per_run}


# --- per-band breakdown ---

def band_groups(scored):
    """{(injector, (low, high)): [eids]}: standard-length single-fault test runs. Other durations are
    their own section, so a band's number is not also a duration effect."""
    groups = defaultdict(list)
    for eid, m in scored.meta.items():
        if m["phase"] == "test" and m["kind"] == "single" and m["duration_s"] == STANDARD_DURATION_S:
            groups[(m["injector"], m["band"])].append(eid)
    return groups


def band_roles(groups):
    """The integrity doc's caveat: of an injector's two test bands the lower lies between training
    bands (interpolation) and the upper just above the highest one (mild extrapolation)."""
    by_injector = defaultdict(list)
    for injector, band in groups:
        by_injector[injector].append(band)
    return {(inj, band): ("interpolation" if rank == 0 else "mild extrapolation")
            for inj, bands in by_injector.items() for rank, band in enumerate(sorted(bands))}


def band_status(point, ci, ref_point, ref_ci, points=DEGRADATION_POINTS):
    """"ok"; "possible" when the point estimate is more than `points` below the reference but the CIs
    overlap; "degraded" when it is and the band's CI lies wholly below the reference's."""
    if point is None or ref_point is None:
        return "n/a"
    if ref_point - point <= points:
        return "ok"
    if ci and ref_ci and ci[1] < ref_ci[0]:
        return "degraded"
    return "possible"


def _cell(scored, family, model, eids, label_type, key):
    """Recall on the `label_type` windows of the runs `eids`: windows flagged (detection) or called
    their label (classification), with a run-bootstrap CI."""
    sub = subset(scored, eids)
    mask = np.asarray([label == label_type for label in sub.labels], bool)
    if family == "detection":
        hit = scored.flags[model][sub.idx]
    else:
        hit = np.asarray([scored.preds[model][i] == label_type for i in sub.idx], bool)
    per_run = per_run_ratio(sub, hit, mask)
    hits, windows = per_run.sum(axis=0)
    cell = {"runs": sub.R, "windows": int(windows), "recall": float(hits / windows) if windows else None,
            "ci95": bootstrap(per_run, ratio_stats, key).get("ratio")}
    if family == "detection":
        summary, _ = detection.latency(sub.runs, sub.rows, scored.flags[model][sub.idx])
        cell["latency_median_s"] = summary.get("median_s")
        cell["missed_runs"] = summary["missed_runs"]
    else:
        wrong = Counter(scored.preds[model][i] for i, hit_label in zip(sub.idx, mask)
                        if hit_label and scored.preds[model][i] != label_type)
        cell["top_errors"] = wrong.most_common(3)
    return cell


def band_table(scored, reference=None):
    """Recall per (injector, band) for every detector and classifier, with each band's gap to the
    injector's other test band and, if given, to `reference` = {(family, model, label_type): {"recall",
    "ci95"}} from validation."""
    groups = band_groups(scored)
    roles = band_roles(groups)
    models = [("detection", m) for m in scored.flags] + [("classification", m) for m in scored.preds]
    cells = {}
    for (injector, band), eids in sorted(groups.items()):
        for family, model in models:
            cells[(family, model, injector, band)] = _cell(
                scored, family, model, eids, INJECTOR_LABEL[injector], f"band/{injector}/{band}/{family}/{model}")
    rows = []
    for (family, model, injector, band), cell in sorted(cells.items()):
        row = {"family": family, "model": model, "injector": injector, "label": INJECTOR_LABEL[injector],
               "band": list(band), "role": roles[(injector, band)], **cell, "vs_other_band": None, "vs_validation": None}
        others = [b for (f, mo, i, b) in cells if (f, mo, i) == (family, model, injector) and b != band]
        if others:
            ref = cells[(family, model, injector, others[0])]
            row["vs_other_band"] = {"ref_band": list(others[0]), "ref_recall": ref["recall"],
                                    "gap": None if None in (ref["recall"], cell["recall"]) else ref["recall"] - cell["recall"],
                                    "status": band_status(cell["recall"], cell["ci95"], ref["recall"], ref["ci95"])}
        ref = (reference or {}).get((family, model, INJECTOR_LABEL[injector]))
        if ref:
            row["vs_validation"] = {"ref_recall": ref["recall"], "ref_ci95": ref["ci95"],
                                    "gap": None if None in (ref["recall"], cell["recall"]) else ref["recall"] - cell["recall"],
                                    "status": band_status(cell["recall"], cell["ci95"], ref["recall"], ref["ci95"])}
        rows.append(row)
    return _clean(rows)


def validation_reference(dataset, registry, detectors, classifiers):
    """{(family, model, label_type): {"recall", "ci95", ...}}: recall on validation's single-fault runs,
    in the training bands. Validation tuned the packages, so this is the optimistic reference."""
    val = {r["experiment_id"]: {"eid": r["experiment_id"], "phase": "val", "kind": "single",
                                "label_type": r["incident_type"]}
           for r in registry if r["phase"] == "val" and r.get("incident_type") in SINGLE_LABELS}
    if not val:
        raise MetricsError("--with-validation-reference: the registry has no validation single-fault runs")
    scored = score_runs(dataset, val, detectors, classifiers)
    out = {}
    for label_type in SINGLE_LABELS:
        eids = [e for e, m in val.items() if m["label_type"] == label_type]
        if not eids:
            continue
        for family, model in [("detection", m) for m in scored.flags] + [("classification", m) for m in scored.preds]:
            c = _cell(scored, family, model, eids, label_type, f"validation/{label_type}/{family}/{model}")
            out[(family, model, label_type)] = {k: c[k] for k in ("recall", "ci95", "runs", "windows")}
    return out


# --- confounders ---

def confounder_windows(scored, eids, both_active=True):
    """Row positions (into scored.rows) of the confounder windows to score. both_active: labeled fault
    windows where two faults overlap for at least half the window (multi_fault_ms, the labeler's own
    evidence). Otherwise every labeled fault window of the runs, as a sensitivity."""
    wanted = set(eids)
    rows = []
    for i, (eid, row) in enumerate(scored.rows):
        if eid not in wanted or row[train.LABEL] == NORMAL:
            continue
        overlap = scored.evidence[(eid, row["window_start"])].get("multi_fault_ms") or 0
        if not both_active or overlap >= HALF_WINDOW_MS:
            rows.append(i)
    return np.asarray(rows, dtype=int)


def _confounder_shares(scored, name, positions, eids, tag):
    sub = subset(scored, eids)
    where = {i: n for n, i in enumerate(sub.idx)}
    sel = [where[i] for i in positions if i in where]
    mask = np.zeros(len(sub.rows), bool)
    mask[sel] = True
    either, first, casc = (np.zeros(len(sub.rows), bool) for _ in range(3))
    called = Counter()
    for n in sel:
        guess = scored.preds[name][sub.idx[n]]
        m = scored.meta[sub.rows[n][0]]
        either[n], first[n], casc[n] = guess in m["fault_types"], guess == m["first_type"], guess == CASCADE
        called[guess] += 1
    total = int(mask.sum())
    out = {"runs": len({sub.rows[n][0] for n in sel}), "windows": total, "predicted": dict(called.most_common())}
    for label, hit in (("either_fault_type", either), ("labeled_first_type", first), ("cascading_failure", casc),
                       ("neither", mask & ~either & ~casc)):
        per_run = per_run_ratio(sub, hit, mask)
        out[label] = {"share": float(per_run[:, 0].sum() / total) if total else None,
                      "ci95": bootstrap(per_run, ratio_stats, f"confounders/{name}/{tag}/{label}").get("ratio")}
    return out


def confounder_block(scored):
    eids = [e for e, m in scored.meta.items() if m["phase"] == "test" and m["kind"] == "confounder"]
    if not eids:
        return None
    both = confounder_windows(scored, eids)
    block = {"runs": len(eids), "windows_both_active": len(both),
             "windows_all_fault_labeled": len(confounder_windows(scored, eids, both_active=False)),
             "runs_with_no_both_active_window": sorted(e for e in eids if not len(confounder_windows(scored, [e]))),
             "by_classifier": {}, "detection": {}}
    for name in scored.preds:
        entry = {"both_active": _confounder_shares(scored, name, both, eids, "both"),
                 "all_fault_labeled": _confounder_shares(scored, name, confounder_windows(scored, eids, False), eids, "labeled"),
                 "by_pair": {}}
        for pair in sorted({scored.meta[e]["pair"] for e in eids}):
            pair_eids = [e for e in eids if scored.meta[e]["pair"] == pair]
            entry["by_pair"][pair] = _confounder_shares(scored, name, confounder_windows(scored, pair_eids), pair_eids, pair)
        block["by_classifier"][name] = entry
    sub = subset(scored, eids)
    where = {i: n for n, i in enumerate(sub.idx)}
    mask = np.zeros(len(sub.rows), bool)
    mask[[where[i] for i in both if i in where]] = True
    for name in scored.flags:
        per_run = per_run_ratio(sub, scored.flags[name][sub.idx], mask)
        total = per_run[:, 1].sum()
        block["detection"][name] = {"flagged_share": float(per_run[:, 0].sum() / total) if total else None,
                                    "ci95": bootstrap(per_run, ratio_stats, f"confounders/detect/{name}").get("ratio")}
    return _clean(block)


# --- extreme slice and varied durations -----

def _per_injector(scored, eids, key):
    """Per injector cells: detector and classifier recall on the fault windows of `eids`."""
    out = {}
    for injector, label in INJECTOR_LABEL.items():
        group = [e for e in eids if scored.meta[e].get("injector") == injector]
        if not group:
            continue
        out[injector] = {"label": label, "runs": len(group)}
        for model in scored.flags:
            out[injector][f"detection:{model}"] = _cell(scored, "detection", model, group, label, f"{key}/{injector}/d/{model}")
        for model in scored.preds:
            out[injector][f"classification:{model}"] = _cell(scored, "classification", model, group, label, f"{key}/{injector}/c/{model}")
    return _clean(out)


def extreme_block(scored):
    eids = [e for e, m in scored.meta.items() if m["phase"] == "stress"]
    if not eids:
        return None
    return {"runs": len(eids), "by_injector": _per_injector(scored, eids, "stress"),
            "detection": {n: detection_block(scored, n, eids, "stress")[0] for n in scored.flags},
            "classification": {n: classification_block(scored, n, eids, "stress") for n in scored.preds}}


def varied_duration_block(scored):
    """2- and 12-minute single faults, per duration and injector. Per-injector cells only: a pooled
    number would weight injectors by how many windows their runs happen to have."""
    groups = defaultdict(list)
    for eid, m in scored.meta.items():
        if m["phase"] == "test" and m["kind"] == "single" and m["duration_s"] != STANDARD_DURATION_S:
            groups[m["duration_s"]].append(eid)
    return {str(d): {"runs": len(eids), "by_injector": _per_injector(scored, eids, f"varied/{d}")}
            for d, eids in sorted(groups.items())}


# --- the report -----

def git_state(root):
    def run(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=15)
    try:
        commit, dirty = run("rev-parse", "HEAD"), run("status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}
    return {"commit": commit.stdout.strip() or None if commit.returncode == 0 else None,
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None}


def package_summary(p):
    keep = ("format", "kind", "task", "estimator", "threshold", "settings", "rows", "runs", "metrics", "libraries")
    return {"role": p.role, "columns": len(p.meta["columns"]), "classes": p.meta.get("classes"),
            **{k: p.meta[k] for k in keep if k in p.meta}}


def build_report(dataset, queues, detectors, classifiers, seed=SEED, resamples=BOOTSTRAP, with_validation=False,
                 allow_partial=False, repo_root=None):
    CONFIG.update(seed=seed, resamples=resamples)
    registry = load_registry(dataset)
    split = check_split(registry, allow_partial)
    meta = tag_runs(registry, load_specs(queues))
    scored = score_runs(dataset, meta, detectors, classifiers)

    test = [e for e, m in meta.items() if m["phase"] == "test"]
    headline = [e for e in test if meta[e]["kind"] not in HEADLINE_EXCLUDES]
    report = {
        "provenance": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": seed, "bootstrap_resamples": resamples,
            "bootstrap_unit": "run (the windows of a run are resampled together)",
            "split": {"test_slots": [TEST_FIRST, TEST_LAST], "stress_first_slot": STRESS_FIRST,
                      "last_training_day": LAST_TRAINING_DAY, **split},
            "counts": {"test_runs": len(test), "headline_runs": len(headline),
                       "test_windows": sum(1 for e, _ in scored.rows if meta[e]["phase"] == "test"),
                       "stress_runs": len(meta) - len(test),
                       "test_runs_by_kind": dict(Counter(meta[e]["kind"] for e in test))},
            "headline_excludes": list(HEADLINE_EXCLUDES),
            "packages": {p.name: package_summary(p) for p in [*detectors, *classifiers]},
            "git": git_state(repo_root or Path(__file__).resolve().parents[2]),
            "environment": {"python": platform.python_version(), "numpy": np.__version__},
            "definitions": {
                "detection": "window-level; every label but NORMAL is ANOMALY (ml/job/detection.py). fpr counts any flagged "
                             "NORMAL window; fpr_healthy only windows with no fault active; fault_start_flagged the NORMAL "
                             "fault-start minutes flagged (faults caught early, which the labels count as false positives).",
                "latency": "per incident run, from the fault's onset to the end of the first flagged labeled anomalous "
                           "window; missed runs are counted apart and left out of the median.",
                "classification": "labeled windows only, six-class; macro-F1 over classes in the truth or the predictions.",
                "per_band": f"standard-length ({STANDARD_DURATION_S}s) single-fault test runs; recall on the fault's windows; "
                            f"flagged when {DEGRADATION_POINTS:.0%} or more below its reference.",
                "confounder_windows": f"labeled non-NORMAL windows with multi_fault_ms >= {HALF_WINDOW_MS} (both faults active).",
            },
        },
        "detection": {}, "classification": {}, "latency_runs": {},
    }
    for name in scored.flags:
        block, runs = detection_block(scored, name, headline, "headline")
        report["detection"][name] = {"headline": block, "with_confounders": detection_block(scored, name, test, "all-test")[0]}
        report["latency_runs"][name] = _clean(runs)
    for name in scored.preds:
        full = classification_block(scored, name, test, "all-test")
        report["classification"][name] = {
            "headline": classification_block(scored, name, headline, "headline"),
            "with_confounders": {k: full[k] for k in ("runs", "windows", "accuracy", "macro_f1", "ci95")},
            "time_to_recognize_cascade": _clean(time_to_recognize(scored, name, [e for e in headline if meta[e]["kind"] == "cascade"]))}
    reference = validation_reference(dataset, registry, detectors, classifiers) if with_validation else None
    rows = band_table(scored, reference)
    flagged = [{**{k: r[k] for k in ("family", "model", "injector", "band")}, "against": ref, "status": r[ref]["status"]}
               for r in rows for ref in ("vs_other_band", "vs_validation") if r[ref] and r[ref]["status"] in ("possible", "degraded")]
    report["per_band"] = {"rows": rows, "validation_reference": bool(with_validation), "flagged": flagged}
    report["extreme_slice"] = extreme_block(scored)
    report["confounders"] = confounder_block(scored)
    report["varied_duration"] = varied_duration_block(scored)
    return report


# --- markdown ---------

def _n(value, digits=3):
    return "n/a" if value is None else f"{value:.{digits}f}"


def _ci(value, ci, digits=3):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" + (f" [{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]" if ci else "")


def _band(b):
    return f"{b[0]:g}–{b[1]:g}"


def _gap(comparison):
    return "–" if not comparison else f"{_n(comparison['gap'])} ({comparison['status']})"


def _recall_table(by_injector, indent=""):
    """Rows of per-injector recall cells (extreme slice, varied durations)."""
    if not by_injector:
        return ["No runs."]
    columns = [k for k in next(iter(by_injector.values())) if ":" in k]
    lines = ["| Injector | Runs | " + " | ".join(c.replace(":", " ") + " recall" for c in columns) + " |",
             "|---|---|" + "---|" * len(columns)]
    for inj, cells in by_injector.items():
        lines.append(f"| {inj} | {cells['runs']} | " + " | ".join(_ci(cells[c]["recall"], cells[c]["ci95"]) for c in columns) + " |")
    return lines


def render_markdown(report):
    L, prov = [], report["provenance"]
    L += ["# M9-2 Detection + classification metrics (test set)", "", "## Scope and provenance", "",
          f"- **Split:** test = slots {prov['split']['test_slots'][0]}–{prov['split']['test_slots'][1]} "
          f"({prov['counts']['test_runs']} runs, {prov['counts']['test_windows']} labeled windows), all generated after "
          f"{prov['split']['last_training_day']}. The extreme slice (slots {prov['split']['stress_first_slot']}+, "
          f"{prov['counts']['stress_runs']} runs) is section 4 and is in no other number.",
          f"- **Headline:** {prov['counts']['headline_runs']} test runs, the test set without "
          f"{', '.join(prov['headline_excludes'])} (a test-only probe with no clean single label; section 5). "
          "\"With confounders\" lines show what including them does. Test runs by kind: "
          + ", ".join(f"{k} {v}" for k, v in sorted(prov["counts"]["test_runs_by_kind"].items())) + ".",
          f"- **Intervals:** 95% percentile bootstrap, {prov['bootstrap_resamples']} resamples of whole runs, seed {prov['seed']}. "
          "Windows of one run are correlated, so resampling windows would make every interval too narrow.",
          f"- **Code:** commit `{prov['git']['commit']}`{' (uncommitted changes present)' if prov['git']['dirty'] else ''}; "
          f"generated {prov['generated_at']}."]
    for name, p in prov["packages"].items():
        extra = f", threshold {p['threshold']:.4f}" if "threshold" in p else ""
        L.append(f"- **Package `{name}`:** {p.get('estimator')} ({p.get('task')}{extra}), {p['columns']} columns.")
    L += ["", "Definitions:", ""] + [f"- *{k.replace('_', ' ')}:* {v}" for k, v in prov["definitions"].items()]

    det = report["detection"]
    if det:
        names = list(det)
        L += ["", "## 1. Detection (headline)", "", "| Metric | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for key, label in (("precision", "Precision"), ("recall", "Recall"), ("f1", "F1"), ("fpr", "FPR, against the labels"),
                           ("fpr_healthy", "FPR, healthy windows only"), ("fault_start_flagged", "Fault-start windows flagged"),
                           ("roc_auc", "ROC AUC")):
            L.append(f"| {label} | " + " | ".join(
                _ci(det[n]["headline"]["metrics"][key], det[n]["headline"]["ci95"].get(key)) for n in names) + " |")
        L += ["", "Counts (windows): " + "; ".join(
            f"{n}: " + ", ".join(f"{k} {v}" for k, v in det[n]["headline"]["counts"].items()) for n in names) + ".", "",
              "**Detection latency** (incident runs; from the fault's onset to the end of the first flagged labeled window):", "",
              "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for key, label in (("incident_runs", "Incident runs"), ("detected_runs", "Detected"), ("missed_runs", "Missed"),
                           ("median_s", "Median (s) [95% CI]"), ("p90_s", "p90 (s)"), ("max_s", "Max (s)"),
                           ("first_window_share", "Caught in first labeled window")):
            cells = []
            for n in names:
                lat = det[n]["headline"]["latency"]
                v = lat.get(key)
                cells.append("n/a" if v is None else str(v) if key.endswith("runs") else
                             _ci(v, lat.get("median_ci95"), 1) if key == "median_s" else _n(v, 3 if key == "first_window_share" else 1))
            L.append(f"| {label} | " + " | ".join(cells) + " |")
        L += ["", "Latency is window-dominated by design (one-minute windows; `Docs/DETECTION.md`). Missed runs are not in the median.", "",
              "**Share flagged, by label** (NORMAL = FPR):", "", "| Label | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for label in sorted({k for n in names for k in det[n]["headline"]["recall_by_label"]}):
            L.append(f"| {label} | " + " | ".join(_n(det[n]["headline"]["recall_by_label"].get(label)) for n in names) + " |")
        L += ["", "With confounders included: " + "; ".join(
            f"{n}: precision {_n(det[n]['with_confounders']['metrics']['precision'])}, recall {_n(det[n]['with_confounders']['metrics']['recall'])}, "
            f"FPR {_n(det[n]['with_confounders']['metrics']['fpr'])}" for n in names) + "."]

    for name, c in report["classification"].items():
        h = c["headline"]
        L += ["", f"## 2. Classification (headline): `{name}`", "",
              f"- Accuracy {_ci(h['accuracy'], h['ci95'].get('accuracy'))}; macro-F1 {_ci(h['macro_f1'], h['ci95'].get('macro_f1'))} "
              f"({h['runs']} runs, {h['windows']} windows).",
              f"- With confounders included: accuracy {_n(c['with_confounders']['accuracy'])}, macro-F1 {_n(c['with_confounders']['macro_f1'])}.",
              "", "| Class | Precision | Recall | F1 | Support |", "|---|---|---|---|---|"]
        for cls in h["classes"]:
            p = h["per_class"][cls]
            L.append(f"| {cls} | {_n(p['precision'])} | {_n(p['recall'])} | {_n(p['f1'])} | {p['support']} |")
        L += ["", "Confusion matrix (rows true, columns predicted; order: " + ", ".join(h["classes"]) + "):", "", "```"]
        L += [f"{cls:20s} " + " ".join(f"{k:5d}" for k in row) for cls, row in zip(h["classes"], h["confusion"])] + ["```"]
        ttr = c["time_to_recognize_cascade"]["summary"]
        L += ["", f"Cascades: {ttr['runs']} runs; time to recognize median {_n(ttr['median'], 1)} s, max {_n(ttr['max'], 1)} s, "
                  f"never recognized in {ttr['never']} (`ml/evaluate.py`'s definition)."]

    pb = report["per_band"]
    L += ["", "## 3. Per-band breakdown (held-out test bands)", "",
          f"Recall on each injector's fault windows, {STANDARD_DURATION_S}-second single-fault runs only. Each injector has two test bands: "
          "the lower lies between training bands (interpolation), the upper just above the highest training band (mild extrapolation; "
          "`Docs/EVALUATION_INTEGRITY.md`, \"An honest caveat\"). A band is flagged when its recall is more than "
          f"{DEGRADATION_POINTS:.0%} below its reference: *possible* if the intervals overlap, *degraded* if they do not. "
          + ("References: the injector's other test band, and validation (training bands; optimistic, since validation tuned the packages)."
             if pb["validation_reference"] else
             "Reference: the injector's other test band (add `--with-validation-reference` for validation's training bands).")]
    for family in ("detection", "classification"):
        for model in sorted({r["model"] for r in pb["rows"] if r["family"] == family}):
            L += ["", f"**{family}: `{model}`**", "",
                  "| Injector | Band | Role | Runs | Windows | Recall [95% CI] | vs other band | vs validation | "
                  + ("Median latency (s)" if family == "detection" else "Most common wrong call") + " |",
                  "|---|---|---|---|---|---|---|---|---|"]
            for r in (r for r in pb["rows"] if r["family"] == family and r["model"] == model):
                o, v = r["vs_other_band"], r["vs_validation"]
                last = _n(r.get("latency_median_s"), 1) if family == "detection" else (
                    ", ".join(f"{k} ×{n}" for k, n in r["top_errors"][:2]) or "none")
                L.append(f"| {r['injector']} | {_band(r['band'])} | {r['role']} | {r['runs']} | {r['windows']} | {_ci(r['recall'], r['ci95'])} | "
                         f"{_gap(o)} | {_gap(v)} | {last} |")
    L += ["", "Gaps are reference recall minus band recall (positive = the band is worse).", ""]
    if pb["flagged"]:
        L.append("**Flagged bands:** " + "; ".join(
            f"{f['model']} {f['injector']} {_band(f['band'])} ({f['family']}, {f['status']} vs {f['against'][3:].replace('_', ' ')})"
            for f in pb["flagged"]) + ". Report these as findings; do not tune around them.")
    else:
        L.append(f"**No band is flagged:** no held-out band falls more than {DEGRADATION_POINTS:.0%} below its reference for any detector or classifier.")

    ex = report["extreme_slice"]
    L += ["", "## 4. Extreme-extrapolation slice (reported separately; not in any number above)", ""]
    if not ex:
        L.append("No stress runs in the registry.")
    else:
        L += [f"{ex['runs']} runs (traffic >10×, latency >1500 ms, load >6×, timeout >5000 ms), the default endpoint mix. "
              "More extreme incidents are easier, so these numbers say nothing about interpolation and must not be averaged into the "
              "headline. With about five runs per injector the intervals are wide.", ""]
        for name, b in ex["detection"].items():
            m = b["metrics"]
            L.append(f"- `{name}` detection: precision {_n(m['precision'])}, recall {_ci(m['recall'], b['ci95'].get('recall'))}, F1 {_n(m['f1'])}, "
                     f"FPR {_n(m['fpr'])}; latency median {_n(b['latency'].get('median_s'), 1)} s, missed runs {b['latency']['missed_runs']}.")
        for name, b in ex["classification"].items():
            L.append(f"- `{name}` classification: accuracy {_ci(b['accuracy'], b['ci95'].get('accuracy'))}, macro-F1 {_n(b['macro_f1'])}.")
        L += [""] + _recall_table(ex["by_injector"])

    cf = report["confounders"]
    L += ["", "## 5. Confounders (a test-only probe)", ""]
    if not cf:
        L.append("No confounder runs.")
    else:
        L += [f"{cf['runs']} runs: two unrelated faults over the same window, labeled with the first one's type, never CASCADING_FAILURE. "
              "The model never sees one in training, and every training example with two faults active is a cascade, so a high "
              "CASCADING_FAILURE share is a finding about what the model learned, not a data error "
              "(`Docs/EVALUATION_INTEGRITY.md`, \"Confounders\"). "
              f"Windows scored: {cf['windows_both_active']} with both faults active (multi_fault_ms ≥ {HALF_WINDOW_MS // 1000} s). "
              "The shares overlap by design (the labeled type is one of the two), so they do not sum to 1; \"neither\" is everything else, NORMAL included.", ""]
        if cf["runs_with_no_both_active_window"]:
            L += [f"⚠ {len(cf['runs_with_no_both_active_window'])} confounder run(s) have no both-active window and are not in these shares: "
                  + ", ".join(cf["runs_with_no_both_active_window"][:6]) + ".", ""]
        for name, e in cf["by_classifier"].items():
            b = e["both_active"]
            L += [f"**`{name}`**, {b['windows']} windows from {b['runs']} runs:", "",
                  "| Predicted as | Share [95% CI] | All fault-labeled windows (sensitivity) |", "|---|---|---|"]
            for key, label in (("either_fault_type", "either fault's type"), ("labeled_first_type", "the labeled (first) type"),
                               ("cascading_failure", "CASCADING_FAILURE"), ("neither", "neither (other / NORMAL)")):
                L.append(f"| {label} | {_ci(b[key]['share'], b[key]['ci95'])} | {_n(e['all_fault_labeled'][key]['share'])} |")
            L += ["", "Predicted classes: " + ", ".join(f"{k} {v}" for k, v in b["predicted"].items()) + ".", "",
                  "| Pair (first + second) | Runs | Windows | Either | First | CASCADING | Neither |", "|---|---|---|---|---|---|---|"]
            for pair, s in e["by_pair"].items():
                L.append(f"| {pair} | {s['runs']} | {s['windows']} | " + " | ".join(
                    _n(s[k]["share"]) for k in ("either_fault_type", "labeled_first_type", "cascading_failure", "neither")) + " |")
            L.append("")
        for name, d in cf["detection"].items():
            L.append(f"`{name}` flagged {_ci(d['flagged_share'], d['ci95'])} of the both-active windows as anomalous.")

    L += ["", "## 6. Varied-duration faults (2 and 12 minutes)", ""]
    if not report["varied_duration"]:
        L.append("No varied-duration runs.")
    for duration, block in report["varied_duration"].items():
        L += [f"**{int(duration) // 60}-minute faults**, {block['runs']} runs (per injector):", ""] + _recall_table(block["by_injector"]) + [""]

    L += ["## Reading these numbers", "",
          "- Everything is window-level, on labeled windows only: onset, recovery, startup and partial windows are excluded by the labeler.",
          "- Bands are the simulator's held-out *parameter regions*. The claim is generalization across them, not to production incidents (`Docs/EVALUATION_INTEGRITY.md`).",
          "- DB throttling's bands are separated by throttle counts, not latency (the retry ladder sets Product's p95), so read its per-band rows with that in mind.",
          "- The Isolation Forest's cut-off is the package's, set on validation; nothing was fitted or tuned on the test set.",
          "- The threshold baseline sits beside these numbers in M9-3."]
    return "\n".join(L) + "\n"


# --- command line ------

def _pairs(parser, items, flag):
    out = {}
    for item in items:
        name, sep, directory = item.partition("=")
        if not sep or not name or not directory:
            parser.error(f"{flag} takes NAME=DIR, got {item!r}")
        if name in out:
            parser.error(f"{flag}: {name!r} given twice")
        out[name] = Path(directory).expanduser()
    return out


def write_outputs(report, out):
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    paths = (out / OUT_JSON, out / OUT_MD)
    # allow_nan=False: a nan that slipped past _clean fails here, not in whoever reads the file.
    paths[0].write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    paths[1].write_text(render_markdown(report))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ml.m9_2_metrics", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", required=True, help="a local copy of datasets/v1/ (tier1/features, tier2/labels, tier2/experiments.jsonl)")
    parser.add_argument("--queues", default=str(Path(__file__).resolve().parents[1] / "simulator" / "queues"),
                        help="the folder with v1-test-1..3.json and v1-stress.json (default: simulator/queues)")
    parser.add_argument("--classifier", action="append", default=[], metavar="NAME=DIR", help="a six-class package folder")
    parser.add_argument("--detector", action="append", default=[], metavar="NAME=DIR", help="an Isolation Forest or binary package folder")
    parser.add_argument("--out", required=True, help="write metrics_m9_2.json and metrics_m9_2.md here")
    parser.add_argument("--with-validation-reference", action="store_true",
                        help="also score validation's single-fault runs, as a training-band reference for the per-band table")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--bootstrap", type=int, default=BOOTSTRAP)
    parser.add_argument("--allow-partial", action="store_true", help="score a test set with missing slots")
    parser.add_argument("--force", action="store_true", help="overwrite an earlier result")
    args = parser.parse_args(argv)
    classifiers, detectors = _pairs(parser, args.classifier, "--classifier"), _pairs(parser, args.detector, "--detector")
    if not classifiers and not detectors:
        parser.error("give at least one --classifier or --detector")
    if args.bootstrap < 100:
        parser.error("--bootstrap must be at least 100")
    out = Path(args.out).expanduser()
    try:
        if not args.force and any((out / f).exists() for f in (OUT_JSON, OUT_MD)):
            raise MetricsError(f"{out} already holds a result; the test set is scored once (--force to overwrite, "
                               f"and keep and mention the earlier result)")
        report = build_report(
            Path(args.dataset).expanduser(), Path(args.queues).expanduser(),
            [load_package(n, d, "detector") for n, d in detectors.items()],
            [load_package(n, d, "classifier") for n, d in classifiers.items()],
            seed=args.seed, resamples=args.bootstrap, with_validation=args.with_validation_reference,
            allow_partial=args.allow_partial)
        paths = write_outputs(report, out)
    except (MetricsError, EvalError, TrainError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print("\n".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())