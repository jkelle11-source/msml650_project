import json
import os
import re
import sys
import uuid
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws



os.environ["AWS_DEFAULT_REGION"] = "us-east-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["TABLE_NAME"] = "test-cart-table"

# The validator ships in the anomalypulse-telemetry Lambda layer; locally it is
# only importable once the layer's python/ dir is on sys.path.
_LAYER_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "layers", "telemetry", "python")
sys.path.insert(0, os.path.abspath(_LAYER_PATH))

from handler import handler
import handler as handler_module
from schema_validator import validate


@pytest.fixture
def cart_table():
    with mock_aws():
        dynamodb = boto3.resource(
            "dynamodb",
            region_name=os.environ["AWS_DEFAULT_REGION"],
        )

        table = dynamodb.create_table(
            TableName=os.environ["TABLE_NAME"],
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "item_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "item_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        handler_module.table = table
        yield table


def test_create_cart(cart_table):
    event = {
        "resource": "/cart",
        "httpMethod": "POST",
        "pathParameters": None,
        "body": json.dumps(
            {
                "user_id": "u1",
                "item_id": "i1",
                "quantity": 2,
            }
        ),
    }

    response = handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["data"] == {
        "user_id": "u1",
        "item_id": "i1",
        "quantity": 2,
    }


def _post_cart_event(body):
    return {
        "resource": "/cart",
        "httpMethod": "POST",
        "pathParameters": None,
        "body": json.dumps(body),
    }


def test_create_cart_missing_ids(cart_table):
    response = handler(
        _post_cart_event({"user_id": "", "item_id": "", "quantity": 2}), None
    )
    assert response["statusCode"] == 400


def test_create_cart_missing_quantity(cart_table):
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1"}), None
    )
    assert response["statusCode"] == 400


@pytest.mark.parametrize("quantity", [0, -1])
def test_create_cart_non_positive_quantity(cart_table, quantity):
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": quantity}), None
    )
    assert response["statusCode"] == 400


def test_get_cart(cart_table):
    # Seed an item so the GET actually exercises retrieval, not just an empty table.
    create_response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 2}), None
    )
    assert create_response["statusCode"] == 200

    event = {
        "resource": "/cart/{user}",
        "httpMethod": "GET",
        "pathParameters": {
            "user": "u1",
        },
    }

    response = handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["data"] == [
        {
            "user_id": "u1",
            "item_id": "i1",
            "quantity": 2,
        }
    ]

def test_delete_cart(cart_table):
    create_event = {
        "resource": "/cart",
        "httpMethod": "POST",
        "pathParameters": None,
        "body": json.dumps(
            {
                "user_id": "u1",
                "item_id": "i1",
                "quantity": 2,
            }
        ),
    }

    handler(create_event, None)
    delete_event = {
        "resource": "/cart/{user}/{item}",
        "httpMethod": "DELETE",
        "pathParameters": {
            "user": "u1",
            "item": "i1",
        },
    }
    response = handler(delete_event, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["data"]["deleted"] is True

def test_delete_missing_item(cart_table):
    event = {
        "resource": "/cart/{user}/{item}",
        "httpMethod": "DELETE",
        "pathParameters": {
            "user": "missing-user",
            "item": "missing-item",
        },
    }

    response = handler(event, None)
    assert response["statusCode"] == 404


def test_create_cart_non_string_id(cart_table):
    # A JSON number for a String-typed key must be rejected as 400, not reach
    # DynamoDB and surface as a 500.
    response = handler(
        _post_cart_event({"user_id": 123, "item_id": "i1", "quantity": 1}), None
    )
    assert response["statusCode"] == 400


def test_get_cart_missing_user(cart_table):
    event = {
        "resource": "/cart/{user}",
        "httpMethod": "GET",
        "pathParameters": None,
    }
    response = handler(event, None)
    assert response["statusCode"] == 400


def test_post_cart_overwrites_quantity(cart_table):
    # POST /cart is "set quantity": re-posting the same item replaces the
    # quantity rather than accumulating it. This pins the intended semantics.
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 2}), None)
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 5}), None)

    get_event = {
        "resource": "/cart/{user}",
        "httpMethod": "GET",
        "pathParameters": {"user": "u1"},
    }
    body = json.loads(handler(get_event, None)["body"])
    assert body["data"] == [{"user_id": "u1", "item_id": "i1", "quantity": 5}]


