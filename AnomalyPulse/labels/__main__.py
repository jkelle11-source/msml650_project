"""Label a dataset's windows (M4-4).

    cd AnomalyPulse
    python -m labels --dataset m4-1-validation --root ~/datasets-mirror          # local copy of the bucket
    python -m labels --dataset v1 --bucket <telemetry bucket>                    # S3, as --profile sim
    python -m labels --manifest path/to/manifest.json                            # one run, printed

Reads tier2/experiments.jsonl (accepted runs only) and each run's manifest, and writes
under tier2/ only:

    tier2/labels/experiment_id=<id>/labels.jsonl   one line per window, labeled or excluded
    tier2/labels/labeling_log.jsonl                one line per experiment: label counts and
                                                   every excluded window with its reason, or
                                                   (error) why it could not be labeled

Only v1's labels go to S3. A validation dataset is labeled from a local copy of
its manifests (Docs/LABELING.md has the sync command): its labels are never
stored. Re-running rewrites the labels (they are a pure function of the
manifest and the policy).
"""
import argparse
import json
import sys
from pathlib import Path

from labels import policy
from simulator import dataset as ds

# The one dataset whose labels are stored in S3, the one the integrity controls guard.
S3_DATASET = ds.DEFAULT_DATASET


def _labels_key(dataset, experiment_id):
    return f"datasets/{dataset}/tier2/labels/experiment_id={experiment_id}/labels.jsonl"


def _check_tier2(key):
    if "/tier2/labels/" not in key or "/tier1/" in key:
        raise ValueError(f"labels are Tier-2 and go only under tier2/labels/: {key}")


def _check_s3(key):
    _check_tier2(key)
    if not key.startswith(f"datasets/{S3_DATASET}/"):
        raise ValueError(f"only {S3_DATASET}'s labels go to S3; a validation dataset's are never "
                         f"stored: {key}")


class LocalStore:
    def __init__(self, root):
        self.root = Path(root).expanduser()

    def read(self, key):
        return (self.root / key).read_text()

    def write(self, key, body):
        _check_tier2(key)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


class S3Store:
    def __init__(self, bucket, profile="sim", region="us-east-2"):
        import boto3
        self.bucket = bucket
        self.s3 = boto3.Session(profile_name=profile, region_name=region).client("s3")

    def read(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")

    def write(self, key, body):
        _check_s3(key)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode("utf-8"))


def label_dataset(dataset, store, out=sys.stdout):
    experiments = [json.loads(line) for line in
                   store.read(f"datasets/{dataset}/tier2/experiments.jsonl").splitlines() if line.strip()]
    log = []
    for exp in experiments:
        eid = exp["experiment_id"]
        manifest_ = json.loads(store.read(f"datasets/{dataset}/tier2/manifests/experiment_id={eid}/manifest.json"))
        try:
            windows = policy.label_experiment(manifest_)
        except policy.LabelingError as err:
            # Same shape as a labeled run's line, with no windows and the reason.
            log.append({**policy.summarize(eid, manifest_, []), "slot": exp.get("slot"), "error": str(err)})
            print(f"NOT LABELED {eid}: {err}", file=out)
            continue
        store.write(_labels_key(dataset, eid),
                    "".join(json.dumps(w.to_record()) + "\n" for w in windows))
        summary = {**policy.summarize(eid, manifest_, windows), "slot": exp.get("slot"), "error": None}
        log.append(summary)
        print(f"{eid} slot {exp.get('slot')}: {summary['labels']}  excluded {summary['excluded_reasons']}",
              file=out)
    store.write(f"datasets/{dataset}/tier2/labels/labeling_log.jsonl",
                "".join(json.dumps(line) + "\n" for line in log))
    failed = [line for line in log if line["error"]]
    totals: dict = {}
    for line in log:
        for k, v in line["labels"].items():
            totals[k] = totals.get(k, 0) + v
    print(f"{len(log) - len(failed)} experiments labeled, {len(failed)} not; windows by label {totals}",
          file=out)
    return log, failed


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m labels", description=__doc__.split("\n\n")[0])
    p.add_argument("--dataset")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--root", help="a local directory laid out like the bucket")
    where.add_argument("--bucket", help="the telemetry bucket (as --profile)")
    p.add_argument("--profile", default="sim")
    p.add_argument("--manifest", help="label one manifest file and print its windows")
    args = p.parse_args(argv)

    if args.manifest:
        m = json.loads(Path(args.manifest).read_text())
        for w in policy.label_experiment(m):
            print(w.window_start, w.label or f"(excluded: {w.excluded})",
                  {k.split("#")[-1]: v // 1000 for k, v in w.fault_ms.items()} or "")
        return 0
    if not args.dataset or not (args.root or args.bucket):
        p.error("--dataset with --root or --bucket, or --manifest")
    if args.bucket and args.dataset != S3_DATASET:
        p.error(f"only {S3_DATASET}'s labels go to S3; label {args.dataset} from a local copy "
                f"with --root (Docs/LABELING.md)")
    store = LocalStore(args.root) if args.root else S3Store(args.bucket, profile=args.profile)
    _, failed = label_dataset(args.dataset, store)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())