"""Tests for report/latency.py, on hand-made runs whose answers are worked out by hand."""
import json

import pytest

from report import latency


def manifest(eid, labeled="DATABASE_THROTTLING", starts=("2026-11-13T22:45:43.000Z",), phase="test",
             started="2026-11-13T22:42:00.000Z"):
    return {"experiment_id": eid, "slot": 701, "phase": phase, "labeled_type": labeled, "started_at": started,
            "faults": [{"fault_id": f"{eid}#{i}", "injector": "x", "target": None, "active_from": s,
                        "active_until": None} for i, s in enumerate(starts)]}


def normal(eid, started):
    return {**manifest(eid, "NORMAL", started=started), "faults": []}


def incident(eid, minute, detected_at, stored_s=28.0, bedrock_ms=27000, iid=None, kind="DATABASE_THROTTLING",
             ended_at="2026-11-13T23:30:00.000Z"):
    return {"incident_id": iid or f"inc-{minute}", "experiment_id": eid, "ended_at": ended_at,
            "prediction": {"started_at": minute, "detected_at": detected_at, "incident_type": kind},
            "latency": {"bedrock_ms": bedrock_ms, "diagnosis_to_stored_s": stored_s, "window_end_to_diagnosis_s": 0}}


def test_the_chain_is_split_into_its_steps():
    # Fault at 22:45:43; flagged minute 22:45 ends 22:46:00 (17 s); classified 169 s later, of which 120 s is
    # CloudWatch's settle and 49 s the rest; stored 28 s later.
    rows = latency.runs([manifest("e1")], [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")])
    (row,) = rows
    assert row["outcome"] == "detected" and row["false_incidents"] == 0 and row["held_back"] is False
    assert row["steps"] == {"window": 17.0, "window_onset_minute": 17.0, "window_extra_minutes": 0.0,
                            "pipeline_wait": 169.0, "cloudwatch_settle": 120.0, "build_and_classify": 49.0,
                            "diagnosis": 28.0, "bedrock": 27.0, "end_to_end": 214.0}


def test_the_settle_is_the_extractor_s_lag():
    template = (latency.ROOT / "infra" / "shared" / "template.yaml").read_text()
    assert f"METRICS_LAG_MINUTES: '{int(latency.CLOUDWATCH_SETTLE_S // 60)}'" in template


def test_the_hold_kinds_are_the_store_s():
    record = pytest.importorskip("incidents.record")
    assert tuple(latency.HOLD_KINDS) == tuple(record.HOLD_KINDS)


def test_a_minute_flagged_later_counts_its_whole_minutes_as_window():
    rows = latency.runs([manifest("e1")], [incident("e1", "2026-11-13T22:46:00.000Z", "2026-11-13T22:49:49.000Z")])
    steps = rows[0]["steps"]
    assert steps["window"] == 77.0 and steps["window_onset_minute"] == 17.0 and steps["window_extra_minutes"] == 60.0


def test_the_clock_starts_at_the_first_fault_of_several():
    m = manifest("e1", "CASCADING_FAILURE", ("2026-11-13T22:47:10.000Z", "2026-11-13T22:45:43.000Z"))
    assert latency.fault_start(m).isoformat() == "2026-11-13T22:45:43+00:00"


def test_an_incident_opened_before_the_fault_is_a_false_one_and_the_next_is_the_detection():
    records = [incident("e1", "2026-11-13T22:40:00.000Z", "2026-11-13T22:43:49.000Z", iid="early",
                        ended_at="2026-11-13T22:44:00.000Z"),
               incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", iid="real")]
    m = manifest("e1", started="2026-11-13T22:39:00.000Z")
    (row,) = latency.runs([m], records)
    assert row["false_incidents"] == 1 and row["incident_id"] == "real" and row["steps"]["window"] == 17.0


def test_the_detection_is_the_earliest_incident_after_the_fault_whatever_the_input_order():
    records = [incident("e1", "2026-11-13T22:52:00.000Z", "2026-11-13T22:55:49.000Z", iid="second"),
               incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", iid="first",
                        ended_at="2026-11-13T22:50:00.000Z")]
    (row,) = latency.runs([manifest("e1")], records)
    assert row["incident_id"] == "first" and row["steps"]["window"] == 17.0


def test_a_minute_ending_exactly_at_the_fault_start_is_before_it():
    m = manifest("e1", starts=("2026-11-13T22:46:00.000Z",))
    (row,) = latency.runs([m], [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z",
                                         ended_at="2026-11-13T22:46:00.000Z")])
    assert row["outcome"] == "missed" and row["false_incidents"] == 1


def test_an_incident_open_just_before_the_fault_and_still_open_covers_it_but_is_not_timed():
    # Fault at 22:45:43; the incident's first minute is 22:44 (ends 22:45:00, 43 s before) and it is still open.
    records = [incident("e1", "2026-11-13T22:44:00.000Z", "2026-11-13T22:47:49.000Z", iid="cover",
                        ended_at="2026-11-13T22:50:00.000Z"),
               incident("e2", "2026-11-13T22:55:00.000Z", "2026-11-13T22:58:49.000Z")]
    manifests = [manifest("e1"), manifest("e2", starts=("2026-11-13T22:55:43.000Z",), started="2026-11-13T22:52:00.000Z")]
    rows = latency.runs(manifests, records)
    assert rows[0]["outcome"] == "covered" and rows[0]["incident_id"] == "cover" and rows[0]["opened_before_s"] == 43.0
    assert rows[0]["incident_from"] == "this run" and rows[0]["false_incidents"] == 0 and "steps" not in rows[0]
    summary = latency.summarize(rows)
    assert summary["detected"] == 1 and summary["covered"] == [
        {"experiment_id": "e1", "slot": 701, "incident_id": "cover", "incident_from": "this run", "opened_before_s": 43.0}]
    assert summary["steps_s"]["window"]["n"] == 1


def test_an_incident_tagged_with_the_next_run_belongs_to_the_run_its_first_minute_is_in():
    # The store tags the run holding the lock about 4 minutes on; a 2-minute fault's run has ended by then.
    manifests = [manifest("e1"), manifest("e2", starts=("2026-11-13T22:51:43.000Z",), started="2026-11-13T22:48:00.000Z")]
    records = [incident("e2", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", iid="mine",
                        ended_at="2026-11-13T22:50:00.000Z"),
               incident("e2", "2026-11-13T22:52:00.000Z", "2026-11-13T22:55:49.000Z", iid="theirs")]
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, records)}
    assert rows["e1"]["outcome"] == "detected" and rows["e1"]["incident_id"] == "mine"
    assert rows["e1"]["tagged_to_another_run"] == 1 and rows["e1"]["steps"]["window"] == 17.0
    assert rows["e2"]["incident_id"] == "theirs" and rows["e2"]["steps"]["window"] == 77.0
    assert latency.summarize(list(rows.values()))["tagged_to_another_run"] == 1


