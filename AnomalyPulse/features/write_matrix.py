"""Write one experiment's feature matrix, guard first (M4-5).

Layout (Docs/DATASET.md): datasets/<name>/tier1/features/experiment_id=<id>/features.jsonl
One line per window: {"window_start": ..., <feature columns>}. window_start is the
join key; experiment_id lives in the path only, never as a column.

Usage:  python -m features.write_matrix <dataset> <experiment_id> <requests.jsonl> <metrics.jsonl>
            [--manifest manifest.json] [--labels labels.jsonl] [--out DIR | --bucket NAME]
With neither --out nor --bucket it only prints what it would write. An upload needs
--manifest (partial windows are dropped) and --labels (temporal features look back over
labeled windows and the startup minute only).
"""
import json
import sys
from pathlib import Path

from features import guard
from features.build_matrix import build_matrix, lookback_windows, load_jsonl

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


def write_features(dataset, experiment_id, records, snapshots, put, bounds=None, lookback=None):
    """Build, check, then call put(key, bytes). If the guard fails, put is never called."""
    key = feature_key(dataset, experiment_id)
    assert "/tier1/" in key and "/tier2/" not in key
    body = to_jsonl(build_matrix(records, snapshots, bounds, lookback))
    put(key, body.encode("utf-8"))
    return key


def local_put(root):
    def put(key, body):
        path = Path(root) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return put


def s3_put(bucket, profile="sim", region="us-east-2"):
    """Put to S3 as the simulator role. Refuses any key outside tier1/features/ and never overwrites."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.Session(profile_name=profile, region_name=region).client("s3")

    def put(key, body):
        if "/tier1/features/" not in key:
            raise ValueError(f"refusing to write outside tier1/features/: {key}")
        try:
            s3.head_object(Bucket=bucket, Key=key)
        except ClientError as err:
            if err.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
        else:
            raise FileExistsError(f"already exists, not overwriting: {key}")
        s3.put_object(Bucket=bucket, Key=key, Body=body)
    return put


if __name__ == "__main__":
    flags = ("--out", "--bucket", "--manifest", "--labels")
    opts = {f: sys.argv[sys.argv.index(f) + 1] for f in flags if f in sys.argv}
    args = [a for a in sys.argv[1:] if a not in flags and a not in opts.values()]
    out, bucket = opts.get("--out"), opts.get("--bucket")
    dataset, experiment_id, req_path, met_path = args
    bounds = lookback = None
    if "--manifest" in opts:  # only the run's start and end are read, never put in a column
        m = json.loads(Path(opts["--manifest"]).read_text())
        bounds = (m["started_at"], m["ended_at"])
    if "--labels" in opts:    # only which windows are labeled (or startup) is read, never put in a column
        lookback = lookback_windows(load_jsonl(opts["--labels"]))
    if bucket and (bounds is None or lookback is None):
        sys.exit("--manifest and --labels are required for an upload, so partial windows are dropped "
                 "and temporal features look back over labeled windows and the startup minute only")
    records, snapshots = load_jsonl(req_path), load_jsonl(met_path)
    if bucket:
        print("uploaded", write_features(dataset, experiment_id, records, snapshots, s3_put(bucket),
                                         bounds, lookback))
    elif out:
        print("wrote", write_features(dataset, experiment_id, records, snapshots, local_put(out),
                                      bounds, lookback))
    else:
        matrix = build_matrix(records, snapshots, bounds, lookback)
        to_jsonl(matrix)  # runs the guard
        print("dry run, would write", feature_key(dataset, experiment_id), f"({len(matrix)} windows)")