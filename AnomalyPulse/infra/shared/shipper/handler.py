import base64, gzip, json, os, boto3
from datetime import datetime, timezone

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET_NAME"]

def handler(event, context):
    
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    service = payload["logGroup"].split("-")[-1] + "-service"
    
    raw_logs = []
    error_logs = []
    
    for e in payload["logEvents"]:
        try:
            json.loads(e["message"])
            raw_logs.append(e["message"])
        except json.JSONDecodeError:
            error_logs.append(e["message"])
    
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    if raw_logs:
        s3.put_object(
            Bucket = BUCKET,
            Key = F"raw/service={service}/dt={date}/part-{context.aws_request_id}.json",
            Body = "\n".join(raw_logs).encode("utf-8")
        )
    
    if error_logs:
        s3.put_object(
            Bucket = BUCKET,
            Key = F"errors/service={service}/dt={date}/part-{context.aws_request_id}.json",
            Body = "\n".join(error_logs).encode("utf-8")
        )
        