def test_an_earlier_run_s_incident_still_open_at_the_fault_start_covers_it():
    manifests = [manifest("e1"), manifest("e2", starts=("2026-11-13T22:53:43.000Z",), started="2026-11-13T22:50:00.000Z")]
    still_open = incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", ended_at=None)
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, [still_open])}
    assert rows["e1"]["outcome"] == "detected"
    # 22:53:43 less the first minute's end, 22:46:00: 7 min 43 s.
    assert rows["e2"]["outcome"] == "covered" and rows["e2"]["incident_from"] == "an earlier run"
    assert rows["e2"]["opened_before_s"] == 463.0 and rows["e2"]["false_incidents"] == 0


def test_an_earlier_run_s_incident_closed_before_the_fault_start_does_not_cover_it():
    manifests = [manifest("e1"), manifest("e2", starts=("2026-11-13T22:53:43.000Z",), started="2026-11-13T22:50:00.000Z")]
    closed = incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", ended_at="2026-11-13T22:53:00.000Z")
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, [closed])}
    assert rows["e2"]["outcome"] == "missed" and rows["e2"]["false_incidents"] == 0


def test_a_normal_run_s_false_alarm_tagged_to_the_next_run_counts_on_the_normal_run():
    manifests = [normal("calm", "2026-11-13T22:42:00.000Z"),
                 manifest("e2", starts=("2026-11-13T22:51:43.000Z",), started="2026-11-13T22:48:00.000Z")]
    records = [incident("e2", "2026-11-13T22:47:00.000Z", "2026-11-13T22:50:49.000Z", iid="alarm",
                        ended_at="2026-11-13T22:50:00.000Z"),
               incident("e2", "2026-11-13T22:51:00.000Z", "2026-11-13T22:54:49.000Z", iid="real")]
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, records)}
    assert rows["calm"]["false_incidents"] == 1 and rows["calm"]["tagged_to_another_run"] == 1
    assert rows["e2"]["false_incidents"] == 0 and rows["e2"]["incident_id"] == "real"
    assert latency.summarize(list(rows.values()))["false_incidents"] == {"before_fault_start": 0, "normal_runs": 1}


