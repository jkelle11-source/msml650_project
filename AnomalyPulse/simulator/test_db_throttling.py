import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from simulator import manifest
from simulator.injectors import Context
from simulator.injectors.db_throttling import DbThrottle

PRODUCTS_TABLE = "anomalypulse-products"
SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"
BAND = {"parameter": "load_ratio", "low": 1.5, "high": 2.5}
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"


def _injector(session, window_ms=60_000):
    now_ms = int(time.time() * 1000)
    activation = DbThrottle.plan(
        EXPERIMENT_ID, 0,
        active_from_ms=now_ms, active_until_ms=now_ms + window_ms,
        rng=random.Random(7), intensity=0.5, band=BAND,
    )
    ctx = Context(experiment_id=EXPERIMENT_ID, session=session, control_plane=None)
    return DbThrottle(ctx, activation)


def _make_products_table(session, *, rcu=10, provisioned=True, items=5):
    if provisioned:
        capacity = {"ProvisionedThroughput": {"ReadCapacityUnits": rcu, "WriteCapacityUnits": rcu}}
    else:
        capacity = {"BillingMode": "PAY_PER_REQUEST"}
    table = session.resource("dynamodb").create_table(
        TableName=PRODUCTS_TABLE,
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        **capacity,
    )
    for i in range(items):
        table.put_item(Item={"id": f"prod-{i + 1:03d}", "name": "thing"})
    session.client("ssm").put_parameter(Name=SSM_TABLE_PARAM, Value=PRODUCTS_TABLE, Type="String")


