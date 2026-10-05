"""Post-deploy M2-2 check: Product telemetry lands in S3 and validates clean.

Sends real requests to both product endpoints, then finds the exact records
they produced (matched by request_id) under
    s3://<telemetry-bucket>/raw/service=product-service/dt=<date>/
and checks each one: exactly one record per request, method/status match what
the client saw, passes the shared schema validator, and has no Tier-2 field.

Usage (needs boto3 + jsonschema, and S3 read on the telemetry bucket):
    python check_telemetry.py --profile <your-profile>
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "layers", "telemetry", "python"))
from schema_validator import validate  # noqa: E402

PREFIX = "raw/service=product-service"
ERROR_PREFIX = "errors/service=product-service"
with open(os.path.join(_HERE, "..", "..", "infra", "shared", "field_catalog.json")) as f:
    TIER2_FIELDS = {n for n, m in json.load(f)["fields"].items() if m["tier"] == "tier2_ground_truth"}

# (path, expected status) -- covers both endpoints plus the 404 path.
REQUESTS = [
    ("/products", 200),
    ("/products/prod-001", 200),
    ("/products/prod-002", 200),
    ("/products/does-not-exist", 404),
]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, r.headers.get("x-amzn-RequestId")
    except urllib.error.HTTPError as e:  # 4xx/5xx still carry the request id
        return e.code, e.headers.get("x-amzn-RequestId")


def _landed(s3, bucket, prefix, since):
    """Yield (key, line) for every line in objects under prefix modified since `since`."""
    for dt in sorted({since.strftime("%Y-%m-%d"), datetime.now(timezone.utc).strftime("%Y-%m-%d")}):
        pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/dt={dt}/")
        for page in pages:
            for obj in page.get("Contents", []):
                if obj["LastModified"] < since:
                    continue
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
                for line in body.splitlines():
                    if line.strip():
                        yield obj["Key"], line


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--profile")
    p.add_argument("--region", default="us-east-2")
    p.add_argument("--wait", type=int, default=180, help="seconds to wait for records to land")
    args = p.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ssm, s3 = session.client("ssm"), session.client("s3")
    api = ssm.get_parameter(Name="/anomalypulse/api/base-url")["Parameter"]["Value"].rstrip("/")
    bucket = ssm.get_parameter(Name="/anomalypulse/telemetry/bucket/name")["Parameter"]["Value"]

    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    failures = 0

    print(f"-- Sending requests to {api} --")
    sent = {}  # request_id -> (path, status)
    for path, expected in REQUESTS:
        status, rid = _get(api + path)
        ok = status == expected and rid
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  GET {path} -> {status} (request_id={rid})")
        if rid:
            sent[rid] = (path, status)

    print(f"-- Waiting up to {args.wait}s for records in s3://{bucket}/{PREFIX}/ --")
    deadline = time.time() + args.wait
    while True:
        found = {}  # request_id -> [record, ...]
        for _, line in _landed(s3, bucket, PREFIX, since):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("request_id") in sent:
                found.setdefault(rec["request_id"], []).append(rec)
        if len(found) == len(sent) or time.time() >= deadline:
            break
        time.sleep(10)

    sample = None
    for rid, (path, status) in sent.items():
        recs = found.get(rid, [])
        if len(recs) != 1:
            failures += 1
            print(f"FAIL  GET {path}: expected 1 record in S3, found {len(recs)} (request_id={rid})")
            continue
        rec = recs[0]
        ok, errors = validate(rec)
        tier2 = set(rec) & TIER2_FIELDS
        problems = []
        if not ok:
            problems.append(f"schema: {errors[0].message}")
        if tier2:
            problems.append(f"Tier-2 fields present: {sorted(tier2)}")
        if rec["http_method"] != "GET" or rec["status_code"] != status:
            problems.append(f"record says {rec['http_method']} {rec['status_code']}, client saw GET {status}")
        if problems:
            failures += 1
            print(f"FAIL  GET {path}: " + "; ".join(problems))
        else:
            sample = sample or rec
            print(f"PASS  GET {path}: 1 record, schema-valid, no Tier-2 ({rec['endpoint']} {rec['status_code']}, "
                  f"latency_ms={rec['latency_ms']}, db_consumed_capacity={rec['db_consumed_capacity']})")

    stray = sum(1 for _ in _landed(s3, bucket, ERROR_PREFIX, since))
    if stray:
        failures += 1
        print(f"FAIL  {stray} unparseable line(s) landed under s3://{bucket}/{ERROR_PREFIX}/")

    if sample:
        print("-- Sample record --")
        print(json.dumps(sample, indent=2))
    print(f"-- {'ALL PASS' if not failures else f'{failures} FAILURE(S)'} --")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
