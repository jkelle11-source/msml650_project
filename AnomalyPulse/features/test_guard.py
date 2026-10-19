"""Tests for the Feature/Label Boundary guard (M4-5)."""
import json

import pytest

from features import guard

TIER2 = ["error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"]

# A realistic Tier-1 feature matrix header. All of these must be allowed.
TIER1_COLUMNS = [
    "request_rate", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
    "error_rate_5xx", "error_rate_4xx", "lambda_duration_ms", "cold_start_count",
    "db_consumed_capacity", "throttled_requests", "read_throttle_events",
    "write_throttle_events", "dependency_latency_ms", "dependency_error_count",
]


def test_tier2_names_come_from_the_catalog():
    assert guard.tier2_names() == set(TIER2)


def test_clean_tier1_matrix_passes():
    guard.check_columns(TIER1_COLUMNS)  # must not raise


@pytest.mark.parametrize("name", TIER2)
def test_each_tier2_name_fails_the_build(name):
    # The deliberate injection: a clean matrix plus one Tier-2 column.
    with pytest.raises(guard.BoundaryViolation) as err:
        guard.check_columns(TIER1_COLUMNS + [name])
    assert name in str(err.value)


@pytest.mark.parametrize("name", ["experiment_id", "slot", "phase", "label", "timeout_ms",
                                  "load_ratio", "retries_per_failure", "injector", "label_from"])
def test_join_keys_labels_and_fault_params_fail(name):
    with pytest.raises(guard.BoundaryViolation):
        guard.check_columns(TIER1_COLUMNS + [name])


def test_every_injector_parameter_is_forbidden():
    # From the registry, so a new injector's parameter is covered without editing the guard.
    from simulator import injectors
    params = {injectors.get(n).parameter for n in injectors.names()} - {None}
    assert {"multiplier", "load_ratio", "added_latency_ms", "timeout_ms", "retries_per_failure"} <= params
    assert params <= guard.forbidden_names()


@pytest.mark.parametrize("name", ["product_db_throttled", "order_error_type_count", "payment_timeout_ms_p95",
                                  "product_load_ratio_change", "cart_severity", "order_incident_type_roll_mean_3"])
def test_a_forbidden_name_inside_a_column_name_fails(name):
    # Real columns are named <service>_<feature>[_<temporal>], so a leak would be too.
    with pytest.raises(guard.BoundaryViolation):
        guard.check_columns(TIER1_COLUMNS + [name])


def test_match_ignores_case():
    with pytest.raises(guard.BoundaryViolation):
        guard.check_columns(["Incident_Type"])


def test_throttled_requests_is_allowed_but_db_throttled_is_not():
    # The trap: the observable CloudWatch count is a legitimate feature,
    # the Tier-2 boolean is not. Whole words, never a substring search.
    guard.check_columns(["throttled_requests", "product_throttled_requests", "product_read_throttle_events"])
    with pytest.raises(guard.BoundaryViolation):
        guard.check_columns(["db_throttled"])
    with pytest.raises(guard.BoundaryViolation):
        guard.check_columns(["product_db_throttled"])


def test_part_of_a_word_is_not_a_match():
    # "labeled" is not "label", "phases" is not "phase": only whole words between underscores count.
    guard.check_columns(["product_labeled_share", "order_phases_count"])


def test_all_bad_columns_are_reported():
    with pytest.raises(guard.BoundaryViolation) as err:
        guard.check_columns(["severity", "request_rate", "error_type"])
    assert "severity" in str(err.value) and "error_type" in str(err.value)
    assert "request_rate" not in str(err.value)


def test_catalog_with_no_tier2_fields_is_refused(tmp_path):
    # An empty list would let everything pass silently, so the guard stops.
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"fields": {"latency_ms": {"tier": "tier1_observable"}}}))
    with pytest.raises(ValueError):
        guard.check_columns(["request_rate"], catalog_path=path)