import json
import os
import re
import time

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

os.environ["AWS_DEFAULT_REGION"] = "us-east-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SECURITY_TOKEN"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["TABLE_NAME"] = "test-orders-table"
os.environ["USE_PAYMENT_STUB"] = "true"

import sys
_LAYER_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "layers", "telemetry", "python")
sys.path.insert(0, os.path.abspath(_LAYER_PATH))

from handler import handler
import handler as handler_module


# The exact per-request telemetry contract from layers/telemetry/python/telemetry_schema.json:
# every service emits these fields (correlation + Tier-1 observable) and NEVER a
# Tier-2 ground-truth field on the request path.
SCHEMA_FIELDS = {
    "timestamp", "request_id", "service", "endpoint", "http_method",
    "status_code", "latency_ms", "lambda_duration_ms", "cold_start",
    "db_latency_ms", "db_consumed_capacity",
    "dependency", "dependency_latency_ms", "dependency_error",
}
TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}


@pytest.fixture
def orders_table():
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
        table = dynamodb.create_table(TableName=os.environ["TABLE_NAME"], KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
                                      AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],BillingMode="PAY_PER_REQUEST")
        handler_module.table = table
        # Each test starts from a cold container so cold_start is deterministic.
        handler_module._cold_start = True
        yield table


# --- helpers ---------------------------------------------------------------

def _post_order_event(body=None, request_id=None):
    body = {"customer_id": "cust-1", "items": [{"sku": "widget", "price": 10, "quantity": 2}]} if body is None else body
    event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None, "body": json.dumps(body)}
    if request_id is not None:
        event["requestContext"] = {"requestId": request_id}
    return event


def _get_order_event(order_id):
    return {"resource": "/orders/{id}", "httpMethod": "GET", "pathParameters": {"id": order_id}}


def _json_logs(capsys):
    """Every JSON log line printed since the last read (the telemetry lines)."""
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]


def _last_log(capsys):
    logs = _json_logs(capsys)
    assert logs, "expected at least one telemetry log line"
    return logs[-1]


class _FakePayload:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


class _FakeLambdaClient:
    """Records invoke() calls and returns a payment-shaped success envelope."""

    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        body = json.dumps({"data": {"service": "payment", "outcome": "success"}, "error": None})
        raw = json.dumps({"statusCode": 200, "body": body}).encode("utf-8")
        return {"Payload": _FakePayload(raw)}


# --- behaviour: create / get -----------------------------------------------

def test_create_order(orders_table):
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 201
    assert body["error"] is None
    assert body["data"]["customer_id"] == "cust-1"
    assert body["data"]["total"] == 20
    assert body["data"]["status"] == "confirmed"
    assert "order_id" in body["data"]


