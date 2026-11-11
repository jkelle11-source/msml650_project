"""The live source (M8-5): the inputs build_dashboard takes, read from where M8 stores them.

- window: the Incidents table's health item (pk "HEALTH", sk "LATEST"), which the pipeline's window builder
  rewrites every minute (Docs/PIPELINE.md, "The health item, for M8-5").
- incident: the newest incident record (pk "INCIDENT", sk started_at), only while its status is OPEN, given to
  build_dashboard through incidents.record.to_incident, never raw: that holds back a flagged diagnosis or one
  made for an earlier type (Docs/INCIDENT_FEED.md, "For M8-5"). build_dashboard shows any incident it is given
  as active, so a CLOSED record must not be passed.
- manifest: the run's manifest, experiments/<experiment_id>/manifest.json in the telemetry bucket, when the
  open record has an experiment_id (a real incident has none, and shows no ground truth).

DynamoDB gives numbers back as Decimal, which json.dumps can't write: everything returned here has been
through record.from_dynamodb. Read only. Nothing here falls back to mock data; a failure raises, and the
handler answers 503 with the reason.
"""
import json

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from incidents import record

HEALTH_KEY = {"pk": "HEALTH", "sk": "LATEST"}


def manifest_key(experiment_id: str) -> str:
    """Where the simulator writes a run's manifest (simulator/manifest.py's manifest_key)."""
    return f"experiments/{experiment_id}/manifest.json"


def latest_health(table) -> dict | None:
    """The health item, with Decimals converted and telemetry_late kept; None if there is no item yet."""
    item = table.get_item(Key=HEALTH_KEY).get("Item")
    return None if item is None else record.from_dynamodb(item)


def open_incident(table) -> dict | None:
    """The newest incident record, converted, if its status is OPEN; otherwise (CLOSED, or none) None.
    sk is started_at, so one Query newest first, limit 1, is the latest incident."""
    items = table.query(KeyConditionExpression=Key("pk").eq(record.PK), ScanIndexForward=False, Limit=1)["Items"]
    if not items or items[0]["status"] != "OPEN":
        return None
    return record.from_dynamodb(items[0])


def manifest_for(incident_record: dict, s3, bucket: str) -> dict | None:
    """The manifest of the record's run, parsed; None when the record has no experiment_id, or when S3 says
    the object doesn't exist (error code NoSuchKey). Any other error raises: without s3:ListBucket, S3
    answers AccessDenied even for a missing object, and that must not read as "no ground truth"."""
    experiment_id = incident_record.get("experiment_id")
    if not experiment_id:
        return None
    try:
        body = s3.get_object(Bucket=bucket, Key=manifest_key(experiment_id))["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NoSuchKey":
            return None
        raise
    return json.loads(body)


def read(table, s3, bucket: str) -> tuple:
    """(window, incident, manifest) for build_dashboard."""
    window = latest_health(table)
    incident_record = open_incident(table)
    if incident_record is None:
        return window, None, None
    return window, record.to_incident(incident_record), manifest_for(incident_record, s3, bucket)
