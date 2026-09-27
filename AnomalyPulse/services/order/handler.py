import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError

SERVICE_NAME = "order-service"  # must match telemetry_schema.json correlation.service enum
TABLE_NAME = os.environ["TABLE_NAME"]
PAYMENT_FUNCTION_NAME = os.environ.get("PAYMENT_FUNCTION_NAME", "anomalypulse-payment")
USE_PAYMENT_STUB = os.environ.get("USE_PAYMENT_STUB", "true").lower() == "true"

dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")
table = dynamodb.Table(TABLE_NAME)

# set once per cold container, then stays False for every warm invocation
# that reuses this execution environment.
_cold_start = True

# helpers
def _success(data, status_code=200):
    return {"statusCode": status_code,"headers": {"Content-Type": "application/json"},"body": json.dumps({"data": data, "error": None}, default=_decimal_to_float)}


def _error(message, status_code, code):
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"data": None, "error": {"message": message, "code": code}})}


def _decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# telemetry
def _now_iso_ms():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _emit_telemetry(request_id, endpoint, http_method, status_code, latency_ms, cold_start, db=None, dependency=None):
    db = db or {}
    dependency = dependency or {}
    record = {"timestamp": _now_iso_ms(),"request_id": request_id,"service": SERVICE_NAME,"endpoint": endpoint,
              "http_method": http_method, "status_code": status_code,"latency_ms": latency_ms,"lambda_duration_ms": latency_ms,  # no finer-grained split available in-handler
              "cold_start": cold_start,"db_latency_ms": db.get("latency_ms"),"db_consumed_capacity": db.get("consumed_capacity"),"dependency": dependency.get("name"),
              "dependency_latency_ms": dependency.get("latency_ms"), "dependency_error": dependency.get("error")}
    print(json.dumps(record))

# payment
def call_payment_service(order_id, amount, customer_id):
    start = time.time()

    if USE_PAYMENT_STUB:
        envelope = {"data": {"service": "payment", "outcome": "success"}, "error": None}
        return {"envelope": envelope, "latency_ms": round((time.time() - start) * 1000, 2), "error": False}
    try:
        response = lambda_client.invoke(FunctionName=PAYMENT_FUNCTION_NAME,InvocationType="RequestResponse",
                                        Payload=json.dumps({ "action": "charge","order_id": order_id,"customer_id": customer_id, "amount": amount,}).encode("utf-8"),)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        payload = json.loads(response["Payload"].read())
        if "body" in payload:  # payment's handler returns {"statusCode":..., "body": "..."} even invoked directly
            payload = json.loads(payload["body"])
        failed = bool(payload.get("error")) or payload.get("data", {}).get("outcome") != "success"
        return {"envelope": payload, "latency_ms": elapsed_ms, "error": failed}
    except ClientError as exc:
        elapsed_ms = round((time.time() - start) * 1000, 2)
        envelope = {"data": None, "error": {"message": str(exc), "code": "PAYMENT_INVOKE_FAILED"}}
        return {"envelope": envelope, "latency_ms": elapsed_ms, "error": True}

def create_order(body):
    customer_id = body.get("customer_id")
    items = body.get("items")

    if not customer_id or not items:
        error = {"message": "customer_id and items are required", "code": "INVALID_BODY"}
        return {"order": None, "error": error, "db": None, "dependency": None}

    total = Decimal(str(sum(item.get("price", 0) * item.get("quantity", 1) for item in items)))
    order_id = str(uuid.uuid4())
    now = int(time.time())
    payment = call_payment_service(order_id, total, customer_id)
    dependency = {"name": "payment-service", "latency_ms": payment["latency_ms"], "error": payment["error"]}

    if payment["envelope"].get("error"):
        return {"order": None, "error": payment["envelope"]["error"], "db": None, "dependency": dependency}

    payment_info = payment["envelope"]["data"]
    order = { "order_id": order_id,"customer_id": customer_id,"items": items,"total": total,"status": "confirmed" if payment_info.get("outcome") == "success" else "payment_failed",
             "payment_id": payment_info.get("payment_id"),"created_at": now,"updated_at": now,}

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
    wall_start = time.time()
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))
    method = event.get("httpMethod", "GET")
    order_id = (event.get("pathParameters") or {}).get("id")
    endpoint = event.get("resource", "")
    db = None
    dependency = None

    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except (json.JSONDecodeError, TypeError):
        response = _error("Malformed JSON body", 400, "INVALID_BODY")
        _finish(request_id, endpoint, method, response, wall_start, is_cold_start, db, dependency)
        return response

    try:
        if method == "POST" and not order_id:
            result = create_order(body)
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
    except Exception as exc:  # noqa: BLE001 - surface a clean 500, log the real error
        print(f"Unhandled error in order handler: {exc}")
        response = _error("Internal server error", 500, "INTERNAL_ERROR")

    _finish(request_id, endpoint, method, response, wall_start, is_cold_start, db, dependency)
    return response

def _finish(request_id, endpoint, method, response, wall_start, is_cold_start, db, dependency):
    latency_ms = round((time.time() - wall_start) * 1000, 2)
    _emit_telemetry(request_id=request_id, endpoint=endpoint, http_method=method, status_code=response["statusCode"], latency_ms=latency_ms,cold_start=is_cold_start,db=db,dependency=dependency,)