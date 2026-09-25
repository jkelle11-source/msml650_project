import json
import os, boto3
import time
import uuid
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}),
    }

def handler(event, context):
    route = event["resource"]
    method = event["httpMethod"]
    path_params = event.get("pathParameters") or {}
    try:
        if route == "/cart" and method == "POST":
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "Invalid JSON body"})
                return resp

            user_id = body.get("user_id")
            item_id = body.get("item_id")
            quantity = body.get("quantity")

            if not user_id or not item_id or quantity is None:
                resp = _resp(400, error={"code": "BAD_REQUEST",
                                         "message": "user_id, item_id, and quantity are required"})
                return resp

            item = {"user_id": user_id, "item_id": item_id, "quantity": quantity}
            table.put_item(Item=item)
            resp = _resp(200, data=item)

        elif route == "/cart/{user}" and method == "GET":
            user_id = path_params.get("user")
            db_resp = table.query(
                KeyConditionExpression=Key("user_id").eq(user_id),
                ReturnConsumedCapacity="TOTAL",
            )
            resp = _resp(200, data=db_resp.get("Items", []))

        elif route == "/cart/{user}/{item}" and method == "DELETE":
            user_id = path_params.get("user")
            item_id = path_params.get("item")
            db_resp = table.delete_item(
                Key={"user_id": user_id, "item_id": item_id},
                ReturnValues="ALL_OLD",
                ReturnConsumedCapacity="TOTAL",
            )
            if "Attributes" not in db_resp:
                resp = _resp(404, error={"code": "NOT_FOUND", "message": "Item not found in cart"})
            else:
                resp = _resp(200, data={"user_id": user_id, "item_id": item_id, "deleted": True})

        else:
          resp = _resp(501, error={"code": "NOT_IMPLEMENTED", "message": "route not handled"})
    except ClientError as e:
        resp = _resp(500, error={"code": "DB_ERROR", "message": str(e)})

    return resp
