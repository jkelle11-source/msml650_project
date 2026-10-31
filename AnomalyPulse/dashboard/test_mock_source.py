"""Tests for the dashboard's mock source (M7-2): each scenario, placed at the current minute."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import builder
import mock_source

NOW = datetime(2026, 10, 30, 14, 34, 5, 123456, tzinfo=timezone.utc)
CONTRACT = Path(__file__).resolve().parents[2] / "Docs" / "DASHBOARD_CONTRACT.md"


def dashboard(scenario, now=NOW):
    return builder.build_dashboard(*mock_source.read(scenario, now), now)


def approx_floats(value):
    """The value with every float compared to 3 decimal places, as the contract's example rounds them."""
    if isinstance(value, dict):
        return {k: approx_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [approx_floats(v) for v in value]
    return pytest.approx(value, abs=0.001) if isinstance(value, float) else value


# ---- read: placing the scenario in time ------------------------

def test_the_scenarios_are_the_contract_s_six():
    assert set(mock_source.SCENARIOS) == {"healthy", "diagnosing", "incident", "cascade_early", "no_ground_truth",
                                          "no_data"}


def test_the_window_is_the_latest_complete_minute():
    # At 14:34:05.123 the minute under way is 14:34; the last one to finish began at 14:33:00.000.
    assert mock_source.read("healthy", NOW)[0]["window_start"] == "2026-10-30T14:33:00.000Z"


def test_on_the_minute_the_window_is_the_one_just_finished():
    now = datetime(2026, 10, 30, 14, 34, tzinfo=timezone.utc)
    assert mock_source.read("healthy", now)[0]["window_start"] == "2026-10-30T14:33:00.000Z"


def test_every_time_is_placed_from_that_minute():
    _, incident, manifest = mock_source.read("incident", NOW)
    assert incident["prediction"]["started_at"] == "2026-10-30T14:32:00.000Z"
    assert incident["prediction"]["detected_at"] == "2026-10-30T14:33:04.000Z"
    assert manifest["activations"][0]["active_from"] == "2026-10-30T14:31:55.000Z"
    assert manifest["activations"][0]["active_until"] == "2026-10-30T14:36:55.000Z"


def test_the_scenarios_move_with_the_clock_across_midnight():
    # Fixed times would go stale and read as NO_DATA. Just after midnight the latest minute is yesterday's last.
    later = datetime(2026, 11, 1, 0, 0, 30, tzinfo=timezone.utc)
    assert mock_source.read("healthy", later)[0]["window_start"] == "2026-10-31T23:59:00.000Z"


def test_no_data_s_window_is_ten_minutes_old():
    assert mock_source.read("no_data", NOW)[0]["window_start"] == "2026-10-30T14:23:00.000Z"


def test_an_unknown_scenario_is_refused_with_the_valid_names():
    with pytest.raises(ValueError, match="cascade_early"):
        mock_source.read("nope", NOW)


# ---- each scenario through the builder -------------------------

@pytest.mark.parametrize("scenario, status, services", [
    ("healthy", "HEALTHY", ["HEALTHY"] * 4),
    ("diagnosing", "INCIDENT", ["HEALTHY", "HEALTHY", "DEGRADED", "DEGRADED"]),
    ("incident", "INCIDENT", ["HEALTHY", "HEALTHY", "DEGRADED", "DEGRADED"]),
    ("cascade_early", "INCIDENT", ["DEGRADED", "DEGRADED", "DEGRADED", "HEALTHY"]),
    ("no_ground_truth", "INCIDENT", ["HEALTHY", "HEALTHY", "DEGRADED", "DEGRADED"]),
    ("no_data", "NO_DATA", ["NO_DATA"] * 4),
])
def test_each_scenario_shows_its_state(scenario, status, services):
    health = dashboard(scenario)["health"]
    assert health["status"] == status
    assert [row["status"] for row in health["services"]] == services


def test_diagnosing_has_ground_truth_but_no_diagnosis_yet():
    incident = dashboard("diagnosing")["incident"]
    assert incident["diagnosis"] is None
    assert incident["ground_truth"] is not None


def test_no_ground_truth_is_diagnosed_with_nothing_injected():
    incident = dashboard("no_ground_truth")["incident"]
    assert incident["diagnosis"] is not None
    assert (incident["ground_truth"], incident["comparison"]) == (None, None)


def test_cascade_early_has_the_model_right_about_the_first_fault():
    incident = dashboard("cascade_early")["incident"]
    assert incident["prediction"]["incident_type"] == "TRAFFIC_SPIKE"
    assert incident["ground_truth"]["incident_type"] == "CASCADING_FAILURE"
    assert [(f["injector"], f["active_now"]) for f in incident["ground_truth"]["faults"]] == [
        ("traffic-spike", True), ("payment-failure", False)]


def test_no_data_shows_no_numbers():
    health = dashboard("no_data")["health"]
    assert health["window_start"] is None
    assert (health["requests_per_min"], health["error_rate"], health["p95_latency_ms"]) == (None, None, None)


def test_the_incident_scenario_is_the_contract_s_example():
    # The example is this scenario at 14:34:05. Its numbers are rounded to 3 places, and the mock's ids are its own.
    example = json.loads(re.search(r"## Example.*?```json\n(.*?)```", CONTRACT.read_text(), re.S).group(1))
    reply = dashboard("incident", datetime(2026, 10, 30, 14, 34, 5, tzinfo=timezone.utc))
    reply["incident"]["incident_id"] = example["incident"]["incident_id"]
    reply["incident"]["ground_truth"]["experiment_id"] = example["incident"]["ground_truth"]["experiment_id"]
    assert reply == approx_floats(example)
