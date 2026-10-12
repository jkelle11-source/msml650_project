import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

# The Lambda-side reader builds its DynamoDB resource at import, which needs a
# region to resolve. Set one before importing it, as the layer's own tests do.
os.environ["AWS_DEFAULT_REGION"] = "us-east-2"

# simulator/ is a package, so it is imported by name from the directory above
# it. The reader lives in the telemetry layer; it is imported here only for the
# contract tests at the bottom of this file.
_ANOMALYPULSE = Path(__file__).parent.parent
sys.path.insert(0, str(_ANOMALYPULSE))
sys.path.insert(0, str(_ANOMALYPULSE / "layers" / "telemetry" / "python"))

import fault_config as reader
from simulator import control_plane as cp

TABLE = "anomalypulse-fault-config"
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"
FAULT_ID = f"{EXPERIMENT_ID}#0"
SERVICE = "payment-service"
PARAMS = {"timeout_ms": 3300, "failure_rate": 0.2}
MINUTE = 60_000

# A run that starts at NOW with its fault active from minute 5 to minute 10.
NOW = 1_791_778_500_000
START = NOW + 5 * MINUTE
END = NOW + 10 * MINUTE


# --- helpers ---------------------------------------------------------------

def _fault(**overrides):
    fields = {
        "service": SERVICE,
        "fault_id": FAULT_ID,
        "injector": "payment-failure",
        "params": PARAMS,
        "active_from_ms": START,
        "active_until_ms": END,
        "now_ms": NOW,
    }
    fields.update(overrides)
    return fields


def _expect_query(stubber, service, items, prefix=f"{EXPERIMENT_ID}#", start_key=None, last_key=None):
    condition = Key("service").eq(service)
    if prefix is not None:
        condition = condition & Key("fault_id").begins_with(prefix)
    expected = {
        "TableName": TABLE,
        "KeyConditionExpression": condition,
        "ProjectionExpression": "fault_id",
        "ConsistentRead": True,
    }
    if start_key is not None:
        expected["ExclusiveStartKey"] = start_key
    response = {"Items": [{"fault_id": {"S": fault_id}} for fault_id in items]}
    if last_key is not None:
        response["LastEvaluatedKey"] = {k: {"S": v} for k, v in last_key.items()}
    stubber.add_response("query", response, expected_params=expected)