def test_response_has_cors_header(cart_table):
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None
    )
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


def test_invalid_json_body_returns_400(cart_table):
    # A malformed JSON body must be rejected as a 400, not crash into a 500.
    event = {
        "resource": "/cart",
        "httpMethod": "POST",
        "pathParameters": None,
        "body": "{not valid json",
    }
    response = handler(event, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "BAD_REQUEST"


def test_create_cart_non_numeric_quantity(cart_table):
    # A non-numeric quantity must be rejected as 400 (the Decimal(InvalidOperation)
    # path), not reach DynamoDB and surface as a 500.
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": "abc"}), None
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["error"]["code"] == "BAD_REQUEST"


def test_unhandled_route_returns_501(cart_table):
    # A method/route combination the service does not implement returns 501.
    event = {
        "resource": "/cart",
        "httpMethod": "PUT",
        "pathParameters": None,
        "body": None,
    }
    response = handler(event, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 501
    assert body["error"]["code"] == "NOT_IMPLEMENTED"


def test_cold_start_only_first_invocation(cart_table, capsys):
    # cold_start is True on the first invocation of a warm container and False
    # thereafter. Reset the module flag so the test is independent of order.
    handler_module.log.cold_start = True
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    handler(_post_cart_event({"user_id": "u1", "item_id": "i2", "quantity": 1}), None)
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines()
               if l.strip().startswith("{")]
    assert records[0]["cold_start"] is True
    assert records[1]["cold_start"] is False


def test_telemetry_records_db_metrics(cart_table, capsys):
    # A request that hits DynamoDB must record non-null db_latency_ms and
    # db_consumed_capacity in the telemetry line (Tier-1 observable signals).
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    record = [json.loads(l) for l in capsys.readouterr().out.splitlines()
              if l.strip().startswith("{")][-1]
    assert record["db_latency_ms"] is not None
    assert record["db_consumed_capacity"] is not None


def test_client_error_returns_db_error_without_leak(cart_table, monkeypatch, capsys):
    # A DynamoDB ClientError (e.g. ProvisionedThroughputExceededException, the
    # M3 throttling path) must return the DB_ERROR code with a generic message,
    # distinct from the generic INTERNAL_ERROR path, and must not leak the cause
    # or any Tier-2 field onto the request path.
    def throttle(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "Throughput exceeds the current capacity"}},
            "PutItem",
        )

    monkeypatch.setattr(cart_table, "put_item", throttle)
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert body["error"]["code"] == "DB_ERROR"
    assert "ProvisionedThroughputExceededException" not in body["error"]["message"]
    printed = capsys.readouterr().out
    assert "ProvisionedThroughputExceededException" not in printed
    assert "error_type" not in printed


def test_internal_error_does_not_leak_detail(cart_table, monkeypatch, capsys):
    # A DynamoDB failure must return a generic 500 message, never the raw
    # exception text, and must not emit any Tier-2 field (e.g. error_type) on
    # the request path (telemetry_schema.json emission rules).
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(cart_table, "put_item", boom)
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None
    )
    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in body["error"]["message"]
    # The cause must not leak into anything the service prints, either.
    printed = capsys.readouterr().out
    assert "secret internal detail" not in printed
    assert "error_type" not in printed


