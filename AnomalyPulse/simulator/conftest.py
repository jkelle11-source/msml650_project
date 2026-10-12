"""Fixtures shared by the simulator's tests."""
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# simulator/ is a package, so it is imported by name from the directory above it.
sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import injectors
from simulator.stack import Stack

REGION = "us-east-2"
BUCKET = "anomalypulse-telemetry-000000000000-us-east-2"
EXPERIMENTS_TABLE = "anomalypulse-experiments"
FAULT_CONFIG_TABLE = "anomalypulse-fault-config"


@pytest.fixture
def registry():
    # Tests that register throwaway injectors get the real registry back afterwards.
    injectors._discover()
    saved = dict(injectors._registry)
    yield injectors._registry
    injectors._registry.clear()
    injectors._registry.update(saved)


@pytest.fixture
def aws(monkeypatch):
    # An in-memory copy of the stack resources the simulator uses. moto answers
    # every boto3 call made while this fixture is active, and evaluates
    # DynamoDB condition expressions for real, which a stub cannot. The
    # credentials are placeholders and AWS_PROFILE is cleared, so nothing here
    # can reach a real account.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    with mock_aws():
        session = boto3.Session(region_name=REGION)

        # Same key schemas as infra/shared/template.yaml.
        dynamodb = session.resource("dynamodb")
        dynamodb.create_table(
            TableName=EXPERIMENTS_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        dynamodb.create_table(
            TableName=FAULT_CONFIG_TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "service", "AttributeType": "S"},
                                  {"AttributeName": "fault_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "service", "KeyType": "HASH"},
                       {"AttributeName": "fault_id", "KeyType": "RANGE"}])

        s3 = session.client("s3")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        # Versioning is only so a test can see each write to the manifest's key.
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})

        ssm = session.client("ssm")
        for name, value in {
            "/anomalypulse/telemetry/bucket/name": BUCKET,
            "/anomalypulse/tables/experiments/name": EXPERIMENTS_TABLE,
            "/anomalypulse/tables/fault-config/name": FAULT_CONFIG_TABLE,
        }.items():
            ssm.put_parameter(Name=name, Value=value, Type="String")

        yield session


@pytest.fixture
def stack(aws):
    return Stack.connect(aws)
