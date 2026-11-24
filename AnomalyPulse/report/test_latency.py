"""Tests for report/latency.py, on hand-made runs whose answers are worked out by hand."""
import json

import pytest

from report import latency


def manifest(eid, labeled="DATABASE_THROTTLING", starts=("2026-11-13T22:45:43.000Z",), phase="test"):
    return {"experiment_id": eid, "slot": 701, "phase": phase, "labeled_type": labeled,
            "started_at": "2026-11-13T22:42:00.000Z",
            "faults": [{"fault_id": f"{eid}#{i}", "injector": "x", "target": None, "active_from": s,
                        "active_until": None} for i, s in enumerate(starts)]}


def incident(eid, minute, detected_at, stored_s=28.0, bedrock_ms=27000, iid=None, kind="DATABASE_THROTTLING",
             ended_at="2026-11-13T23:30:00.000Z"):
    return {"incident_id": iid or f"inc-{minute}", "experiment_id": eid, "ended_at": ended_at,
            "prediction": {"started_at": minute, "detected_at": detected_at, "incident_type": kind},
            "latency": {"bedrock_ms": bedrock_ms, "diagnosis_to_stored_s": stored_s, "window_end_to_diagnosis_s": 0}}


def test_the_chain_is_split_into_its_steps():
    # Fault at 22:45:43; flagged minute 22:45 ends 22:46:00 (17 s); classified 169 s later; stored 28 s later.
    rows = latency.runs([manifest("e1")], [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")])
    (row,) = rows
    assert row["outcome"] == "detected" and row["false_incidents"] == 0
    assert row["steps"] == {"window": 17.0, "window_onset_minute": 17.0, "window_extra_minutes": 0.0,
                            "pipeline_wait": 169.0, "diagnosis": 28.0, "bedrock": 27.0, "end_to_end": 214.0}


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
    (row,) = latency.runs([manifest("e1")], records)
    assert row["false_incidents"] == 1 and row["incident_id"] == "real" and row["steps"]["window"] == 17.0


def test_a_minute_ending_exactly_at_the_fault_start_is_before_it():
    m = manifest("e1", starts=("2026-11-13T22:46:00.000Z",))
    (row,) = latency.runs([m], [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z",
                                         ended_at="2026-11-13T22:46:00.000Z")])
    assert row["outcome"] == "missed" and row["false_incidents"] == 1


def test_an_incident_open_just_before_the_fault_and_still_open_covers_it_but_is_not_timed():
    # Fault at 22:45:43; the incident's first minute is 22:44 (ends 22:45:00, 43 s before) and it is still open.
    records = [incident("e1", "2026-11-13T22:44:00.000Z", "2026-11-13T22:47:49.000Z", iid="cover"),
               incident("e2", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")]
    rows = latency.runs([manifest("e1"), manifest("e2")], records)
    assert rows[0]["outcome"] == "early" and rows[0]["incident_id"] == "cover" and rows[0]["opened_before_s"] == 43.0
    assert rows[0]["false_incidents"] == 0 and "steps" not in rows[0]
    summary = latency.summarize(rows)
    assert summary["detected"] == 1 and summary["early"] == [{"experiment_id": "e1", "slot": 701, "opened_before_s": 43.0}]
    assert summary["steps_s"]["window"]["n"] == 1


def test_a_fault_run_with_no_incident_is_missed_and_a_normal_run_s_incidents_are_false():
    manifests = [manifest("miss"), {**manifest("calm", "NORMAL"), "faults": []}]
    records = [incident("calm", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")]
    rows = {r["experiment_id"]: r for r in latency.runs(manifests, records)}
    assert rows["miss"]["outcome"] == "missed"
    assert rows["calm"]["outcome"] == "normal" and rows["calm"]["false_incidents"] == 1
    summary = latency.summarize(list(rows.values()))
    assert summary["missed"] == ["miss"] and summary["false_incidents"] == {"before_fault_start": 0, "normal_runs": 1}


def test_shares_are_of_the_mean_and_add_up_to_one():
    records = [incident("e1", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z"),
               incident("e2", "2026-11-13T22:46:00.000Z", "2026-11-13T22:49:51.000Z", stored_s=40.0)]
    summary = latency.summarize(latency.runs([manifest("e1"), manifest("e2")], records))
    assert summary["detected"] == 2
    assert summary["steps_s"]["window"]["mean"] == 47.0 and summary["steps_s"]["pipeline_wait"]["mean"] == 170.0
    assert sum(summary["share_of_mean"].values()) == pytest.approx(1.0, abs=0.002)


def test_the_test_set_and_the_stress_slice_are_summarized_apart():
    manifests = [manifest("t"), manifest("s", phase="stress")]
    records = [incident("t", "2026-11-13T22:45:00.000Z", "2026-11-13T22:48:49.000Z")]
    result = latency.build(manifests, records)
    assert result["summary"]["test"]["detected"] == 1 and result["summary"]["stress"]["missed"] == ["s"]


@pytest.mark.skipif(not (latency.RECORDS.exists() and latency.MANIFESTS.exists()), reason="needs the exported data")
def test_on_the_real_data_every_record_s_own_wait_matches_the_chain_s():
    records = json.loads(latency.RECORDS.read_text())
    rows = latency.runs(json.loads(latency.MANIFESTS.read_text()), records)
    by_id = {r["incident_id"]: r for r in records}
    detected = [r for r in rows if r["outcome"] == "detected"]
    assert len(rows) == 370 and detected
    test = latency.summarize([r for r in rows if r["phase"] == "test"])
    # M9-5 (Docs/GENAI_EVALUATION.md): 285 of the 290 fault runs opened an incident, 8 on healthy runs.
    assert test["fault_runs"] == 290 and test["detected"] + len(test["early"]) == 285 and len(test["missed"]) == 5
    assert test["false_incidents"]["normal_runs"] == 8
    for row in detected:
        own = by_id[row["incident_id"]]["latency"]["window_end_to_diagnosis_s"]
        assert row["steps"]["pipeline_wait"] == pytest.approx(own, abs=0.01)
        assert row["steps"]["window"] > 0
