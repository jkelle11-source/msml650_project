"""Build a dataset's feature matrix (M4-5).

    cd AnomalyPulse
    python -m features --dataset m4-1-validation --root ~/mirror      # a local copy of the layout
    python -m features --dataset v1 --bucket <telemetry bucket>        # S3, as --profile sim

Label the dataset first (python -m labels): each run's labels are read. For every run in
tier2/experiments.jsonl (accepted runs only) it reads tier1/requests and tier1/metrics for
the features and, from tier2, only the manifest's start and end (partial windows are
dropped) and the labels' window list (which windows temporal features look back over, and
the join check). It writes under tier1/features/ only:

    tier1/features/experiment_id=<id>/features.jsonl   one line per complete window
    tier1/features/feature_log.jsonl                    one line per experiment: windows,
                                                        windows missing a CloudWatch snapshot,
                                                        and (error) why it has no features

A run that fails the join check, or is missing a file, gets no features and an error in
the log. A forbidden name in a column stops the whole build: it would be in every run.
Only v1's features go to S3; a validation dataset's stay local, as its labels do.
Re-running rewrites them, as labels are rewritten: features depend on which windows the
labels include.
"""
import argparse
import json
import sys
from pathlib import Path

from features import windows
from features.join_labels import JoinError
from features.request_features import SERVICES
from features.write_matrix import write_features
from simulator import dataset as ds

# The one dataset whose features are stored in S3, as for labels.
S3_DATASET = ds.DEFAULT_DATASET


def _log_key(dataset):
    return f"datasets/{dataset}/tier1/features/feature_log.jsonl"


def _check_tier1(key):
    if "/tier1/features/" not in key or "/tier2/" in key:
        raise ValueError(f"features are Tier-1 and go only under tier1/features/: {key}")


def _check_s3(key):
    _check_tier1(key)
    if not key.startswith(f"datasets/{S3_DATASET}/"):
        raise ValueError(f"only {S3_DATASET}'s features go to S3; a validation dataset's are never "
                         f"stored: {key}")


class LocalStore:
    def __init__(self, root):
        self.root = Path(root).expanduser()

    def read(self, key):
        try:
            return (self.root / key).read_text()
        except FileNotFoundError:
            raise FileNotFoundError(key) from None

    def write(self, key, body):
        _check_tier1(key)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


class S3Store:
    def __init__(self, bucket, profile="sim", region="us-east-2"):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.Session(profile_name=profile, region_name=region).client("s3")

    def read(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")
        except self.s3.exceptions.NoSuchKey:
            raise FileNotFoundError(key) from None

    def write(self, key, body):
        _check_s3(key)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode("utf-8"))


def _jsonl(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def missing_snapshots(matrix, snapshots):
    """The matrix's windows for which some service has no CloudWatch snapshot."""
    have = {(windows.metrics_window(s["timestamp"]), s["service"]) for s in snapshots}
    return [w for w in matrix if any((w, svc) not in have for svc in SERVICES)]


def build_dataset(dataset, store, out=sys.stdout):
    base = f"datasets/{dataset}"
    experiments = _jsonl(store.read(f"{base}/tier2/experiments.jsonl"))
    log = []
    for exp in experiments:
        eid = exp["experiment_id"]
        line = {"experiment_id": eid, "windows": 0, "missing_snapshot": [], "error": None}
        try:
            manifest_ = json.loads(store.read(f"{base}/tier2/manifests/experiment_id={eid}/manifest.json"))
            label_rows = _jsonl(store.read(f"{base}/tier2/labels/experiment_id={eid}/labels.jsonl"))
            records = _jsonl(store.read(f"{base}/tier1/requests/experiment_id={eid}/requests.jsonl"))
            snapshots = _jsonl(store.read(f"{base}/tier1/metrics/experiment_id={eid}/metrics.jsonl"))
            _, matrix = write_features(dataset, eid, records, snapshots, label_rows,
                                       (manifest_["started_at"], manifest_["ended_at"]), store.write)
        except FileNotFoundError as err:
            hint = " (label the dataset first: python -m labels)" if "/tier2/labels/" in str(err) else ""
            line["error"] = f"missing {err}{hint}"
        except JoinError as err:
            line["error"] = str(err)
        else:
            line.update(windows=len(matrix), missing_snapshot=missing_snapshots(matrix, snapshots))
        log.append(line)
        if line["error"]:
            print(f"NOT BUILT {eid}: {line['error']}", file=out)
        else:
            gap = f", {len(line['missing_snapshot'])} missing a CloudWatch snapshot" if line["missing_snapshot"] else ""
            print(f"{eid}: {line['windows']} windows{gap}", file=out)
    store.write(_log_key(dataset), "".join(json.dumps(line) + "\n" for line in log))
    failed = [line for line in log if line["error"]]
    print(f"{len(log) - len(failed)} experiments built, {len(failed)} not; "
          f"{sum(line['windows'] for line in log)} windows, "
          f"{sum(len(line['missing_snapshot']) for line in log)} missing a CloudWatch snapshot", file=out)
    return log, failed


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m features", description=__doc__.split("\n\n")[0])
    p.add_argument("--dataset", required=True)
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--root", help="a local directory laid out like the bucket")
    where.add_argument("--bucket", help="the telemetry bucket (as --profile)")
    p.add_argument("--profile", default="sim")
    args = p.parse_args(argv)

    if args.bucket and args.dataset != S3_DATASET:
        p.error(f"only {S3_DATASET}'s features go to S3; build {args.dataset} from a local copy "
                f"with --root (Docs/FEATURE_MATRIX.md)")
    store = LocalStore(args.root) if args.root else S3Store(args.bucket, profile=args.profile)
    _, failed = build_dataset(args.dataset, store)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
