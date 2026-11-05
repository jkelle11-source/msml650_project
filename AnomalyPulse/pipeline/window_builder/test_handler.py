"""Local unit tests for the window builder (M8-1). No live AWS needed: S3 and the
bus are faked in memory, or stubbed with botocore's Stubber, which checks each
request against the API model. The environment variables are set before import,
so the handler can read them when the module loads.

The expected health numbers are worked out by hand, and checked against the
models' own request features. Each window is checked against build_matrix run
over a whole stream: building minute W from minutes W-2..W must give the same
row, as it did on all 1,540 validation windows (2026-11-04). What a run builds
is worked out by hand from what the bucket holds and the time."""
import io
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import boto3
import pytest
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # AnomalyPulse/, for features
from features.build_matrix import build_matrix, columns  # noqa: E402
from features.request_features import SERVICES  # noqa: E402
from features.windows import request_window  # noqa: E402

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("BUS_NAME", "test-bus")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
import handler  # noqa: E402  (must follow the env vars above)


def _module(name, path):
    """Another Lambda's handler.py, loaded under its own name: the files the window builder reads
    are laid out by the Aggregator's and the metrics extractor's own code, never by the builder's."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregator = _module("aggregator_handler", "pipeline/aggregator/handler.py")
extractor = _module("metrics_extractor_handler", "infra/shared/metrics_extractor/handler.py")


def _extractor_key(service, stamp):
    """The key the metrics extractor writes a snapshot stamped `stamp` under."""
    with mock.patch.object(extractor.s3, "put_object"):
        return extractor.write_metrics_to_s3("test-bucket", service, {}, now=stamp)

W = "2026-11-04T07:04:00.000Z"
FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _rec(service, request_id, latency_ms, status_code, timestamp="2026-11-04T07:04:10.000Z"):
    """A record with the telemetry contract's 14 fields."""
    is_order = service == "order-service"
    return {"timestamp": timestamp, "request_id": request_id, "service": service,
            "endpoint": "/x", "http_method": "GET", "status_code": status_code,
            "latency_ms": latency_ms, "lambda_duration_ms": latency_ms * 0.9, "cold_start": False,
            "db_latency_ms": None if service == "payment-service" else 2.0,
            "db_consumed_capacity": None if service == "payment-service" else 1.0,
            "dependency": "payment-service" if is_order else None,
            "dependency_latency_ms": latency_ms * 0.5 if is_order else None,
            "dependency_error": False if is_order else None}


def _ts(minute, second):
    t = datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc) + timedelta(minutes=minute, seconds=second)
    return t.strftime(FMT)[:-4] + "Z"  # milliseconds, as the services print them


def _snapshot(service, minute):
    """The metrics extractor's snapshot for window 07:<minute>: stamped at the minute's end."""
    metrics = {"lambda": {"throttle_count": 0.0}}
    if service != "payment-service":
        metrics["dynamodb"] = {"consumed_read_capacity": 1.0 + minute, "consumed_write_capacity": 0.5,
                               "throttled_requests": 0, "read_throttle_events": 0, "write_throttle_events": 0}
    return {"timestamp": _ts(minute + 1, 0), "service": service, "metrics": metrics}


def _stream(minutes=8, idle=(5,)):
    """Records and snapshots for 07:00 .. 07:<minutes-1>. Every service has traffic every minute except
    the idle ones, which keep their snapshots, as the extractor writes one every minute."""
    records, snapshots = [], []
    for m in range(minutes):
        for svc in SERVICES:
            snapshots.append(_snapshot(svc, m))
            if m in idle:
                continue
            for j in range(4 + m % 3):
                status = 503 if (m == 3 and j == 0) else 200
                rid = f"{m}-{j}"  # order and payment share it: a checkout's id is passed on
                records.append(_rec(svc, rid, 10.0 + 5 * m + 3 * j, status, _ts(m, 5 + 10 * j)))
    return records, snapshots


def _minute(ts):
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


def _slice(records, snapshots, window_start):
    """The records and snapshots of W-2..W, as the handler will read them."""
    hi = _minute(window_start)
    lo = hi - timedelta(minutes=2)
    recs = [r for r in records if lo <= _minute(r["timestamp"]).replace(second=0, microsecond=0) <= hi]
    snaps = [s for s in snapshots if lo <= _minute(s["timestamp"]) - timedelta(minutes=1) <= hi]
    return recs, snaps


# The hand-checkable minute (the design's case): p1 arrives twice, p9 belongs to 07:05.
HAND = [
    _rec("product-service", "p1", 10, 200), _rec("product-service", "p2", 20, 200),
    _rec("product-service", "p3", 30, 503), _rec("product-service", "p1", 10, 200),
    _rec("cart-service", "c1", 40, 200),
    _rec("payment-service", "y1", 100, 200), _rec("payment-service", "y2", 300, 500),
    _rec("product-service", "p9", 99, 200, timestamp="2026-11-04T07:05:00.000Z"),
]


# ---- dedupe ---------------------------------------------------------------------

def test_dedupe_keeps_the_first_of_each_service_and_request_id():
    recs = [{"service": "order-service", "request_id": "r1", "latency_ms": 212.4},
            {"service": "payment-service", "request_id": "r1", "latency_ms": 180.1},  # same id, other service
            {"service": "order-service", "request_id": "r1", "latency_ms": 999.0},    # a repeat
            {"service": "cart-service", "request_id": "r2", "latency_ms": 38.0}]
    out = handler.dedupe(recs)
    assert out == [recs[0], recs[1], recs[3]]
    assert out[0] is recs[0]  # the first copy wins


