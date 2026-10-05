"""Post-deploy M2-5 check: the metrics extractor runs and lands snapshots in S3.

Invokes anomalypulse-metrics-extractor once synchronously, then fetches the exact
snapshot keys it reports writing and checks each one:
    - lands under s3://<telemetry-bucket>/metrics/service=<svc>/dt=<date>/
    - parses as JSON carrying {service, timestamp, metrics}
    - the service in the body matches the key's partition
    - each present metrics block (dynamodb / lambda) carries its Tier-1 signal keys

Values may legitimately be 0 or null on an idle system, so this checks that the
metrics land and are well-formed (the M2-5 DoD: "Extracted metrics land in S3 and
are joinable"), not their magnitude.

Usage (needs boto3, lambda:InvokeFunction on the extractor, and S3 read on the
telemetry bucket):
    python check_metrics.py --profile <your-profile>
"""
import argparse
import json
import sys

import boto3

FUNCTION_NAME = "anomalypulse-metrics-extractor"
BUCKET_PARAM = "/anomalypulse/telemetry/bucket/name"

# Keys each block must carry (see metrics_extractor/handler.py). Presence only --
# the values can be 0/None when the window was idle.
DYNAMODB_KEYS = {"table_name", "consumed_read_capacity", "consumed_write_capacity",
                 "throttled_requests", "read_throttle_events", "write_throttle_events"}
LAMBDA_KEYS = {"function_name", "avg_duration_ms", "throttle_count",
               "cold_starts", "avg_init_duration_ms"}
TOP_LEVEL = {"service", "timestamp", "metrics"}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile")
    p.add_argument("--region", default="us-east-2")
    args = p.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ssm, s3, lam = session.client("ssm"), session.client("s3"), session.client("lambda")
    bucket = ssm.get_parameter(Name=BUCKET_PARAM)["Parameter"]["Value"]

    failures = 0

    print(f"-- Invoking {FUNCTION_NAME} --")
    resp = lam.invoke(FunctionName=FUNCTION_NAME, InvocationType="RequestResponse")
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        print(f"FAIL  {FUNCTION_NAME} raised: {payload}")
        sys.exit(1)

    written = payload.get("written", [])
    failed = payload.get("failed", [])
    print(f"   window_end={payload.get('window_end')}  written={len(written)}  failed={failed}")
    if failed:
        failures += 1
        print(f"FAIL  extractor reported failed service(s): {failed}")
    if not written:
        print("FAIL  extractor wrote no snapshots (nothing to verify)")
        sys.exit(1)

    print(f"-- Verifying {len(written)} snapshot(s) in s3://{bucket}/metrics/ --")
    sample = None
    for key in written:
        if not key.startswith("metrics/service="):
            failures += 1
            print(f"FAIL  unexpected key prefix: {key}")
            continue
        try:
            rec = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode())
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL  {key}: cannot read/parse ({exc})")
            continue

        problems = []
        if not TOP_LEVEL <= set(rec):
            problems.append(f"missing top-level fields: {sorted(TOP_LEVEL - set(rec))}")
        svc = rec.get("service", "")
        if f"service={svc}/" not in key:
            problems.append(f"body service '{svc}' does not match key partition")
        metrics = rec.get("metrics") or {}
        if "dynamodb" not in metrics and "lambda" not in metrics:
            problems.append("neither a dynamodb nor a lambda block present")
        if "dynamodb" in metrics and not DYNAMODB_KEYS <= set(metrics["dynamodb"]):
            problems.append(f"dynamodb block missing {sorted(DYNAMODB_KEYS - set(metrics['dynamodb']))}")
        if "lambda" in metrics and not LAMBDA_KEYS <= set(metrics["lambda"]):
            problems.append(f"lambda block missing {sorted(LAMBDA_KEYS - set(metrics['lambda']))}")

        if problems:
            failures += 1
            print(f"FAIL  {key}: " + "; ".join(problems))
        else:
            sample = sample or rec
            print(f"PASS  {svc}: snapshot landed ({'+'.join(sorted(metrics))})")

    if sample:
        print("-- Sample snapshot --")
        print(json.dumps(sample, indent=2))
    print(f"-- {'ALL PASS' if not failures else f'{failures} FAILURE(S)'} --")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
