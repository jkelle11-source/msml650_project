"""Which one-minute window does a record belong to? (M4-5)

A window is named by its start time, in the telemetry timestamp format
(UTC, millisecond precision, trailing Z).
"""
from datetime import datetime, timedelta, timezone

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse(ts):
    return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc)


def _format(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def request_window(ts):
    """Window start for a request record: its timestamp, rounded down to the minute."""
    return _format(_parse(ts).replace(second=0, microsecond=0))


def metrics_window(ts):
    """Window start for a metrics snapshot.

    A snapshot stamped T covers [T - 1 min, T) (Docs/DATASET.md), so the window
    it describes starts one minute before its timestamp. Assumes T is on a
    minute boundary, as the extractor writes it.
    """
    return _format(_parse(ts) - timedelta(minutes=1))

def is_complete(window_start, started_at, ended_at):
    """True if the window lies wholly inside [started_at, ended_at)."""
    start = _parse(window_start)
    return _parse(started_at) <= start and start + timedelta(minutes=1) <= _parse(ended_at)