def test_dedupe_of_nothing_is_nothing():
    assert handler.dedupe([]) == []


def test_dedupe_leaves_its_input_alone():
    recs = [_rec("cart-service", "c1", 40, 200), _rec("cart-service", "c1", 40, 200)]
    handler.dedupe(recs)
    assert len(recs) == 2


# ---- health ----------------------------------------------------------------------
# health() returns the dashboard builder's input (dashboard/builder.py): the window's name, the
# system p95, and 3 of the features' columns for each service. The builder works out the system
# requests and 5xx share from the service columns itself.

dashboard = _module("dashboard_builder", "dashboard/builder.py")

HEALTH_KEYS = {"window_start", "system_p95_latency_ms"} | {
    f"{p}_{c}" for p in ("product", "cart", "order", "payment")
    for c in ("request_rate", "error_rate_5xx", "p95_latency_ms")}


def test_health_of_the_hand_case():
    h = handler.health(HAND, W)

    assert set(h) == HEALTH_KEYS  # only what the dashboard reads: 14 keys
    assert h["window_start"] == W
    # Product p1, p2, p3 (p1's repeat dropped, p9 is 07:05's). Sorted [10, 20, 30]: p95 = 20 + 0.9 * 10.
    assert h["product_request_rate"] == pytest.approx(3 / 60)
    assert h["product_error_rate_5xx"] == pytest.approx(1 / 3)
    assert h["product_p95_latency_ms"] == pytest.approx(29.0)
    assert h["cart_request_rate"] == pytest.approx(1 / 60)
    assert h["cart_error_rate_5xx"] == pytest.approx(0.0)
    assert h["cart_p95_latency_ms"] == pytest.approx(40.0)
    # Order had no requests: what request_features gives a quiet service.
    assert (h["order_request_rate"], h["order_error_rate_5xx"], h["order_p95_latency_ms"]) == (0.0, None, None)
    # Payment [100, 300]: p95 = 100 + 0.95 * 200.
    assert h["payment_request_rate"] == pytest.approx(2 / 60)
    assert h["payment_error_rate_5xx"] == pytest.approx(0.5)
    assert h["payment_p95_latency_ms"] == pytest.approx(290.0)
    # Product, Cart and Order's records, sorted [10, 20, 30, 40]: p95 = 30 + 0.85 * 10.
    assert h["system_p95_latency_ms"] == pytest.approx(38.5)


def test_the_system_p95_comes_from_the_records_not_from_the_services_p95s():
    # Percentiles don't combine (Docs/DASHBOARD_CONTRACT.md): from Product's 29.0 and Cart's 40.0,
    # their p95 is 39.45, their max 40.0 and their mean 34.5; the records' own p95 is 38.5.
    assert handler.health(HAND, W)["system_p95_latency_ms"] == pytest.approx(38.5)


def test_the_dashboard_reads_the_hand_case_as_the_contract_says():
    # The numbers the old health file held, now worked out by the dashboard's own builder.
    now = datetime(2026, 11, 4, 7, 5, 10, tzinfo=timezone.utc)
    shown = dashboard.build_dashboard(handler.health(HAND, W), None, None, now)["health"]

    assert shown == {
        "window_start": W, "status": "HEALTHY", "active_incidents": 0,
        "requests_per_min": 4, "error_rate": pytest.approx(0.25), "p95_latency_ms": pytest.approx(38.5),
        "services": [
            {"service": "product-service", "status": "HEALTHY", "requests_per_min": 3,
             "error_rate": pytest.approx(1 / 3), "p95_latency_ms": pytest.approx(29.0)},
            {"service": "cart-service", "status": "HEALTHY", "requests_per_min": 1,
             "error_rate": pytest.approx(0.0), "p95_latency_ms": pytest.approx(40.0)},
            {"service": "order-service", "status": "IDLE", "requests_per_min": 0,
             "error_rate": None, "p95_latency_ms": None},
            {"service": "payment-service", "status": "HEALTHY", "requests_per_min": 2,
             "error_rate": pytest.approx(0.5), "p95_latency_ms": pytest.approx(290.0)}]}


def test_the_dashboard_counts_a_busy_minute_exactly():
    # 23:36 on 2026-11-04: Product 874, Cart 145, Order 180, Payment 82. Each rate is n / 60, and the
    # builder's round(rate * 60) must give n back: the system count is 874 + 145 + 180 = 1,199.
    counts = {"product-service": 874, "cart-service": 145, "order-service": 180, "payment-service": 82}
    recs = [_rec(s, f"{s}-{i}", 20.0, 200, _ts(4, i % 60)) for s, n in counts.items() for i in range(n)]
    now = datetime(2026, 11, 4, 7, 5, 10, tzinfo=timezone.utc)
    shown = dashboard.build_dashboard(handler.health(recs, W), None, None, now)["health"]

    assert shown["requests_per_min"] == 1199
    assert [s["requests_per_min"] for s in shown["services"]] == [874, 145, 180, 82]
    assert all(type(s["requests_per_min"]) is int for s in shown["services"])


def test_health_of_an_idle_minute():
    # request_features has no row for a minute with no records; health still gives every column.
    h = handler.health([], W)
    assert h == {"window_start": W, "system_p95_latency_ms": None,
                 **{f"{p}_request_rate": 0.0 for p in ("product", "cart", "order", "payment")},
                 **{f"{p}_error_rate_5xx": None for p in ("product", "cart", "order", "payment")},
                 **{f"{p}_p95_latency_ms": None for p in ("product", "cart", "order", "payment")}}