def test_telemetry_line_is_tier1_only(cart_table, capsys):
    # The emitted telemetry record must contain exactly the correlation +
    # Tier-1 fields from telemetry_schema.json, and no Tier-2 field.
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]
    record = json.loads(lines[-1])

    expected_fields = {
        "timestamp", "request_id", "service", "endpoint", "http_method",
        "status_code", "latency_ms", "lambda_duration_ms", "cold_start",
        "db_latency_ms", "db_consumed_capacity",
        "dependency", "dependency_latency_ms", "dependency_error",
    }
    tier2_fields = {"error_type", "db_throttled", "incident_type", "severity",
                    "fault_injection_params"}
    assert set(record.keys()) == expected_fields
    assert tier2_fields.isdisjoint(record.keys())


# --- telemetry validates against the shared schema (M2-3) --------------------

_TIMESTAMP_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _records(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines()
            if l.strip().startswith("{")]


def _assert_valid(record):
    ok, errors = validate(record)
    assert ok, errors[0].message if errors else "invalid"


def test_each_route_emits_one_schema_valid_record(cart_table, capsys):
    calls = [
        ("rid-post", "/cart", "POST", {
            "resource": "/cart", "httpMethod": "POST", "pathParameters": None,
            "body": json.dumps({"user_id": "u1", "item_id": "i1", "quantity": 2})}),
        ("rid-get", "/cart/{user}", "GET", {
            "resource": "/cart/{user}", "httpMethod": "GET",
            "pathParameters": {"user": "u1"}}),
        ("rid-delete", "/cart/{user}/{item}", "DELETE", {
            "resource": "/cart/{user}/{item}", "httpMethod": "DELETE",
            "pathParameters": {"user": "u1", "item": "i1"}}),
    ]
    for request_id, endpoint, method, event in calls:
        event["requestContext"] = {"requestId": request_id}
        response = handler(event, None)
        assert response["statusCode"] == 200

        records = _records(capsys)
        assert len(records) == 1, f"{method} {endpoint} emitted {len(records)} records"
        record = records[0]
        _assert_valid(record)
        assert record["service"] == "cart-service"
        assert record["endpoint"] == endpoint
        assert record["http_method"] == method
        assert record["request_id"] == request_id
        assert _TIMESTAMP_MS.match(record["timestamp"])
        assert record["db_latency_ms"] is not None
        assert record["db_consumed_capacity"] is not None
        assert record["dependency"] is None
        assert record["dependency_latency_ms"] is None
        assert record["dependency_error"] is None


_GET = {"resource": "/cart/{user}", "httpMethod": "GET"}
_DELETE = {"resource": "/cart/{user}/{item}", "httpMethod": "DELETE"}


# (event, expected status, whether the request reaches DynamoDB)
@pytest.mark.parametrize("event, status, hits_db", [
    ({"resource": "/cart", "httpMethod": "POST", "pathParameters": None,
      "body": "{not valid json"}, 400, False),
    (_post_cart_event({"user_id": "", "item_id": "i1", "quantity": 1}), 400, False),
    (_post_cart_event({"user_id": "u1", "item_id": 7, "quantity": 1}), 400, False),
    (_post_cart_event({"user_id": "u1", "item_id": "i1"}), 400, False),
    (_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": "abc"}), 400, False),
    (_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 0}), 400, False),
    ({**_GET, "pathParameters": None}, 400, False),
    ({**_DELETE, "pathParameters": None}, 400, False),
    ({**_DELETE, "pathParameters": {"user": "u1"}}, 400, False),
    ({**_DELETE, "pathParameters": {"user": "nobody", "item": "nothing"}}, 404, True),
    ({"resource": "/cart", "httpMethod": "DELETE", "pathParameters": None,
      "body": None}, 501, False),
])
def test_error_paths_emit_schema_valid_record(cart_table, capsys, event, status, hits_db):
    response = handler(event, None)
    assert response["statusCode"] == status
    records = _records(capsys)
    assert len(records) == 1
    _assert_valid(records[0])
    assert records[0]["status_code"] == status
    assert (records[0]["db_latency_ms"] is not None) == hits_db
    assert (records[0]["db_consumed_capacity"] is not None) == hits_db


