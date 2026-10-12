"""The deployed stack's resources that the simulator uses (M3-1).

Names are looked up in SSM under /anomalypulse/, where template.yaml publishes
them, so nothing here is tied to one account.
"""
from dataclasses import dataclass

import boto3

from simulator.control_plane import ControlPlane
from simulator.lock import Lock

_BUCKET = "/anomalypulse/telemetry/bucket/name"
_EXPERIMENTS_TABLE = "/anomalypulse/tables/experiments/name"
_FAULT_CONFIG_TABLE = "/anomalypulse/tables/fault-config/name"


@dataclass(frozen=True)
class Stack:
    session: boto3.Session  # under the simulator role
    s3: object              # S3 client, for manifests
    bucket: str             # the telemetry bucket
    lock: Lock
    control_plane: ControlPlane

    @classmethod
    def connect(cls, session: boto3.Session) -> "Stack":
        names = [_BUCKET, _EXPERIMENTS_TABLE, _FAULT_CONFIG_TABLE]
        response = session.client("ssm").get_parameters(Names=names)
        found = {parameter["Name"]: parameter["Value"] for parameter in response["Parameters"]}
        missing = [name for name in names if name not in found]
        if missing:
            raise RuntimeError(f"SSM parameters not found: {', '.join(missing)}. "
                               f"Is the anomalypulse-shared stack deployed in this region?")

        dynamodb = session.resource("dynamodb")
        return cls(
            session=session,
            s3=session.client("s3"),
            bucket=found[_BUCKET],
            lock=Lock(dynamodb.Table(found[_EXPERIMENTS_TABLE])),
            control_plane=ControlPlane(dynamodb.Table(found[_FAULT_CONFIG_TABLE])),
        )
