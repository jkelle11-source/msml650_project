import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from schema_validator import validate

SERVICE_NAME = "order-service" 
TABLE_NAME = os.environ["TABLE_NAME"]
PAYMENT_FUNCTION_NAME = os.environ.get("PAYMENT_FUNCTION_NAME", "anomalypulse-payment")
USE_PAYMENT_STUB = os.environ.get("USE_PAYMENT_STUB", "true").lower() == "true"

dynamodb = boto3.resource("dynamodb")
_PAYMENT_INVOKE_CONFIG = Config(connect_timeout=2, read_timeout=6, retries={"max_attempts": 1})
lambda_client = boto3.client("lambda", config=_PAYMENT_INVOKE_CONFIG)
table = dynamodb.Table(TABLE_NAME)
_cold_start = True

_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
}

# helpers
def _success(data, status_code=200):
    return {"statusCode": status_code,"headers": dict(_CORS_HEADERS),"body": json.dumps({"data": data, "error": None}, default=_decimal_to_float)}


def _error(message, status_code, code):
    return {"statusCode": status_code, "headers": dict(_CORS_HEADERS), "body": json.dumps({"data": None, "error": {"message": message, "code": code}})}


def _decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# telemetry
def _now_ms() -> str:
    # ms precision, UTC, trailing Z -- matches the schema regex (exactly 3 decimals)
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _build_telemetry_record(request_id, endpoint, http_method, status_code, latency_ms, lambda_duration_ms, cold_start, db=None, dependency=None):
    db = db or {}
    dependency = dependency or {}
    return {"timestamp": _now_ms(),"request_id": request_id,"service": SERVICE_NAME,"endpoint": endpoint,
            "http_method": http_method, "status_code": status_code,"latency_ms": latency_ms,"lambda_duration_ms": lambda_duration_ms,
            "cold_start": cold_start,"db_latency_ms": db.get("latency_ms"),"db_consumed_capacity": db.get("consumed_capacity"),"dependency": dependency.get("name"),
            "dependency_latency_ms": dependency.get("latency_ms"), "dependency_error": dependency.get("error")}


def _emit_telemetry(**kwargs):
    record = _build_telemetry_record(**kwargs)
    ok, errors = validate(record)
    if not ok:
        print(json.dumps({"telemetry_error": str(errors[0].message)}))
        return
    print(json.dumps(record))  # one line -> CloudWatch -> shipper -> S3

# payment
def call_payment_service(order_id, amount, customer_id, request_id):
    start = time.time()

    if USE_PAYMENT_STUB:
        envelope = {"data": {"service": "payment", "outcome": "success"}, "error": None}
        return {"envelope": envelope, "latency_ms": round((time.time() - start) * 1000, 2), "error": False}
    try:
        response = lambda_client.invoke(FunctionName=PAYMENT_FUNCTION_NAME,InvocationType="RequestResponse",
                                        Payload=json.dumps({ "resource": "/payments","httpMethod": "POST",
                                                            "body": json.dumps({"order_id": order_id,"customer_id": customer_id,"amount": amount}, default=_decimal_to_float),
                                                            "requestContext": {"requestId": request_id},}, default=_decimal_to_float).encode("utf-8"),)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        if response.get("FunctionError"):
            envelope = {"data": None, "error": {"message": "payment service error", "code": "PAYMENT_INVOKE_FAILED"}}
            return {"envelope": envelope, "latency_ms": elapsed_ms, "error": True}
        payload = json.loads(response["Payload"].read())
        if isinstance(payload, dict) and "body" in payload:  # payment returns {"statusCode":..., "body": "..."} even invoked directly
            payload = json.loads(payload["body"])
        data = payload.get("data") or {}
        failed = bool(payload.get("error")) or data.get("outcome") != "success"
        if failed and not payload.get("error"):
            payload = {"data": None, "error": {"message": "unexpected payment response", "code": "PAYMENT_INVOKE_FAILED"}}
        return {"envelope": payload, "latency_ms": elapsed_ms, "error": failed}
    except (BotoCoreError, ClientError) as exc:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        envelope = {"data": None, "error": {"message": str(exc), "code": "PAYMENT_INVOKE_FAILED"}}
        return {"envelope": envelope, "latency_ms": elapsed_ms, "error": True}

