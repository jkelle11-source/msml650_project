import os
import json
import boto3

from datetime import datetime

BUCKET = os.environ["BUCKET_NAME"]

s3 = boto3.client("s3")

def minute_prefix(timestamp: str) -> str:
    """'2026-11-02T19:47:21.123Z' -> 'pipeline/records/dt=2026-11-02/minute=19-47/'"""
    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    return f"pipeline/records/dt={parsed:%Y-%m-%d}/minute={parsed:%H-%M}/"
    

def handler(event, context) -> dict:
    """{"batchItemFailures": [{"itemIdentifier": <messageId>}, ...]}: the messages to retry."""
    minutes = {}
    ids = {}
    failures = set()
    
    for payload in event["Records"]:
        try:
            envelope = json.loads(payload["body"])
            parsed = [(minute_prefix(r["timestamp"]), r) for r in envelope["detail"]["records"]]
        except Exception:
            failures.add(payload["messageId"])
            continue
        for prefix, record in parsed:
            minutes.setdefault(prefix, []).append(record)
            ids.setdefault(prefix, []).append(payload["messageId"])
            
    for prefix, records in minutes.items():
        try:
            s3.put_object(Bucket = BUCKET,
                        Key = prefix + f"part-{context.aws_request_id}.jsonl",
                        Body = "\n".join(json.dumps(r) for r in records))
        except Exception:
            failures.update(ids[prefix])
            
    return {"batchItemFailures": [{"itemIdentifier": message_id} 
                                  for message_id in sorted(failures)]}