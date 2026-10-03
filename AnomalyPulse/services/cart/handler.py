import json
import os, boto3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from schema_validator import validate

table = boto3.resource('dynamodb').Table(os.environ['TABLE_NAME'])

_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o % 1 == 0 else float(o)
        return super().default(o)


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": dict(_CORS_HEADERS),
        "body": json.dumps({"data": data, "error": error}, cls=_DecimalEncoder),
    }


def _valid_id(value):
    """A cart key attribute (user_id/item_id) must be a non-empty string.

    DynamoDB declares user_id/item_id as type "S"; a non-string (e.g. a JSON
    number) would otherwise reach put_item and surface as an opaque 500. We
    reject it as a 400 here instead.
    """
    return isinstance(value, str) and value.strip() != ""


def log(request_id, endpoint, method, status_code, start_time, start_proc,
         db_latency_ms=None, db_consumed_capacity=None):
    latency_ms = round((time.time() - start_time) * 1000, 3)
    lambda_duration_ms = round((time.process_time() - start_proc) * 1000, 3)
    cold_start = log.cold_start
    log.cold_start = False
    line = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "request_id": request_id,
        "service": "cart-service",
        "endpoint": endpoint,
        "http_method": method,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "lambda_duration_ms": lambda_duration_ms,
        "cold_start": cold_start,
        "db_latency_ms": db_latency_ms,
        "db_consumed_capacity": db_consumed_capacity,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }
    ok, errors = validate(line)
    if not ok:
        print(json.dumps({"telemetry_error": str(errors[0].message)}))
        return
    print(json.dumps(line))


log.cold_start = True


def handler(event, context):
    start_time = time.time()
    start_proc = time.process_time()
    request_id = (
        (event.get("requestContext") or {}).get("requestId")
        or getattr(context, "aws_request_id", None)
        or str(uuid.uuid4())
    )
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
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            user_id = body.get("user_id")
            item_id = body.get("item_id")
            quantity = body.get("quantity")

            if not _valid_id(user_id) or not _valid_id(item_id):
                resp = _resp(400, error={"code": "BAD_REQUEST",
                                         "message": "user_id and item_id are required non-empty strings"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            if quantity is None:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "quantity is required"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            try:
                quantity = Decimal(str(quantity))
            except InvalidOperation:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "quantity must be a number"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            # is_finite() is checked first (and short-circuits) because NaN/Infinity
            # are valid Decimals: Decimal("NaN") <= 0 raises InvalidOperation and
            # Decimal("Infinity") sails through to put_item, so both would otherwise
            # surface as a 500 for what is really a bad client request.
            if not quantity.is_finite() or quantity <= 0:
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "quantity must be a positive number"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            item = {"user_id": user_id, "item_id": item_id, "quantity": quantity}

            db_start = time.time()
            try:
                db_resp = table.put_item(Item=item, ReturnConsumedCapacity="TOTAL")
            finally:
                db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            resp = _resp(200, data=item)

        elif route == "/cart/{user}" and method == "GET":
            user_id = path_params.get("user")

            if not _valid_id(user_id):
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "user is required"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            db_start = time.time()
            try:
                db_resp = table.query(
                    KeyConditionExpression=Key("user_id").eq(user_id),
                    ReturnConsumedCapacity="TOTAL",
                )
            finally:
                db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            resp = _resp(200, data=db_resp.get("Items", []))

        elif route == "/cart/{user}/{item}" and method == "DELETE":
            user_id = path_params.get("user")
            item_id = path_params.get("item")

            if not _valid_id(user_id) or not _valid_id(item_id):
                resp = _resp(400, error={"code": "BAD_REQUEST", "message": "user and item are required"})
                log(request_id, route, method, resp["statusCode"], start_time, start_proc)
                return resp

            db_start = time.time()
            try:
                db_resp = table.delete_item(
                    Key={"user_id": user_id, "item_id": item_id},
                    ReturnValues="ALL_OLD",
                    ReturnConsumedCapacity="TOTAL",
                )
            finally:
                db_latency_ms = round((time.time() - db_start) * 1000, 3)
            db_consumed_capacity = db_resp.get("ConsumedCapacity", {}).get("CapacityUnits")

            if "Attributes" not in db_resp:
                resp = _resp(404, error={"code": "NOT_FOUND", "message": "Item not found in cart"})
            else:
                resp = _resp(200, data={"user_id": user_id, "item_id": item_id, "deleted": True})

        else:
            resp = _resp(501, error={"code": "NOT_IMPLEMENTED", "message": "route not handled"})

    except ClientError:
        resp = _resp(500, error={"code": "DB_ERROR", "message": "A database error occurred"})
    except Exception:
        resp = _resp(500, error={"code": "INTERNAL_ERROR", "message": "An internal error occurred"})

    log(request_id, route, method, resp["statusCode"], start_time, start_proc,
         db_latency_ms=db_latency_ms, db_consumed_capacity=db_consumed_capacity)
    return resp