def test_the_dashboard_shows_an_idle_minute_as_idle_not_as_no_data():
    now = datetime(2026, 11, 4, 7, 5, 10, tzinfo=timezone.utc)
    shown = dashboard.build_dashboard(handler.health([], W), None, None, now)["health"]
    assert (shown["status"], shown["requests_per_min"], shown["error_rate"], shown["p95_latency_ms"]) == \
        ("HEALTHY", 0, None, None)
    assert [s["status"] for s in shown["services"]] == ["IDLE"] * 4


def test_payment_is_left_out_of_the_system_p95():
    # Payment is reached only through Order: its latency is already inside Order's.
    h = handler.health([_rec("payment-service", "y1", 100, 500)], W)
    assert h["system_p95_latency_ms"] is None
    assert h["payment_request_rate"] == pytest.approx(1 / 60)
    assert h["payment_error_rate_5xx"] == pytest.approx(1.0)


@pytest.mark.parametrize("statuses, expected", [
    ([200, 201, 302, 404, 499], 0.0),
    ([500, 200, 200, 200], 0.25),
    ([599, 503, 200, 404], 0.5),
    ([500, 599], 1.0),
])
def test_error_rate_is_the_share_of_5xx(statuses, expected):
    recs = [_rec("cart-service", f"c{i}", 10, s) for i, s in enumerate(statuses)]
    assert handler.health(recs, W)["cart_error_rate_5xx"] == pytest.approx(expected)


def test_health_counts_only_its_own_minute():
    recs = [_rec("cart-service", "a", 10, 200, "2026-11-04T07:03:59.999Z"),   # 07:03
            _rec("cart-service", "b", 20, 200, "2026-11-04T07:04:00.000Z"),   # first instant of 07:04
            _rec("cart-service", "c", 30, 200, "2026-11-04T07:04:59.999Z"),   # last instant of 07:04
            _rec("cart-service", "d", 40, 200, "2026-11-04T07:05:00.000Z")]   # 07:05
    h = handler.health(recs, W)
    assert h["cart_request_rate"] == pytest.approx(2 / 60)
    assert h["cart_p95_latency_ms"] == pytest.approx(29.5)  # [20, 30]: 20 + 0.95 * 10


def test_health_of_a_minute_whose_records_are_all_another_minutes():
    # Records were read, but none is this minute's: as idle, never an error.
    h = handler.health([_rec("cart-service", "d", 40, 200, "2026-11-04T07:05:00.000Z")], W)
    assert h == handler.health([], W)


def test_health_drops_repeats_itself():
    once = handler.health(HAND, W)
    twice = handler.health(HAND + HAND, W)
    assert twice == once


def test_health_columns_are_the_full_windows_own():
    # The dashboard and the models describe a busy minute with the same numbers: health's columns
    # equal the 106-column window's, built over the whole stream.
    records, snapshots = _stream()
    matrix = build_matrix(records, snapshots)
    for m in (0, 1, 3, 4, 6, 7):
        w = _ts(m, 0)
        h = handler.health(records, w)
        for col in HEALTH_KEYS - {"window_start", "system_p95_latency_ms"}:
            assert h[col] == matrix[w][col], (w, col)


def test_an_idle_minutes_request_rate_is_0_where_the_full_window_has_none():
    # The one difference: the window leaves an idle minute's request columns empty, and the
    # dashboard's builder would fail on round(None * 60). Health says 0 requests.
    records, snapshots = _stream(idle=(5,))
    w = _ts(5, 0)
    assert build_matrix(records, snapshots)[w]["cart_request_rate"] is None
    assert handler.health(records, w)["cart_request_rate"] == 0.0


def test_health_is_json():
    h = handler.health(HAND, W)
    assert json.loads(json.dumps(h)) == h  # it is written to S3 as JSON


# ---- build_window ----------------------------------------------------------------

def test_each_window_from_three_minutes_matches_the_whole_stream():
    records, snapshots = _stream()
    whole = build_matrix(records, snapshots)
    for w in whole:
        recs, snaps = _slice(records, snapshots, w)
        assert handler.build_window(recs, snaps, w) == {"window_start": w, **whole[w]}, w


def test_a_window_holds_window_start_and_every_column():
    records, snapshots = _stream()
    window = handler.build_window(*_slice(records, snapshots, _ts(4, 0)), _ts(4, 0))
    assert set(window) == {"window_start", *columns()}
    assert len(window) == 107


def test_repeated_records_do_not_change_a_window():
    records, snapshots = _stream()
    w = _ts(4, 0)
    recs, snaps = _slice(records, snapshots, w)
    assert handler.build_window(recs + recs[:20], snaps, w) == handler.build_window(recs, snaps, w)


def test_no_window_when_its_minute_has_no_records_and_no_snapshot():
    assert handler.build_window([], [], W) is None
    # Earlier minutes alone don't make one: 07:03's records, nothing for 07:04.
    earlier = [_rec("product-service", "x", 10, 200, "2026-11-04T07:03:10.000Z")]
    assert handler.build_window(earlier, [_snapshot("product-service", 3)], W) is None


def test_an_idle_minute_with_its_snapshot_is_a_window_with_empty_request_columns():
    records, snapshots = _stream(idle=(5,))
    window = handler.build_window(*_slice(records, snapshots, _ts(5, 0)), _ts(5, 0))
    assert window is not None
    assert window["product_request_rate"] is None
    assert window["product_db_consumed_read_capacity"] == 6.0  # from its snapshot


# ---- is_complete -----------------------------------------------------------------

