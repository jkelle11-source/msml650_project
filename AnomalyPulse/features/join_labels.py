"""Join one run's feature rows to its label rows on window_start, and check for orphans (M4-5).

Features come from features.jsonl (Tier-1), labels from labels.jsonl (Tier-2, M4-4).
The join happens here, after the matrix is written, so the label never enters the matrix.
Usage:  python3 join_labels.py <features.jsonl> <labels.jsonl>
"""
import json
import sys

KEY = "window_start"
# A window Melisa excludes for being only partly inside the run has no feature row by design.
NO_FEATURES_OK = {"partial_window"}


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def join(feature_rows, label_rows):
    """Return (training_rows, report). Raises ValueError on an orphan."""
    features = {r[KEY]: r for r in feature_rows}
    labels = {r[KEY]: r for r in label_rows}
    if len(features) != len(feature_rows) or len(labels) != len(label_rows):
        raise ValueError("duplicate window_start in features or labels")

    features_without_label = sorted(set(features) - set(labels))
    labels_without_features = sorted(
        w for w in set(labels) - set(features)
        if labels[w].get("excluded_reason") not in NO_FEATURES_OK)
    if features_without_label or labels_without_features:
        raise ValueError(f"orphans: features with no label {features_without_label}; "
                         f"labels with no features {labels_without_features}")

    training = [dict(features[w], label=labels[w]["label"])
                for w in sorted(features) if labels[w].get("included")]
    report = {"feature_rows": len(features), "label_rows": len(labels),
              "training_rows": len(training),
              "excluded_with_features": sum(1 for w in features if not labels[w].get("included"))}
    return training, report


if __name__ == "__main__":
    training, report = join(load(sys.argv[1]), load(sys.argv[2]))
    print(report)
    for row in training:
        print(row[KEY], row["label"])