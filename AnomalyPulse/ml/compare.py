"""Validation comparison report for M5-3: Model 0 and the classifiers, side by side.

Works on predictions only (lists of labels), so it needs no model, no AWS and no sklearn.
Anomaly view: anything but NORMAL counts as an anomaly, which is the only view Model 0 has.
Six-class view: accuracy, macro-F1, per-class precision/recall and the confusion matrix.
"""
from ml import model0

NORMAL = model0.NORMAL
CASCADE = "CASCADING_FAILURE"


def _check(truth, predicted):
    if len(truth) != len(predicted):
        raise ValueError(f"truth has {len(truth)} labels, predictions have {len(predicted)}")


def accuracy(truth, predicted):
    _check(truth, predicted)
    return sum(t == p for t, p in zip(truth, predicted)) / len(truth) if truth else 0.0


def confusion(truth, predicted, classes):
    """Rows are the true class, columns the predicted class, both in `classes` order."""
    _check(truth, predicted)
    index = {c: i for i, c in enumerate(classes)}
    matrix = [[0] * len(classes) for _ in classes]
    for t, p in zip(truth, predicted):
        if t not in index or p not in index:
            raise ValueError(f"unknown class in ({t!r}, {p!r}); classes are {list(classes)}")
        matrix[index[t]][index[p]] += 1
    return matrix


def per_class(matrix, classes):
    out = {}
    for i, c in enumerate(classes):
        tp = matrix[i][i]
        support = sum(matrix[i])
        predicted = sum(row[i] for row in matrix)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[c] = {"precision": precision, "recall": recall, "f1": f1,
                  "support": support, "predicted": predicted}
    return out


def macro_f1(per):
    """Mean F1 over the classes that appear in the truth or in the predictions."""
    present = [v["f1"] for v in per.values() if v["support"] or v["predicted"]]
    return sum(present) / len(present) if present else 0.0


def evaluate_classifier(truth, predicted, classes):
    matrix = confusion(truth, predicted, classes)
    per = per_class(matrix, classes)
    return {"accuracy": accuracy(truth, predicted), "macro_f1": macro_f1(per),
            "confusion": matrix, "per_class": per,
            "anomaly": model0.score(truth, [p != NORMAL for p in predicted])}


def compare(truth, classifier_predictions, model0_flags, classes):
    """classifier_predictions: {name: labels}; model0_flags: {variant: [bool]}."""
    report = {"model0": {v: model0.score(truth, f) for v, f in model0_flags.items()},
              "classifiers": {n: evaluate_classifier(truth, p, classes)
                              for n, p in classifier_predictions.items()},
              "cascade": None}
    windows = sum(1 for t in truth if t == CASCADE)
    if windows:
        m0 = {v: s["recall_by_class"].get(CASCADE, 0.0) for v, s in report["model0"].items()}
        best = max(m0.values()) if m0 else 0.0
        clf = {n: {"exact": r["per_class"][CASCADE]["recall"],
                   "flagged": r["anomaly"]["recall_by_class"].get(CASCADE, 0.0)}
               for n, r in report["classifiers"].items()}
        report["cascade"] = {"windows": windows, "model0_recall": m0, "classifier_recall": clf,
                             "margin": {n: v["exact"] - best for n, v in clf.items()}}
    return report


def render_markdown(report, classes):
    L = ["# M5-3 validation comparison", "",
         "## Anomaly view (anything but NORMAL is an anomaly)", "",
         "| Model | Precision | Recall | F1 | FPR |", "|---|---|---|---|---|"]
    row = lambda name, s: (f"| {name} | {s['precision']:.3f} | {s['recall']:.3f} | "
                           f"{s['f1']:.3f} | {s['fpr']:.3f} |")
    for v, s in report["model0"].items():
        L.append(row(f"Model 0 ({v})", s))
    for n, r in report["classifiers"].items():
        L.append(row(n, r["anomaly"]))
    L += ["", "## Six-class view", "", "| Model | Accuracy | Macro-F1 |", "|---|---|---|"]
    for n, r in report["classifiers"].items():
        L.append(f"| {n} | {r['accuracy']:.4f} | {r['macro_f1']:.4f} |")
    for n, r in report["classifiers"].items():
        L += ["", f"### {n}: per class", "",
              "| Class | Precision | Recall | F1 | Support |", "|---|---|---|---|---|"]
        for c in classes:
            p = r["per_class"][c]
            L.append(f"| {c} | {p['precision']:.3f} | {p['recall']:.3f} | {p['f1']:.3f} | {p['support']} |")
        L += ["", "Confusion matrix (rows true, columns predicted, in this order: "
              + ", ".join(classes) + ")", "", "```"]
        L += [f"{c:20s} " + " ".join(f"{k:5d}" for k in m) for c, m in zip(classes, r["confusion"])]
        L.append("```")
    cas = report["cascade"]
    if cas:
        L += ["", f"## Cascade margin over Model 0 ({cas['windows']} cascade windows)", "",
              "Model 0 recall on cascades: "
              + ", ".join(f"{v} {x:.3f}" for v, x in cas["model0_recall"].items()), "",
              "| Classifier | Exact-class recall | Flagged as anomaly | Margin over best Model 0 |",
              "|---|---|---|---|"]
        for n, v in cas["classifier_recall"].items():
            L.append(f"| {n} | {v['exact']:.3f} | {v['flagged']:.3f} | {cas['margin'][n]:+.3f} |")
    return "\n".join(L) + "\n"
