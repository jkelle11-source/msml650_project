import json
import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from simulator import manifest
from simulator.injectors import Context
from simulator.injectors import db_throttling
from simulator.injectors.db_throttling import DbThrottle

PRODUCTS_TABLE = "anomalypulse-products-v2"
SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"
BAND = {"parameter": "load_ratio", "low": 1.5, "high": 2.5}
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"


def _injector(session, window_ms=60_000, active_from_ms=None):
    now_ms = int(time.time() * 1000) if active_from_ms is None else active_from_ms
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


@pytest.fixture
def no_surge(monkeypatch):
    # moto never throttles, so a surge would only end on its time limit. Most
    # tests want the steady phase from the start.
    monkeypatch.setattr(db_throttling, "_SURGE_MAX_S", 0)


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


def _counts(injector, phase="steady"):
    c = injector._counts[phase]
    return c["attempted"], c["throttled"], c["errors"]


# --- configure ---------------------------------------------------------------

def test_configure_puts_the_ratio_inside_the_band():
    assert DbThrottle.configure(0.0, BAND, random.Random(1)) == {"load_ratio": 1.5}
    assert DbThrottle.configure(0.5, BAND, random.Random(1)) == {"load_ratio": 2.0}
    assert DbThrottle.configure(1.0, BAND, random.Random(1)) == {"load_ratio": 2.5}


# --- start / stop --------------------------------------------------------------

def test_start_reads_the_tables_capacity(aws, no_surge):
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


def test_the_steady_load_runs_at_ratio_times_capacity_and_stops(aws, no_surge):
    # 10 RCU x ratio 2.0 = 20 reads/s: four 5-key batches a second.
    _make_products_table(aws, rcu=10, items=5)
    injector = _injector(aws)
    injector.start()
    time.sleep(1.5)
    assert injector.stop() is None

    reads, throttled, _ = _counts(injector)
    assert reads % 5 == 0                 # whole batches
    assert 20 <= reads <= 40
    assert throttled == 0                 # moto never throttles
    observed = injector.observe()
    assert observed["surge_end"] == "time_limit" and observed["surge_s"] < 0.2
    assert 0.8 <= observed["achieved_fraction"] <= 1.2

    time.sleep(0.3)
    assert _counts(injector)[0] == reads  # the load really stopped


def test_the_window_opens_with_a_surge_until_the_bank_is_spent(aws, monkeypatch):
    # The surge reads at 10x capacity until DynamoDB serves only its capacity;
    # then the band's ratio (here 2.0x) takes over.
    monkeypatch.setattr(db_throttling, "_SURGE_RATIO", 10)
    _make_products_table(aws, rcu=10, items=5)
    injector = _injector(aws)
    injector.start()
    time.sleep(0.5)
    injector._bank_spent_ms = int(time.time() * 1000)   # as if DynamoDB served only 10 RCU/s
    time.sleep(1.0)
    injector.stop()

    surge_reads, steady_reads = _counts(injector, "surge")[0], _counts(injector, "steady")[0]
    observed = injector.observe()
    assert observed["surge_end"] == "bank_spent"
    assert 0.4 <= observed["surge_s"] <= 0.7
    assert 30 <= surge_reads <= 70            # ~100 reads/s for ~0.5 s
    assert 10 <= steady_reads <= 30           # ~20 reads/s for ~1 s
    assert observed["bank_spent_at"] == manifest.to_iso(injector._bank_spent_ms)


@pytest.fixture
def surging(aws):
    """An injector a second into its surge, at 10 RCU: the bank is spent at <= 12.5 reads/s served."""
    injector = _injector(aws)
    injector._table, injector._provisioned_rcu = PRODUCTS_TABLE, 10
    injector._started_s = time.monotonic() - 2

    def calls(answers):
        # Calls of 5 keys: 'ok' read them all, 'refused' had all 5 throttled.
        ok = _FakeClient({"Responses": {}, "UnprocessedKeys": {}})
        refused = _FakeClient(_client_error("ProvisionedThroughputExceededException"))
        for answer in answers:
            injector._read(refused if answer == "refused" else ok, _request(5), "surge")

    injector.calls = calls
    return injector


