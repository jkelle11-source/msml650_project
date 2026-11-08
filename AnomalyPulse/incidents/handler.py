"""The M8-3 Lambda: AWS around incidents/core.py.

    handler(event, context): one Diagnosis Lambda success event in (EventBridge, asynchronous).

Every decision is in core.py; this file only reads and writes. It names no table itself: the
names come from SSM at the first call of each execution environment.
"""
import json
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from incidents import core, record

PARAMETERS = {
    "incidents": "/anomalypulse/tables/incidents/name",
    "experiments": "/anomalypulse/tables/experiments/name",
    "bucket": "/anomalypulse/telemetry/bucket/name",
    "explain": "/anomalypulse/genai/explain-function",
}
STATE_KEY = {"pk": "INCIDENT_STATE", "sk": "CURRENT"}
LOCK_KEY = {"pk": "LOCK"}
# The explain Lambda takes 6-32 s and has its own retry; botocore's default read timeout (60 s)
# is shorter than this Lambda's 150 s, and a botocore retry would double the wait.
EXPLAIN_READ_TIMEOUT_S = 140


class AwsDeps:
    """core.process's dependencies, over boto3 resources and clients (or fakes with the same methods)."""

    def __init__(self, table, experiments, s3, lambda_client, bucket, explain_function):
        self.table = table
        self.experiments = experiments
        self.s3 = s3
        self.lambda_client = lambda_client
        self.bucket = bucket
        self.explain_function = explain_function

    def now(self):
        return datetime.now(timezone.utc)

    def get_state(self):
        item = self.table.get_item(Key=STATE_KEY, ConsistentRead=True).get("Item")
        return None if item is None else record.from_dynamodb(item["state"])

    def put_state(self, state):
        self.table.put_item(Item=record.to_dynamodb({**STATE_KEY, "state": state}))

    def get_record(self, started_at):
        item = self.table.get_item(Key={"pk": record.PK, "sk": started_at}, ConsistentRead=True).get("Item")
        return None if item is None else record.from_dynamodb(item)

    def put_record(self, rec):
        self.table.put_item(Item=record.to_dynamodb(rec))

    def read_window(self, key):
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return json.loads(body)

    def read_lock(self):
        item = self.experiments.get_item(Key=LOCK_KEY, ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {"experiment_id": item["experiment_id"], "expires_at": int(item["expires_at"])}

    def explain(self, evidence):
        try:
            response = self.lambda_client.invoke(FunctionName=self.explain_function,
                                                 InvocationType="RequestResponse",
                                                 Payload=json.dumps(evidence).encode("utf-8"))
        except (BotoCoreError, ClientError) as err:
            raise core.ExplainError(type(err).__name__, str(err)) from None
        body = response["Payload"].read()
        try:
            data = json.loads(body)
        except ValueError:
            raise core.ExplainError("BadReply", "the explain Lambda's reply is not JSON") from None
        if response.get("FunctionError"):
            data = data if isinstance(data, dict) else {}
            raise core.ExplainError(str(data.get("errorType", "FunctionError")),
                                    str(data.get("errorMessage", "")))
        return data

    def emit(self, line):
        print(json.dumps(line))


def build_deps():
    import boto3
    from botocore.config import Config

    ssm = boto3.client("ssm")
    names = {key: ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
             for key, parameter in PARAMETERS.items()}
    dynamodb = boto3.resource("dynamodb")
    lambda_client = boto3.client("lambda", config=Config(
        connect_timeout=5, read_timeout=EXPLAIN_READ_TIMEOUT_S, retries={"total_max_attempts": 1}))
    return AwsDeps(dynamodb.Table(names["incidents"]), dynamodb.Table(names["experiments"]),
                   boto3.client("s3"), lambda_client, names["bucket"], names["explain"])


_deps = None


def handler(event, context):
    global _deps
    if _deps is None:
        _deps = build_deps()
    result = core.process(event, _deps)
    print(json.dumps({"event": "incident_step", **result}))
    return result