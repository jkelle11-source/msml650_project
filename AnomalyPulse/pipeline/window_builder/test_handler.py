"""Local unit tests for the window builder (M8-1). No live AWS needed. The
environment variables are set before import, so the handler can read them when
the module loads.

The expected health numbers are worked out by hand, and checked against the
models' own request features. Each window is checked against build_matrix run
over a whole stream: building minute W from minutes W-2..W must give the same
row, as it did on all 1,540 validation windows (2026-11-04)."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # AnomalyPulse/, for features
from features.build_matrix import build_matrix, columns  # noqa: E402
from features.request_features import SERVICES, request_features  # noqa: E402

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("BUS_NAME", "test-bus")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
import handler  # noqa: E402  (must follow the env vars above)

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

def test_health_of_the_hand_case():
    h = handler.health(HAND, W)

    assert set(h) == {"window_start", "requests_per_min", "error_rate", "p95_latency_ms", "services"}
    assert h["window_start"] == W
    # Product, Cart and Order: p1, p2, p3, c1. Sorted [10, 20, 30, 40]: p95 = 30 + 0.85 * 10.
    assert h["requests_per_min"] == 4
    assert h["error_rate"] == pytest.approx(0.25)
    assert h["p95_latency_ms"] == pytest.approx(38.5)
    assert [s["service"] for s in h["services"]] == list(SERVICES)
    by = {s["service"]: s for s in h["services"]}
    assert all(set(s) == {"service", "requests_per_min", "error_rate", "p95_latency_ms"} for s in h["services"])
    assert by["product-service"] == {"service": "product-service", "requests_per_min": 3,
                                     "error_rate": pytest.approx(1 / 3), "p95_latency_ms": pytest.approx(29.0)}
    assert by["cart-service"] == {"service": "cart-service", "requests_per_min": 1,
                                  "error_rate": pytest.approx(0.0), "p95_latency_ms": pytest.approx(40.0)}
    assert by["order-service"] == {"service": "order-service", "requests_per_min": 0,
                                   "error_rate": None, "p95_latency_ms": None}
    assert by["payment-service"] == {"service": "payment-service", "requests_per_min": 2,
                                     "error_rate": pytest.approx(0.5), "p95_latency_ms": pytest.approx(290.0)}


def test_health_counts_requests_as_whole_numbers():
    h = handler.health(HAND, W)
    assert type(h["requests_per_min"]) is int
    assert all(type(s["requests_per_min"]) is int for s in h["services"])


def test_health_of_an_idle_minute_lists_every_service_with_no_numbers():
    h = handler.health([], W)
    assert (h["requests_per_min"], h["error_rate"], h["p95_latency_ms"]) == (0, None, None)
    assert h["services"] == [{"service": s, "requests_per_min": 0, "error_rate": None, "p95_latency_ms": None}
                             for s in SERVICES]


def test_payment_is_left_out_of_the_system_numbers():
    # Payment is reached only through Order: counting both would count each checkout twice.
    h = handler.health([_rec("payment-service", "y1", 100, 500)], W)
    assert (h["requests_per_min"], h["error_rate"], h["p95_latency_ms"]) == (0, None, None)
    assert h["services"][3]["requests_per_min"] == 1


@pytest.mark.parametrize("statuses, expected", [
    ([200, 201, 302, 404, 499], 0.0),
    ([500, 200, 200, 200], 0.25),
    ([599, 503, 200, 404], 0.5),
    ([500, 599], 1.0),
])
def test_error_rate_is_the_share_of_5xx(statuses, expected):
    recs = [_rec("cart-service", f"c{i}", 10, s) for i, s in enumerate(statuses)]
    h = handler.health(recs, W)
    assert h["error_rate"] == pytest.approx(expected)
    assert h["services"][1]["error_rate"] == pytest.approx(expected)


def test_health_counts_only_its_own_minute():
    recs = [_rec("cart-service", "a", 10, 200, "2026-11-04T07:03:59.999Z"),   # 07:03
            _rec("cart-service", "b", 20, 200, "2026-11-04T07:04:00.000Z"),   # first instant of 07:04
            _rec("cart-service", "c", 30, 200, "2026-11-04T07:04:59.999Z"),   # last instant of 07:04
            _rec("cart-service", "d", 40, 200, "2026-11-04T07:05:00.000Z")]   # 07:05
    h = handler.health(recs, W)
    assert h["services"][1]["requests_per_min"] == 2
    assert h["requests_per_min"] == 2


def test_health_drops_repeats_itself():
    once = handler.health(HAND, W)
    twice = handler.health(HAND + HAND, W)
    assert twice == once


def test_health_agrees_with_the_models_request_features():
    # The dashboard's per-service numbers must be the features' own: same percentile, same 5xx rule.
    records, _ = _stream()
    for m in (0, 3, 7):
        w = _ts(m, 0)
        h = handler.health(records, w)
        feats = request_features(handler.dedupe(records))[w]
        for item, svc in zip(h["services"], SERVICES):
            p = svc.replace("-service", "")
            assert item["requests_per_min"] == pytest.approx(feats[f"{p}_request_rate"] * 60)
            assert item["error_rate"] == pytest.approx(feats[f"{p}_error_rate_5xx"])
            assert item["p95_latency_ms"] == pytest.approx(feats[f"{p}_p95_latency_ms"])


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