def test_create_order_with_float_prices(orders_table):
    # Regression: item prices arrive from the JSON body as floats, which DynamoDB's
    # serializer rejects unless coerced to Decimal. The other create tests all use
    # integer prices, so this path (a float total that must persist and round-trip)
    # went uncovered until the live smoke test (price 19.99) hit it. 19.99 * 2 = 39.98.
    response = handler(_post_order_event(
        {"customer_id": "cust-float", "items": [{"product_id": "prod-001", "price": 19.99, "quantity": 2}]}
    ), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 201, body
    assert body["error"] is None
    assert body["data"]["total"] == 39.98
    # the stored items (float prices) must round-trip back out of DynamoDB
    order_id = body["data"]["order_id"]
    got = json.loads(handler(_get_order_event(order_id), None)["body"])
    assert got["data"]["items"][0]["price"] == 19.99


def test_create_order_invalid_input(orders_table):
    response = handler(_post_order_event({"customer_id": "cust-1"}), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 400
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_BODY"


def test_create_order_malformed_json(orders_table):
    event = {"resource": "/orders", "httpMethod": "POST", "pathParameters": None, "body": "{not valid json"}
    response = handler(event, None)
    assert response["statusCode"] == 400


def test_get_order(orders_table):
    create_response = handler(_post_order_event({"customer_id": "cust-2", "items": [{"sku": "gadget", "price": 5, "quantity": 3}]}), None)
    order_id = json.loads(create_response["body"])["data"]["order_id"]
    response = handler(_get_order_event(order_id), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["data"]["order_id"] == order_id
    assert body["data"]["customer_id"] == "cust-2"


def test_get_order_not_found(orders_table):
    response = handler(_get_order_event("does-not-exist"), None)
    assert response["statusCode"] == 404


def test_unsupported_method(orders_table):
    event = {"resource": "/orders/{id}", "httpMethod": "DELETE", "pathParameters": {"id": "some-id"}}
    response = handler(event, None)
    assert response["statusCode"] == 404


def test_get_without_id_is_unsupported_route(orders_table, capsys):
    # GET /orders with no {id} matches neither create (POST) nor get (GET + id),
    # so it falls through to the catch-all 404. Pins that else-branch and its one
    # schema-valid record: the rejected path makes no DB call and no dependency call.
    response = handler({"resource": "/orders", "httpMethod": "GET", "pathParameters": None}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 404
    assert body["error"]["code"] == "NOT_FOUND"
    log = _last_log(capsys)
    assert set(log) == SCHEMA_FIELDS
    assert TIER2_FIELDS.isdisjoint(log)
    assert log["http_method"] == "GET"
    assert log["endpoint"] == "/orders"
    assert log["status_code"] == 404
    assert log["db_latency_ms"] is None
    assert log["dependency"] is None


# --- behaviour: payment dependency -----------------------------------------

def test_create_order_payment_service_error(orders_table, monkeypatch):
    monkeypatch.setattr(handler_module, "call_payment_service",
                        lambda order_id, amount, customer_id, request_id: {"envelope": {"data": None, "error": {"message": "timeout", "code": "PAYMENT_TIMEOUT"}}, "latency_ms": 3000.0, "error": True,},)
    response = handler(_post_order_event({"customer_id": "cust-4", "items": [{"sku": "widget", "price": 10, "quantity": 1}]}), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 502
    assert body["data"] is None
    assert body["error"]["code"] == "PAYMENT_TIMEOUT"


def test_real_payment_failure_envelope_becomes_502_no_order_persisted(orders_table, monkeypatch, capsys):
    # Exercises payment's REAL 5xx failure shape (statusCode + a JSON body carrying a
    # structured error), which the success-only fake never covers and which the live
    # smoke path would hit under the PAYMENT_FAILURE_RATE incident knob. Order must
    # turn it into a clean 502, record dependency_error telemetry, and persist NO
    # order row.
    def fail_invoke(**kwargs):
        body = json.dumps({"data": None, "error": {"code": "PAYMENT_UNAVAILABLE", "message": "payment service failed"}})
        raw = json.dumps({"statusCode": 503, "body": body}).encode("utf-8")
        return {"Payload": _FakePayload(raw)}

    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", type("C", (), {"invoke": staticmethod(fail_invoke)})())
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 502, body
    assert body["error"]["code"] == "PAYMENT_UNAVAILABLE"
    assert orders_table.scan()["Count"] == 0  # a failed payment must not persist an order
    log = _last_log(capsys)
    assert log["dependency"] == "payment-service"
    assert log["dependency_error"] is True


def test_payment_function_error_becomes_502_not_500(orders_table, monkeypatch, capsys):
    # An UNHANDLED exception inside payment returns a Lambda FunctionError, where the
    # payload is {"errorMessage","errorType"} - not our envelope. Order must treat it
    # as a dependency failure (clean 502 + dependency_error), not dereference the
    # missing envelope into a blank 500.
    def error_invoke(**kwargs):
        raw = json.dumps({"errorMessage": "boom", "errorType": "RuntimeError"}).encode("utf-8")
        return {"FunctionError": "Unhandled", "Payload": _FakePayload(raw)}

    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", type("C", (), {"invoke": staticmethod(error_invoke)})())
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 502, body
    assert orders_table.scan()["Count"] == 0
    log = _last_log(capsys)
    assert log["dependency"] == "payment-service"
    assert log["dependency_error"] is True


def test_botocore_error_on_invoke_becomes_502_with_dependency_error(orders_table, monkeypatch, capsys):
    # A ReadTimeoutError/ConnectionError from the payment invoke is a dependency
    # failure: call_payment_service catches BotoCoreError and returns an error
    # envelope, so the caller returns 502 AND records dependency_error=True in
    # telemetry rather than falling through to a blank 500.
    from botocore.exceptions import ReadTimeoutError

    def boom_invoke(**kwargs):
        raise ReadTimeoutError(endpoint_url="lambda", request=None)

    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", type("C", (), {"invoke": staticmethod(boom_invoke)})())
    response = handler(_post_order_event(), None)
    assert response["statusCode"] == 502
    log = _last_log(capsys)
    assert log["dependency"] == "payment-service"
    assert log["dependency_error"] is True


def test_request_id_propagated_to_payment(orders_table, monkeypatch, capsys):
    # The upstream request_id must be forwarded to payment (under requestContext,
    # where payment reads it) so the two services' log lines share a correlation
    # key (PROJECT_PLAN Section 3), and order's own telemetry must carry it too.
    fake = _FakeLambdaClient()
    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", fake)
    response = handler(_post_order_event(request_id="req-order-123"), None)
    assert response["statusCode"] == 201
    assert len(fake.calls) == 1
    sent = json.loads(fake.calls[0]["Payload"].decode("utf-8"))
    assert sent["requestContext"]["requestId"] == "req-order-123"
    assert _last_log(capsys)["request_id"] == "req-order-123"


def test_request_id_falls_back_to_context(orders_table, capsys):
    # With no upstream requestContext, order falls back to the Lambda request id.
    ctx = type("Ctx", (), {"aws_request_id": "lambda-req-1"})()
    handler(_post_order_event(), ctx)
    assert _last_log(capsys)["request_id"] == "lambda-req-1"


def test_request_id_joins_order_and_payment_records(orders_table, monkeypatch, capsys):
    # End-to-end correlation (M2-4 DoD; PROJECT_PLAN Section 3): the id order forwards
    # to payment must be the SAME id payment stamps on its own telemetry line, so the
    # two services' records are joinable by request_id. We capture the exact payload
    # order sends to payment and feed it to payment's REAL handler, then assert both
    # emitted records carry the one id -- proving the join, not just that order sends it.
    import importlib.util

    fake = _FakeLambdaClient()
    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", fake)
    handler(_post_order_event(request_id="corr-xyz"), None)
    order_log = _last_log(capsys)

    # Load payment's handler under a distinct module name -- order's handler.py already
    # owns the "handler" entry in sys.modules, so a plain import would return the wrong one.
    pay_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "payment", "handler.py"))
    spec = importlib.util.spec_from_file_location("payment_handler", pay_path)
    payment_handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(payment_handler)

    sent_payload = json.loads(fake.calls[0]["Payload"].decode("utf-8"))
    payment_handler.handler(sent_payload, None)
    payment_log = _last_log(capsys)

    assert order_log["request_id"] == "corr-xyz"
    assert payment_log["request_id"] == "corr-xyz"
    assert order_log["service"] == "order-service"
    assert payment_log["service"] == "payment-service"


def test_payment_failed_without_error_field_synthesizes_502(orders_table, monkeypatch, capsys):
    # Defensive dependency path: payment returns a NON-success outcome but omits the
    # structured error field (an unexpected shape). call_payment_service must synthesize
    # an error so this stays on the clean-502 path instead of dereferencing null data
    # into a blank 500, and must still record dependency_error and persist no order.
    def weird_invoke(**kwargs):
        body = json.dumps({"data": {"outcome": "declined"}, "error": None})
        raw = json.dumps({"statusCode": 200, "body": body}).encode("utf-8")
        return {"Payload": _FakePayload(raw)}

    monkeypatch.setattr(handler_module, "USE_PAYMENT_STUB", False)
    monkeypatch.setattr(handler_module, "lambda_client", type("C", (), {"invoke": staticmethod(weird_invoke)})())
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 502, body
    assert body["error"]["code"] == "PAYMENT_INVOKE_FAILED"
    assert orders_table.scan()["Count"] == 0  # a non-success payment must not persist an order
    log = _last_log(capsys)
    assert log["dependency"] == "payment-service"
    assert log["dependency_error"] is True


# --- telemetry contract -----------------------------------------------------

def test_emit_telemetry_never_crashes_on_invalid_record(capsys):
    # The telemetry path must never crash the request (handler.py's "NEVER let
    # telemetry crash the request" guarantee): a record that fails schema validation
    # is logged as a telemetry_error and swallowed, not raised. http_method "PATCH"
    # is outside the schema enum, so the built record fails validation.
    handler_module._emit_telemetry(
        request_id="r", endpoint="/orders", http_method="PATCH",
        status_code=201, latency_ms=1.0, lambda_duration_ms=1.0, cold_start=False,
    )
    logs = _json_logs(capsys)
    assert len(logs) == 1
    assert "telemetry_error" in logs[0]


def test_decimal_to_float_rejects_unsupported_type():
    # The json.dumps default hook only knows how to coerce Decimal; any other
    # unserializable type must raise TypeError rather than pass silently.
    with pytest.raises(TypeError):
        handler_module._decimal_to_float(object())


def test_dependency_latency_raises_wall_clock_not_cpu(orders_table, monkeypatch, capsys):
    # A slow payment dependency is I/O wait: it must raise wall-clock latency_ms
    # but NOT CPU lambda_duration_ms. That split is what keeps DEPENDENCY_FAILURE
    # distinct from LAMBDA_DEGRADATION (added computation) for the classifier.
    def slow_payment(order_id, amount, customer_id, request_id):
        time.sleep(0.2)
        return {"envelope": {"data": {"service": "payment", "outcome": "success"}, "error": None}, "latency_ms": 200.0, "error": False}

    monkeypatch.setattr(handler_module, "call_payment_service", slow_payment)
    handler(_post_order_event(), None)
    log = _last_log(capsys)
    assert log["latency_ms"] >= 200
    assert log["lambda_duration_ms"] < 200


def test_log_line_matches_schema_on_create(orders_table, capsys):
    handler(_post_order_event(request_id="req-abc"), None)
    log = _last_log(capsys)
    assert set(log) == SCHEMA_FIELDS
    assert TIER2_FIELDS.isdisjoint(log)
    assert log["service"] == "order-service"
    assert log["endpoint"] == "/orders"
    assert log["http_method"] == "POST"
    assert log["request_id"] == "req-abc"
    assert log["status_code"] == 201
    # POST /orders calls the payment dependency and writes to DynamoDB.
    assert log["dependency"] == "payment-service"
    assert log["dependency_latency_ms"] is not None
    assert log["dependency_error"] is False
    assert log["db_latency_ms"] is not None
    assert log["db_consumed_capacity"] is not None
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", log["timestamp"])


def test_log_line_matches_schema_on_get(orders_table, capsys):
    order_id = json.loads(handler(_post_order_event(), None)["body"])["data"]["order_id"]
    capsys.readouterr()  # drop the create's log line
    handler(_get_order_event(order_id), None)
    log = _last_log(capsys)
    assert set(log) == SCHEMA_FIELDS
    assert TIER2_FIELDS.isdisjoint(log)
    assert log["http_method"] == "GET"
    assert log["endpoint"] == "/orders/{id}"
    # GET makes no downstream call, so the dependency fields are null...
    assert log["dependency"] is None
    assert log["dependency_latency_ms"] is None
    assert log["dependency_error"] is None
    # ...but it still hits DynamoDB.
    assert log["db_latency_ms"] is not None


def test_exactly_one_log_line_per_request(orders_table, capsys):
    # The "one JSON log line per request" invariant must hold on every path,
    # including the malformed-body and unsupported-route rejections.
    handler(_post_order_event(), None)
    assert len(_json_logs(capsys)) == 1
    handler({"resource": "/orders", "httpMethod": "POST", "pathParameters": None, "body": "{bad"}, None)
    assert len(_json_logs(capsys)) == 1
    handler({"resource": "/orders/{id}", "httpMethod": "DELETE", "pathParameters": {"id": "x"}}, None)
    assert len(_json_logs(capsys)) == 1


def test_cold_start_only_first_invocation(orders_table, capsys):
    handler(_post_order_event(), None)
    first = _last_log(capsys)
    handler(_post_order_event(), None)
    second = _last_log(capsys)
    assert first["cold_start"] is True
    assert second["cold_start"] is False


def test_response_has_cors_header(orders_table):
    # The dashboard is a cross-origin browser client, so every response
    # (success and error envelopes alike) must carry Access-Control-Allow-Origin.
    ok = handler(_post_order_event(), None)
    assert ok["headers"]["Access-Control-Allow-Origin"] == "*"
    err = handler(_post_order_event({"customer_id": "cust-1"}), None)
    assert err["headers"]["Access-Control-Allow-Origin"] == "*"


# --- error paths must not leak the cause ------------------------------------

def test_internal_error_returns_envelope_without_leak(orders_table, monkeypatch, capsys):
    # An unexpected DynamoDB failure must yield a generic 500 (never the raw
    # exception text) and must not print the cause or any Tier-2 field anywhere.
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(handler_module.table, "put_item", boom)
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in response["body"]
    printed = capsys.readouterr().out
    assert "secret internal detail" not in printed
    log = [json.loads(l) for l in printed.splitlines() if l.strip().startswith("{")][-1]
    assert TIER2_FIELDS.isdisjoint(log)
    assert log["status_code"] == 500


def test_dynamodb_client_error_returns_db_error_without_leak(orders_table, monkeypatch, capsys):
    # A DynamoDB ClientError (e.g. ProvisionedThroughputExceededException, the M3
    # throttling path) gets the dedicated DB_ERROR code - distinct from the generic
    # INTERNAL_ERROR - with a generic message and no leak of the cause on the
    # request path. (Matches services/cart.)
    def throttle(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throughput exceeds the current capacity"}}, "PutItem")

    monkeypatch.setattr(handler_module.table, "put_item", throttle)
    response = handler(_post_order_event(), None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 500
    assert body["error"]["code"] == "DB_ERROR"
    assert "ProvisionedThroughputExceededException" not in body["error"]["message"]
    assert "ProvisionedThroughputExceededException" not in response["body"]
    printed = capsys.readouterr().out
    assert "ProvisionedThroughputExceededException" not in printed
    assert "error_type" not in printed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
