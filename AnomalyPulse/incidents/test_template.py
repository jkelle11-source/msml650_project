"""Checks on infra/incidents/template.yaml (M8-3): the properties the design depends on. They read
the file as YAML and can't tell whether CloudFormation will accept it; Jake's deploy does that.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_template.py -q
"""
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from incidents import handler  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "infra" / "incidents" / "template.yaml"


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
RESOURCES = yaml.load(TEMPLATE.read_text(), Loader=Loader)["Resources"]
FUNCTION = RESOURCES["IncidentsFunction"]["Properties"]


def statements():
    out = []
    for policy in RESOURCES["IncidentsRole"]["Properties"]["Policies"]:
        out += policy["PolicyDocument"]["Statement"]
    return out


def actions(statement):
    a = statement["Action"]
    return [a] if isinstance(a, str) else a


def table_statement(table):
    found = [s for s in statements() if any(a.startswith("dynamodb:") for a in actions(s))
             and table in json.dumps(s["Resource"])]
    assert len(found) == 1
    return found[0]


def test_one_event_at_a_time_and_a_timeout_longer_than_the_explain_lambdas():
    assert FUNCTION["ReservedConcurrentExecutions"] == 1
    assert FUNCTION["Timeout"] == 150 > 120
    assert FUNCTION["Handler"] == "incidents.handler.handler"


def test_failures_go_to_the_queue_and_there_is_no_success_destination():
    config = FUNCTION["EventInvokeConfig"]
    assert config["MaximumRetryAttempts"] == 2
    assert config["DestinationConfig"]["OnFailure"]["Type"] == "SQS"
    assert "OnSuccess" not in config["DestinationConfig"]


def test_the_rule_reads_the_diagnosis_lambdas_success_events_on_the_pipeline_bus():
    rule = RESOURCES["DiagnosedWindowRule"]["Properties"]
    assert rule["EventBusName"] == "anomalypulse-pipeline"
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["lambda"]
    assert pattern["detail-type"] == ["Lambda Function Invocation Result - Success"]
    assert "function:anomalypulse-diagnosis" in json.dumps(pattern["detail"])
    assert "DeadLetterConfig" in rule["Targets"][0] and "InputPath" not in rule["Targets"][0]


def test_only_the_rule_can_invoke_the_function_and_send_to_the_queue():
    assert "DiagnosedWindowRule" in json.dumps(RESOURCES["DiagnosedWindowPermission"]["Properties"]["SourceArn"])
    policy = RESOURCES["IncidentsFailureQueuePolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert "DiagnosedWindowRule" in json.dumps(policy["Condition"])


def test_every_dynamodb_statement_is_locked_to_its_partition_keys():
    for statement in statements():
        if any(a.startswith("dynamodb:") for a in actions(statement)):
            assert "dynamodb:LeadingKeys" in statement["Condition"]["ForAllValues:StringEquals"]
            assert statement["Condition"]["Null"]["dynamodb:LeadingKeys"] == "false"


def test_the_incidents_table_is_reached_only_through_its_own_partitions():
    statement = table_statement("anomalypulse-incidents")
    assert sorted(actions(statement)) == ["dynamodb:GetItem", "dynamodb:PutItem"]
    keys = statement["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"]
    assert sorted(keys) == ["INCIDENT", "INCIDENT_STATE"]
    assert "HEALTH" not in json.dumps(statement)


def test_the_experiments_table_is_read_for_the_lock_item_only():
    statement = table_statement("anomalypulse-experiments")
    assert actions(statement) == ["dynamodb:GetItem"]
    assert statement["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"] == ["LOCK"]


def test_no_statement_allows_everything():
    for statement in statements():
        assert all(a != "*" and not a.endswith(":*") for a in actions(statement))
        assert "'*'" not in json.dumps(statement["Resource"]) and statement["Resource"] != "*"


def test_windows_are_read_from_the_pipelines_prefix_only():
    reads = [s for s in statements() if "s3:GetObject" in actions(s)]
    assert len(reads) == 1 and "/pipeline/windows/*" in json.dumps(reads[0]["Resource"])


def test_every_parameter_the_handler_reads_is_allowed():
    text = json.dumps(RESOURCES["IncidentsRole"])
    for name in handler.PARAMETERS.values():
        assert f"parameter{name}" in text


def test_the_function_may_invoke_only_the_explain_lambda():
    statement = [s for s in statements() if "lambda:InvokeFunction" in actions(s)]
    assert len(statement) == 1 and "function:anomalypulse-explain" in json.dumps(statement[0]["Resource"])