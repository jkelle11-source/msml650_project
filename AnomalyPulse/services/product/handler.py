"""Product Service (M1-2): GET /products and GET /products/{id}, read-only against Products."""
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

SERVICE = "product-service"

_cold_start = True
_table_obj = None


def _table():
    # Created lazily so the module imports without boto3 (local unit tests)
    # and the client is reused across warm invocations.
    global _table_obj
    if _table_obj is None:
        import boto3
        _table_obj = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table_obj


def _json_default(obj):
    # DynamoDB returns numbers as Decimal, which json can't serialize.
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}, default=_json_default),
    }


def _err(status, code, message):
    return _resp(status, error={"code": code, "message": message})


class _DbStats:
    """Accumulates DynamoDB time and consumed capacity across calls in one request."""

    def __init__(self):
        self.latency_ms = None
        self.capacity = None

    def call(self, fn, **kwargs):
        start = time.perf_counter()
        try:
            result = fn(ReturnConsumedCapacity="TOTAL", **kwargs)
        finally:
            self.latency_ms = (self.latency_ms or 0.0) + (time.perf_counter() - start) * 1000
        units = (result.get("ConsumedCapacity") or {}).get("CapacityUnits")
        if units is not None:
            self.capacity = (self.capacity or 0.0) + float(units)
        return result


def _list_products(db):
    items, kwargs = [], {}
    while True:  # scan is paginated at 1 MB
        page = db.call(_table().scan, **kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda p: p["id"])
    return _resp(200, data=items)


def _get_product(event, db):
    pid = (event.get("pathParameters") or {}).get("id")
    if not pid:
        return _err(400, "BAD_REQUEST", "product id is required")
    item = db.call(_table().get_item, Key={"id": pid}).get("Item")
    if item is None:
        return _err(404, "NOT_FOUND", f"product '{pid}' not found")
    return _resp(200, data=item)


def _route(event, db):
    route, method = event.get("resource"), event.get("httpMethod")
    if route == "/products" and method == "GET":
        return _list_products(db)
    if route == "/products/{id}" and method == "GET":
        return _get_product(event, db)
    return _err(501, "NOT_IMPLEMENTED", f"{method} {route} is not handled by product-service")


def _log(event, context, status, start, db, cold):
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    request_id = (event.get("requestContext") or {}).get("requestId") or getattr(
        context, "aws_request_id", None
    )
    # One line per request, matching infra/shared/telemetry_schema.json.
    # Tier-1 and correlation fields only; never emit Tier-2 (error_type, db_throttled, ...).
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "request_id": request_id,
        "service": SERVICE,
        "endpoint": event.get("resource"),
        "http_method": event.get("httpMethod"),
        "status_code": status,
        "latency_ms": elapsed_ms,
        "lambda_duration_ms": elapsed_ms,
        "cold_start": cold,
        "db_latency_ms": round(db.latency_ms, 3) if db.latency_ms is not None else None,
        "db_consumed_capacity": db.capacity,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }))


def handler(event, context):
    global _cold_start
    cold, _cold_start = _cold_start, False
    start = time.perf_counter()
    db = _DbStats()
    try:
        resp = _route(event, db)
    except Exception:
        # Structured error, never a stack trace. The cause stays out of the log line (Tier-2).
        resp = _err(500, "INTERNAL_ERROR", "failed to read products")
    _log(event, context, resp["statusCode"], start, db, cold)
    return resp