def test_a_throttled_surge_is_not_a_spent_bank(surging):
    # M4-1's validation from the runner: half the surge's reads throttled
    # within ~3 s, while the bank still served 2x and 4x for minutes.
    surging.calls(["ok", "refused"] * 10)              # 50 reads/s served, 5x capacity
    assert surging._first_throttle_ms is not None
    assert surging._bank_spent_ms is None


def test_serving_only_capacity_means_the_bank_is_spent(surging):
    before = int(time.time() * 1000)
    surging.calls(["refused"] * 18 + ["ok"] * 2)       # 10 reads/s served: capacity
    assert surging._bank_spent_ms >= before


def test_a_few_calls_are_not_enough_to_judge(surging):
    surging.calls(["refused"] * (db_throttling._SUSTAIN_MIN_CALLS - 1))
    assert surging._bank_spent_ms is None


def test_nothing_is_judged_in_the_surges_first_second(surging):
    surging._started_s = time.monotonic()
    surging.calls(["refused"] * 20)
    assert surging._bank_spent_ms is None


def test_only_the_last_second_of_surge_reads_counts(surging, monkeypatch):
    monkeypatch.setattr(db_throttling, "_SUSTAIN_S", 0.2)
    surging.calls(["ok"] * 9)
    time.sleep(0.3)                                    # those have aged out
    surging.calls(["refused"] * 10)                    # nothing served in the last 0.2 s
    assert surging._bank_spent_ms is not None


def test_failed_calls_are_not_served_reads(surging):
    broken = _FakeClient(_client_error("ValidationException"))
    for _ in range(20):
        surging._read(broken, _request(5), "surge")
    assert surging._bank_spent_ms is not None          # nothing served


def test_steady_reads_never_mark_the_bank_spent(surging):
    refused = _FakeClient(_client_error("ProvisionedThroughputExceededException"))
    for _ in range(20):
        surging._read(refused, _request(5), "steady")
    assert surging._bank_spent_ms is None


def test_a_surge_that_never_throttles_ends_at_its_time_limit(aws, monkeypatch):
    monkeypatch.setattr(db_throttling, "_SURGE_MAX_S", 0.3)
    _make_products_table(aws, rcu=10, items=5)
    injector = _injector(aws)
    injector.start()
    time.sleep(0.8)
    injector.stop()
    observed = injector.observe()
    assert observed["surge_end"] == "time_limit"
    assert 0.25 <= observed["surge_s"] <= 0.45
    assert observed["first_throttle_at"] is None and observed["label_from"] is None
    assert observed["bank_spent_at"] is None


