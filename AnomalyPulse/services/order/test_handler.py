import json
import os
import boto3
import pytest
from moto import mock_aws

os.environ["AWS_DEFAULT_REGION"] = "us-east-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["TABLE_NAME"] = "test-orders-table"
os.environ["USE_PAYMENT_STUB"] = "true"

from handler import handler
import handler as handler_module

@pytest.fixture
def orders_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
        table = dynamodb.create_table(TableName=os.environ["TABLE_NAME"], KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
                                      AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],BillingMode="PAY_PER_REQUEST")
        handler_module.table = table
        yield table
        
def test_create_order(orders_table):
    event = {"resource": "/orders","httpMethod": "POST","pathParameters": None, "body": json.dumps({"customer_id": "cust-1","items": [{"sku": "widget", "price": 10, "quantity": 2}],}),}
    response = handler(event, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 201
    assert body["error"] is None
    assert body["data"]["customer_id"] == "cust-1"
    assert body["data"]["total"] == 20
    assert body["data"]["status"] == "confirmed"
    assert "order_id" in body["data"]

def test_create_order_invalid_input(orders_table):
    event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None, "body": json.dumps({"customer_id": "cust-1"}),}
    response = handler(event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 400
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_BODY"

def test_create_order_malformed_json(orders_table):
    event = { "resource": "/orders","httpMethod": "POST", "pathParameters": None,"body": "{not valid json"}
    response = handler(event, None)
    assert response["statusCode"] == 400

def test_get_order(orders_table):
    create_event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None,
                    "body": json.dumps({"customer_id": "cust-2", "items": [{"sku": "gadget", "price": 5, "quantity": 3}],}),}
    create_response = handler(create_event, None)
    order_id = json.loads(create_response["body"])["data"]["order_id"]
    get_event = {"resource": "/orders/{id}", "httpMethod": "GET", "pathParameters": {"id": order_id},}
    response = handler(get_event, None)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["data"]["order_id"] == order_id
    assert body["data"]["customer_id"] == "cust-2"

def test_get_order_not_found(orders_table):
    event = {"resource": "/orders/{id}", "httpMethod": "GET", "pathParameters": {"id": "does-not-exist"},}
    response = handler(event, None)
    assert response["statusCode"] == 404

def test_create_order_payment_declined(orders_table, monkeypatch):
    monkeypatch.setattr(handler_module, "call_payment_service", lambda order_id, amount, customer_id: {
        "envelope": {"data": {"service": "payment", "outcome": "declined"}, "error": None},"latency_ms": 5.0, "error": False,},)
    event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None, "body": json.dumps({"customer_id": "cust-3", "items": [{"sku": "widget", "price": 10, "quantity": 1}],}), }
    response = handler(event, None)
    body = json.loads(response["body"])
    # Order still gets created, just marked payment_failed. this matches the handler's current logic of never raising on a declined payment.
    assert response["statusCode"] == 201
    assert body["data"]["status"] == "payment_failed"

def test_create_order_payment_service_error(orders_table, monkeypatch):
    monkeypatch.setattr(handler_module, "call_payment_service",
                        lambda order_id, amount, customer_id: {"envelope": {"data": None, "error": {"message": "timeout", "code": "PAYMENT_TIMEOUT"}}, "latency_ms": 3000.0, "error": True,},)
    event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None,
              "body": json.dumps({ "customer_id": "cust-4", "items": [{"sku": "widget", "price": 10, "quantity": 1}],}),}
    response = handler(event, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 502
    assert body["data"] is None
    assert body["error"]["code"] == "PAYMENT_TIMEOUT"

def test_unsupported_method(orders_table):
    event = {"resource": "/orders/{id}","httpMethod": "DELETE","pathParameters": {"id": "some-id"},}
    response = handler(event, None)
    assert response["statusCode"] == 404

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
