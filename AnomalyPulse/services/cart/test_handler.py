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


def test_create_cart_invalid_input(cart_table):
    event = {
        "resource": "/cart",
        "httpMethod": "POST",
        "pathParameters": None,
        "body": json.dumps(
            {
                "user_id": "",
                "item_id": "",
                "quantity": -1,
            }
        ),
    }

    response = handler(event, None)
    assert response["statusCode"] == 400

def test_get_cart(cart_table):
    event = {
        "resource": "/cart/{user}",
        "httpMethod": "GET",
        "pathParameters": {
            "user": "u1",
        },
    }

    response = handler(event, None)
    assert response["statusCode"] == 200

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

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))