"""Tests for the matrix builder, including the guard wiring (M4-5)."""
import pytest

from features import build_matrix as bm
from features import guard

REC = {"timestamp": "2026-10-16T03:19:10.000Z", "service": "product-service",
       "status_code": 200, "latency_ms": 10.0, "lambda_duration_ms": 3.0,
       "cold_start": False, "dependency": None,
       "dependency_latency_ms": None, "dependency_error": None}
SNAP = {"timestamp": "2026-10-16T03:20:00.000Z", "service": "product-service",
        "metrics": {"dynamodb": {"consumed_read_capacity": 45.0, "consumed_write_capacity": 0.0,
                                 "throttled_requests": 0, "read_throttle_events": 0,
                                 "write_throttle_events": 0},
                    "lambda": {"throttle_count": 0.0}}}


def test_requests_and_metrics_share_one_row_per_window():
    rows = bm.build_matrix([REC], [SNAP])
    assert list(rows) == ["2026-10-16T03:19:00.000Z"]
    row = rows["2026-10-16T03:19:00.000Z"]
    assert "product_p50_latency_ms" in row and "product_throttled_requests" in row


def test_injected_tier2_column_fails_the_build(monkeypatch):
    real = bm.request_features

    def leaky(records):
        rows = real(records)
        for row in rows.values():
            row["incident_type"] = "TRAFFIC_SPIKE"
        return rows

    monkeypatch.setattr(bm, "request_features", leaky)
    with pytest.raises(guard.BoundaryViolation):
        bm.build_matrix([REC], [SNAP])


def test_clean_build_does_not_raise():
    bm.build_matrix([REC], [SNAP])

def test_matrix_includes_temporal_columns():
    later = dict(REC, timestamp="2026-10-16T03:20:10.000Z")
    rows = bm.build_matrix([REC, later], [SNAP])
    first, second = list(rows.values())[:2]
    assert second["product_p95_latency_ms_change"] == 0
    assert first["product_p95_latency_ms_change"] is None
    assert "product_p95_latency_ms_roll_mean_3" in second

def test_partial_windows_are_dropped_before_temporal_features():
    recs = [dict(REC, timestamp=t) for t in
            ("2026-10-16T03:19:50.000Z", "2026-10-16T03:20:10.000Z", "2026-10-16T03:21:10.000Z")]
    rows = bm.build_matrix(recs, [SNAP], bounds=("2026-10-16T03:19:50.000Z", "2026-10-16T03:22:30.000Z"))
    assert list(rows) == ["2026-10-16T03:20:00.000Z", "2026-10-16T03:21:00.000Z"]
    # the part-minute first window is gone, so the first kept window has no history
    assert rows["2026-10-16T03:20:00.000Z"]["product_p95_latency_ms_change"] is None


def test_an_excluded_window_never_feeds_a_later_one():
    # 03:20 is excluded (say, a DB-throttling surge): 03:21 compares with 03:19.
    recs = [dict(REC, timestamp="2026-10-16T03:19:10.000Z", latency_ms=10.0),
            dict(REC, timestamp="2026-10-16T03:20:10.000Z", latency_ms=900.0),
            dict(REC, timestamp="2026-10-16T03:21:10.000Z", latency_ms=50.0)]
    labels = [{"window_start": "2026-10-16T03:19:00.000Z", "included": True},
              {"window_start": "2026-10-16T03:20:00.000Z", "included": False,
               "excluded_reason": "db_throttling_onset"},
              {"window_start": "2026-10-16T03:21:00.000Z", "included": True}]
    rows = bm.build_matrix(recs, [SNAP], lookback=bm.lookback_windows(labels))
    assert rows["2026-10-16T03:21:00.000Z"]["product_p95_latency_ms_change"] == 40
    assert rows["2026-10-16T03:20:00.000Z"]["product_p95_latency_ms_change"] == 890   # still has a row


def test_the_startup_minute_is_history_for_the_first_labeled_window():
    # Without it, every run's first labeled window (always NORMAL) would have no temporal values.
    recs = [dict(REC, timestamp="2026-10-16T03:19:10.000Z", latency_ms=10.0),
            dict(REC, timestamp="2026-10-16T03:20:10.000Z", latency_ms=12.0)]
    labels = [{"window_start": "2026-10-16T03:19:00.000Z", "included": False, "excluded_reason": "startup"},
              {"window_start": "2026-10-16T03:20:00.000Z", "included": True}]
    assert bm.lookback_windows(labels) == {"2026-10-16T03:19:00.000Z", "2026-10-16T03:20:00.000Z"}
    rows = bm.build_matrix(recs, [SNAP], lookback=bm.lookback_windows(labels))
    assert rows["2026-10-16T03:20:00.000Z"]["product_p95_latency_ms_change"] == 2
    assert rows["2026-10-16T03:20:00.000Z"]["product_p95_latency_ms_roll_mean_3"] == 11


@pytest.mark.parametrize("reason", ["db_throttling_onset", "recovery", "uncounted_fault", "partial_window"])
def test_other_excluded_windows_are_not_history(reason):
    labels = [{"window_start": "2026-10-16T03:19:00.000Z", "included": False, "excluded_reason": reason}]
    assert bm.lookback_windows(labels) == set()