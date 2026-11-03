"""Local unit tests for the telemetry aggregator (M8-1). No live AWS needed: the
S3 client is mocked, or stubbed with botocore's Stubber, which checks each
request against the PutObject API model. BUCKET_NAME and the region are set
before import because the handler reads them when the module loads.

Every expected key and line is worked out by hand: a record goes to the minute
its timestamp rounds down to, each minute is one file per invocation, and a
message fails as a whole. The rounding is also checked against
features/windows.py, which built the training windows."""
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from botocore.stub import ANY, Stubber

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # AnomalyPulse/, for features.windows
from features.windows import request_window  # noqa: E402

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
import handler  # noqa: E402  (must follow the env vars above)

PREFIX = "pipeline/records/dt=2026-11-02/"


def _rec(request_id, service, timestamp, **extra):
    """A telemetry-shaped record."""
    return {"timestamp": timestamp, "request_id": request_id, "service": service,
            "status_code": 200, "latency_ms": 41.2, **extra}


def _msg(message_id, records=None, body=None):
    """An SQS message as Lambda delivers it: the body is the EventBridge envelope as a JSON string."""
    if body is None:
        body = json.dumps({"version": "0", "id": f"evt-{message_id}", "detail-type": "TelemetryRecords",
                           "source": "anomalypulse.telemetry", "account": "000000000000",
                           "time": "2026-11-02T19:48:01Z", "region": "us-east-2", "resources": [],
                           "detail": {"records": records}})
    return {"messageId": message_id, "receiptHandle": f"rh-{message_id}", "body": body,
            "attributes": {"ApproximateReceiveCount": "1"}, "messageAttributes": {},
            "eventSource": "aws:sqs", "awsRegion": "us-east-2",
            "eventSourceARN": "arn:aws:sqs:us-east-2:000000000000:anomalypulse-telemetry"}


def _event(*messages):
    return {"Records": list(messages)}


def _ctx(request_id="inv-1"):
    return mock.Mock(aws_request_id=request_id)


def _capture_puts(fail_on=()):
    """Patch s3.put_object; return (puts, patcher). Each call's kwargs are appended to puts.
    A key containing any string in fail_on raises instead, as a failed PUT would."""
    puts = []

    def put_object(**kwargs):
        if any(s in kwargs["Key"] for s in fail_on):
            raise RuntimeError(f"test failure writing {kwargs['Key']}")
        puts.append(kwargs)
        return {"ETag": '"e"'}

    return puts, mock.patch.object(handler.s3, "put_object", side_effect=put_object)


def _files(puts):
    """{key: [record, ...]} from the captured puts, each line parsed back."""
    return {p["Key"]: [json.loads(line) for line in p["Body"].split("\n")] for p in puts}


def _failed(result):
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


# The hand-checkable batch: m1 and m2 hold records from two minutes, m3 isn't JSON.
R1 = _rec("r1", "product-service", "2026-11-02T19:47:10.000Z")
R2 = _rec("r2", "cart-service", "2026-11-02T19:47:59.999Z")
R3 = _rec("r3", "order-service", "2026-11-02T19:48:00.000Z")
R4 = _rec("r4", "product-service", "2026-11-02T19:47:30.500Z")


def _hand_batch():
    return _event(_msg("m1", [R1, R2]), _msg("m2", [R3, R4]), _msg("m3", body="not json"))


# ---- minute_prefix -------------------------------------------------------------

@pytest.mark.parametrize("timestamp, expected", [
    ("2026-11-02T19:47:21.123Z", "pipeline/records/dt=2026-11-02/minute=19-47/"),
    ("2026-11-02T19:47:00.000Z", "pipeline/records/dt=2026-11-02/minute=19-47/"),  # first instant of the minute
    ("2026-11-02T19:47:59.999Z", "pipeline/records/dt=2026-11-02/minute=19-47/"),  # last instant: still 19:47
    ("2026-11-02T09:05:00.000Z", "pipeline/records/dt=2026-11-02/minute=09-05/"),  # zero-padded
    ("2026-11-02T23:59:59.999Z", "pipeline/records/dt=2026-11-02/minute=23-59/"),
    ("2026-11-03T00:00:00.000Z", "pipeline/records/dt=2026-11-03/minute=00-00/"),  # midnight starts a new day
])
def test_minute_prefix_rounds_down_to_the_minute(timestamp, expected):
    assert handler.minute_prefix(timestamp) == expected


