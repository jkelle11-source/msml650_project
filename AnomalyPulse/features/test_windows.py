"""Tests for window assignment (M4-5)."""
import windows


def test_request_is_rounded_down_to_its_minute():
    assert windows.request_window("2026-10-16T03:19:55.418Z") == "2026-10-16T03:19:00.000Z"


def test_request_at_the_end_of_a_minute_stays_in_it():
    assert windows.request_window("2026-10-16T03:19:59.999Z") == "2026-10-16T03:19:00.000Z"


def test_request_exactly_on_a_boundary_starts_the_new_window():
    assert windows.request_window("2026-10-16T03:20:00.000Z") == "2026-10-16T03:20:00.000Z"


def test_metrics_snapshot_describes_the_minute_before_its_timestamp():
    assert windows.metrics_window("2026-10-16T03:20:00.000Z") == "2026-10-16T03:19:00.000Z"


def test_metrics_window_crosses_midnight():
    assert windows.metrics_window("2026-10-17T00:00:00.000Z") == "2026-10-16T23:59:00.000Z"


def test_a_request_and_its_minutes_metrics_share_a_window():
    # A request at 03:19:55 and the snapshot stamped 03:20:00 describe the same minute.
    request = windows.request_window("2026-10-16T03:19:55.418Z")
    metrics = windows.metrics_window("2026-10-16T03:20:00.000Z")
    assert request == metrics

STARTED, ENDED = "2026-10-16T03:19:54.000Z", "2026-10-16T03:29:54.000Z"


def test_a_window_inside_the_run_is_complete():
    assert windows.is_complete("2026-10-16T03:20:00.000Z", STARTED, ENDED)


def test_the_first_window_of_a_run_is_partial():
    assert not windows.is_complete("2026-10-16T03:19:00.000Z", STARTED, ENDED)


def test_the_last_window_of_a_run_is_partial():
    assert not windows.is_complete("2026-10-16T03:29:00.000Z", STARTED, ENDED)