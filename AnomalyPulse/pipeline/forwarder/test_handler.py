"""Local unit tests for the telemetry forwarder (M8-1). No live AWS needed: the
events client is mocked, or stubbed with botocore's Stubber, which checks each
request against the PutEvents API model. BUS_NAME and the region are set before
import because the handler reads them when the module loads.

Every expected count is worked out by hand: 100 records per entry, at most 10
entries per put_events call."""
import base64
import gzip
import json
import os
from unittest import mock

import pytest
from botocore.stub import ANY, Stubber

os.environ.setdefault("BUS_NAME", "test-bus")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
import handler  # noqa: E402  (must follow the env vars above)


def _records(n):
    """n telemetry-shaped records, each with a unique request_id."""
    return [{"timestamp": "2026-10-31T08:51:11.123Z", "request_id": f"req-{i}",
             "service": "product-service", "status_code": 200, "latency_ms": 41.2}
            for i in range(n)]


def _event(records, message_type="DATA_MESSAGE"):
    """A CloudWatch Logs subscription event: base64(gzip(json(payload))), one log event per record."""
    payload = {"messageType": message_type, "logGroup": "/aws/lambda/anomalypulse-product",
               "logEvents": [{"id": str(i), "timestamp": 1793436671123, "message": json.dumps(r)}
                             for i, r in enumerate(records)]}
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("utf-8")}}


def _control_event():
    """The message CloudWatch Logs sends when a subscription filter is created. Not JSON telemetry."""
    payload = {"messageType": "CONTROL_MESSAGE", "logGroup": "", "logStream": "",
               "subscriptionFilters": [],
               "logEvents": [{"id": "", "timestamp": 1793436671123,
                              "message": "CWL CONTROL MESSAGE: Checking health of destination Lambda."}]}
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode("utf-8"))).decode("utf-8")}}


def _capture_put_events(failed=0, code="ThrottlingException"):
    """Patch events.put_events; return (calls, patcher). Each call's Entries list is appended to calls.
    `failed` entries of every call come back with an ErrorCode, as PutEvents reports a partial failure."""
    calls = []

    def put_events(Entries):
        calls.append(Entries)
        results = [{"EventId": f"id-{i}"} for i in range(len(Entries))]
        for r in results[:failed]:
            r.clear()
            r.update(ErrorCode=code, ErrorMessage="test failure")
        return {"FailedEntryCount": min(failed, len(Entries)), "Entries": results}

    return calls, mock.patch.object(handler.events, "put_events", side_effect=put_events)


def _sizes(calls):
    """Records per entry, call by call: [[100, 100, 50]] is one call of three entries."""
    return [[len(json.loads(e["Detail"])["records"]) for e in entries] for entries in calls]


def test_250_records_go_out_in_one_call_of_three_entries():
    calls, patcher = _capture_put_events()
    with patcher:
        result = handler.handler(_event(_records(250)), None)

    assert _sizes(calls) == [[100, 100, 50]]
    assert result == {"records": 250, "events": 3}


def test_1050_records_go_out_in_two_calls_of_ten_and_one_entries():
    calls, patcher = _capture_put_events()
    with patcher:
        result = handler.handler(_event(_records(1050)), None)

    assert _sizes(calls) == [[100] * 10, [50]]
    assert result == {"records": 1050, "events": 11}


@pytest.mark.parametrize("n, expected", [
    (1, [1]),
    (99, [99]),
    (100, [100]),
    (101, [100, 1]),
    (200, [100, 100]),
    (201, [100, 100, 1]),
])
def test_entries_hold_up_to_100_records_each(n, expected):
    batch = handler.entries(_records(n))
    assert isinstance(batch, list)
    assert [len(json.loads(e["Detail"])["records"]) for e in batch] == expected


def test_no_records_make_no_entries():
    assert handler.entries([]) == []


def test_every_record_is_sent_once_in_order_and_unchanged():
    records = _records(1050)
    calls, patcher = _capture_put_events()
    with patcher:
        handler.handler(_event(records), None)

    sent = [r for entries in calls for e in entries for r in json.loads(e["Detail"])["records"]]
    assert sent == records  # dicts, as printed: parsed from each log line, not left as strings


def test_each_entry_names_the_bus_source_and_detail_type_the_rule_matches():
    for entry in handler.entries(_records(150)):
        assert set(entry) == {"EventBusName", "Source", "DetailType", "Detail"}
        assert entry["EventBusName"] == "test-bus"  # BUS_NAME; without it the event lands on the default bus
        assert entry["Source"] == "anomalypulse.telemetry"
        assert entry["DetailType"] == "TelemetryRecords"


def test_detail_is_a_json_object_string_holding_only_the_records():
    entry = handler.entries(_records(3))[0]
    assert isinstance(entry["Detail"], str)
    detail = json.loads(entry["Detail"])
    assert isinstance(detail, dict)  # EventBridge refuses a Detail that is a bare JSON list
    assert list(detail) == ["records"]


def test_every_entry_stays_far_under_the_sqs_message_limit():
    # The largest record in 781,135 validation records was 409 bytes; pad these past that.
    records = [dict(r, endpoint="/products/" + "x" * 300) for r in _records(1000)]
    assert len(json.dumps(records[0]).encode("utf-8")) > 409
    for entry in handler.entries(records):
        size = sum(len(entry[k].encode("utf-8")) for k in ("Source", "DetailType", "Detail"))
        assert size < 64 * 1024  # SQS refuses a message over 256 KB by default; EventBridge adds an envelope


def test_requests_match_the_putevents_api_model():
    stubber = Stubber(handler.events)
    for n in (10, 1):  # 1,050 records: a call of 10 entries, then a call of 1
        stubber.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e"}] * n},
                             {"Entries": ANY})
    with stubber:
        result = handler.handler(_event(_records(1050)), None)
    stubber.assert_no_pending_responses()
    assert result == {"records": 1050, "events": 11}


def test_a_control_message_sends_nothing():
    calls, patcher = _capture_put_events()
    with patcher:
        result = handler.handler(_control_event(), None)

    assert calls == []
    assert result == {"records": 0, "events": 0}


def test_a_partial_failure_raises_with_the_count_and_error_code():
    # put_events doesn't raise when entries fail; the handler must, so Lambda retries the delivery.
    calls, patcher = _capture_put_events(failed=1)
    with patcher, pytest.raises(RuntimeError) as err:
        handler.handler(_event(_records(250)), None)

    assert len(calls) == 1
    assert "1 of 3" in str(err.value)
    assert "ThrottlingException" in str(err.value)


def test_a_failure_in_a_later_call_raises_too():
    calls = []

    def put_events(Entries):
        calls.append(Entries)
        failed = 1 if len(calls) == 2 else 0
        results = [{"EventId": "e"} for _ in Entries]
        if failed:
            results[0] = {"ErrorCode": "InternalFailure", "ErrorMessage": "test failure"}
        return {"FailedEntryCount": failed, "Entries": results}

    with mock.patch.object(handler.events, "put_events", side_effect=put_events), \
            pytest.raises(RuntimeError, match="InternalFailure"):
        handler.handler(_event(_records(1050)), None)
    assert len(calls) == 2