@pytest.mark.parametrize("window, expected", [
    ({"window_start": W, "a": 0.0, "b": 1.5}, True),
    ({"window_start": W, "a": 0.0, "b": None}, False),
    ({"window_start": W, "a": False, "b": 0}, True),     # zero and False are values, not empty
    ({"window_start": None, "a": 1.0}, True),            # window_start is a name, not a column
])
def test_is_complete(window, expected):
    assert handler.is_complete(window) is expected


def test_which_minutes_of_a_stream_are_complete():
    # 07:00 has no history; 07:05 is idle; 07:06's _change compares with the idle minute;
    # by 07:07 the history is whole again.
    records, snapshots = _stream(minutes=8, idle=(5,))
    complete = {}
    for m in range(8):
        w = _ts(m, 0)
        complete[m] = handler.is_complete(handler.build_window(*_slice(records, snapshots, w), w))
    assert complete == {0: False, 1: True, 2: True, 3: True, 4: True, 5: False, 6: False, 7: True}


# ---- keys: where the other Lambdas put things -------------------------------------

KEY_MINUTES = [datetime(2026, 11, 4, 7, 4, tzinfo=timezone.utc), datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc),
               datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc), datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
               datetime(2026, 11, 4, 9, 5, tzinfo=timezone.utc)]


@pytest.mark.parametrize("w", KEY_MINUTES)
def test_minute_name_is_the_window_name_of_the_minutes_records(w):
    name = handler.minute_name(w)
    assert name == request_window(name[:17] + "00.000Z") == request_window(name[:17] + "59.999Z")
    assert name.endswith(":00.000Z")


@pytest.mark.parametrize("w", KEY_MINUTES)
def test_records_prefix_is_where_the_aggregator_files_the_minute(w):
    assert handler.records_prefix(w) == aggregator.minute_prefix(handler.minute_name(w)[:17] + "31.207Z")


@pytest.mark.parametrize("w", KEY_MINUTES)
def test_snapshot_key_is_where_the_extractor_writes_the_minutes_snapshot(w):
    # Stamped with the window's end: the window starting at w ends at w + 1 minute.
    assert handler.snapshot_key("order-service", w) == _extractor_key("order-service", w + timedelta(minutes=1))


def test_the_new_keys_by_hand():
    w = datetime(2026, 11, 4, 23, 59, tzinfo=timezone.utc)
    assert handler.health_key(w) == "pipeline/health/dt=2026-11-04/minute=23-59.json"
    assert handler.window_key(w) == "pipeline/windows/dt=2026-11-04/minute=23-59.json"
    assert handler.snapshot_key("cart-service", w) == \
        "metrics/service=cart-service/dt=2026-11-05/metrics-20261105T000000000000Z.json"


# ---- run: one pass over the bucket -----------------------------------------------

UTC = timezone.utc


def _at(hour, minute, second=0, day=4):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


class FakeS3:
    """The S3 calls the handler makes, over a dict of key -> body. Missing keys fail as S3
    fails them (HeadObject 404, GetObject NoSuchKey). `fail` maps (operation, key) to an
    error code raised instead. Every call, and every event put, goes into `calls` in order.
    GetObject can be called from several threads at once: `delay` maps a key to the seconds
    its read takes, and `most_in_flight` is the most reads that were ever under way together."""

    def __init__(self, page_size=1000):
        self.objects, self.calls, self.fail, self.page_size = {}, [], {}, page_size
        self.delay, self.in_flight, self.most_in_flight = {}, 0, 0
        self._lock = threading.Lock()

    def _call(self, op, key):
        self.calls.append((op, key))
        if (op, key) in self.fail:
            raise ClientError({"Error": {"Code": self.fail[(op, key)], "Message": "test"}}, op)

    def head_object(self, Bucket, Key):
        assert Bucket == "test-bucket"
        self._call("head", Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {}

    def get_object(self, Bucket, Key):
        assert Bucket == "test-bucket"
        with self._lock:
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            time.sleep(self.delay.get(Key, 0))
            self._call("get", Key)
            if Key not in self.objects:
                raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
            return {"Body": io.BytesIO(self.objects[Key].encode())}
        finally:
            with self._lock:
                self.in_flight -= 1

    def put_object(self, Bucket, Key, Body):
        assert Bucket == "test-bucket"
        self._call("put", Key)
        self.objects[Key] = Body
        return {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                assert Bucket == "test-bucket"
                fake.calls.append(("list", Prefix))
                keys = sorted(k for k in fake.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), fake.page_size):
                    chunk = keys[i:i + fake.page_size]
                    yield {"KeyCount": len(chunk), **({"Contents": [{"Key": k} for k in chunk]} if chunk else {})}
        return Paginator()

    def puts(self):
        return [k for op, k in self.calls if op == "put"]


class FakeEvents:
    """put_events, logging each entry into the shared call log. `failed` entries of every call
    come back with an ErrorCode, as PutEvents reports a partial failure."""

    def __init__(self, calls):
        self.calls, self.entries, self.failed = calls, [], 0

    def put_events(self, Entries):
        self.entries += Entries
        self.calls += [("event", json.loads(e["Detail"])["key"]) for e in Entries]
        results = [{"EventId": "e"} for _ in Entries]
        for r in results[:self.failed]:
            r.clear()
            r.update(ErrorCode="InternalFailure", ErrorMessage="test")
        return {"FailedEntryCount": min(self.failed, len(Entries)), "Entries": results}


class FakeTable:
    """put_item on the Incidents table, logging ("item", window_start) into the shared call log.
    It refuses what DynamoDB refuses: values boto3 can't serialize (floats), and a write whose
    condition doesn't hold on the stored item, which raises ConditionalCheckFailedException.
    `fail` is an error code every write raises instead. `writes` counts accepted writes."""

    def __init__(self, calls):
        self.calls, self.items, self.writes, self.fail = calls, {}, 0, None

    @staticmethod
    def _holds(cond, stored):
        """The condition, as DynamoDB evaluates it: a comparison with a missing attribute is false."""
        kind, values = type(cond).__name__, cond.get_expression()["values"]
        if kind == "Or":
            return FakeTable._holds(values[0], stored) or FakeTable._holds(values[1], stored)
        if kind == "And":
            return FakeTable._holds(values[0], stored) and FakeTable._holds(values[1], stored)
        if kind == "Not":
            return not FakeTable._holds(values[0], stored)
        name = values[0].name
        if kind == "AttributeNotExists":
            return stored is None or name not in stored
        if kind == "AttributeExists":
            return stored is not None and name in stored
        if stored is None or name not in stored:
            return False
        compare = {"Equals": "__eq__", "NotEquals": "__ne__", "LessThan": "__lt__",
                   "LessThanEquals": "__le__", "GreaterThan": "__gt__", "GreaterThanEquals": "__ge__"}[kind]
        return getattr(stored[name], compare)(values[1])

    def put_item(self, Item, ConditionExpression=None):
        serialize = TypeSerializer().serialize
        for value in Item.values():
            serialize(value)  # floats raise TypeError here, as in boto3
        key = (Item["pk"], Item["sk"])
        self.calls.append(("item", Item.get("window_start")))
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail, "Message": "test"}}, "PutItem")
        if ConditionExpression is not None and not self._holds(ConditionExpression, self.items.get(key)):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException",
                                         "Message": "The conditional request failed"}}, "PutItem")
        self.items[key] = dict(Item)
        self.writes += 1
        return {}


