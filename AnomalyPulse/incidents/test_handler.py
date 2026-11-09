"""Tests for the M8-3 Lambda's AWS layer, with boto3 faked. No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_handler.py -q
"""
import copy
import io
import json
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from incidents import core, handler, record
from incidents.test_core import REPLY, event, failing, ws, window


def _no_floats(value):
    if isinstance(value, float):
        raise TypeError("Float types are not supported. Use Decimal types instead.")
    if isinstance(value, dict):
        for v in value.values():
            _no_floats(v)
    if isinstance(value, list):
        for v in value:
            _no_floats(v)


class FakeTable:
    """A DynamoDB table that, like the real one, refuses a float."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(tuple(sorted(Key.items())))
        return {"Item": copy.deepcopy(item)} if item is not None else {}

    def put_item(self, Item):
        _no_floats(Item)
        key = tuple(sorted((k, v) for k, v in Item.items() if k in ("pk", "sk")))
        self.items[key] = copy.deepcopy(Item)


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


class FakeLambda:
    def __init__(self, reply=None, function_error=False, error=None):
        self.reply = REPLY if reply is None else reply
        self.function_error, self.error, self.calls = function_error, error, []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        response = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(self.reply).encode())}
        if self.function_error:
            response["FunctionError"] = "Unhandled"
        return response


def build(lock=None, lam=None, objects=None):
    experiments = FakeTable()
    if lock:
        experiments.items[(("pk", "LOCK"),)] = lock
    return handler.AwsDeps(FakeTable(), experiments, FakeS3(objects), lam or FakeLambda(),
                           "bucket", "anomalypulse-explain")


def test_state_round_trips_through_the_table_with_no_floats_stored():
    deps = build()
    state = {"incident": {"status": "OPEN", "confidence": 0.93, "tally": {"X": {"minutes": 1}}},
             "recent_normal": [window(ws(0))], "baseline": {"order_p95_latency_ms": 110.5}}
    deps.put_state(state)
    stored = deps.table.items[(("pk", "INCIDENT_STATE"), ("sk", "CURRENT"))]
    assert stored["state"]["incident"]["confidence"] == Decimal("0.93")
    assert deps.get_state() == state


def test_a_missing_state_reads_as_none():
    assert build().get_state() is None


def test_a_record_round_trips():
    deps = build()
    rec = record.open_record({"started_at": ws(2), "incident_type": "DEPENDENCY_FAILURE", "confidence": 0.9},
                             {"latency": {"diagnosed_at": ws(5), "since_window_end_s": 167.25}},
                             ["order-service"], {"estimator": "e", "job": "j"})
    deps.put_record(rec)
    assert deps.get_record(ws(2)) == rec
    assert deps.get_record(ws(9)) is None


def test_a_window_is_read_from_s3():
    deps = build(objects={"pipeline/windows/x.json": window(ws(1))})
    assert deps.read_window("pipeline/windows/x.json") == window(ws(1))


def test_no_lock_reads_as_none():
    assert build().read_lock() is None


def test_a_lock_reads_back_with_an_int_expiry():
    deps = build(lock={"pk": "LOCK", "experiment_id": "exp-1", "held_by": "jake", "expires_at": Decimal("1793000000")})
    assert deps.read_lock() == {"experiment_id": "exp-1", "expires_at": 1793000000}
    assert isinstance(deps.read_lock()["expires_at"], int)


def test_explain_returns_the_reply_and_invokes_synchronously():
    lam = FakeLambda()
    deps = build(lam=lam)
    assert deps.explain({"incident_id": "inc-1"}) == REPLY
    call = lam.calls[0]
    assert call["FunctionName"] == "anomalypulse-explain" and call["InvocationType"] == "RequestResponse"
    assert json.loads(call["Payload"]) == {"incident_id": "inc-1"}


def test_a_retry_sends_the_revision_beside_the_inputs_fields():
    lam = FakeLambda()
    revise = {"answer": {"summary": "s"}, "findings": ["summary: the number 9 is not in or derivable from the input"]}
    assert build(lam=lam).explain({"incident_id": "inc-1"}, revise=revise) == REPLY
    assert json.loads(lam.calls[0]["Payload"]) == {"incident_id": "inc-1", "revise": revise}


def test_a_function_error_becomes_an_explain_error_with_its_type():
    lam = FakeLambda(reply={"errorType": "DiagnosisFailed", "errorMessage": "refused"}, function_error=True)
    with pytest.raises(core.ExplainError) as err:
        build(lam=lam).explain({})
    assert err.value.kind == "DiagnosisFailed" and err.value.message == "refused"


def test_a_timeout_becomes_an_explain_error():
    lam = FakeLambda(error=ReadTimeoutError(endpoint_url="https://lambda.us-east-2.amazonaws.com"))
    with pytest.raises(core.ExplainError) as err:
        build(lam=lam).explain({})
    assert err.value.kind == "ReadTimeoutError"


def test_a_throttle_becomes_an_explain_error():
    lam = FakeLambda(error=ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "slow"}}, "Invoke"))
    with pytest.raises(core.ExplainError) as err:
        build(lam=lam).explain({})
    assert err.value.kind == "ClientError"


def test_emit_prints_one_json_line(capsys):
    build().emit({"event": "incident_diagnosis", "RoundTripSeconds": 180.0})
    assert json.loads(capsys.readouterr().out) == {"event": "incident_diagnosis", "RoundTripSeconds": 180.0}


def test_a_whole_step_through_the_aws_layer():
    deps = build()
    for m, typ, win in ((0, None, None), (1, None, None), (2, "DEPENDENCY_FAILURE", failing(2))):
        core.process(event(m, typ, win), deps)
    stored = record.from_dynamodb(deps.table.items[(("pk", "INCIDENT"), ("sk", ws(2)))])
    assert stored["status"] == "OPEN" and stored["diagnosis"] == REPLY["diagnosis"]
    assert len(deps.lambda_client.calls) == 1
    assert deps.get_state()["incident"]["status"] == "OPEN"


def test_the_state_item_stays_small():
    deps = build()
    wide = {f"c{i}": 123.456789 for i in range(106)}
    deps.put_state({"incident": None, "recent_normal": [{"window_start": ws(m), **wide} for m in range(5)],
                    "baseline": wide})
    size = len(json.dumps(record.from_dynamodb(deps.table.items[(("pk", "INCIDENT_STATE"), ("sk", "CURRENT"))]))
               )
    assert size < 100_000


def test_the_handler_runs_a_step_and_returns_a_summary(monkeypatch, capsys):
    monkeypatch.setattr(handler, "_deps", build())
    result = handler.handler(event(0), None)
    assert result == {"action": "processed", "changes": [], "incident": None, "window_from": "event"}
    assert "incident_step" in capsys.readouterr().out