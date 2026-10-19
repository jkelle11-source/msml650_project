"""Write one experiment's feature matrix, guard and join check first (M4-5).

Layout (Docs/DATASET.md): datasets/<name>/tier1/features/experiment_id=<id>/features.jsonl
One line per complete window: {"window_start": ..., <every column of build_matrix.columns()>}.
window_start is the join key; experiment_id lives in the path only, never as a column.
`python -m features` writes a whole dataset with it.
"""
import json

from features import guard, join_labels
from features.build_matrix import build_matrix, lookback_windows

KEY = join_labels.KEY


def feature_key(dataset, experiment_id):
    return f"datasets/{dataset}/tier1/features/experiment_id={experiment_id}/features.jsonl"


def to_jsonl(matrix):
    """Serialize the matrix. The guard runs on the final columns, as the last step before writing."""
    columns = {c for row in matrix.values() for c in row}
    if KEY in columns:
        raise ValueError(f"{KEY} is the join key, not a feature column")
    guard.check_columns(columns)
    return "".join(json.dumps({KEY: w, **row}) + "\n" for w, row in matrix.items())


def write_features(dataset, experiment_id, records, snapshots, label_rows, bounds, put):
    """Build the run's matrix, guard it, check it joins the run's labels, then put(key, text).

    bounds is the manifest's (started_at, ended_at): partial windows are dropped.
    label_rows is the run's labels.jsonl: temporal features look back over its
    lookback_windows(), and every feature row must have a label row and the reverse,
    partial windows aside. Only the bounds and the window list are read from Tier-2,
    never a column. If any check fails, put is never called. Returns (key, matrix).
    """
    key = feature_key(dataset, experiment_id)
    assert "/tier1/" in key and "/tier2/" not in key
    matrix = build_matrix(records, snapshots, bounds, lookback_windows(label_rows))
    body = to_jsonl(matrix)
    join_labels.join([{KEY: w, **row} for w, row in matrix.items()], label_rows)
    put(key, body)
    return key, matrix
