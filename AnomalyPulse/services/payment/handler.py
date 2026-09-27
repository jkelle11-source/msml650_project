import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

SERVICE = "payment-service"


def _resp(status, data=None, error=None):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"data": data, "error": error}),
    }

def _read_knobs():
    latency_ms = float(os.environ.get("PAYMENT_LATENCY_MS", "0") or "0")
    failure_rate = float(os.environ.get("PAYMENT_FAILURE_RATE", "0.0") or "0.0")
    timeout_on = os.environ.get("PAYMENT_TIMEOUT", "false").lower() == "true"
    return latency_ms, failure_rate, timeout_on


def _apply_latency(latency_ms):
    if latency_ms > 0:
        time.sleep(latency_ms / 1000.0)


def _apply_timeout(timeout_on):
    if timeout_on:
        time.sleep(30)  # long enough that a caller's own timeout trips first

def _log(request_id, status_code, start_time, outcome):
    line = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "request_id": request_id,
        "service": SERVICE,
        "endpoint": "/payments",
        "http_method": "POST",
        "status_code": status_code,
        "latency_ms": round((time.time() - start_time) * 1000, 3),
        "lambda_duration_ms": round((time.time() - start_time) * 1000, 3),
        "cold_start": _log.cold_start,
        "db_latency_ms": None,
        "db_consumed_capacity": None,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }
    print(json.dumps(line))
    _log.cold_start = False


_log.cold_start = True

def handler(event, context):
    start_time = time.time()
    request_id = (event.get("requestContext") or {}).get("requestId") or str(uuid.uuid4())

    latency_ms, failure_rate, timeout_on = _read_knobs()

    _apply_timeout(timeout_on)
    _apply_latency(latency_ms)

    if random.random() < failure_rate:
        outcome = "failure"
        resp = _resp(402, error={"code": "PAYMENT_DECLINED", "message": "Payment failed"})
    else:
        outcome = "success"
        resp = _resp(200, data={"service": "payment", "outcome": outcome})

    _log(request_id, resp["statusCode"], start_time, outcome)
    return resp