def test_an_incident_whose_first_minute_starts_as_a_run_starts_belongs_to_that_run():
    manifests = [manifest("e1"), manifest("e2", started="2026-11-13T22:48:00.000Z")]
    rec = incident("e1", "2026-11-13T22:48:00.000Z", "2026-11-13T22:51:49.000Z")
    assert latency.owners(manifests, [rec]) == {rec["incident_id"]: "e2"}


def test_a_fault_running_exactly_half_its_first_minute_counts_as_half_or_more():
    rows = [{"steps": {"window_onset_minute": 30.0, "window_extra_minutes": 0.0}},
            {"steps": {"window_onset_minute": 29.9, "window_extra_minutes": 60.0}}]
    assert latency.extra_minute(rows) == {"onset_minute_half_or_more": {"runs": 1, "extra_minute": 0},
                                          "onset_minute_under_half": {"runs": 1, "extra_minute": 1}}


def test_an_incident_before_the_first_run_belongs_to_no_run():
    rec = incident("e1", "2026-11-13T22:40:00.000Z", "2026-11-13T22:43:49.000Z")
    assert latency.owners([manifest("e1")], [rec]) == {rec["incident_id"]: None}


def test_a_fault_run_with_no_incident_is_missed_and_a_normal_run_s_incidents_are_false():
    manifests = [manifest("miss"), normal("calm", "2026-11-13T22:52:00.000Z")]
    records = [incident("calm", "2026-11-13T22:55:00.000Z", "2026-11-13T22:58:49.000Z")]
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, records)}
    assert rows["miss"]["outcome"] == "missed"
    assert rows["calm"]["outcome"] == "normal" and rows["calm"]["false_incidents"] == 1
    summary = latency.summarize(list(rows.values()))
    assert summary["missed"] == ["miss"] and summary["false_incidents"] == {"before_fault_start": 0, "normal_runs": 1}


def test_a_first_diagnosis_held_for_wording_is_marked_and_a_later_type_s_hold_is_not():
    rec = incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")
    held = [{"incident_id": rec["incident_id"], "for_type": "DATABASE_THROTTLING", "flagged": ["wording"], "kept": True}]
    later = [{"incident_id": rec["incident_id"], "for_type": "CASCADING_FAILURE", "flagged": ["metric"], "kept": False},
             {"incident_id": rec["incident_id"], "for_type": "DATABASE_THROTTLING", "flagged": ["citation"], "kept": True}]
    shown = [{"incident_id": rec["incident_id"], "for_type": "DATABASE_THROTTLING", "flagged": ["metric"], "kept": False}]
    assert latency.runs([manifest("e1")], [rec], held)[0]["held_back"] is True
    assert latency.runs([manifest("e1")], [rec], later)[0]["held_back"] is False
    assert latency.runs([manifest("e1")], [rec], shown)[0]["held_back"] is False
    assert latency.summarize(latency.runs([manifest("e1")], [rec], held))["held_back"] == 1


def test_stats_are_the_median_mean_and_nearest_rank_p90():
    assert latency.stats([10, 1, 2, 3, 4, 5, 6, 7, 8, 9]) == {"n": 10, "median": 5.5, "mean": 5.5, "p90": 9,
                                                               "min": 1, "max": 10}
    assert latency.stats([1, 2, 30])["median"] == 2 and latency.stats([1, 2, 30])["mean"] == 11.0
    assert latency.stats([]) == {"n": 0}


def test_shares_are_of_the_mean_and_add_up_to_one():
    # Windows 17 and 77 s (mean 47), waits 169 and 171 s (120 s settle each; the rest 49 and 51, mean 50),
    # diagnoses 28 and 40 s (mean 34): shares of 251 s.
    records = [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", ended_at="2026-11-13T22:50:00.000Z"),
               incident("e2", "2026-11-13T22:56:00.000Z", "2026-11-13T22:59:51.000Z", stored_s=40.0)]
    manifests = [manifest("e1"), manifest("e2", starts=("2026-11-13T22:55:43.000Z",), started="2026-11-13T22:52:00.000Z")]
    summary = latency.summarize(latency.runs(manifests, records))
    assert summary["detected"] == 2
    steps = summary["steps_s"]
    assert steps["window"]["mean"] == 47.0 and steps["pipeline_wait"]["mean"] == 170.0
    assert steps["build_and_classify"]["mean"] == 50.0 and steps["diagnosis"]["mean"] == 34.0
    assert summary["share_of_mean"] == {"window": round(47 / 251, 3), "cloudwatch_settle": round(120 / 251, 3),
                                        "build_and_classify": round(50 / 251, 3), "diagnosis": round(34 / 251, 3)}
    assert sum(summary["share_of_mean"].values()) == pytest.approx(1.0, abs=0.002)


