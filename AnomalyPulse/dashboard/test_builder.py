"""Tests for the dashboard read API's contract builder (M7-2)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import builder

SERVICES = ("product-service", "cart-service", "order-service", "payment-service")

# One window as the feature files hold it, reduced to the columns service_rows reads. Cart had no
# requests, so its error rate and p95 are empty (None), as the features write them.
WINDOW = {"window_start": "2026-10-30T14:33:00.000Z",
          "product_request_rate": 15.0, "product_error_rate_5xx": 0.0, "product_p95_latency_ms": 41.0,
          "cart_request_rate": 0.0, "cart_error_rate_5xx": None, "cart_p95_latency_ms": None,
          "order_request_rate": 123 / 60, "order_error_rate_5xx": 30 / 123, "order_p95_latency_ms": 2950.0,
          "payment_request_rate": 1.0, "payment_error_rate_5xx": 0.5, "payment_p95_latency_ms": 2900.0}


def test_service_rows_reads_each_service_from_its_own_columns():
    assert builder.service_rows(WINDOW) == [
        {"service": "product-service", "requests_per_min": 900, "error_rate": 0.0, "p95_latency_ms": 41.0},
        {"service": "cart-service", "requests_per_min": 0, "error_rate": None, "p95_latency_ms": None},
        {"service": "order-service", "requests_per_min": 123, "error_rate": 30 / 123, "p95_latency_ms": 2950.0},
        {"service": "payment-service", "requests_per_min": 60, "error_rate": 0.5, "p95_latency_ms": 2900.0},
    ]


def test_requests_per_min_is_an_int():
    # 123.0 == 123 in Python, so the comparison above can't tell a float from an int.
    counts = [row["requests_per_min"] for row in builder.service_rows(WINDOW)]
    assert [type(c) for c in counts] == [int, int, int, int]


def test_every_count_comes_back_from_its_rate():
    # A feature's rate is count / 60. For 123 requests that is 2.05, and 2.05 * 60 is
    # 122.99999999999999, so truncating gives 122; 47 counts under 3,000 fall short like that.
    for n in range(3001):
        window = {**WINDOW, "order_request_rate": n / 60}
        assert builder.service_rows(window)[2]["requests_per_min"] == n


def test_no_window_gives_four_named_rows_with_no_numbers():
    assert builder.service_rows(None) == [
        {"service": s, "requests_per_min": None, "error_rate": None, "p95_latency_ms": None} for s in SERVICES]


def test_a_missing_column_is_refused():
    window = {k: v for k, v in WINDOW.items() if k != "payment_p95_latency_ms"}
    with pytest.raises(KeyError, match="payment_p95_latency_ms"):
        builder.service_rows(window)


def test_an_empty_window_is_refused_not_read_as_no_window():
    # None means there is no recent window; {} is a window with every column missing.
    with pytest.raises(KeyError):
        builder.service_rows({})


# ---- system_kpis ----------------------------------------------

BUSY = {**WINDOW, "system_p95_latency_ms": 260.0}
EXTERNAL_PREFIXES = ("product", "cart", "order")


def test_system_kpis_count_product_cart_and_order_and_weight_their_error_rates():
    # 900 + 0 + 123 requests; 900 * 0.0 + 123 * (30/123) = 30 errors. Cart had none, so its
    # empty error rate adds nothing. A plain mean of the rates would be (0 + 30/123) / 2 = 0.122.
    kpis = builder.system_kpis(BUSY)
    assert kpis == {"requests_per_min": 1023, "error_rate": pytest.approx(30 / 1023), "p95_latency_ms": 260.0}
    assert type(kpis["requests_per_min"]) is int


def test_system_kpis_never_count_payment():
    # Payment is reached only through Order, so its requests and errors are already in Order's.
    louder = {**BUSY, "payment_request_rate": 50.0, "payment_error_rate_5xx": 1.0, "payment_p95_latency_ms": 9000.0}
    assert builder.system_kpis(louder) == builder.system_kpis(BUSY)


def test_system_p95_comes_from_its_own_column_not_the_services_p95s():
    # Percentiles don't combine: the services' p95s (41, 2950) say nothing about the system's.
    assert builder.system_kpis({**BUSY, "system_p95_latency_ms": 1234.5})["p95_latency_ms"] == 1234.5


def test_system_kpis_with_no_external_requests_have_no_error_rate():
    idle = {**BUSY, "system_p95_latency_ms": None,
            **{f"{p}_request_rate": 0.0 for p in EXTERNAL_PREFIXES},
            **{f"{p}_error_rate_5xx": None for p in EXTERNAL_PREFIXES},
            **{f"{p}_p95_latency_ms": None for p in EXTERNAL_PREFIXES}}
    assert builder.system_kpis(idle) == {"requests_per_min": 0, "error_rate": None, "p95_latency_ms": None}


def test_system_kpis_with_no_window_are_all_none():
    assert builder.system_kpis(None) == {"requests_per_min": None, "error_rate": None, "p95_latency_ms": None}


def test_system_kpis_refuse_a_window_without_the_system_p95():
    with pytest.raises(KeyError, match="system_p95_latency_ms"):
        builder.system_kpis(dict(WINDOW))


# ---- health_status --------------------------------------------

# WINDOW started at 14:33:00 UTC. The tests build their times by hand rather than parsing them,
# so they don't share the code's parsing.
INCIDENT = {"incident_id": "inc-1"}  # only whether there is one matters here


def at(hour, minute, second=0, ms=0, day=30):
    return datetime(2026, 10, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc)


def test_a_fresh_window_is_healthy_or_incident():
    # 2 minutes old. The real clock is long past 14:35 on Oct 30, so code that read it would say NO_DATA.
    assert builder.health_status(WINDOW, None, at(14, 35)) == "HEALTHY"
    assert builder.health_status(WINDOW, INCIDENT, at(14, 35)) == "INCIDENT"


def test_a_window_exactly_three_minutes_old_is_still_fresh():
    # "More than 3 minutes" is stale, so 3 minutes on the dot is not.
    assert builder.health_status(WINDOW, INCIDENT, at(14, 36)) == "INCIDENT"


def test_a_window_over_three_minutes_old_is_no_data_even_during_an_incident():
    # The pipeline has stopped: the page must say so, not show the last minute as current.
    assert builder.health_status(WINDOW, INCIDENT, at(14, 36, 1)) == "NO_DATA"
    assert builder.health_status(WINDOW, None, at(14, 36, 0, ms=1)) == "NO_DATA"


def test_a_window_a_day_old_is_no_data():
    # 1 day and 10 s old. timedelta.seconds drops the day and gives 10.
    assert builder.health_status(WINDOW, None, at(14, 33, 10, day=31)) == "NO_DATA"


def test_no_window_is_no_data_even_during_an_incident():
    assert builder.health_status(None, INCIDENT, at(14, 35)) == "NO_DATA"


def test_health_status_refuses_an_empty_window():
    # As in service_rows: None is no window; {} is a window missing its columns.
    with pytest.raises(KeyError, match="window_start"):
        builder.health_status({}, None, at(14, 35))


# ---- service_status -------------------------------------------

def affecting(*services):
    return {"incident_id": "inc-1", "prediction": {"affected_services": list(services)}}


def statuses(health, incident, window=WINDOW):
    return [builder.service_status(row, health, incident) for row in builder.service_rows(window)]


def test_with_no_incident_each_service_is_healthy_or_idle():
    # In WINDOW, Cart had no requests; Product, Order and Payment had 900, 123 and 60.
    assert statuses("HEALTHY", None) == ["HEALTHY", "IDLE", "HEALTHY", "HEALTHY"]


def test_the_services_the_incident_affects_are_degraded():
    assert statuses("INCIDENT", affecting("order-service", "payment-service")) == [
        "HEALTHY", "IDLE", "DEGRADED", "DEGRADED"]


def test_an_affected_service_with_no_requests_is_degraded_not_idle():
    # If Payment fails so badly that Order stops reaching it, a calm IDLE would hide it.
    assert statuses("INCIDENT", affecting("cart-service", "order-service")) == [
        "HEALTHY", "DEGRADED", "DEGRADED", "HEALTHY"]


def test_every_service_is_no_data_when_the_system_is():
    # A stale window still has numbers, and the incident record stays open when the pipeline stops.
    assert statuses("NO_DATA", affecting("order-service", "payment-service")) == ["NO_DATA"] * 4


def test_no_window_gives_every_service_no_data():
    assert statuses("NO_DATA", None, window=None) == ["NO_DATA"] * 4


# ---- fault_rows -----------------------------------------------

# A cascade as its manifest records it: a traffic spike, then a payment failure. Activations carry
# more than the panel shows (fault_id, intensity, band, params, observed); those must not reach it.
CASCADE = {"experiment_id": "exp-20261030T143055Z-3f9a", "status": "RUNNING",
           "incident_type": "CASCADING_FAILURE", "severity": "HIGH",
           "activations": [
               {"fault_id": "exp-20261030T143055Z-3f9a#0", "injector": "traffic-spike", "target": None,
                "intensity": 0.8, "band": None, "params": {"multiplier": 3}, "observed": None,
                "active_from": "2026-10-30T14:31:55.000Z", "active_until": "2026-10-30T14:36:55.000Z"},
               {"fault_id": "exp-20261030T143055Z-3f9a#1", "injector": "payment-failure",
                "target": "payment-service", "intensity": 0.5, "band": None, "params": {"failure_rate": 0.5},
                "observed": None,
                "active_from": "2026-10-30T14:33:30.000Z", "active_until": "2026-10-30T14:38:30.000Z"},
           ]}


def test_fault_rows_copy_four_fields_of_each_activation_in_manifest_order():
    # At 14:33 the spike covers the whole minute and the payment failure exactly half of it.
    assert builder.fault_rows(CASCADE, "2026-10-30T14:33:00.000Z") == [
        {"injector": "traffic-spike", "target": None, "active_from": "2026-10-30T14:31:55.000Z",
         "active_until": "2026-10-30T14:36:55.000Z", "active_now": True},
        {"injector": "payment-failure", "target": "payment-service", "active_from": "2026-10-30T14:33:30.000Z",
         "active_until": "2026-10-30T14:38:30.000Z", "active_now": True},
    ]


@pytest.mark.parametrize("minute, spike, payment", [
    ("14:31", False, False),  # spike 5 s (14:31:55-14:32:00); payment not started
    ("14:32", True, False),   # spike 60 s; payment starts 90 s after the minute does
    ("14:33", True, True),    # payment exactly 30 s (14:33:30-14:34:00)
    ("14:36", True, True),    # spike 55 s (to 14:36:55); payment 60 s
    ("14:37", False, True),   # spike ended 5 s before the minute began
    ("14:38", False, True),   # payment exactly 30 s (14:38:00-14:38:30)
    ("14:39", False, False),  # both over
])
def test_active_now_needs_30_seconds_of_the_minute(minute, spike, payment):
    rows = builder.fault_rows(CASCADE, f"2026-10-30T{minute}:00.000Z")
    assert [row["active_now"] for row in rows] == [spike, payment]
    assert all(type(row["active_now"]) is bool for row in rows)


def test_active_now_needs_the_full_30_seconds():
    # 14:33:30.001-14:34:00 is 29.999 s: under half the minute.
    fault = {**CASCADE["activations"][1], "active_from": "2026-10-30T14:33:30.001Z"}
    rows = builder.fault_rows({**CASCADE, "activations": [fault]}, "2026-10-30T14:33:00.000Z")
    assert rows[0]["active_now"] is False


def test_with_no_window_active_now_is_none():
    rows = builder.fault_rows(CASCADE, None)
    assert [row["injector"] for row in rows] == ["traffic-spike", "payment-failure"]
    assert [row["active_now"] for row in rows] == [None, None]


def test_every_injector_is_judged_by_its_written_times():
    # The labeling policy counts db-throttling only from observed.label_from, after the surge; the panel
    # shows what was injected, from active_from. The policy never counts retry-amplification, but it is listed.
    manifest = {**CASCADE, "activations": [
        {"fault_id": "x#0", "injector": "db-throttling", "target": "product-service", "intensity": 0.6,
         "band": None, "params": {}, "observed": {"label_from": "2026-10-30T14:35:10.000Z"},
         "active_from": "2026-10-30T14:33:00.000Z", "active_until": "2026-10-30T14:40:00.000Z"},
        {"fault_id": "x#1", "injector": "retry-amplification", "target": "order-service", "intensity": 0.4,
         "band": None, "params": {}, "observed": None,
         "active_from": "2026-10-30T14:33:00.000Z", "active_until": "2026-10-30T14:40:00.000Z"}]}
    rows = builder.fault_rows(manifest, "2026-10-30T14:33:00.000Z")
    assert [(row["injector"], row["active_now"]) for row in rows] == [
        ("db-throttling", True), ("retry-amplification", True)]


def test_a_run_with_no_faults_has_no_fault_rows():
    # A NORMAL run: an alarm during it has ground truth, with nothing injected.
    assert builder.fault_rows({**CASCADE, "incident_type": "NORMAL", "severity": None, "activations": []},
                              "2026-10-30T14:33:00.000Z") == []


# ---- detection_latency_s --------------------------------------

def test_detection_latency_runs_from_the_earliest_fault():
    # 14:33:04 - 14:31:55 (the spike) = 69 s, the contract's example.
    latency = builder.detection_latency_s("2026-10-30T14:33:04.000Z", CASCADE)
    assert latency == 69.0
    assert type(latency) is float


def test_detection_latency_uses_the_earliest_fault_not_the_first_listed():
    reordered = {**CASCADE, "activations": CASCADE["activations"][::-1]}
    assert builder.detection_latency_s("2026-10-30T14:33:04.000Z", reordered) == 69.0


def test_detection_latency_keeps_milliseconds():
    # 14:33:04.250 - 14:31:55.000 = 69.25 s. timedelta.seconds would give 69.
    assert builder.detection_latency_s("2026-10-30T14:33:04.250Z", CASCADE) == 69.25


def test_an_alarm_at_the_moment_the_first_fault_began_is_zero():
    latency = builder.detection_latency_s("2026-10-30T14:31:55.000Z", CASCADE)
    assert latency == 0.0
    assert type(latency) is float


def test_an_alarm_before_any_fault_has_no_latency():
    # 5 s early. timedelta(seconds=-5).seconds is 86395.
    assert builder.detection_latency_s("2026-10-30T14:31:50.000Z", CASCADE) is None


def test_a_run_with_no_faults_has_no_latency():
    assert builder.detection_latency_s("2026-10-30T14:33:04.000Z", {**CASCADE, "activations": []}) is None


# ---- build_dashboard ------------------------------------------

# The incident source's record: an alarm on BUSY's minute that Bedrock hasn't answered yet.
DETECTED = {"incident_id": "inc-20261030T143200Z",
            "prediction": {"incident_type": "CASCADING_FAILURE", "confidence": 0.93,
                           "started_at": "2026-10-30T14:32:00.000Z", "detected_at": "2026-10-30T14:33:04.000Z",
                           "affected_services": ["order-service", "payment-service"],
                           "model": {"estimator": "logistic_regression", "job": "anomalypulse-hpo-1"}},
            "diagnosis": None}
DIAGNOSIS = {"severity": "MEDIUM", "summary": "Order failures rose.", "root_cause": "Payment dependency timeout",
             "evidence": ["Order 5xx share rose to 24%."], "recommended_actions": ["Inspect payment service health"]}


def no_data_services():
    return [{"service": s, "status": "NO_DATA", "requests_per_min": None, "error_rate": None, "p95_latency_ms": None}
            for s in SERVICES]


def cascade_faults(spike, payment):
    return [{"injector": "traffic-spike", "target": None, "active_from": "2026-10-30T14:31:55.000Z",
             "active_until": "2026-10-30T14:36:55.000Z", "active_now": spike},
            {"injector": "payment-failure", "target": "payment-service", "active_from": "2026-10-30T14:33:30.000Z",
             "active_until": "2026-10-30T14:38:30.000Z", "active_now": payment}]


def test_a_minute_with_an_incident_and_its_experiment():
    assert builder.build_dashboard(BUSY, DETECTED, CASCADE, at(14, 34, 5)) == {
        "contract_version": 1,
        "generated_at": "2026-10-30T14:34:05.000Z",
        "health": {
            "window_start": "2026-10-30T14:33:00.000Z", "status": "INCIDENT",
            "requests_per_min": 1023, "error_rate": pytest.approx(30 / 1023), "p95_latency_ms": 260.0,
            "active_incidents": 1,
            "services": [
                {"service": "product-service", "status": "HEALTHY", "requests_per_min": 900, "error_rate": 0.0,
                 "p95_latency_ms": 41.0},
                {"service": "cart-service", "status": "IDLE", "requests_per_min": 0, "error_rate": None,
                 "p95_latency_ms": None},
                {"service": "order-service", "status": "DEGRADED", "requests_per_min": 123, "error_rate": 30 / 123,
                 "p95_latency_ms": 2950.0},
                {"service": "payment-service", "status": "DEGRADED", "requests_per_min": 60, "error_rate": 0.5,
                 "p95_latency_ms": 2900.0}]},
        "incident": {
            "incident_id": "inc-20261030T143200Z",
            "prediction": DETECTED["prediction"],
            "diagnosis": None,
            "ground_truth": {"experiment_id": "exp-20261030T143055Z-3f9a", "incident_type": "CASCADING_FAILURE",
                             "severity": "HIGH", "faults": cascade_faults(True, True)},
            "comparison": {"detection_latency_s": 69.0}}}


def test_a_stale_window_shows_no_numbers_but_keeps_the_incident():
    # 3 min 1 s old. The window still holds numbers; none may reach the page as if current.
    assert builder.build_dashboard(BUSY, DETECTED, CASCADE, at(14, 36, 1)) == {
        "contract_version": 1,
        "generated_at": "2026-10-30T14:36:01.000Z",
        "health": {"window_start": None, "status": "NO_DATA",
                   "requests_per_min": None, "error_rate": None, "p95_latency_ms": None,
                   "active_incidents": 1, "services": no_data_services()},
        "incident": {
            "incident_id": "inc-20261030T143200Z",
            "prediction": DETECTED["prediction"],
            "diagnosis": None,
            "ground_truth": {"experiment_id": "exp-20261030T143055Z-3f9a", "incident_type": "CASCADING_FAILURE",
                             "severity": "HIGH", "faults": cascade_faults(None, None)},
            "comparison": {"detection_latency_s": 69.0}}}


def test_no_window_and_no_incident():
    assert builder.build_dashboard(None, None, None, at(14, 34, 5)) == {
        "contract_version": 1,
        "generated_at": "2026-10-30T14:34:05.000Z",
        "health": {"window_start": None, "status": "NO_DATA",
                   "requests_per_min": None, "error_rate": None, "p95_latency_ms": None,
                   "active_incidents": 0, "services": no_data_services()},
        "incident": None}


def test_a_healthy_minute_has_no_incident_even_while_an_experiment_runs():
    # Before the model raises an alarm there is nothing to put ground truth beside.
    reply = builder.build_dashboard(BUSY, None, CASCADE, at(14, 34, 5))
    assert reply["health"]["status"] == "HEALTHY"
    assert reply["health"]["active_incidents"] == 0
    assert [row["status"] for row in reply["health"]["services"]] == ["HEALTHY", "IDLE", "HEALTHY", "HEALTHY"]
    assert reply["incident"] is None


def test_a_real_incident_has_no_ground_truth_or_comparison():
    # No manifest, as in production. The diagnosis passes through unchanged.
    diagnosed = {**DETECTED, "diagnosis": DIAGNOSIS}
    incident = builder.build_dashboard(BUSY, diagnosed, None, at(14, 34, 5))["incident"]
    assert incident == {"incident_id": "inc-20261030T143200Z", "prediction": DETECTED["prediction"],
                        "diagnosis": DIAGNOSIS, "ground_truth": None, "comparison": None}


def test_an_alarm_before_any_fault_still_has_a_comparison():
    # Ground truth exists, so comparison does; it has no latency to report.
    early = {**DETECTED, "prediction": {**DETECTED["prediction"], "detected_at": "2026-10-30T14:31:50.000Z"}}
    incident = builder.build_dashboard(BUSY, early, CASCADE, at(14, 34, 5))["incident"]
    assert incident["ground_truth"] is not None
    assert incident["comparison"] == {"detection_latency_s": None}


def test_the_incident_record_s_other_keys_are_dropped():
    # M8's Incidents table rows will carry table keys and expiry times; the contract has five keys.
    stored = {**DETECTED, "pk": "INCIDENT#inc-20261030T143200Z", "expires_at": 1793000000}
    incident = builder.build_dashboard(BUSY, stored, CASCADE, at(14, 34, 5))["incident"]
    assert list(incident) == ["incident_id", "prediction", "diagnosis", "ground_truth", "comparison"]


def test_generated_at_has_exactly_milliseconds():
    now = datetime(2026, 10, 30, 14, 34, 5, 123456, tzinfo=timezone.utc)
    assert builder.build_dashboard(BUSY, None, None, now)["generated_at"] == "2026-10-30T14:34:05.123Z"


def test_the_reply_survives_json():
    # What the handler sends is json.dumps of this; a datetime or a tuple would not come back the same.
    reply = builder.build_dashboard(BUSY, DETECTED, CASCADE, at(14, 34, 5))
    assert json.loads(json.dumps(reply)) == reply


# ---- iso -------------------------------------------------------

def test_iso_writes_utc_with_milliseconds_and_z():
    assert builder.iso(datetime(2026, 10, 30, 14, 34, 5, 123456, tzinfo=timezone.utc)) == "2026-10-30T14:34:05.123Z"


def test_iso_refuses_a_time_with_no_zone():
    # It would come out as 2026-10-30T14:34:05.000, with no Z, and parse back as naive.
    with pytest.raises(ValueError, match="no time zone"):
        builder.iso(datetime(2026, 10, 30, 14, 34, 5))


def test_iso_writes_another_zone_s_time_in_utc():
    # 16:34:05 at UTC+2 is 14:34:05 UTC; written as is it would end +02:00, not Z.
    two_hours_east = timezone(timedelta(hours=2))
    assert builder.iso(datetime(2026, 10, 30, 16, 34, 5, tzinfo=two_hours_east)) == "2026-10-30T14:34:05.000Z"
