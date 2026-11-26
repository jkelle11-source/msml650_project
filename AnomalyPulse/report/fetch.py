"""Download what M9-6's latency chain needs beside M9-5's incident records: each test and stress run's
manifest, for when its faults really started. Read-only.

    cd AnomalyPulse
    python -m report.fetch --profile <profile>

Writes report/data/manifests.json: one item per v1 test or stress run, with its slot, phase and labeled
type from datasets/v1's registry, and from its manifest the run's start and each fault's injector, target,
active_from and active_until. M9-2's latency clock starts at a run's first labeled window (for DB throttling,
label_from, after the injector has already started), so the live chain needs the manifests' active_from.
"""
import argparse
import json
from pathlib import Path

DATA = Path(__file__).parent / "data"
BUCKET_PARAMETER = "/anomalypulse/telemetry/bucket/name"
REGISTRY = "datasets/v1/tier2/experiments.jsonl"
PHASES = ("test", "stress")
FAULT_KEYS = ("fault_id", "injector", "target", "active_from", "active_until")


def manifest_key(experiment_id):
    """Where the simulator writes a run's manifest (simulator/manifest.py's manifest_key)."""
    return f"experiments/{experiment_id}/manifest.json"


def summarize(run, manifest):
    """The fields the latency chain reads, from a registry entry and its manifest."""
    return {
        "experiment_id": run["experiment_id"],
        "slot": run["slot"],
        "phase": run["phase"],
        "labeled_type": run["incident_type"],
        "started_at": manifest.get("started_at"),
        "faults": [{k: a.get(k) for k in FAULT_KEYS} for a in manifest.get("activations") or []],
    }


def registry(s3, bucket):
    body = s3.get_object(Bucket=bucket, Key=REGISTRY)["Body"].read().decode()
    return [e for e in map(json.loads, body.splitlines()) if e.get("phase") in PHASES]


def manifests(s3, bucket, runs):
    out = []
    for run in runs:
        body = s3.get_object(Bucket=bucket, Key=manifest_key(run["experiment_id"]))["Body"].read()
        out.append(summarize(run, json.loads(body)))
    return sorted(out, key=lambda m: m["slot"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name="us-east-2")
    s3 = session.client("s3")
    bucket = session.client("ssm").get_parameter(Name=BUCKET_PARAMETER)["Parameter"]["Value"]
    items = manifests(s3, bucket, registry(s3, bucket))
    DATA.mkdir(exist_ok=True)
    (DATA / "manifests.json").write_text(json.dumps(items, indent=1) + "\n")
    counts = {p: sum(m["phase"] == p for m in items) for p in PHASES}
    print(f"{len(items)} manifests ({counts}), written to {DATA / 'manifests.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
