"""The open/close rule (incident.py) on minute sequences shaped like PIPELINE.md's measurements."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import incident


def minute(m, anomalous, incident_type=None, confidence=None):
    return {"window_start": f"2026-11-06T14:{m:02d}:00.000Z", "anomalous": anomalous,
            "incident_type": incident_type, "confidence": confidence}


def run(minutes, state=None):
    changes = []
    for p in minutes:
        state, c = incident.step(state, p)
        changes.append(c)
    return state, changes


def test_a_traffic_spike_opens_and_closes_through_its_recovery_tail():
    # healthy; 3 spike minutes; the detector flags 3 more (the classifier names the first); healthy.
    state, changes = run([minute(0, False),
                          minute(1, True, "TRAFFIC_SPIKE", 0.91), minute(2, True, "TRAFFIC_SPIKE", 0.99),
                          minute(3, True, "TRAFFIC_SPIKE", 0.98),
                          minute(4, True, "TRAFFIC_SPIKE", 0.70), minute(5, True), minute(6, True),
                          minute(7, False)])
    assert changes == [[], ["opened"], [], [], [], [], ["closed"], []]
    assert state["status"] == "CLOSED"
    assert (state["started_at"], state["ended_at"]) == ("2026-11-06T14:01:00.000Z", "2026-11-06T14:05:00.000Z")
    assert (state["incident_type"], state["confidence"]) == ("TRAFFIC_SPIKE", 0.70)


def test_a_cascade_changes_type_once_two_faults_overlap():
    state, changes = run([minute(1, True, "DATABASE_THROTTLING", 0.8), minute(2, True, "CASCADING_FAILURE", 0.9),
                          minute(3, True, "CASCADING_FAILURE", 0.95)])
    assert changes == [["opened"], ["type_changed"], []]
    assert state["status"] == "OPEN" and state["started_at"] == "2026-11-06T14:01:00.000Z"


def test_one_quiet_minute_inside_a_fault_doesnt_close_it():
    state, changes = run([minute(1, True, "LAMBDA_DEGRADATION", 0.9), minute(2, True),
                          minute(3, True, "LAMBDA_DEGRADATION", 0.9)])
    assert changes == [["opened"], [], []] and state["status"] == "OPEN" and state["quiet"] == 0


def test_flagged_minutes_the_classifier_calls_normal_open_nothing():
    # After idle time: the detector flags the first full minute, the classifier says NORMAL.
    state, changes = run([minute(0, True), minute(1, True), minute(2, False)])
    assert changes == [[], [], []] and state["status"] == "CLOSED" and "started_at" not in state


def test_a_classified_minute_the_detector_clears_counts_for_nothing():
    state, changes = run([minute(0, False, "TRAFFIC_SPIKE", 0.6)])
    assert changes == [[]] and state["status"] == "CLOSED"


def test_a_gap_closes_the_incident_at_its_last_counted_minute():
    state, changes = run([minute(1, True, "DEPENDENCY_FAILURE", 0.9), minute(9, False)])
    assert changes == [["opened"], ["closed"]]
    assert state["ended_at"] == "2026-11-06T14:02:00.000Z"


def test_after_a_gap_the_same_window_can_open_the_next_incident():
    state, changes = run([minute(1, True, "DEPENDENCY_FAILURE", 0.9), minute(9, True, "TRAFFIC_SPIKE", 0.9)])
    assert changes == [["opened"], ["closed", "opened"]]
    assert state["started_at"] == "2026-11-06T14:09:00.000Z" and state["incident_type"] == "TRAFFIC_SPIKE"


def test_repeats_and_stragglers_are_ignored():
    opened, _ = run([minute(1, True, "TRAFFIC_SPIKE", 0.9), minute(2, True, "TRAFFIC_SPIKE", 0.9)])
    again, changes = run([minute(2, True, "TRAFFIC_SPIKE", 0.9), minute(1, False)], opened)
    assert changes == [[], []] and again == opened


def test_a_quiet_minute_delivered_twice_counts_once():
    # EventBridge delivers at least once: the repeat of 14:02 must not be a second quiet minute.
    state, changes = run([minute(1, True, "TRAFFIC_SPIKE", 0.9), minute(2, False), minute(2, False)])
    assert changes == [["opened"], [], []] and state["status"] == "OPEN" and state["quiet"] == 1
    state, changes = run([minute(3, False)], state)
    assert changes == [["closed"]] and state["ended_at"] == "2026-11-06T14:02:00.000Z"


def test_a_straggler_after_the_close_opens_nothing():
    # Closed at 14:03; 14:05 is applied; 14:04 then arrives late and is ignored, though it counts.
    closed, _ = run([minute(1, True, "TRAFFIC_SPIKE", 0.9), minute(2, False), minute(3, False),
                     minute(5, False)])
    state, changes = run([minute(4, True, "TRAFFIC_SPIKE", 0.9)], closed)
    assert changes == [[]] and state == closed and state["last_window"] == "2026-11-06T14:05:00.000Z"


def test_a_type_named_only_as_the_fault_lets_go_doesnt_replace_its_type():
    # Validation's dependency failures: 5 minutes DEPENDENCY_FAILURE, then one LAMBDA_DEGRADATION.
    state, changes = run([minute(m, True, "DEPENDENCY_FAILURE", c) for m, c in
                          [(1, 0.9), (2, 0.95), (3, 1.0), (4, 1.0), (5, 0.97)]]
                         + [minute(6, True, "LAMBDA_DEGRADATION", 0.84), minute(7, False), minute(8, False)])
    assert changes == [["opened"], [], [], [], [], [], [], ["closed"]]
    assert (state["incident_type"], state["confidence"]) == ("DEPENDENCY_FAILURE", 0.97)
    assert state["ended_at"] == "2026-11-06T14:07:00.000Z"      # the LAMBDA minute still counted


def test_a_cascade_becomes_cascading_once_it_has_as_many_minutes():
    # 3 minutes of the first fault, then CASCADING_FAILURE: a tie at 3-3 goes to the newer type.
    state, changes = run([minute(m, True, "DATABASE_THROTTLING", 0.9) for m in (1, 2, 3)]
                         + [minute(m, True, "CASCADING_FAILURE", c) for m, c in [(4, 0.8), (5, 0.9), (6, 0.99)]])
    assert changes == [["opened"], [], [], [], [], ["type_changed"]]
    assert (state["incident_type"], state["confidence"]) == ("CASCADING_FAILURE", 0.99)


def test_the_confidence_is_the_reported_types_latest():
    state, _ = run([minute(1, True, "LAMBDA_DEGRADATION", 0.9), minute(2, True, "LAMBDA_DEGRADATION", 0.95),
                    minute(3, True, "TRAFFIC_SPIKE", 0.6)])
    assert (state["incident_type"], state["confidence"]) == ("LAMBDA_DEGRADATION", 0.95)
    assert state["tally"]["TRAFFIC_SPIKE"] == {"minutes": 1, "last": "2026-11-06T14:03:00.000Z", "confidence": 0.6}