def test_calls_beyond_the_pool_are_dropped_and_counted(aws, no_surge, monkeypatch):
    # One call in flight at a time, and each takes 0.3 s: most scheduled calls
    # find the pool full. They are counted, not queued to go out late.
    monkeypatch.setattr(db_throttling, "_MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(DbThrottle, "_read", lambda self, client, request, phase="steady": time.sleep(0.3))
    _make_products_table(aws, rcu=10, items=5)
    injector = _injector(aws)
    injector.start()
    time.sleep(1.0)
    injector.stop()
    assert injector._calls_dropped >= 2


def test_stop_before_start_is_safe(aws):
    injector = _injector(aws)
    assert injector.stop() is None
    assert injector.stop() is None
    observed = injector.observe()
    assert observed["steady_reads_per_s"] is None and observed["surge_s"] is None


def test_stop_twice_after_start_is_safe(aws, no_surge):
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
    assert _counts(injector) == (5, 0, 0)
    assert injector._first_throttle_ms is None


def test_unprocessed_keys_are_throttled_reads(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    partial = {"Responses": {}, "UnprocessedKeys": {
        PRODUCTS_TABLE: {"Keys": [{"id": "prod-001"}, {"id": "prod-002"}], "ConsistentRead": True}}}
    before = int(time.time() * 1000)
    injector._read(_FakeClient(partial), _request(5), "surge")
    assert _counts(injector, "surge") == (5, 2, 0)
    assert injector._first_throttle_ms >= before


def test_a_refused_batch_throttles_every_key(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    injector._read(_FakeClient(_client_error("ProvisionedThroughputExceededException")), _request(5))
    assert _counts(injector) == (5, 5, 0)


def test_other_failures_are_errors_not_throttles(aws):
    injector = _injector(aws)
    injector._table = PRODUCTS_TABLE
    injector._read(_FakeClient(_client_error("ValidationException")), _request(5))
    injector._read(_FakeClient(EndpointConnectionError(endpoint_url="https://x")), _request(5))
    assert _counts(injector) == (10, 0, 10)
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

def test_observe_reports_the_steady_phase_against_its_target(aws):
    injector = _injector(aws)
    injector._table, injector._provisioned_rcu, injector._target_reads_per_s = None, 10, 20.0
    injector._counts["surge"].update(attempted=300, throttled=50, errors=0)
    injector._counts["steady"].update(attempted=190, throttled=95, errors=5)
    injector._first_throttle_ms = injector.activation.active_from_ms + 3_000
    injector._bank_spent_ms = injector.activation.active_from_ms + 30_000
    injector._started_s, injector._surge_ended_s, injector._stopped_s = 1000.0, 1030.0, 1040.0
    injector._surge_end = "throttled"

    observed = injector.observe()

    assert observed["surge_s"] == 30.0
    assert observed["steady_reads_per_s"] == 19.0
    assert observed["achieved_fraction"] == 0.95
    assert observed["steady_throttle_rate"] == 0.5
    assert observed["reads_attempted"] == 490 and observed["read_errors"] == 5
    assert observed["first_throttle_at"] == manifest.to_iso(injector._first_throttle_ms)
    assert observed["bank_spent_at"] == manifest.to_iso(injector._bank_spent_ms)
    # Labeling starts 5 s after the bank was spent, not after the first throttle.
    assert observed["label_from"] == manifest.to_iso(injector._bank_spent_ms + 5_000)
    assert "cw_throttled_requests" not in observed     # no table, no CloudWatch


def test_observe_sums_cloudwatch_throttles_and_the_apps_own_reads(aws):
    _make_products_table(aws, rcu=10)
    # A window starting on a minute boundary, so the minutes before it are whole.
    start_ms = (int(time.time()) // 60 - 5) * 60_000
    injector = _injector(aws, window_ms=180_000, active_from_ms=start_ms)
    injector._table, injector._provisioned_rcu, injector._target_reads_per_s = PRODUCTS_TABLE, 10, 20.0
    injector._started_s, injector._surge_ended_s, injector._stopped_s = 0.0, 10.0, 110.0
    injector._counts["steady"]["attempted"] = 2000          # 20 reads/s for 100 s

    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    cloudwatch = aws.client("cloudwatch")
    table = {"Name": "TableName", "Value": PRODUCTS_TABLE}

    def operation(name):
        return [table, {"Name": "Operation", "Value": name}]

    for minute in range(1, 4):      # the app's own reads before the window: 3 RCU/s
        cloudwatch.put_metric_data(Namespace="AWS/DynamoDB", MetricData=[
            {"MetricName": "ConsumedReadCapacityUnits", "Timestamp": start - timedelta(minutes=minute),
             "Value": 180, "Dimensions": [table]}])
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
    assert observed["app_reads_per_s"] == 3.0
    assert observed["offered_ratio"] == 2.3     # (20 + 3) / 10


def test_observe_is_plain_json_for_the_manifest(aws, no_surge):
    _make_products_table(aws, rcu=10)
    injector = _injector(aws)
    injector.start()
    time.sleep(0.3)
    injector.stop()
    observed = injector.observe()
    assert json.loads(json.dumps(observed)) == observed
    assert observed["cw_throttled_requests"] == 0
    assert observed["cw_throttled_requests_by_operation"] == {}
