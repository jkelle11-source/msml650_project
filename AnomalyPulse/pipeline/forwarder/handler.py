import base64, gzip, json, os, boto3

events = boto3.client("events")
BUS_NAME = os.environ["BUS_NAME"]

def entries(records) -> list[dict]:
    """PutEvents entries, up to 100 records each."""
    out = []
    for i in range(0, len(records), 100):
        out.append({
            "EventBusName": BUS_NAME,
            "Source": "anomalypulse.telemetry",
            "DetailType": "TelemetryRecords",
            "Detail": json.dumps({"records": records[i:i + 100]})
        })
    return out
    
def handler(event, context) -> dict:
    """{"records": <records sent>, "events": <entries sent>}."""
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    
    if payload["messageType"] == "CONTROL_MESSAGE":
        return {"records": 0, "events": 0}
    
    records = [json.loads(e["message"]) for e in payload["logEvents"]]
    batch = entries(records)
    
    for i in range(0, len(batch), 10):
        chunk = batch[i:i + 10]
        response = events.put_events(Entries = chunk)
        # A partial failure doesn't raise: PutEvents reports it in FailedEntryCount.
        if response["FailedEntryCount"]:
            codes = sorted({e["ErrorCode"] for e in response["Entries"] if "ErrorCode" in e})
            raise RuntimeError(f"PutEvents: {response['FailedEntryCount']} of {len(chunk)} entries failed {codes}")
    
    return {"records": len(records), "events": len(batch)}
    