class _FakeClient:
    """Answers batch_get_item with whatever `answer` says: a response dict or an exception."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def batch_get_item(self, RequestItems):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _request(keys=5):
    return {PRODUCTS_TABLE: {"Keys": [{"id": f"prod-{i:03d}"} for i in range(keys)],
                             "ConsistentRead": True}}


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "BatchGetItem")


# --- configure ---------------------------------------------------------------

def test_configure_puts_the_ratio_inside_the_band():
    assert DbThrottle.configure(0.0, BAND, random.Random(1)) == {"load_ratio": 1.5}
    assert DbThrottle.configure(0.5, BAND, random.Random(1)) == {"load_ratio": 2.0}
    assert DbThrottle.configure(1.0, BAND, random.Random(1)) == {"load_ratio": 2.5}


# --- start / stop --------------------------------------------------------------

def test_start_reads_the_tables_capacity(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    injector.stop()
    assert injector._provisioned_rcu == 10
    assert injector._target_reads_per_s == 20      # ratio 2.0 x 10 RCU


def test_start_refuses_an_on_demand_table(aws):
    _make_products_table(aws, provisioned=False)
    injector = _injector(aws)
    with pytest.raises(RuntimeError, match="PROVISIONED"):
        injector.start()


def test_start_refuses_an_empty_table(aws):
    _make_products_table(aws, items=0)
    injector = _injector(aws)
    with pytest.raises(RuntimeError, match="no items"):
        injector.start()


def test_load_runs_at_ratio_times_capacity_and_stops(aws):
    # 10 RCU x ratio 2.0 = 20 reads/s: four 5-key batches a second.
    _make_products_table(aws, rcu=10, items=5)
    injector = _injector(aws)

    injector.start()
    assert len(injector._threads) == 1
    time.sleep(1.5)
    assert injector.stop() is None

    assert injector._threads == []
    reads = injector._attempted
    assert reads % 5 == 0                 # whole batches
    assert 15 <= reads <= 45
    assert injector._throttled == 0       # moto never throttles

    time.sleep(0.3)
    assert injector._attempted == reads   # the load really stopped


def test_many_reads_a_second_need_few_calls(aws):
    # 1,000 RCU x 2.0 = 2,000 reads/s: 80 calls of 25 keys, so 8 workers, not 50.
    _make_products_table(aws, rcu=1000, items=25)
    injector = _injector(aws)
    injector.start()
    workers = len(injector._threads)
    injector.stop()
    assert workers == 8


def test_stop_before_start_is_safe(aws):
    injector = _injector(aws)
    assert injector.stop() is None
    assert injector.stop() is None
    assert "achieved_reads_per_s" not in injector.observe()


def test_stop_twice_after_start_is_safe(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    assert injector.stop() is None
    assert injector.stop() is None


# --- counting reads ------------------------------------------------------------

def test_a_full_batch_counts_every_key_read(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    injector._read(_FakeClient({"Responses": {}, "UnprocessedKeys": {}}), _request(5))
    assert (injector._attempted, injector._throttled, injector._errors) == (5, 0, 0)
    assert injector._first_throttle_ms is None


def test_unprocessed_keys_are_throttled_reads(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    partial = {"Responses": {}, "UnprocessedKeys": {
        PRODUCTS_TABLE: {"Keys": [{"id": "prod-001"}, {"id": "prod-002"}], "ConsistentRead": True}}}
    before = int(time.time() * 1000)
    injector._read(_FakeClient(partial), _request(5))
    assert (injector._attempted, injector._throttled, injector._errors) == (5, 2, 0)
    assert injector._first_throttle_ms >= before


def test_a_refused_batch_throttles_every_key(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    injector._read(_FakeClient(_client_error("ProvisionedThroughputExceededException")), _request(5))
    assert (injector._attempted, injector._throttled, injector._errors) == (5, 5, 0)


def test_other_failures_are_errors_not_throttles(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    injector._read(_FakeClient(_client_error("ValidationException")), _request(5))
    injector._read(_FakeClient(EndpointConnectionError(endpoint_url="https://x")), _request(5))
    assert (injector._attempted, injector._throttled, injector._errors) == (10, 0, 10)
    assert injector._first_throttle_ms is None


def test_the_first_throttle_time_is_kept(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    refused = _FakeClient(_client_error("ProvisionedThroughputExceededException"))
    injector._read(refused, _request(5))
    first = injector._first_throttle_ms
    time.sleep(0.01)
    injector._read(refused, _request(5))
    assert injector._first_throttle_ms == first


# --- observe --------------------------------------------------------------------

def test_observe_reports_the_clients_view(aws):
    injector = _injector(aws)
    injector._table, injector._provisioned_rcu, injector._target_reads_per_s = None, 10, 20.0
    injector._attempted, injector._throttled, injector._errors = 100, 25, 5
    injector._first_throttle_ms = injector.activation.active_from_ms + 90_000
    injector._started_s, injector._stopped_s = 1000.0, 1010.0

    observed = injector.observe()

    assert observed["target_reads_per_s"] == 20.0
    assert observed["achieved_reads_per_s"] == 10.0
    assert observed["client_throttle_rate"] == 0.25
    assert observed["read_errors"] == 5
    assert observed["first_throttle_at"] == manifest.to_iso(injector._first_throttle_ms)
    assert "cw_throttled_requests" not in observed     # no table, no CloudWatch


def test_observe_sums_cloudwatch_throttles_over_every_operation(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws, window_ms=180_000)               # a 3-minute window
    injector._table, injector._provisioned_rcu = PRODUCTS_TABLE, 10

    start = datetime.fromtimestamp(injector.activation.active_from_ms / 1000, timezone.utc)
    cloudwatch = aws.client("cloudwatch")
    table = {"Name": "TableName", "Value": PRODUCTS_TABLE}

    def operation(name):
        return [table, {"Name": "Operation", "Value": name}]

    for minute, (get_item, scan, events, consumed) in enumerate([(7, 2, 9, 600), (11, 4, 13, 590)]):
        stamp = start + timedelta(minutes=minute, seconds=30)
        cloudwatch.put_metric_data(Namespace="AWS/DynamoDB", MetricData=[
            {"MetricName": "ThrottledRequests", "Timestamp": stamp, "Value": get_item,
             "Dimensions": operation("GetItem")},
            {"MetricName": "ThrottledRequests", "Timestamp": stamp, "Value": scan,
             "Dimensions": operation("Scan")},
            {"MetricName": "ReadThrottleEvents", "Timestamp": stamp, "Value": events,
             "Dimensions": [table]},
            {"MetricName": "ConsumedReadCapacityUnits", "Timestamp": stamp, "Value": consumed,
             "Dimensions": [table]},
        ])

    observed = injector.observe()

    assert observed["cw_throttled_requests_by_operation"] == {"GetItem": 18, "Scan": 6}
    assert observed["cw_throttled_requests"] == 24
    assert observed["cw_read_throttle_events"] == 22
    assert observed["cw_consumed_read_capacity"] == 1190


def test_observe_is_plain_json_for_the_manifest(aws):
    import json
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    time.sleep(0.3)
    injector.stop()
    observed = injector.observe()
    assert json.loads(json.dumps(observed)) == observed
    assert observed["cw_throttled_requests"] == 0
    assert observed["cw_throttled_requests_by_operation"] == {}