@pytest.mark.parametrize("timestamp", [
    "2026-11-02T19:47:21.123Z", "2026-11-02T19:47:59.999Z", "2026-11-02T00:00:00.000Z",
    "2026-11-02T23:59:59.999Z", "2026-12-31T23:59:59.999Z", "2027-01-01T00:00:00.001Z",
    "2026-11-02T19:47:21.1Z", "2026-11-02T19:47:21.123456Z",  # %f takes 1-6 digits, as in training
])
def test_minute_prefix_agrees_with_the_training_windows(timestamp):
    window = request_window(timestamp)  # e.g. "2026-11-02T19:47:00.000Z"
    expected = f"pipeline/records/dt={window[:10]}/minute={window[11:13]}-{window[14:16]}/"
    assert handler.minute_prefix(timestamp) == expected


@pytest.mark.parametrize("timestamp", [
    "2026-11-02T19:47:21Z",            # no fraction
    "2026-11-02 19:47:21.123Z",        # space, not T
    "2026-11-02T19:47:21.123",         # no Z
    "2026-11-02T19:47:21.123+00:00",   # an offset, not Z
    "2026-11-02T25:47:21.123Z",        # no such hour
    "garbage",
    "",
])
def test_minute_prefix_rejects_what_training_would_not_parse(timestamp):
    with pytest.raises(ValueError):
        handler.minute_prefix(timestamp)
    with pytest.raises(ValueError):
        request_window(timestamp)


# ---- handler: the hand-checkable batch -----------------------------------------

def test_the_hand_batch_writes_two_minutes_and_fails_only_m3():
    puts, patcher = _capture_puts()
    with patcher:
        result = handler.handler(_hand_batch(), _ctx("inv-1"))

    assert _files(puts) == {
        PREFIX + "minute=19-47/part-inv-1.jsonl": [R1, R2, R4],  # arrival order
        PREFIX + "minute=19-48/part-inv-1.jsonl": [R3],
    }
    assert len(puts) == 2
    assert all(p["Bucket"] == "test-bucket" for p in puts)
    assert result == {"batchItemFailures": [{"itemIdentifier": "m3"}]}


def test_a_failed_put_fails_only_the_messages_that_fed_that_minute():
    # m2 fed 19:48, so it's retried; m1 fed only 19:47, which was written.
    puts, patcher = _capture_puts(fail_on=("minute=19-48",))
    with patcher:
        result = handler.handler(_hand_batch(), _ctx())

    assert _files(puts) == {PREFIX + "minute=19-47/part-inv-1.jsonl": [R1, R2, R4]}
    assert sorted(_failed(result)) == ["m2", "m3"]


def test_a_batch_with_nothing_wrong_fails_nothing():
    puts, patcher = _capture_puts()
    with patcher:
        result = handler.handler(_event(_msg("m1", [R1, R2]), _msg("m2", [R3, R4])), _ctx())

    assert result == {"batchItemFailures": []}
    assert len(puts) == 2


# ---- handler: one file per minute ----------------------------------------------

def test_each_minute_is_written_once_however_many_messages_fed_it():
    messages = [_msg(f"m{i}", [_rec(f"r{i}-{j}", "product-service", f"2026-11-02T19:47:{j:02d}.000Z")
                               for j in range(3)]) for i in range(30)]
    puts, patcher = _capture_puts()
    with patcher:
        result = handler.handler(_event(*messages), _ctx())

    assert len(puts) == 1
    assert len(_files(puts)[PREFIX + "minute=19-47/part-inv-1.jsonl"]) == 90
    assert result == {"batchItemFailures": []}


def test_one_message_spanning_two_minutes_goes_to_both_files():
    puts, patcher = _capture_puts()
    with patcher:
        handler.handler(_event(_msg("m1", [R1, R3])), _ctx())

    assert _files(puts) == {PREFIX + "minute=19-47/part-inv-1.jsonl": [R1],
                            PREFIX + "minute=19-48/part-inv-1.jsonl": [R3]}


def test_files_are_named_by_the_invocation_so_a_retry_writes_a_new_file():
    puts, patcher = _capture_puts()
    with patcher:
        handler.handler(_event(_msg("m1", [R1])), _ctx("inv-1"))
        handler.handler(_event(_msg("m1", [R1])), _ctx("inv-2"))  # the same message, retried

    assert [p["Key"] for p in puts] == [PREFIX + "minute=19-47/part-inv-1.jsonl",
                                        PREFIX + "minute=19-47/part-inv-2.jsonl"]


