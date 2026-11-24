"""Tests for cost/collect_m9.py, with fake AWS clients (no AWS, no charge). Expected values are worked out by hand.

    python3 -m pytest cost -q        # from AnomalyPulse/
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from cost import collect_m9 as c

U = timezone.utc


def t(*args):
    return datetime(*args, tzinfo=U)


def attempt(dataset="v1", phase="test", batch="b", verdict="accepted", label="NORMAL", start="", end="", eid="e"):
    return {"dataset": dataset, "phase": phase, "batch_id": batch, "verdict": verdict, "label": label,
            "started_at": start, "ended_at": end, "experiment_id": eid, "slot": 1}


def test_minutes_round_down_and_up_and_leave_whole_minutes_alone():
    assert c.floor_minute(t(2026, 11, 16, 13, 31, 58, 200000)) == t(2026, 11, 16, 13, 31)
    assert c.ceil_minute(t(2026, 11, 16, 17, 7, 5, 500000)) == t(2026, 11, 16, 17, 8)
    assert c.ceil_minute(t(2026, 11, 16, 17, 7)) == t(2026, 11, 16, 17, 7)


def test_a_batch_window_runs_from_its_first_start_to_its_last_end_plus_the_tail():
    rows = [attempt(phase="stress", verdict="rejected", start="2026-11-16T13:31:58.2Z", end="2026-11-16T13:41:58.4Z"),
            attempt(phase="stress", start="2026-11-16T16:52:05.4Z", end="2026-11-16T17:02:05.5Z"),
            attempt(phase="train", batch="old", start="2026-10-20T00:00:00Z", end="2026-10-20T00:10:00Z"),
            attempt(dataset="m9-1-validation", phase=None, batch="val", start="2026-11-13T16:00:00Z",
                    end="2026-11-13T16:10:00Z")]
    # 17:02:05.5 + 5 min = 17:07:05.5, rounded up; the training and validation rows are someone else's
    assert c.batch_windows(rows) == {"b": {"start": t(2026, 11, 16, 13, 31), "end": t(2026, 11, 16, 17, 8),
                                           "attempts": 2, "accepted": 1}}


def test_normal_runs_are_accepted_normal_test_runs_over_their_whole_minutes():
    rows = [attempt(start="2026-11-14T10:00:12.5Z", end="2026-11-14T10:10:12.6Z", eid="n1"),
            attempt(label="TRAFFIC_SPIKE", start="2026-11-14T10:10:30Z", end="2026-11-14T10:20:30Z", eid="s"),
            attempt(verdict="rejected", start="2026-11-14T10:20:30Z", end="2026-11-14T10:30:30Z", eid="r"),
            attempt(phase="stress", start="2026-11-16T14:00:00Z", end="2026-11-16T14:10:00Z", eid="x")]
    runs = c.normal_runs(rows)
    # 10:00:12.5 rounds up to 10:01 and 10:10:12.6 down to 10:10: nine whole minutes
    assert runs == [{"experiment_id": "n1", "slot": 1, "start": t(2026, 11, 14, 10, 1), "end": t(2026, 11, 14, 10, 10)}]


class FakeCW:
    """get_metric_data answering each query Id with the points given for it."""
    def __init__(self, points):
        self.points, self.calls = points, []

    def get_metric_data(self, **kw):
        self.calls.append(kw)
        res = [{"Id": q["Id"], "Timestamps": [p[0] for p in self.points.get(q["Id"], [])],
                "Values": [p[1] for p in self.points.get(q["Id"], [])]} for q in kw["MetricDataQueries"]]
        return {"MetricDataResults": res}


def test_batch_usage_sums_each_minute_and_turns_milliseconds_into_gb_seconds():
    memory = {"anomalypulse-b": 512, "anomalypulse-a": 1024}           # sorted: a is inv_0, b is inv_1
    cw = FakeCW({"api": [(t(2026, 11, 14, 1), 100.0), (t(2026, 11, 14, 1, 1), 50.0)],
                 "inv_0": [(t(2026, 11, 14, 1), 4.0)], "dur_0": [(t(2026, 11, 14, 1), 2000.0)],
                 "log_0": [(t(2026, 11, 14, 1), 300.0)], "dur_1": [(t(2026, 11, 14, 1), 4000.0)]})
    u = c.batch_usage(cw, memory, t(2026, 11, 14, 1), t(2026, 11, 14, 2))
    # a: 2,000 ms at 1 GB = 2 GB-s; b: 4,000 ms at 0.5 GB = 2 GB-s, and no invocation points means 0
    assert u == {"api": 150, "functions": {"anomalypulse-a": {"invocations": 4, "gb_s": 2.0, "log_bytes": 300},
                                           "anomalypulse-b": {"invocations": 0, "gb_s": 2.0, "log_bytes": 0}}}
    assert all(q["MetricStat"]["Period"] == 60 for q in cw.calls[0]["MetricDataQueries"])


def test_table_usage_sums_each_table_s_read_and_write_units():
    cw = FakeCW({"r_0": [(t(2026, 11, 14, 1), 1.0), (t(2026, 11, 14, 1, 1), 2.5)], "w_0": [(t(2026, 11, 14, 1), 4.0)]})
    # sorted: a is r_0/w_0; b has no points at all
    assert c.table_usage(cw, ["anomalypulse-b", "anomalypulse-a"], t(2026, 11, 14, 1), t(2026, 11, 14, 2)) == {
        "anomalypulse-a": {"read_units": 3.5, "write_units": 4.0},
        "anomalypulse-b": {"read_units": 0.0, "write_units": 0.0}}


class FakeOpsCW:
    """get_metric_data answering by metric, statistic and period, like CloudWatch, not by query Id. A whole-window
    query (its Period the window's length) gets one point; a 1-minute query gets the minutes given for it."""
    def __init__(self, window_s, whole, minutes):
        self.window_s, self.whole, self.minutes, self.calls = window_s, whole, minutes, []

    def get_metric_data(self, **kw):
        self.calls.append(kw)
        res = []
        for q in kw["MetricDataQueries"]:
            s = q["MetricStat"]
            dims = {d["Name"]: d["Value"] for d in s["Metric"]["Dimensions"]}
            key = (s["Metric"]["MetricName"], s["Stat"], next(iter(dims.values())))
            if s["Period"] == self.window_s:
                v = self.whole.get(key)
                pts = [] if v is None else [(kw["StartTime"], v)]
            elif s["Period"] == 60:
                pts = self.minutes.get(key, [])
            else:
                pts = [(kw["StartTime"], 1.0), (kw["StartTime"], 2.0)]
            res.append({"Id": q["Id"], "Timestamps": [p[0] for p in pts], "Values": [p[1] for p in pts]})
        return {"MetricDataResults": res}


def ops_fake():
    api, fn, queue = "anomalypulse-api", "anomalypulse-a", "anomalypulse-telemetry"
    whole = {("Latency", "p50", api): 12.3456, ("Latency", "p95", api): 20.0, ("Latency", "p99", api): 30.0,
             ("Latency", "Maximum", api): 40.0, ("Count", "Sum", api): 600.0, ("4XXError", "Sum", api): 1.0,
             ("Duration", "p50", fn): 5.0, ("Duration", "p95", fn): 6.0, ("Duration", "p99", fn): 7.0,
             ("Duration", "Maximum", fn): 8.0, ("Errors", "Sum", fn): 0.0, ("ConcurrentExecutions", "Maximum", fn): 2.0,
             ("NumberOfMessagesSent", "Sum", queue): 10.0, ("NumberOfMessagesDeleted", "Sum", queue): 10.0,
             ("ApproximateNumberOfMessagesVisible", "Maximum", queue): 1.0,
             ("ApproximateAgeOfOldestMessage", "Maximum", queue): 15.0,
             ("ApproximateNumberOfMessagesVisible", "Maximum", "anomalypulse-telemetry-dlq"): 3.0,
             ("ApproximateNumberOfMessagesVisible", "Minimum", "anomalypulse-telemetry-dlq"): 0.0}
    m0 = t(2026, 11, 14, 10)
    minutes = {("Count", "Sum", api): [(m0, 100.0), (t(2026, 11, 14, 10, 1), 300.0), (t(2026, 11, 14, 10, 3), 200.0)],
               ("ApproximateNumberOfMessagesVisible", "Maximum", queue): [(m0, 0.0), (t(2026, 11, 14, 10, 1), 1.0),
                                                                          (t(2026, 11, 14, 10, 2), 0.0)]}
    return FakeOpsCW(300, whole, minutes)


def test_ops_stats_reads_each_statistic_over_the_whole_window_and_the_per_minute_peaks():
    cw = ops_fake()
    o = c.ops_stats(cw, {"anomalypulse-a": 512}, t(2026, 11, 14, 10), t(2026, 11, 14, 10, 5))
    assert o["period_s"] == 300
    # whole-window statistics, rounded to 3 places; a statistic with no data is None, not 0
    assert o["api"]["latency_p50"] == 12.346 and o["api"]["count"] == 600 and o["api"]["5xxerror"] is None
    assert o["functions"]["anomalypulse-a"] == {"duration_p50": 5.0, "duration_p95": 6.0, "duration_p99": 7.0,
                                                "duration_maximum": 8.0, "errors": 0.0, "throttles": None,
                                                "concurrency_max": 2.0}
    assert o["queue"]["NumberOfMessagesSent"] == 10 and o["queue"]["ApproximateAgeOfOldestMessage"] == 15
    # a failure queue's most messages visible: its Maximum, not another statistic
    assert o["failure_queues"]["anomalypulse-telemetry-dlq"] == 3.0
    assert o["failure_queues"]["anomalypulse-incidents-failed"] is None
    # 600 requests over the window's 5 minutes, two of which had no point: 120 a minute, not 600 / 3
    assert o["api"]["requests_per_minute_mean"] == 120.0 and o["api"]["requests_per_minute_max"] == 300
    assert (o["queue"]["visible_minute_median"], o["queue"]["visible_minute_max"]) == (0.0, 1.0)
    assert (o["queue"]["age_s_minute_median"], o["queue"]["age_s_minute_max"]) == (None, None)


def test_ops_stats_refuses_a_whole_window_statistic_split_into_several_points():
    class Split(FakeOpsCW):
        def get_metric_data(self, **kw):
            r = super().get_metric_data(**kw)
            r["MetricDataResults"][0]["Values"] += [1.0]
            r["MetricDataResults"][0]["Timestamps"] += [kw["StartTime"]]
            return r
    cw = ops_fake()
    with pytest.raises(RuntimeError):
        c.ops_stats(Split(cw.window_s, cw.whole, cw.minutes), {"anomalypulse-a": 512}, t(2026, 11, 14, 10), t(2026, 11, 14, 10, 5))


class FakeCE:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        return self.pages[len(self.calls) - 1]


def test_ce_daily_reads_every_page_and_dates_each_row():
    g = lambda svc, cost: {"Keys": [svc, "ut"], "Metrics": {"UnblendedCost": {"Amount": str(cost)},
                                                            "UsageQuantity": {"Amount": "2", "Unit": "u"}}}
    ce = FakeCE([{"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-14"}, "Groups": [g("A", 1.5)]}], "NextPageToken": "p"},
                 {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-15"}, "Groups": [g("B", 0.25)]}]}])
    rows = c.ce_daily(ce, "2026-11-14", "2026-11-16")
    assert rows == [{"service": "A", "usage_type": "ut", "cost": 1.5, "qty": 2.0, "unit": "u", "day": "2026-11-14"},
                    {"service": "B", "usage_type": "ut", "cost": 0.25, "qty": 2.0, "unit": "u", "day": "2026-11-15"}]
    assert ce.calls[1]["NextPageToken"] == "p" and ce.calls[0]["Granularity"] == "DAILY"


class FakeLogs:
    def __init__(self, replies):
        self.replies, self.started = replies, []

    def start_query(self, **kw):
        self.started.append(kw)
        return {"queryId": "q"}

    def get_query_results(self, queryId):
        return self.replies.pop(0)


def test_diagnosis_reports_waits_for_the_query_and_reads_its_row(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    row = [{"field": "calls", "value": "1417"}, {"field": "billed_ms", "value": "39193"}]
    logs = FakeLogs([{"status": "Running", "results": []}, {"status": "Complete", "results": [row]}])
    r = c.diagnosis_reports(logs, t(2026, 11, 13, 22, 42), t(2026, 11, 14, 22, 25))
    assert r["calls"] == 1417 and r["billed_ms"] == 39193 and r["init_ms"] == 0
    # whole seconds, and the inclusive end one second before the window's end
    assert logs.started[0]["startTime"] == int(t(2026, 11, 13, 22, 42).timestamp())
    assert logs.started[0]["endTime"] == int(t(2026, 11, 14, 22, 25).timestamp()) - 1


def test_no_calls_gives_zeros_and_a_failed_query_raises(monkeypatch):
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    idle = c.diagnosis_reports(FakeLogs([{"status": "Complete", "results": []}]), t(2026, 11, 20), t(2026, 11, 22))
    assert idle == dict.fromkeys(c.REPORT_FIELDS, 0.0)
    with pytest.raises(RuntimeError):
        c.diagnosis_reports(FakeLogs([{"status": "Failed", "results": []}]), t(2026, 11, 20), t(2026, 11, 22))


def test_sk_time_is_written_like_the_incident_table_s_sort_key():
    assert c.sk_time(t(2026, 11, 14, 3, 12)) == "2026-11-14T03:12:00.000Z"
    assert c.sk_time(t(2026, 11, 14, 3, 12, 5, 123456)) == "2026-11-14T03:12:05.123Z"


class FakeTable:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def query(self, **kw):
        self.calls.append(kw)
        return self.pages[len(self.calls) - 1]


def test_batch_incidents_reads_every_page_and_leaves_out_the_window_s_end():
    item = lambda sk, calls, kind: {"sk": sk, "bedrock_calls": Decimal(calls), "prediction": {"incident_type": kind}}
    table = FakeTable([{"Items": [item("2026-11-14T01:00:00.000Z", 2, "TRAFFIC_SPIKE")], "LastEvaluatedKey": {"k": 1}},
                       {"Items": [item("2026-11-14T01:30:00.000Z", 1, "DEPENDENCY_FAILURE"),
                                  item("2026-11-14T02:00:00.000Z", 3, "TRAFFIC_SPIKE")]}])
    r = c.batch_incidents(table, t(2026, 11, 14, 1), t(2026, 11, 14, 2))
    # between() includes 02:00:00.000, which is the next window's
    assert r == {"incidents": 2, "bedrock_calls": 3, "types": {"DEPENDENCY_FAILURE": 1, "TRAFFIC_SPIKE": 1}}
    assert table.calls[1]["ExclusiveStartKey"] == {"k": 1}