@pytest.mark.parametrize("op, event", [
    ("put_item", _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1})),
    ("query", {**_GET, "pathParameters": {"user": "u1"}}),
    ("delete_item", {**_DELETE, "pathParameters": {"user": "u1", "item": "i1"}}),
])
def test_db_failure_still_emits_schema_valid_record(cart_table, monkeypatch, capsys, op, event):
    def throttle(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                     "Message": "Throughput exceeds the current capacity"}},
                          op)

    monkeypatch.setattr(cart_table, op, throttle)
    response = handler(event, None)
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["error"]["code"] == "DB_ERROR"
    records = _records(capsys)
    assert len(records) == 1
    _assert_valid(records[0])
    assert records[0]["status_code"] == 500
    # The failed call is still a DB call: its duration is the observable
    # throttling signal, while no capacity figure comes back.
    assert records[0]["db_latency_ms"] is not None
    assert records[0]["db_consumed_capacity"] is None


def test_internal_error_still_emits_schema_valid_record(cart_table, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(cart_table, "query", boom)
    response = handler({**_GET, "pathParameters": {"user": "u1"}}, None)
    assert response["statusCode"] == 500
    records = _records(capsys)
    assert len(records) == 1
    _assert_valid(records[0])
    assert records[0]["status_code"] == 500


def test_get_empty_cart_returns_empty_list(cart_table, capsys):
    response = handler({**_GET, "pathParameters": {"user": "nobody"}}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"] == []
    records = _records(capsys)
    assert len(records) == 1
    _assert_valid(records[0])


def test_fractional_quantity_round_trips(cart_table):
    response = handler(
        _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1.5}), None
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["data"]["quantity"] == 1.5


def test_encoder_rejects_non_decimal_unserializable():
    with pytest.raises(TypeError):
        json.dumps({"x": object()}, cls=handler_module._DecimalEncoder)


def test_timing_fields_are_consistent(cart_table, capsys):
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    record = _records(capsys)[0]
    # The DB call happens inside the handler, so wall-clock latency bounds it.
    assert record["latency_ms"] >= record["db_latency_ms"] >= 0
    assert record["lambda_duration_ms"] >= 0


def test_unsupported_method_is_not_emitted_as_a_record(cart_table, capsys):
    # http_method is an enum (GET/POST/DELETE) in the schema, so a method outside
    # it cannot form a valid record; it is reported as a telemetry_error instead.
    response = handler({"resource": "/cart", "httpMethod": "PUT",
                        "pathParameters": None, "body": None}, None)
    assert response["statusCode"] == 501
    printed = _records(capsys)
    assert len(printed) == 1
    assert set(printed[0]) == {"telemetry_error"}


def test_request_id_prefers_api_gateway_over_lambda_context(cart_table, capsys):
    event = _post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1})
    event["requestContext"] = {"requestId": "apigw-req-1"}
    handler(event, SimpleNamespace(aws_request_id="lambda-req-1"))
    assert _records(capsys)[0]["request_id"] == "apigw-req-1"


def test_request_id_falls_back_to_generated_uuid(cart_table, capsys):
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    first, second = (r["request_id"] for r in _records(capsys))
    assert uuid.UUID(first) and uuid.UUID(second)
    assert first != second


def test_request_id_falls_back_to_lambda_context(cart_table, capsys):
    handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}),
            SimpleNamespace(aws_request_id="lambda-req-1"))
    assert _records(capsys)[0]["request_id"] == "lambda-req-1"


def test_invalid_record_is_dropped_without_failing_request(cart_table, monkeypatch, capsys):
    monkeypatch.setattr(handler_module, "validate",
                        lambda record: (False, [SimpleNamespace(message="bad record")]))
    handler_module.log.cold_start = True
    response = handler(_post_cart_event({"user_id": "u1", "item_id": "i1", "quantity": 1}), None)
    assert response["statusCode"] == 200
    printed = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert printed == [{"telemetry_error": "bad record"}]
    assert handler_module.log.cold_start is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))