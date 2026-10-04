"""Local unit tests for the telemetry shipper. No live AWS needed: the s3 client
is mocked via unittest.mock, and BUCKET_NAME is set before import because the
handler reads it into a module constant at import time."""
import base64
import gzip
import json
import os
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("BUCKET_NAME", "test-bucket")
import handler  # noqa: E402  (must follow the env var above; handler reads BUCKET_NAME at import)


class _Ctx:
    """Minimal Lambda context stub; the handler only reads aws_request_id."""
    aws_request_id = "req-123"


def _event(log_group, messages):
    """Build a CloudWatch Logs subscription event: base64(gzip(json(payload)))."""
    payload = {"logGroup": log_group, "logEvents": [{"message": m} for m in messages]}
    data = base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("utf-8")
    return {"awslogs": {"data": data}}


def _capture_puts():
    """Patch s3.put_object; return (calls_list, patcher) where each call's kwargs
    are appended to calls_list."""
    calls = []
    patcher = mock.patch.object(handler.s3, "put_object", side_effect=lambda **kw: calls.append(kw))
    return calls, patcher


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_decodes_payload_and_writes_valid_json_to_raw():
    msgs = ['{"request_id": "a", "status_code": 200}', '{"request_id": "b", "status_code": 500}']
    calls, patcher = _capture_puts()
    with patcher:
        handler.handler(_event("/aws/lambda/anomalypulse-product", msgs), _Ctx())

    assert len(calls) == 1
    put = calls[0]
    assert put["Bucket"] == "test-bucket"
    assert put["Key"] == f"raw/service=product-service/dt={_today()}/part-req-123.json"
    # Body is the newline-joined raw messages, byte-for-byte.
    assert put["Body"].decode("utf-8") == "\n".join(msgs)


def test_unparseable_lines_go_to_errors_valid_to_raw():
    msgs = ['{"ok": 1}', 'this is not json', '{"ok": 2}']
    calls, patcher = _capture_puts()
    with patcher:
        handler.handler(_event("/aws/lambda/anomalypulse-order", msgs), _Ctx())

    by_prefix = {k["Key"].split("/", 1)[0]: k for k in calls}
    assert set(by_prefix) == {"raw", "errors"}
    assert by_prefix["raw"]["Body"].decode("utf-8") == '{"ok": 1}\n{"ok": 2}'
    assert by_prefix["errors"]["Body"].decode("utf-8") == "this is not json"
    assert by_prefix["raw"]["Key"].startswith("raw/service=order-service/")
    assert by_prefix["errors"]["Key"].startswith("errors/service=order-service/")


def test_all_invalid_writes_only_errors():
    calls, patcher = _capture_puts()
    with patcher:
        handler.handler(_event("/aws/lambda/anomalypulse-cart", ["nope", "also nope"]), _Ctx())

    assert len(calls) == 1
    assert calls[0]["Key"].startswith("errors/service=cart-service/")
    assert calls[0]["Body"].decode("utf-8") == "nope\nalso nope"


def test_no_events_writes_nothing():
    # Both lists empty -> neither conditional put_object fires.
    calls, patcher = _capture_puts()
    with patcher:
        handler.handler(_event("/aws/lambda/anomalypulse-payment", []), _Ctx())

    assert calls == []


def test_service_name_derived_from_log_group():
    # Pins the S3 partition contract for each subscribed service. NOTE: this
    # relies on single-token service names (split("-")[-1]); a hyphenated log
    # group such as anomalypulse-metrics-extractor would derive "extractor-service".
    cases = {
        "/aws/lambda/anomalypulse-product": "product-service",
        "/aws/lambda/anomalypulse-cart": "cart-service",
        "/aws/lambda/anomalypulse-order": "order-service",
        "/aws/lambda/anomalypulse-payment": "payment-service",
    }
    for log_group, expected in cases.items():
        calls, patcher = _capture_puts()
        with patcher:
            handler.handler(_event(log_group, ['{"ok": 1}']), _Ctx())
        assert calls[0]["Key"] == f"raw/service={expected}/dt={_today()}/part-req-123.json"
