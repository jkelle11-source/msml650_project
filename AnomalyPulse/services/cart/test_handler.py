import json
import os

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

from handler import handler
import handler as handler_module


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

    monkeypatch.setattr(handler_module.table, "put_item", throttle)
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

    monkeypatch.setattr(handler_module.table, "put_item", boom)
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))