def test_one_failed_minute_does_not_stop_the_others():
    r5 = _rec("r5", "payment-service", "2026-11-02T19:49:05.000Z")
    puts, patcher = _capture_puts(fail_on=("minute=19-48",))
    with patcher:
        result = handler.handler(_event(_msg("m1", [R1]), _msg("m2", [R3]), _msg("m3", [r5])), _ctx())

    assert sorted(_files(puts)) == [PREFIX + "minute=19-47/part-inv-1.jsonl",
                                    PREFIX + "minute=19-49/part-inv-1.jsonl"]
    assert _failed(result) == ["m2"]


# ---- handler: what is written ---------------------------------------------------

def test_records_are_written_as_received_one_json_object_per_line():
    full = _rec("r1", "order-service", "2026-11-02T19:47:10.000Z", http_method="POST", endpoint="/orders",
                cold_start=False, db_latency_ms=None, dependency="payment-service",
                dependency_error=None, dependency_latency_ms=12.5, lambda_duration_ms=55.1)
    puts, patcher = _capture_puts()
    with patcher:
        handler.handler(_event(_msg("m1", [full, R2])), _ctx())

    body = puts[0]["Body"]
    lines = body.split("\n")
    assert len(lines) == 2 and not body.endswith("\n")  # the shipper's format: no blank lines
    assert [json.loads(line) for line in lines] == [full, R2]


def test_duplicates_are_kept_for_the_window_builder_to_drop():
    # Every hop delivers at least once; dropping copies is step 6's job, by (service, request_id).
    puts, patcher = _capture_puts()
    with patcher:
        handler.handler(_event(_msg("m1", [R1, R2]), _msg("m2", [R1])), _ctx())

    assert _files(puts)[PREFIX + "minute=19-47/part-inv-1.jsonl"] == [R1, R2, R1]


# ---- handler: bad messages ------------------------------------------------------

@pytest.mark.parametrize("body", [
    "not json",
    "[]",                                            # JSON, but not an envelope
    json.dumps({"detail": {}}),                      # no records
    json.dumps({"detail": {"records": [{"request_id": "x"}]}}),  # a record with no timestamp
])
def test_a_message_that_cannot_be_read_fails_and_writes_nothing(body):
    puts, patcher = _capture_puts()
    with patcher:
        result = handler.handler(_event(_msg("bad", body=body)), _ctx())

    assert puts == []
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_a_message_with_one_bad_timestamp_writes_none_of_its_records():
    # Its good records would be written again on every retry; the whole message is retried instead.
    bad = _rec("r9", "cart-service", "2026-11-02T19:47:40Z")  # no fraction
    puts, patcher = _capture_puts()
    with patcher:
        result = handler.handler(_event(_msg("m1", [R1]), _msg("m2", [R2, R4, bad])), _ctx())

    assert _files(puts) == {PREFIX + "minute=19-47/part-inv-1.jsonl": [R1]}
    assert _failed(result) == ["m2"]


def test_a_message_that_fed_two_failed_minutes_is_listed_once():
    # Lambda reads the list as the IDs to retry; each must be one of the batch's, once.
    puts, patcher = _capture_puts(fail_on=("minute=19-47", "minute=19-48"))
    with patcher:
        result = handler.handler(_event(_msg("m1", [R1, R3]), _msg("m2", [R2])), _ctx())

    assert puts == []
    assert sorted(_failed(result)) == ["m1", "m2"]
    assert len(_failed(result)) == len(set(_failed(result)))


def test_every_returned_id_is_a_message_in_the_batch():
    batch = _hand_batch()
    puts, patcher = _capture_puts(fail_on=("minute=19-47",))
    with patcher:
        result = handler.handler(batch, _ctx())

    assert set(_failed(result)) <= {m["messageId"] for m in batch["Records"]}
    assert sorted(_failed(result)) == ["m1", "m2", "m3"]


# ---- the S3 request itself ---------------------------------------------------------

def test_requests_match_the_putobject_api_model():
    stubber = Stubber(handler.s3)
    for minute in ("19-47", "19-48"):
        stubber.add_response("put_object", {"ETag": '"e"'},
                             {"Bucket": "test-bucket", "Key": PREFIX + f"minute={minute}/part-inv-1.jsonl",
                              "Body": ANY})
    with stubber:
        result = handler.handler(_event(_msg("m1", [R1, R3])), _ctx("inv-1"))
    stubber.assert_no_pending_responses()
    assert result == {"batchItemFailures": []}