def test_the_extra_minute_is_counted_by_how_much_of_the_first_minute_the_fault_ran():
    # e1: 17 s of its first minute, flagged a minute later. e2: 40 s, flagged in it. e3: 17 s, flagged in it.
    manifests = [manifest("e1"),
                 manifest("e2", starts=("2026-11-13T22:55:20.000Z",), started="2026-11-13T22:52:00.000Z"),
                 manifest("e3", "TRAFFIC_SPIKE", ("2026-11-13T23:05:43.000Z",), started="2026-11-13T23:02:00.000Z")]
    records = [incident("e1", "2026-11-13T22:46:00.000Z", "2026-11-13T22:49:49.000Z", ended_at="2026-11-13T22:51:00.000Z"),
               incident("e2", "2026-11-13T22:55:00.000Z", "2026-11-13T22:58:49.000Z", ended_at="2026-11-13T23:01:00.000Z"),
               incident("e3", "2026-11-13T23:05:00.000Z", "2026-11-13T23:08:49.000Z", kind="DATABASE_THROTTLING")]
    summary = latency.summarize(latency.runs(manifests, records))
    assert summary["extra_minute"] == {"onset_minute_half_or_more": {"runs": 1, "extra_minute": 0},
                                       "onset_minute_under_half": {"runs": 2, "extra_minute": 1}}
    # By the run's labeled type, not the type the incident showed.
    assert summary["by_type"]["DATABASE_THROTTLING"]["window"]["n"] == 2
    assert summary["by_type"]["DATABASE_THROTTLING"]["extra_minute"] == 1
    assert summary["by_type"]["TRAFFIC_SPIKE"]["window"]["n"] == 1


def test_the_test_set_and_the_stress_slice_are_summarized_apart():
    manifests = [manifest("t"), manifest("s", phase="stress", starts=("2026-11-13T22:55:43.000Z",),
                                         started="2026-11-13T22:52:00.000Z")]
    records = [incident("t", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z", ended_at="2026-11-13T22:50:00.000Z")]
    result = latency.build(manifests, records)
    assert result["summary"]["test"]["detected"] == 1 and result["summary"]["test"]["missed"] == []
    assert result["summary"]["stress"]["missed"] == ["s"] and result["summary"]["stress"]["detected"] == 0


def test_git_state_ignores_the_script_s_own_output(monkeypatch):
    out = latency.RESULTS.relative_to(latency.ROOT.parent).as_posix()
    for status, dirty in ((f" M {out}\n", False), (f" M {out}\n M AnomalyPulse/report/latency.py\n", True), ("", False)):
        monkeypatch.setattr(latency, "git", lambda *a, s=status: "abc\n" if a[0] == "rev-parse" else s)
        assert latency.git_state() == {"git_commit": "abc", "git_dirty": dirty}


@pytest.mark.skipif(not (latency.RECORDS.exists() and latency.MANIFESTS.exists() and latency.RETRIES.exists()),
                    reason="needs the exported data")
def test_on_the_real_data_every_fault_run_is_detected_or_covered():
    records = json.loads(latency.RECORDS.read_text())
    retries = [json.loads(line) for line in latency.RETRIES.read_text().splitlines() if line.strip()]
    rows = latency.runs(json.loads(latency.MANIFESTS.read_text()), records, retries)
    by_id = {r["incident_id"]: r for r in records}
    detected = [r for r in rows if r["outcome"] == "detected"]
    assert len(rows) == 370 and detected
    test = latency.summarize([r for r in rows if r["phase"] == "test"])
    # Counted independently from the raw S3 manifests and the Incidents table (M9-6 review, 2026-11-25).
    assert test["fault_runs"] == 290 and test["detected"] == 284 and test["missed"] == []
    assert sorted(c["slot"] for c in test["covered"]) == [707, 764, 818, 930, 1031, 1049]
    assert sorted(c["slot"] for c in test["covered"] if c["incident_from"] == "an earlier run") == [764, 818]
    assert test["false_incidents"] == {"before_fault_start": 0, "normal_runs": 8}
    assert test["tagged_to_another_run"] == 4 and test["held_back"] == 8
    # Shares of the mean (independent recount: window 47.6 s, settle 120 s, the rest 50.0 s, diagnosis 29.3 s).
    assert test["share_of_mean"] == {"window": 0.193, "cloudwatch_settle": 0.486, "build_and_classify": 0.203,
                                     "diagnosis": 0.119}
    for row in detected:
        own = by_id[row["incident_id"]]["latency"]["window_end_to_diagnosis_s"]
        assert row["steps"]["pipeline_wait"] == pytest.approx(own, abs=0.01)
        assert row["steps"]["window"] > 0 and row["steps"]["build_and_classify"] > 0
