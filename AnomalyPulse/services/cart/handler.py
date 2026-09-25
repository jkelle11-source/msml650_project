import json
import os, boto3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}, cls=_DecimalEncoder),
    }


def log(request_id, endpoint, method, status_code, start_time,
         db_latency_ms=None, db_consumed_capacity=None):
    line = {    
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "request_id": request_id,
        "service": "cart-service",
        "endpoint": endpoint,
        "http_method": method,       
        "status_code": status_code,
        "latency_ms": round((time.time() - start_time) * 1000, 3),
        "lambda_duration_ms": round((time.time() - start_time) * 1000, 3),
        "cold_start": log.cold_start,
        "db_latency_ms": db_latency_ms,
        "db_consumed_capacity": db_consumed_capacity,
        "dependency": None,  
        "dependency_latency_ms": None,
        "dependency_error": None,
    }
    print(json.dumps(line))
    log.cold_start = False


log.cold_start = True


def handler(event, context):
    start_time = time.time()
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))
    route = event.get("resource", "")
    method = event.get("httpMethod", "")
    path_params = event.get("pathParameters") or {}

    db_latency_ms = None
    db_consumed_capacity = None

    try:
        if route == "/cart" and method == "POST":
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "Invalid JSON body"})
                log(request_id, route, method, resp["statusCode"], start_time)
                return resp

            user_id = body.get("user_id")
            item_id = body.get("item_id")
            quantity = body.get("quantity")

            if not user_id or not item_id or quantity is None:
                resp = _resp(400, error={"code": "BAD_REQUEST",
                                         "message": "user_id, item_id, and quantity are required"})
                log(request_id, route, method, resp["statusCode"], start_time)
                return resp

            try:
                quantity = Decimal(str(quantity))
            except InvalidOperation:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "quantity must be a number"})
                log(request_id, route, method, resp["statusCode"], start_time)
                return resp

            item = {"user_id": user_id, "item_id": item_id, "quantity": quantity}

            db_start = time.time()
            db_resp = table.put_item(Item=item, ReturnConsumedCapacity="TOTAL")
            db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            resp = _resp(200, data=item)

        elif route == "/cart/{user}" and method == "GET":
            user_id = path_params.get("user")

            db_start = time.time()
            db_resp = table.query(
                KeyConditionExpression=Key("user_id").eq(user_id),
                ReturnConsumedCapacity="TOTAL",
            )
            db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            resp = _resp(200, data=db_resp.get("Items", []))

        elif route == "/cart/{user}/{item}" and method == "DELETE":
            user_id = path_params.get("user")
            item_id = path_params.get("item")

            db_start = time.time()
            db_resp = table.delete_item(
                Key={"user_id": user_id, "item_id": item_id},
                ReturnValues="ALL_OLD",
                ReturnConsumedCapacity="TOTAL",
            )
            db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            if "Attributes" not in db_resp:
                resp = _resp(404, error={"code": "NOT_FOUND", "message": "Item not found in cart"})
            else:
                resp = _resp(200, data={"user_id": user_id, "item_id": item_id, "deleted": True})

        else:
            resp = _resp(501, error={"code": "NOT_IMPLEMENTED", "message": "route not handled"})

    except ClientError as e:
        resp = _resp(500, error={"code": "DB_ERROR", "message": str(e)})
    except Exception as e:
        resp = _resp(500, error={"code": "INTERNAL_ERROR", "message": str(e)})

    log(request_id, route, method, resp["statusCode"], start_time,
         db_latency_ms=db_latency_ms, db_consumed_capacity=db_consumed_capacity)
    return resp