def _expect_delete(stubber, service, fault_id):
    stubber.add_response("delete_item", {}, expected_params={
        "TableName": TABLE, "Key": {"service": service, "fault_id": fault_id}})


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def plane():
    # Stubber intercepts every call on the table's real client. A queued
    # response is checked against the expected parameters and returned without
    # touching the network; a call with nothing queued raises. So no test in
    # this file can reach AWS, even by mistake. The credentials are placeholders.
    resource = boto3.resource("dynamodb", region_name="us-east-2",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
    table = resource.Table(TABLE)
    with Stubber(table.meta.client) as stubber:
        yield cp.ControlPlane(table), stubber


# --- _to_dynamo: plain Python numbers -> what DynamoDB accepts ------------------

def test_floats_become_decimals_and_nothing_else_changes():
    # boto3's DynamoDB resource raises on a float. Ints, bools, strings and None
    # go through as they are; in particular True must not turn into a number.
    converted = cp._to_dynamo({"rate": 0.2, "ms": 800, "on": True, "mode": "5xx", "none": None})
    assert converted == {"rate": Decimal("0.2"), "ms": 800, "on": True, "mode": "5xx", "none": None}
    assert type(converted["rate"]) is Decimal
    assert type(converted["ms"]) is int
    assert type(converted["on"]) is bool


def test_floats_are_converted_at_any_depth():
    converted = cp._to_dynamo({"steps": [0.5, {"deep": 1.5}], "pair": (0.25, 2)})
    assert converted == {"steps": [Decimal("0.5"), {"deep": Decimal("1.5")}],
                         "pair": [Decimal("0.25"), 2]}


def test_decimal_keeps_the_digits_the_float_was_written_with():
    # Decimal(0.1) would be 0.1000000000000000055511151231257827..., which is
    # more digits than DynamoDB stores. Going through str() keeps it at 0.1.
    assert cp._to_dynamo(0.1) == Decimal("0.1")


# --- schedule ------------------------------------------------------------------

def test_schedule_writes_one_item_for_the_fault(plane):
    control_plane, stubber = plane
    stubber.add_response("put_item", {}, expected_params={
        "TableName": TABLE,
        "Item": {
            "service": SERVICE,
            "fault_id": FAULT_ID,
            "injector": "payment-failure",
            "params": {"timeout_ms": 3300, "failure_rate": Decimal("0.2")},
            "active_from_ms": START,
            "active_until_ms": END,
            # DynamoDB's TTL attribute is epoch SECONDS: an hour after the window.
            "expires_at": END // 1000 + 3600,
        },
    })
    control_plane.schedule(**_fault())
    stubber.assert_no_pending_responses()


def test_schedule_rejects_a_service_no_handler_reads(plane):
    # An item under a partition nobody queries would be a fault that never
    # happens, in a run whose manifest says it did. Nothing is queued on the
    # stubber, so a write here would raise a stubbing error instead.
    control_plane, _ = plane
    with pytest.raises(ValueError, match="unknown service"):
        control_plane.schedule(**_fault(service="payments"))


@pytest.mark.parametrize("active_until_ms", [START, START - 1])
def test_schedule_rejects_a_window_with_no_length(plane, active_until_ms):
    control_plane, _ = plane
    with pytest.raises(ValueError, match="before active_until_ms"):
        control_plane.schedule(**_fault(active_until_ms=active_until_ms))


@pytest.mark.parametrize("now_ms", [START - cp.MIN_LEAD_MS + 1, START, START + 1])
def test_schedule_rejects_a_fault_that_starts_too_soon(plane, now_ms):
    # Each execution environment caches its items for a few seconds. An item
    # written inside that time would switch on at different moments in different
    # environments, so the window in the manifest would be wrong.
    control_plane, _ = plane
    with pytest.raises(ValueError, match="at least 10000 ms"):
        control_plane.schedule(**_fault(now_ms=now_ms))


def test_schedule_accepts_a_fault_exactly_the_minimum_lead_ahead(plane):
    control_plane, stubber = plane
    stubber.add_response("put_item", {})
    control_plane.schedule(**_fault(now_ms=START - cp.MIN_LEAD_MS))
    stubber.assert_no_pending_responses()


def test_schedule_measures_the_lead_from_the_wall_clock_by_default(plane):
    control_plane, stubber = plane
    now = int(time.time() * 1000)
    stubber.add_response("put_item", {})
    control_plane.schedule(**_fault(now_ms=None, active_from_ms=now + MINUTE,
                                    active_until_ms=now + 2 * MINUTE))
    with pytest.raises(ValueError, match="at least 10000 ms"):
        control_plane.schedule(**_fault(now_ms=None))  # START is long past
    stubber.assert_no_pending_responses()


def test_minimum_lead_is_longer_than_the_readers_cache():
    # The whole point of the lead: by the time the window opens, every cache has
    # been refreshed at least once since the item was written.
    assert cp.MIN_LEAD_MS > reader._TTL_SECONDS * 1000


# --- cancel --------------------------------------------------------------------

def test_cancel_deletes_the_item_unconditionally(plane):
    # No condition on the delete, so cancelling a fault that was never written,
    # or cancelling twice, succeeds like any other DynamoDB delete of a missing
    # item. That is what lets an injector's stop() be called at any time.
    control_plane, stubber = plane
    _expect_delete(stubber, SERVICE, FAULT_ID)
    _expect_delete(stubber, SERVICE, FAULT_ID)
    control_plane.cancel(SERVICE, FAULT_ID)
    control_plane.cancel(SERVICE, FAULT_ID)
    stubber.assert_no_pending_responses()


def test_cancel_experiment_deletes_its_faults_in_every_service(plane):
    # A cascade puts faults under several services. Each partition is asked for
    # this experiment's ids with a strongly consistent read, so a fault written
    # a moment ago is not missed.
    control_plane, stubber = plane
    _expect_query(stubber, "product-service", [f"{EXPERIMENT_ID}#1"])
    _expect_delete(stubber, "product-service", f"{EXPERIMENT_ID}#1")
    _expect_query(stubber, "cart-service", [])
    _expect_query(stubber, "order-service", [])
    _expect_query(stubber, "payment-service", [f"{EXPERIMENT_ID}#0", f"{EXPERIMENT_ID}#2"])
    _expect_delete(stubber, "payment-service", f"{EXPERIMENT_ID}#0")
    _expect_delete(stubber, "payment-service", f"{EXPERIMENT_ID}#2")
    assert control_plane.cancel_experiment(EXPERIMENT_ID) == 3
    stubber.assert_no_pending_responses()


def test_cancel_experiment_with_nothing_left_deletes_nothing(plane):
    control_plane, stubber = plane
    for service in cp.SERVICES:
        _expect_query(stubber, service, [])
    assert control_plane.cancel_experiment(EXPERIMENT_ID) == 0
    stubber.assert_no_pending_responses()


def test_cancel_experiment_follows_pagination(plane):
    control_plane, stubber = plane
    page_key = {"service": "product-service", "fault_id": f"{EXPERIMENT_ID}#0"}
    _expect_query(stubber, "product-service", [f"{EXPERIMENT_ID}#0"], last_key=page_key)
    _expect_delete(stubber, "product-service", f"{EXPERIMENT_ID}#0")
    _expect_query(stubber, "product-service", [f"{EXPERIMENT_ID}#1"], start_key=page_key)
    _expect_delete(stubber, "product-service", f"{EXPERIMENT_ID}#1")
    for service in cp.SERVICES[1:]:
        _expect_query(stubber, service, [])
    assert control_plane.cancel_experiment(EXPERIMENT_ID) == 2
    stubber.assert_no_pending_responses()


def test_cancel_all_deletes_every_fault_whatever_its_experiment(plane):
    control_plane, stubber = plane
    other = "exp-20261011T230000Z-0b1c#0"
    _expect_query(stubber, "product-service", [], prefix=None)
    _expect_query(stubber, "cart-service", [], prefix=None)
    _expect_query(stubber, "order-service", [other], prefix=None)
    _expect_delete(stubber, "order-service", other)
    _expect_query(stubber, "payment-service", [FAULT_ID], prefix=None)
    _expect_delete(stubber, "payment-service", FAULT_ID)
    assert control_plane.cancel_all() == 2
    stubber.assert_no_pending_responses()


def test_services_are_the_ones_telemetry_names():
    # The partition key has to match what each handler passes to active_faults().
    assert cp.SERVICES == ("product-service", "cart-service", "order-service", "payment-service")


# --- contract with the Lambda-side reader ---------------------------------------
# The writer (this package) and the reader (layers/telemetry/python/fault_config.py)
# were written separately and only meet in DynamoDB. A mismatch between them
# (an attribute name, seconds for milliseconds, a number type) would not raise
# anywhere: the fault would simply never apply, and the run would be mislabeled.

@pytest.fixture
def read_back(monkeypatch):
    # Hands the reader an item exactly as DynamoDB would return it after
    # schedule() wrote it, and returns reader.active_faults.
    def _read_back(**fault):
        item = cp._item(**{k: v for k, v in fault.items() if k != "now_ms"})
        wire = {name: TypeSerializer().serialize(value) for name, value in item.items()}
        stubber.add_response("query", {"Items": [wire]})
        return reader.active_faults

    reader._cache.clear()
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    monkeypatch.setattr(reader, "_TTL_SECONDS", 60.0)  # one fetch serves the whole test
    with Stubber(reader._dynamodb.meta.client) as stubber:
        yield _read_back
    reader._cache.clear()


def test_reader_applies_a_scheduled_fault_only_inside_its_window(read_back):
    active_faults = read_back(**_fault())
    assert active_faults(SERVICE, now_ms=START - 1) == []
    assert [f["fault_id"] for f in active_faults(SERVICE, now_ms=START)] == [FAULT_ID]
    assert [f["fault_id"] for f in active_faults(SERVICE, now_ms=END - 1)] == [FAULT_ID]
    assert active_faults(SERVICE, now_ms=END) == []


def test_reader_gets_back_the_params_the_injector_resolved(read_back):
    # Floats went in as Decimals; the handler must get plain numbers back, of the
    # same type, so it can pass them straight to time.sleep() or a comparison.
    params = {"timeout_ms": 3300, "failure_rate": 0.2, "modes": ["latency", "5xx"],
              "nested": {"jitter": 0.05}}
    active_faults = read_back(**_fault(params=params))
    fault = active_faults(SERVICE, now_ms=START)[0]
    assert fault["injector"] == "payment-failure"
    assert fault["params"] == params
    assert type(fault["params"]["timeout_ms"]) is int
    assert type(fault["params"]["failure_rate"]) is float
    assert type(fault["params"]["nested"]["jitter"]) is float
