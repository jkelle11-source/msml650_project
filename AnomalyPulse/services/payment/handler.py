"""Payment Service (M1-5): POST /payments, with fault-injection knobs (latency, failures, timeout)."""
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

SERVICE = "payment-service"

_cold_start = True


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}),
    }


def _err(status, code, message):
    return _resp(status, error={"code": code, "message": message})


def _read_knobs():
    # Read at call time so the incident simulator can flip knobs without a redeploy.
    latency_ms = float(os.environ.get("PAYMENT_LATENCY_MS", "0") or "0")
    failure_rate = float(os.environ.get("PAYMENT_FAILURE_RATE", "0.0") or "0.0")
    timeout_on = os.environ.get("PAYMENT_TIMEOUT", "false").lower() == "true"
    return latency_ms, failure_rate, timeout_on


def _apply_latency(latency_ms):
    if latency_ms > 0:
        time.sleep(latency_ms / 1000.0)


def _apply_timeout(timeout_on):
    if timeout_on:
        time.sleep(30)  # exceeds the Lambda timeout so the caller's own timeout trips first


def _process(event):
    route, method = event.get("resource"), event.get("httpMethod")
    if route != "/payments" or method != "POST":
        return _err(501, "NOT_IMPLEMENTED", f"{method} {route} is not handled by payment-service")

    latency_ms, failure_rate, timeout_on = _read_knobs()
    _apply_timeout(timeout_on)
    _apply_latency(latency_ms)

    # An injected failure is a *service* fault, so it surfaces as a 5xx (PROJECT_PLAN Section 5:
    # "intermittent 5xx errors"). This keeps the DEPENDENCY_FAILURE incident's observable
    # signature in the 5xx-rate feature rather than the distinct 4xx-rate one (Section 3).
    if random.random() < failure_rate:
        return _err(503, "PAYMENT_UNAVAILABLE", "payment service failed")
    return _resp(200, data={"service": "payment", "outcome": "success"})


def _log(event, context, status, start, cold):
    elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
    request_id = (
        (event.get("requestContext") or {}).get("requestId")
        or getattr(context, "aws_request_id", None)
        or str(uuid.uuid4())
    )
    # One line per request, matching infra/shared/telemetry_schema.json. Tier-1 and correlation
    # fields only; never emit Tier-2 (error_type, db_throttled, ...). Payment makes no DB call and
    # has no downstream dependency, so those fields are always null.
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
        "db_latency_ms": None,
        "db_consumed_capacity": None,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }))


def handler(event, context):
    global _cold_start
    cold, _cold_start = _cold_start, False
    start = time.perf_counter()
    try:
        resp = _process(event)
    except Exception:
        # Structured error, never a stack trace. The cause stays out of the log line (Tier-2).
        resp = _err(500, "INTERNAL_ERROR", "payment processing failed")
    _log(event, context, resp["statusCode"], start, cold)
    return resp
