import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

SERVICE_NAME = "order-service"  # must match telemetry_schema.json correlation.service enum
TABLE_NAME = os.environ["TABLE_NAME"]
PAYMENT_FUNCTION_NAME = os.environ.get("PAYMENT_FUNCTION_NAME", "anomalypulse-payment")
USE_PAYMENT_STUB = os.environ.get("USE_PAYMENT_STUB", "true").lower() == "true"

dynamodb = boto3.resource("dynamodb")
# The payment invoke is synchronous and runs inside the order function's own 10s
# Lambda timeout. Bound botocore's read timeout BELOW that (its default is 60s) so
# a hung/slow payment - the PAYMENT_TIMEOUT incident knob sleeps 30s - surfaces as
# a ReadTimeoutError that call_payment_service catches (a clean 502 WITH dependency
# telemetry), instead of the order Lambda being hard-killed at 10s with no
# dependency line emitted. 6s sits between the injected-latency incident (a few
# seconds, which should still succeed and show as high dependency_latency_ms) and
# the timeout incident (30s). No retries: a 10s budget has no room for one, and a
# retry would only mask the dependency signal the incident classifier keys on.
_PAYMENT_INVOKE_CONFIG = Config(connect_timeout=2, read_timeout=6, retries={"max_attempts": 1})
lambda_client = boto3.client("lambda", config=_PAYMENT_INVOKE_CONFIG)
table = dynamodb.Table(TABLE_NAME)

# set once per cold container, then stays False for every warm invocation
# that reuses this execution environment.
_cold_start = True

# CORS: the browser dashboard (Section 14) is a different origin, so every
# response must carry these headers or the browser blocks the read. The API's
# OPTIONS preflight is handled at the gateway (Cors: block in template.yaml);
# each Lambda still sets them on its own responses. (Matches product/cart/payment.)
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
def _now_iso_ms():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _emit_telemetry(request_id, endpoint, http_method, status_code, latency_ms, lambda_duration_ms, cold_start, db=None, dependency=None):
    # The schema (telemetry_schema.json) distinguishes latency_ms (end-to-end wall
    # clock, includes the synchronous payment invoke) from lambda_duration_ms (CPU
    # compute time of the handler body, excludes I/O wait). Keeping them separate is
    # load-bearing here: a slow payment dependency must raise wall-clock latency_ms
    # without inflating lambda_duration_ms, so DEPENDENCY_FAILURE stays distinct from
    # LAMBDA_DEGRADATION for the classifier. (Matches product/cart/payment.)
    db = db or {}
    dependency = dependency or {}
    record = {"timestamp": _now_iso_ms(),"request_id": request_id,"service": SERVICE_NAME,"endpoint": endpoint,
              "http_method": http_method, "status_code": status_code,"latency_ms": latency_ms,"lambda_duration_ms": lambda_duration_ms,
              "cold_start": cold_start,"db_latency_ms": db.get("latency_ms"),"db_consumed_capacity": db.get("consumed_capacity"),"dependency": dependency.get("name"),
              "dependency_latency_ms": dependency.get("latency_ms"), "dependency_error": dependency.get("error")}
    print(json.dumps(record))

# payment
def call_payment_service(order_id, amount, customer_id, request_id):
    start = time.time()

    if USE_PAYMENT_STUB:
        envelope = {"data": {"service": "payment", "outcome": "success"}, "error": None}
        return {"envelope": envelope, "latency_ms": round((time.time() - start) * 1000, 2), "error": False}
    try:
        # Invoke payment with the same event shape API Gateway would deliver for
        # POST /payments, so a single payment code path serves both its HTTP callers
        # and this direct Lambda-to-Lambda invoke - and, crucially, payment's
        # fault-injection knobs (PAYMENT_LATENCY_MS / PAYMENT_FAILURE_RATE /
        # PAYMENT_TIMEOUT) still apply on this dependency path, which the
        # DEPENDENCY_FAILURE incident relies on (Sections 4/5). Payment routes on
        # resource + httpMethod, so both must be present.
        # Propagate our request_id under requestContext so payment stamps the same
        # correlation id on its telemetry line (payment reads requestContext.requestId
        # first). This is what lets order and payment log lines be joined by request_id
        # for cross-service ordering (PROJECT_PLAN Section 3).
        # amount is a Decimal (order total); default=_decimal_to_float keeps json.dumps
        # from raising TypeError on it when building the invoke payload (both the inner
        # body string and the outer envelope).
        response = lambda_client.invoke(FunctionName=PAYMENT_FUNCTION_NAME,InvocationType="RequestResponse",
                                        Payload=json.dumps({ "resource": "/payments","httpMethod": "POST",
                                                            "body": json.dumps({"order_id": order_id,"customer_id": customer_id,"amount": amount}, default=_decimal_to_float),
                                                            "requestContext": {"requestId": request_id},}, default=_decimal_to_float).encode("utf-8"),)
        elapsed_ms = round((time.time() - start) * 1000, 2)
        # An unhandled exception inside payment comes back as a Lambda FunctionError,
        # where Payload is {"errorMessage", "errorType"} rather than our envelope.
        # Treat it as a dependency failure so it becomes a clean 502 with dependency
        # telemetry, not a blank 500 from dereferencing a missing field below.
        if response.get("FunctionError"):
            envelope = {"data": None, "error": {"message": "payment service error", "code": "PAYMENT_INVOKE_FAILED"}}
            return {"envelope": envelope, "latency_ms": elapsed_ms, "error": True}
        payload = json.loads(response["Payload"].read())
        if isinstance(payload, dict) and "body" in payload:  # payment returns {"statusCode":..., "body": "..."} even invoked directly
            payload = json.loads(payload["body"])
        # payment's error envelope carries data:null, so read outcome defensively -
        # `payload.get("data", {})` would return None (the key exists), and None has
        # no .get. `or {}` collapses a null/absent data to an empty dict.
        data = payload.get("data") or {}
        failed = bool(payload.get("error")) or data.get("outcome") != "success"
        # Invariant the caller relies on: a failed payment ALWAYS has a structured
        # error field. An unexpected shape (failed but no error, e.g. data:null with
        # no error) would otherwise slip past create_order's error gate and
        # dereference the null data into a 500; synthesize an error to keep it on the
        # clean-502 path.
        if failed and not payload.get("error"):
            payload = {"data": None, "error": {"message": "unexpected payment response", "code": "PAYMENT_INVOKE_FAILED"}}
        return {"envelope": payload, "latency_ms": elapsed_ms, "error": failed}
    except (BotoCoreError, ClientError) as exc:
        # BotoCoreError covers ReadTimeoutError/ConnectionError - i.e. exactly the
        # dependency-failure/timeout cases. Return a clean error envelope (rather than
        # letting it propagate to the generic 500) so the caller still records
        # dependency telemetry with dependency_error=True.
        elapsed_ms = round((time.time() - start) * 1000, 2)
        envelope = {"data": None, "error": {"message": str(exc), "code": "PAYMENT_INVOKE_FAILED"}}
        return {"envelope": envelope, "latency_ms": elapsed_ms, "error": True}

