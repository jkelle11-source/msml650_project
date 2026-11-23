"""Download what M9-5 scores: the test set's and stress slice's stored incidents, and the incident
store's retry log lines over the same days. Read-only.

    cd AnomalyPulse
    python -m genai.m9_5.fetch --profile <profile>

Writes genai/m9_5/data/records.json (one item per incident, as record.from_dynamodb gives it, plus
the run's slot, phase and labeled type from datasets/v1's registry) and retries.jsonl (the
incident_retry lines, M8-3). An incident belongs to a run by its experiment_id, which the store took
from the experiment lock; incidents outside v1's test and stress runs are left out.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data"
TABLE = "anomalypulse-incidents"
LOG_GROUP = "/aws/lambda/anomalypulse-incidents"
BUCKET_PARAMETER = "/anomalypulse/telemetry/bucket/name"
REGISTRY = "datasets/v1/tier2/experiments.jsonl"
PHASES = ("test", "stress")
# The first test run started 2026-11-13T22:42Z and the stress slice's last ended 2026-11-16T17:07Z
# (Docs/TEST_SET.md, "Status"); the store's last write for a run comes a few minutes after it.
FROM, TO = "2026-11-13T22:00:00Z", "2026-11-16T18:30:00Z"


def _epoch(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def registry(session):
    s3, ssm = session.client("s3"), session.client("ssm")
    bucket = ssm.get_parameter(Name=BUCKET_PARAMETER)["Parameter"]["Value"]
    body = s3.get_object(Bucket=bucket, Key=REGISTRY)["Body"].read().decode()
    return {e["experiment_id"]: e for e in map(json.loads, body.splitlines()) if e.get("phase") in PHASES}


def incidents(session):
    from boto3.dynamodb.types import TypeDeserializer
    from incidents import record
    client, td, items, start = session.client("dynamodb"), TypeDeserializer(), [], None
    while True:
        kwargs = {"TableName": TABLE, "KeyConditionExpression": "pk = :p AND sk BETWEEN :a AND :b",
                  "ExpressionAttributeValues": {":p": {"S": "INCIDENT"}, ":a": {"S": FROM}, ":b": {"S": TO}}}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = client.query(**kwargs)
        items += [record.from_dynamodb({k: td.deserialize(v) for k, v in i.items()}) for i in page["Items"]]
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def retries(session):
    logs = session.client("logs")
    query = logs.start_query(logGroupName=LOG_GROUP, startTime=_epoch(FROM), endTime=_epoch(TO), limit=10000,
                             queryString="filter @message like /incident_retry/ | fields @message")["queryId"]
    while (result := logs.get_query_results(queryId=query))["status"] in ("Scheduled", "Running"):
        time.sleep(2)
    if result["status"] != "Complete":
        raise RuntimeError(f"the log query ended {result['status']}")
    lines = [json.loads(f["value"]) for row in result["results"] for f in row if f["field"] == "@message"]
    return sorted(lines, key=lambda line: (line["incident_id"], line["for_type"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name="us-east-2")
    runs = registry(session)
    records = []
    for item in incidents(session):
        run = runs.get(item.get("experiment_id"))
        if run:
            records.append({**item, "slot": run["slot"], "phase": run["phase"],
                            "labeled_type": run["incident_type"]})
    records.sort(key=lambda r: r["sk"])
    ids = {r["incident_id"] for r in records}
    lines = [line for line in retries(session) if line["incident_id"] in ids]
    DATA.mkdir(exist_ok=True)
    (DATA / "records.json").write_text(json.dumps(records, indent=1) + "\n")
    (DATA / "retries.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    counts = {p: sum(r["phase"] == p for r in records) for p in PHASES}
    print(f"{len(records)} incidents ({counts}), {len(lines)} retry lines, written to {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
