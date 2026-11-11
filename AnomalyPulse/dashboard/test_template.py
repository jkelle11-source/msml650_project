"""Checks on infra/dashboard/template.yaml (M8-5): the properties the live source depends on. They read the
file as YAML and can't tell whether CloudFormation will accept it; the deploy does that.

Run from AnomalyPulse/:  python3 -m pytest dashboard/test_template.py -q
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

import handler  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "infra" / "dashboard" / "template.yaml"


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


Loader.add_multi_constructor("!", _tag)
DOC = yaml.load(TEMPLATE.read_text(), Loader=Loader)
RESOURCES = DOC["Resources"]
PARAMETERS = DOC["Parameters"]
FUNCTION = RESOURCES["DashboardFunction"]["Properties"]
ENVIRONMENT = FUNCTION["Environment"]["Variables"]


def statements():
    out = []
    for policy in RESOURCES["DashboardRole"]["Properties"]["Policies"]:
        out += policy["PolicyDocument"]["Statement"]
    return out


def actions(statement):
    action = statement["Action"]
    return [action] if isinstance(action, str) else action


def test_the_function_has_every_setting_the_live_read_needs():
    assert set(handler.LIVE_SETTINGS) <= set(ENVIRONMENT)
    assert ENVIRONMENT["INCIDENTS_TABLE"] == {"Ref": "IncidentsTableName"}
    assert ENVIRONMENT["TELEMETRY_BUCKET"] == {"Ref": "TelemetryBucketName"}


def test_the_source_is_live_by_default_and_can_only_be_live_or_mock():
    assert ENVIRONMENT["DASHBOARD_SOURCE"] == {"Ref": "DashboardSource"}
    assert PARAMETERS["DashboardSource"]["Default"] == "live"
    assert sorted(PARAMETERS["DashboardSource"]["AllowedValues"]) == ["live", "mock"]


def test_the_table_and_bucket_names_come_from_their_ssm_parameters():
    assert PARAMETERS["IncidentsTableName"]["Default"] == "/anomalypulse/tables/incidents/name"
    assert PARAMETERS["TelemetryBucketName"]["Default"] == "/anomalypulse/telemetry/bucket/name"
    for name in ("IncidentsTableName", "TelemetryBucketName"):
        assert PARAMETERS[name]["Type"] == "AWS::SSM::Parameter::Value<String>"


def test_the_role_only_reads():
    granted = {a for s in statements() for a in actions(s)}
    assert granted == {"dynamodb:GetItem", "dynamodb:Query", "s3:ListBucket", "s3:GetObject"}
    assert all(s["Effect"] == "Allow" for s in statements())


def test_the_table_reads_are_limited_to_the_health_and_incident_partitions():
    table, = [s for s in statements() if "dynamodb:Query" in actions(s)]
    assert sorted(table["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"]) == ["HEALTH", "INCIDENT"]
    assert table["Condition"]["Null"]["dynamodb:LeadingKeys"] == "false"
    assert "${IncidentsTableName}" in table["Resource"]["Sub"]


def test_objects_are_readable_only_at_the_manifests():
    get, = [s for s in statements() if "s3:GetObject" in actions(s)]
    assert get["Resource"] == {"Sub": "arn:aws:s3:::${TelemetryBucketName}/experiments/*/manifest.json"}


def test_list_bucket_lets_a_missing_manifest_read_as_not_found():
    listing, = [s for s in statements() if "s3:ListBucket" in actions(s)]
    assert listing["Resource"] == {"Sub": "arn:aws:s3:::${TelemetryBucketName}"}


def test_cors_allows_only_the_console_s_addresses():
    cors = RESOURCES["DashboardApi"]["Properties"]["CorsConfiguration"]
    assert sorted(cors["AllowOrigins"]) == ["http://127.0.0.1:4173", "http://localhost:4173"]
    assert cors["AllowMethods"] == ["GET"]