def create_order(body, request_id):
    customer_id = body.get("customer_id")
    items = body.get("items")

    if not customer_id or not items:
        error = {"message": "customer_id and items are required", "code": "INVALID_BODY"}
        return {"order": None, "error": error, "db": None, "dependency": None}

    # Item prices arrive from the JSON body as floats, which DynamoDB's serializer
    # rejects ("Float types are not supported. Use Decimal types instead."). The
    # items are stored verbatim on the order, so normalize every float in them to
    # Decimal up front - this keeps both the computed total and the stored items
    # DynamoDB-safe. The JSON response converts them back via _decimal_to_float.
    items = json.loads(json.dumps(items), parse_float=Decimal)

    total = Decimal(str(sum(item.get("price", 0) * item.get("quantity", 1) for item in items)))
    order_id = str(uuid.uuid4())
    now = int(time.time())
    payment = call_payment_service(order_id, total, customer_id, request_id)
    dependency = {"name": "payment-service", "latency_ms": payment["latency_ms"], "error": payment["error"]}

    if payment["envelope"].get("error"):
        return {"order": None, "error": payment["envelope"]["error"], "db": None, "dependency": dependency}

    # We only reach here when payment succeeded: call_payment_service surfaces any
    # non-success outcome as an error, handled above. Payment failures in this system
    # are dependency incidents (5xx / timeout) that return a 502 and persist no order
    # - there is no business "declined" outcome in the payment contract (PROJECT_PLAN
    # "Payment Failure"; Milestone 03), so a persisted order is always confirmed.
    # NOTE: the payment service's contract returns only {"service", "outcome"} - no
    # payment_id - so we do not store one here. If payment starts returning an id,
    # add it back and update services/payment accordingly.
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
    # Prefer the upstream (API Gateway) request id so it can be propagated to payment
    # for cross-service correlation (Section 3); fall back to the Lambda id, then a uuid.
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
        # A DynamoDB failure (e.g. ProvisionedThroughputExceededException, the M3
        # throttling path) gets its own DB_ERROR code, distinct from the generic
        # INTERNAL_ERROR, so downstream can tell a database fault from any other.
        # Payment-invoke ClientErrors never reach here - call_payment_service
        # catches those. The cause is Tier-2 ground truth and stays out of both the
        # response and stdout; the observable throttling signal is the 500 +
        # db_latency + CloudWatch ThrottledRequests count. (Matches services/cart.)
        response = _error("A database error occurred", 500, "DB_ERROR")
    except Exception:  # noqa: BLE001 - surface a clean 500; keep the cause off stdout
        # The exception message can encode the cause (Tier-2 ground truth), so it must
        # never reach stdout on the request path - only the Tier-1 telemetry line does.
        # (Matches services/product, cart, payment.)
        response = _error("Internal server error", 500, "INTERNAL_ERROR")

    _finish(request_id, endpoint, method, response, wall_start, proc_start, is_cold_start, db, dependency)
    return response

def _finish(request_id, endpoint, method, response, wall_start, proc_start, is_cold_start, db, dependency):
    latency_ms = round((time.time() - wall_start) * 1000, 2)
    lambda_duration_ms = round((time.process_time() - proc_start) * 1000, 2)
    _emit_telemetry(request_id=request_id, endpoint=endpoint, http_method=method, status_code=response["statusCode"], latency_ms=latency_ms,lambda_duration_ms=lambda_duration_ms,cold_start=is_cold_start,db=db,dependency=dependency,)