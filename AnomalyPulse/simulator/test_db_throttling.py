import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from simulator.injectors import Context
from simulator.injectors.db_throttling import DbThrottle

PRODUCTS_TABLE = "anomalypulse-products"
SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"
BAND = {"parameter": "load_ratio", "low": 1.5, "high": 2.5}
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"


def _injector(session):
    now_ms = int(time.time() * 1000)
    activation = DbThrottle.plan(
        EXPERIMENT_ID, 0,
        active_from_ms=now_ms, active_until_ms=now_ms + 60_000,
        rng=random.Random(7), intensity=0.5, band=BAND,
    )
    ctx = Context(experiment_id=EXPERIMENT_ID, session=session, control_plane=None)
    return DbThrottle(ctx, activation)


def _make_products_table(session, *, rcu=10, provisioned=True):
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
    table.put_item(Item={"id": "p1"})      # start() needs at least one item to read
    session.client("ssm").put_parameter(Name=SSM_TABLE_PARAM, Value=PRODUCTS_TABLE, Type="String")



def test_configure_puts_the_ratio_inside_the_band():
    assert DbThrottle.configure(0.0, BAND, random.Random(1)) == {"load_ratio": 1.5}
    assert DbThrottle.configure(0.5, BAND, random.Random(1)) == {"load_ratio": 2.0}
    assert DbThrottle.configure(1.0, BAND, random.Random(1)) == {"load_ratio": 2.5}



def test_start_reads_the_tables_capacity(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    injector.stop()
    assert injector._provisioned_rcu == 10


def test_start_refuses_an_on_demand_table(aws):
    _make_products_table(aws, provisioned=False)
    injector = _injector(aws)
    with pytest.raises(RuntimeError, match="PROVISIONED"):
        injector.start()



def test_load_runs_at_ratio_times_capacity_and_stops(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)

    injector.start()
    assert len(injector._threads) > 0
    time.sleep(1.5)
    assert injector.stop() is None

    assert injector._threads == []
    reads = injector._attempted
    assert 10 <= reads <= 40

    time.sleep(0.3)
    assert injector._attempted == reads



def test_stop_before_start_is_safe(aws):
    injector = _injector(aws)
    assert injector.stop() is None
    assert injector.stop() is None


def test_stop_twice_after_start_is_safe(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    assert injector.stop() is None
    assert injector.stop() is None

def _injector(session, window_ms=60_000):                      # change the existing helper
    now_ms = int(time.time() * 1000)
    activation = DbThrottle.plan(
        EXPERIMENT_ID, 0,
        active_from_ms=now_ms, active_until_ms=now_ms + window_ms,   # was + 60_000
        rng=random.Random(7), intensity=0.5, band=BAND,
    )
    ctx = Context(experiment_id=EXPERIMENT_ID, session=session, control_plane=None)
    return DbThrottle(ctx, activation)


def test_observe_sums_cloudwatch_throttles(aws):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws, window_ms=180_000)               # a 3-minute window
    injector._table, injector._provisioned_rcu = PRODUCTS_TABLE, 10
    injector._attempted, injector._throttled = 100, 25

    start = datetime.fromtimestamp(injector.activation.active_from_ms / 1000, timezone.utc)
    cloudwatch = aws.client("cloudwatch")
    table = {"Name": "TableName", "Value": PRODUCTS_TABLE}
    for minute, (throttled, events) in enumerate([(7, 9), (11, 13)]):
        stamp = start + timedelta(minutes=minute, seconds=30)
        cloudwatch.put_metric_data(Namespace="AWS/DynamoDB", MetricData=[
            {"MetricName": "ThrottledRequests", "Timestamp": stamp, "Value": throttled,
             "Dimensions": [table, {"Name": "Operation", "Value": "GetItem"}]},
            {"MetricName": "ReadThrottleEvents", "Timestamp": stamp, "Value": events,
             "Dimensions": [table]},
        ])

    observed = injector.observe()

    assert observed["cw_throttled_requests"] == 18
    assert observed["cw_read_throttle_events"] == 22
    assert observed["client_throttle_rate"] == 0.25