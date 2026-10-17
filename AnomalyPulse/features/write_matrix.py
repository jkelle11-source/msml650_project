"""Write one experiment's feature matrix, guard first (M4-5).

Layout (Docs/DATASET.md): datasets/<name>/tier1/features/experiment_id=<id>/features.jsonl
One line per window: {"window_start": ..., <feature columns>}. window_start is the
join key; experiment_id lives in the path only, never as a column.

Usage:  python3 write_matrix.py <dataset> <experiment_id> <requests.jsonl> <metrics.jsonl> [--out DIR]
Without --out it only prints what it would write.
"""
import json
import sys
from pathlib import Path

import guard
from build_matrix import build_matrix, load_jsonl

KEY = "window_start"


def feature_key(dataset, experiment_id):
    return f"datasets/{dataset}/tier1/features/experiment_id={experiment_id}/features.jsonl"


def to_jsonl(matrix):
    """Serialize the matrix. The guard runs on the final columns, as the last step before writing."""
    columns = {c for row in matrix.values() for c in row}
    if KEY in columns:
        raise ValueError(f"{KEY} is the join key, not a feature column")
    guard.check_columns(columns)
    return "".join(json.dumps({KEY: w, **row}) + "\n" for w, row in matrix.items())


def write_features(dataset, experiment_id, records, snapshots, put):
    """Build, check, then call put(key, bytes). If the guard fails, put is never called."""
    key = feature_key(dataset, experiment_id)
    assert "/tier1/" in key and "/tier2/" not in key
    body = to_jsonl(build_matrix(records, snapshots))
    put(key, body.encode("utf-8"))
    return key


def local_put(root):
    def put(key, body):
        path = Path(root) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return put


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--out"]
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    if out:
        args.remove(out)
    dataset, experiment_id, req_path, met_path = args
    records, snapshots = load_jsonl(req_path), load_jsonl(met_path)
    if out:
        print("wrote", write_features(dataset, experiment_id, records, snapshots, local_put(out)))
    else:
        matrix = build_matrix(records, snapshots)
        to_jsonl(matrix)  # runs the guard
        print("dry run, would write", feature_key(dataset, experiment_id), f"({len(matrix)} windows)")