@pytest.fixture
def bucket():
    s3 = FakeS3()
    ev = FakeEvents(s3.calls)
    s3.table = FakeTable(s3.calls)
    with mock.patch.object(handler, "s3", s3), mock.patch.object(handler, "events", ev), \
            mock.patch.object(handler, "table", s3.table):
        yield s3, ev


def _load(s3, records, snapshots, snapshots_until):
    """The bucket as the Aggregator and the metrics extractor leave it: each minute's records
    split over two files, and the snapshots stamped up to snapshots_until."""
    by_minute = {}
    for r in records:
        by_minute.setdefault(_minute(r["timestamp"]).replace(second=0, microsecond=0), []).append(r)
    for w, recs in by_minute.items():
        half = len(recs) // 2
        for part, chunk in (("a", recs[:half]), ("b", recs[half:])):
            if chunk:
                prefix = aggregator.minute_prefix(recs[0]["timestamp"])
                s3.objects[prefix + f"part-{part}.jsonl"] = "\n".join(json.dumps(r) for r in chunk)
    for snap in snapshots:
        stamp = _minute(snap["timestamp"])
        if stamp <= snapshots_until:
            s3.objects[_extractor_key(snap["service"], stamp)] = json.dumps(snap)


def _built_already(s3, first, last):
    for m in range(int((last - first).total_seconds() // 60) + 1):
        s3.objects[handler.window_key(first + timedelta(minutes=m))] = "{}"


def _shift(records, snapshots, delta):
    def move(ts):
        return (_minute(ts) + delta).strftime(FMT)[:-4] + "Z"
    return ([dict(r, timestamp=move(r["timestamp"])) for r in records],
            [dict(s, timestamp=move(s["timestamp"])) for s in snapshots])


def _names(*minutes, hour=7, day=4):
    return [handler.minute_name(_at(hour, m, day=day)) for m in minutes]


def _hand_bucket(s3, idle=()):
    """The design's case at 07:08:10: records for 07:00-07:07, windows 06:58-07:03 built, and
    snapshots stamped up to 07:05:00 (windows up to 07:04), written by the extractor at 07:07:30."""
    records, snapshots = _stream(minutes=8, idle=idle)
    _load(s3, records, snapshots, snapshots_until=_at(7, 5))
    _built_already(s3, _at(6, 58), _at(7, 3))
    return records, snapshots


def _row(records, snapshots, w):
    name = handler.minute_name(w)
    return {"window_start": name, **build_matrix(records, snapshots)[name]}


def test_the_hand_case_builds_three_healths_and_window_07_04(bucket):
    s3, ev = bucket
    records, snapshots = _hand_bucket(s3)

    result = handler.run(_at(7, 8, 10))

    assert result == {"health": _names(7, 6, 5), "windows": _names(4), "announced": _names(4)}
    assert s3.puts() == [handler.health_key(_at(7, 7)), handler.health_key(_at(7, 6)),
                         handler.health_key(_at(7, 5)), handler.window_key(_at(7, 4))]
    for m in (5, 6, 7):
        assert json.loads(s3.objects[handler.health_key(_at(7, m))]) == handler.health(records, _names(m)[0])
    assert json.loads(s3.objects[handler.window_key(_at(7, 4))]) == _row(records, snapshots, _at(7, 4))


def test_the_announcement_holds_the_window_in_the_diagnosis_lambdas_input_shape(bucket):
    s3, ev = bucket
    records, snapshots = _hand_bucket(s3)
    handler.run(_at(7, 8, 10))

    assert len(ev.entries) == 1
    entry = ev.entries[0]
    assert {k: entry[k] for k in ("EventBusName", "Source", "DetailType")} == {
        "EventBusName": "test-bus", "Source": "anomalypulse.pipeline", "DetailType": "WindowReady"}
    assert json.loads(entry["Detail"]) == {"windows": [_row(records, snapshots, _at(7, 4))],
                                           "key": handler.window_key(_at(7, 4))}


def test_the_window_is_announced_before_its_file_is_written(bucket):
    # The file marks it done: written first, a failed announcement would never be retried.
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    key = handler.window_key(_at(7, 4))
    assert [op for op, k in s3.calls if k == key] == ["head", "event", "put"]


def test_a_second_run_rebuilds_health_but_builds_and_announces_nothing_again(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    again = handler.run(_at(7, 8, 40))

    assert again == {"health": _names(7, 6, 5), "windows": [], "announced": []}
    assert len(ev.entries) == 1


def test_the_newest_health_is_written_before_the_older_minutes_are_read(bucket):
    # The dashboard shows the newest, and it is due about 14 s into a run: it must not wait
    # for two more minutes of files.
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    before = s3.calls[:s3.calls.index(("put", handler.health_key(_at(7, 7))))]
    assert [p for op, p in before if op == "list"] == [handler.records_prefix(_at(7, 7))]


def test_the_minute_now_is_in_is_never_built(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 59))
    assert not [k for k in s3.puts() if "minute=07-08" in k]


def test_a_window_waits_until_all_four_snapshots_are_in(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    payment = _extractor_key("payment-service", _at(7, 5))  # window 07:04's, stamped at its end
    held = s3.objects.pop(payment)

    assert handler.run(_at(7, 8, 10))["windows"] == []
    assert ev.entries == []

    s3.objects[payment] = held
    assert handler.run(_at(7, 9, 10))["windows"] == _names(4)


def test_a_window_reads_its_minute_and_the_two_before_it(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    listed = {p for op, p in s3.calls if op == "list"}
    # Window 07:04 reads 07:02-07:04; health reads 07:05-07:07. Nothing else.
    assert listed == {handler.records_prefix(_at(7, m)) for m in range(2, 8)}


@pytest.mark.parametrize("idle", [3, 4])
def test_an_idle_minute_stays_in_the_history_and_its_window_is_written_not_announced(bucket, idle):
    # 07:03 idle: window 07:04's _change compares with it (empty), as over the whole stream; without
    # 07:03's snapshot it would compare with 07:02 instead. 07:04 idle: no request columns at all.
    s3, ev = bucket
    records, snapshots = _hand_bucket(s3, idle=(idle,))

    result = handler.run(_at(7, 8, 10))

    assert result["windows"] == _names(4) and result["announced"] == []
    assert json.loads(s3.objects[handler.window_key(_at(7, 4))]) == _row(records, snapshots, _at(7, 4))
    assert ev.entries == []


def test_across_midnight_a_window_finds_its_snapshot_under_the_next_day(bucket):
    s3, ev = bucket
    records, snapshots = _shift(*_stream(minutes=8, idle=()), _at(23, 56) - _at(7, 0))  # 23:56 .. 00:03
    _load(s3, records, snapshots, snapshots_until=_at(0, 0, day=5))                    # windows up to 23:59
    _built_already(s3, _at(23, 52), _at(23, 58))

    result = handler.run(_at(0, 2, 40, day=5))

    assert result == {"health": _names(1, 0, hour=0, day=5) + _names(59, hour=23),
                      "windows": _names(59, hour=23), "announced": _names(59, hour=23)}
    assert handler.health_key(_at(23, 59)) == "pipeline/health/dt=2026-11-04/minute=23-59.json"
    assert ("get", "metrics/service=product-service/dt=2026-11-05/metrics-20261105T000000000000Z.json") in s3.calls
    assert json.loads(s3.objects[handler.window_key(_at(23, 59))]) == _row(records, snapshots, _at(23, 59))


def test_record_files_are_read_across_pages(bucket):
    s3, ev = bucket
    s3.page_size = 1  # every file on a page of its own
    records, snapshots = _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    assert json.loads(s3.objects[handler.window_key(_at(7, 4))]) == _row(records, snapshots, _at(7, 4))
    assert json.loads(s3.objects[handler.health_key(_at(7, 7))]) == handler.health(records, _names(7)[0])


# ---- reading a minute's files at once ---------------------------------------------

def test_a_minutes_files_are_read_readers_at_a_time(bucket):
    # 20 files that each take 0.1 s: one at a time is 2 s; READERS at once, two rounds.
    s3, ev = bucket
    prefix = handler.records_prefix(_at(7, 4))
    for i in range(20):
        key = prefix + f"part-{i:02d}.jsonl"
        s3.objects[key] = json.dumps(_rec("cart-service", f"r{i}", 30.0, 200))
        s3.delay[key] = 0.1

    records = handler._read_records(prefix)

    assert [r["request_id"] for r in records] == [f"r{i}" for i in range(20)]
    assert s3.most_in_flight == handler.READERS == 16


def test_files_come_back_in_key_order_whichever_finishes_first(bucket):
    # part-a is the slowest, so it finishes last; its copy of r1 must still be the one kept.
    s3, ev = bucket
    prefix = handler.records_prefix(_at(7, 4))
    first = _rec("order-service", "r1", 212.4, 200)
    s3.objects[prefix + "part-a.jsonl"] = json.dumps(first)
    s3.objects[prefix + "part-b.jsonl"] = json.dumps(_rec("cart-service", "r2", 30.0, 200))
    s3.objects[prefix + "part-c.jsonl"] = json.dumps(dict(first, latency_ms=999.0))  # a repeat
    s3.delay[prefix + "part-a.jsonl"] = 0.3

    records = handler._read_records(prefix)

    assert [(r["request_id"], r["latency_ms"]) for r in records] == [("r1", 212.4), ("r2", 30.0), ("r1", 999.0)]
    assert [r["latency_ms"] for r in handler.dedupe(records)] == [212.4, 30.0]


def test_a_record_file_that_fails_fails_its_minute_and_stops_nothing_else(bucket):
    # The read fails on another thread; it must still reach run(), never read as an empty file.
    s3, ev = bucket
    _hand_bucket(s3)
    s3.fail[("get", handler.records_prefix(_at(7, 7)) + "part-a.jsonl")] = "InternalError"

    with pytest.raises(RuntimeError, match=r"^1 failed: health 2026-11-04T07:07:00.000Z"):
        handler.run(_at(7, 8, 10))
    assert handler.health_key(_at(7, 7)) not in s3.objects
    assert handler.health_key(_at(7, 6)) in s3.objects and handler.health_key(_at(7, 5)) in s3.objects
    assert handler.window_key(_at(7, 4)) in s3.objects and len(ev.entries) == 1


def test_the_s3_client_has_a_connection_for_every_reader():
    # boto3 keeps 10 by default: readers past the 10th would wait for one.
    assert handler.s3.meta.config.max_pool_connections >= handler.READERS


# ---- run: the health item ---------------------------------------------------------

def _item(s3):
    return s3.table.items.get(("HEALTH", "LATEST"))


def test_the_newest_minutes_health_becomes_the_item(bucket):
    s3, ev = bucket
    records, _ = _hand_bucket(s3)
    handler.run(_at(7, 8, 10))

    item = _item(s3)
    assert set(s3.table.items) == {("HEALTH", "LATEST")}
    assert item["window_start"] == "2026-11-04T07:07:00.000Z"
    # 07:07 has 4 + 7 % 3 = 5 requests per service: 5 / 60, as a Decimal from the float's text.
    assert item["product_request_rate"] == Decimal("0.08333333333333333")
    # The same numbers as the minute's health file, every float stored as a Decimal.
    expected = handler.health(records, "2026-11-04T07:07:00.000Z")
    assert set(item) == {"pk", "sk"} | set(expected)
    for k, v in expected.items():
        assert type(item[k]) is not float
        assert (item[k] is None) if v is None else (float(item[k]) == v if isinstance(v, float) else item[k] == v)


def test_only_the_newest_minute_is_written_to_the_item(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    assert [w for op, w in s3.calls if op == "item"] == ["2026-11-04T07:07:00.000Z"]


def test_the_item_is_written_after_its_health_file_and_before_the_older_minutes_are_read(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    at = s3.calls.index(("item", "2026-11-04T07:07:00.000Z"))
    assert s3.calls.index(("put", handler.health_key(_at(7, 7)))) < at
    assert s3.calls.index(("list", handler.records_prefix(_at(7, 6)))) > at


def test_the_item_moves_forward_with_each_run(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    handler.run(_at(7, 9, 10))
    assert _item(s3)["window_start"] == "2026-11-04T07:08:00.000Z"
    assert s3.table.writes == 2


def test_an_older_minute_never_replaces_a_newer_one_and_is_not_an_error(bucket):
    # A run that is behind (a replay, a manual invoke) must not take the dashboard back in time.
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 9, 10))
    result = handler.run(_at(7, 6, 10))  # raises nothing: a refused write is not a failure

    assert _item(s3)["window_start"] == "2026-11-04T07:08:00.000Z"
    assert s3.table.writes == 1
    assert result["health"] == _names(5, 4, 3)


def test_the_same_minute_can_be_written_again(bucket):
    # <=, not <: a rerun of the newest minute refreshes it, records that came late included.
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    handler.run(_at(7, 8, 40))
    assert s3.table.writes == 2


@pytest.mark.parametrize("code", ["AccessDeniedException", "ProvisionedThroughputExceededException"])
def test_any_other_item_failure_fails_the_newest_health_and_stops_nothing_else(bucket, code):
    s3, ev = bucket
    _hand_bucket(s3)
    s3.table.fail = code

    with pytest.raises(RuntimeError, match=rf"^1 failed: health 2026-11-04T07:07:00.000Z: .*{code}"):
        handler.run(_at(7, 8, 10))
    assert all(handler.health_key(_at(7, m)) in s3.objects for m in (5, 6, 7))
    assert handler.window_key(_at(7, 4)) in s3.objects and len(ev.entries) == 1


# ---- run: failures ----------------------------------------------------------------

@pytest.mark.parametrize("op, code", [("head", "403"), ("head", "AccessDenied"), ("get", "AccessDenied")])
def test_only_a_missing_key_reads_as_missing(bucket, op, code):
    # A 403 read as "no window yet" would rebuild and announce the same window every minute.
    s3, ev = bucket
    _hand_bucket(s3)
    key = handler.window_key(_at(7, 4)) if op == "head" else handler.snapshot_key("cart-service", _at(7, 4))
    s3.fail[(op, key)] = code

    with pytest.raises(RuntimeError, match="window 2026-11-04T07:04:00.000Z"):
        handler.run(_at(7, 8, 10))
    assert ev.entries == []
    assert handler.window_key(_at(7, 4)) not in s3.objects
    assert all(handler.health_key(_at(7, m)) in s3.objects for m in (5, 6, 7))  # the rest still ran


def test_a_failed_announcement_writes_no_file_and_the_next_run_tries_again(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    ev.failed = 1
    with pytest.raises(RuntimeError, match="WindowReady"):
        handler.run(_at(7, 8, 10))
    assert handler.window_key(_at(7, 4)) not in s3.objects

    ev.failed = 0
    assert handler.run(_at(7, 9, 10))["announced"] == _names(4)
    assert handler.window_key(_at(7, 4)) in s3.objects


def test_a_failed_window_write_is_announced_again_next_run(bucket):
    # The one duplicate this order allows; M8-2 tolerates repeats, as every hop delivers at least once.
    s3, ev = bucket
    _hand_bucket(s3)
    s3.fail[("put", handler.window_key(_at(7, 4)))] = "InternalError"
    with pytest.raises(RuntimeError, match="window 2026-11-04T07:04:00.000Z"):
        handler.run(_at(7, 8, 10))

    del s3.fail[("put", handler.window_key(_at(7, 4)))]
    assert handler.run(_at(7, 9, 10))["announced"] == _names(4)
    assert len(ev.entries) == 2
    assert handler.window_key(_at(7, 4)) in s3.objects


def test_a_failed_health_write_names_its_minute_and_stops_nothing_else(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    s3.fail[("put", handler.health_key(_at(7, 6)))] = "InternalError"

    with pytest.raises(RuntimeError, match=r"1 failed: health 2026-11-04T07:06:00.000Z"):
        handler.run(_at(7, 8, 10))
    assert handler.health_key(_at(7, 5)) in s3.objects and handler.health_key(_at(7, 7)) in s3.objects
    assert handler.window_key(_at(7, 4)) in s3.objects and len(ev.entries) == 1


def test_every_failure_is_reported_together(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    s3.fail[("put", handler.health_key(_at(7, 6)))] = "InternalError"
    s3.fail[("head", handler.window_key(_at(7, 4)))] = "403"

    with pytest.raises(RuntimeError) as err:
        handler.run(_at(7, 8, 10))
    message = str(err.value)
    assert message.startswith("2 failed:")
    assert "health 2026-11-04T07:06:00.000Z" in message and "window 2026-11-04T07:04:00.000Z" in message


# ---- the AWS requests themselves ---------------------------------------------------

def test_the_announcement_matches_the_putevents_api_model(bucket):
    s3, _ = bucket
    _hand_bucket(s3)
    client = boto3.client("events", region_name="us-east-2")
    stubber = Stubber(client)
    stubber.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e"}]}, {"Entries": ANY})
    with mock.patch.object(handler, "events", client), stubber:
        handler.run(_at(7, 8, 10))
    stubber.assert_no_pending_responses()


def test_the_item_matches_the_putitem_api_model():
    # The real boto3 table, its client stubbed. The stub sees the item before boto3 types it, so
    # the numbers are the hand case's as Decimals, each from its float's own text, and None stays
    # None; boto3 then types the request and checks it against the PutItem model.
    table = boto3.resource("dynamodb", region_name="us-east-2").Table("test-table")
    stubber = Stubber(table.meta.client)
    D = Decimal
    stubber.add_response("put_item", {}, {
        "TableName": "test-table",
        "Item": {"pk": "HEALTH", "sk": "LATEST", "window_start": W, "system_p95_latency_ms": D("38.5"),
                 "product_request_rate": D("0.05"), "product_error_rate_5xx": D("0.3333333333333333"),
                 "product_p95_latency_ms": D("29.0"),
                 "cart_request_rate": D("0.016666666666666666"), "cart_error_rate_5xx": D("0.0"),
                 "cart_p95_latency_ms": D("40.0"),
                 "order_request_rate": D("0.0"), "order_error_rate_5xx": None, "order_p95_latency_ms": None,
                 "payment_request_rate": D("0.03333333333333333"), "payment_error_rate_5xx": D("0.5"),
                 "payment_p95_latency_ms": D("290.0")},
        "ConditionExpression": ANY})
    with mock.patch.object(handler, "table", table), stubber:
        handler._put_health_report(handler.health(HAND, W))
    stubber.assert_no_pending_responses()


def test_the_items_condition_is_what_dynamodb_is_sent():
    # The condition object, as boto3 writes it into the request.
    from boto3.dynamodb.conditions import ConditionExpressionBuilder
    table = mock.Mock()
    with mock.patch.object(handler, "table", table):
        handler._put_health_report(handler.health(HAND, W))
    built = ConditionExpressionBuilder().build_expression(table.put_item.call_args.kwargs["ConditionExpression"])
    names, values = built.attribute_name_placeholders, built.attribute_value_placeholders
    expression = built.condition_expression
    for placeholder, name in names.items():
        expression = expression.replace(placeholder, name)
    for placeholder, value in values.items():
        expression = expression.replace(placeholder, repr(value))
    assert expression == f"(attribute_not_exists(window_start) OR window_start <= {W!r})"


def test_an_announcement_is_far_under_eventbridges_limit(bucket):
    s3, ev = bucket
    _hand_bucket(s3)
    handler.run(_at(7, 8, 10))
    assert len(ev.entries[0]["Detail"].encode("utf-8")) < 32 * 1024  # PutEvents allows 256 KB per call


def test_the_handler_runs_at_the_current_time_in_utc():
    with mock.patch.object(handler, "run", return_value={"health": []}) as run:
        before = datetime.now(UTC)
        assert handler.handler({}, None) == {"health": []}
        after = datetime.now(UTC)
    (now,), _ = run.call_args
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert before <= now <= after