def create_order(body, request_id):
    customer_id = body.get("customer_id")
    items = body.get("items")

    if not customer_id or not items:
        error = {"message": "customer_id and items are required", "code": "INVALID_BODY"}
        return {"order": None, "error": error, "db": None, "dependency": None}

    items = json.loads(json.dumps(items), parse_float=Decimal)

    total = Decimal(str(sum(item.get("price", 0) * item.get("quantity", 1) for item in items)))
    order_id = str(uuid.uuid4())
    now = int(time.time())
    payment = call_payment_service(order_id, total, customer_id, request_id)
    dependency = {"name": "payment-service", "latency_ms": payment["latency_ms"], "error": payment["error"]}

    if payment["envelope"].get("error"):
        return {"order": None, "error": payment["envelope"]["error"], "db": None, "dependency": dependency}

    order = { "order_id": order_id,"customer_id": customer_id,"items": items,"total": total,"status": "confirmed",
             "created_at": now,"updated_at": now,}

    db_start = time.time()
    put_response = table.put_item(Item=order, ReturnConsumedCapacity="TOTAL")
    db = {"latency_ms": round((time.time() - db_start) * 1000, 2), "consumed_capacity": put_response.get("ConsumedCapacity", {}).get("CapacityUnits")}

    return {"order": order, "error": None, "db": db, "dependency": dependency}

def get_order(order_id):
    db_start = time.time()
    response = table.get_item(Key={"order_id": order_id}, ReturnConsumedCapacity="TOTAL")
    db = {"latency_ms": round((time.time() - db_start) * 1000, 2), "consumed_capacity": response.get("ConsumedCapacity", {}).get("CapacityUnits")}
    return {"order": response.get("Item"), "db": db}

# lambda
def handler(event, context):
    global _cold_start
    is_cold_start = _cold_start
    _cold_start = False
    wall_start = time.time()          # wall clock, for latency_ms
    proc_start = time.process_time()  # CPU time, for lambda_duration_ms
    request_id = (event.get("requestContext") or {}).get("requestId") or getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    method = event.get("httpMethod", "GET")
    order_id = (event.get("pathParameters") or {}).get("id")
    endpoint = event.get("resource", "")
    db = None
    dependency = None

    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except (json.JSONDecodeError, TypeError):
        response = _error("Malformed JSON body", 400, "INVALID_BODY")
        _finish(request_id, endpoint, method, response, wall_start, proc_start, is_cold_start, db, dependency)
        return response

    try:
        if method == "POST" and not order_id:
            result = create_order(body, request_id)
            db, dependency = result["db"], result["dependency"]
            if result["error"]:
                status_code = 400 if result["error"]["code"] == "INVALID_BODY" else 502
                response = _error(result["error"]["message"], status_code, result["error"]["code"])
            else:
                response = _success(result["order"], 201)
        elif method == "GET" and order_id:
            result = get_order(order_id)
            db = result["db"]
            response = _success(result["order"]) if result["order"] else _error("Order not found", 404, "NOT_FOUND")
        else:
            response = _error(f"Unsupported route: {method} {endpoint}", 404, "NOT_FOUND")
    except ClientError:
        response = _error("A database error occurred", 500, "DB_ERROR")
    except Exception:  
        response = _error("Internal server error", 500, "INTERNAL_ERROR")

    _finish(request_id, endpoint, method, response, wall_start, proc_start, is_cold_start, db, dependency)
    return response

def _finish(request_id, endpoint, method, response, wall_start, proc_start, is_cold_start, db, dependency):
    latency_ms = round((time.time() - wall_start) * 1000, 2)
    lambda_duration_ms = round((time.process_time() - proc_start) * 1000, 2)
    _emit_telemetry(request_id=request_id, endpoint=endpoint, http_method=method, status_code=response["statusCode"], latency_ms=latency_ms,lambda_duration_ms=lambda_duration_ms,cold_start=is_cold_start,db=db,dependency=dependency,)