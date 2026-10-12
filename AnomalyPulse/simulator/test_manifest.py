import calendar
import copy
import json
import random
import re
import sys
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

# simulator/ is a package, so it is imported by name from the directory above
# it. pytest's default import mode already puts that directory on the path;
# this line keeps the import working under any other mode or runner.
sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import manifest as mf

BUCKET = "anomalypulse-telemetry-000000000000-us-east-2"
EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"
KEY = f"experiments/{EXPERIMENT_ID}/manifest.json"
MINUTE = 60_000

# The run in the interface doc's example manifest: it starts at 04:15:00, the fault is
# active from 04:20 to 04:25, and it ends at 04:30. Epoch milliseconds, worked
# out with the stdlib and not with the code under test.
T0 = calendar.timegm((2026, 10, 12, 4, 15, 0)) * 1000

TOP_LEVEL_KEYS = ["schema_version", "experiment_id", "status", "incident_type", "severity",
                  "seed", "created_by", "started_at", "ended_at", "workload", "activations",
                  "telemetry"]
ACTIVATION_KEYS = ["fault_id", "injector", "target", "intensity", "band", "params", "observed",
                   "active_from", "active_until"]


# --- helpers ---------------------------------------------------------------

def _activation(index=0, **overrides):
    fields = {
        "injector": "payment-failure",
        "target": "payment-service",
        "intensity": 0.8,
        "band": {"parameter": "timeout_ms", "low": 2500, "high": 3500},
        "params": {"timeout_ms": 3300},
        "active_from_ms": T0 + 5 * MINUTE,
        "active_until_ms": T0 + 10 * MINUTE,
    }
    fields.update(overrides)
    return mf.build_activation(EXPERIMENT_ID, index, **fields)


def _running(**overrides):
    # The manifest as it is first written, before the run has an end time.
    fields = {
        "experiment_id": EXPERIMENT_ID,
        "incident_type": "DEPENDENCY_FAILURE",
        "seed": 20261012,
        "created_by": "jake",
        "started_at_ms": T0,
        "workload": {"request_rate_rps": 20},
        "activations": [_activation()],
        "bucket": BUCKET,
    }
    fields.update(overrides)
    return mf.build_manifest(**fields)


def _final(status="COMPLETED", ended_at_ms=T0 + 15 * MINUTE, **overrides):
    return mf.finalize(_running(**overrides), status, ended_at_ms)


def _normal(**overrides):
    return _final(incident_type="NORMAL", activations=[], **overrides)


def _errors(manifest):
    valid, errors = mf.validate(manifest)
    assert valid == (errors == [])  # the flag and the list always agree
    return errors


def _steps(where):
    # "activations/0/intensity" -> ["activations", 0, "intensity"]
    return [int(part) if part.isdigit() else part for part in where.split("/")]


def _changed(manifest, where, value):
    # A deep copy of `manifest` with the value at `where` replaced (or added).
    result = copy.deepcopy(manifest)
    *parents, last = _steps(where)
    target = result
    for part in parents:
        target = target[part]
    target[last] = value
    return result


def _without(manifest, where):
    result = copy.deepcopy(manifest)
    *parents, last = _steps(where)
    target = result
    for part in parents:
        target = target[part]
    del target[last]
    return result


def _expect_put(stubber, manifest):
    # Spells the stored bytes out with json directly, so this also pins the
    # on-disk format: UTF-8 JSON, two-space indent, keys in the order built.
    stubber.add_response("put_object", {}, expected_params={
        "Bucket": BUCKET,
        "Key": KEY,
        "Body": json.dumps(manifest, indent=2).encode("utf-8"),
        "ContentType": "application/json",
    })


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def s3():
    # Stubber intercepts every call on a real S3 client. A queued response is
    # validated against the service model and returned without touching the
    # network; a call with nothing queued raises. So no test in this file can
    # reach AWS, even by mistake. The credentials are placeholders.
    client = boto3.client("s3", region_name="us-east-2",
                          aws_access_key_id="testing", aws_secret_access_key="testing")
    with Stubber(client) as stubber:
        yield client, stubber


# --- time: epoch milliseconds <-> telemetry's timestamp format ----------------

def test_to_iso_writes_telemetrys_timestamp_format():
    # UTC, millisecond precision, a literal Z: the same string a telemetry
    # record carries, so M4 can slice raw/ with the manifest's own timestamps.
    assert mf.to_iso(T0) == "2026-10-12T04:15:00.000Z"
    assert mf.to_iso(T0 + 123) == "2026-10-12T04:15:00.123Z"
    assert mf.to_iso(0) == "1970-01-01T00:00:00.000Z"


def test_to_epoch_ms_reads_it_back():
    assert mf.to_epoch_ms("2026-10-12T04:15:00.123Z") == T0 + 123


def test_conversion_round_trips_exactly():
    # The control plane's window (epoch ms) and the manifest's window (ISO) must
    # describe the same instant, to the millisecond. A conversion that goes
    # through a float gets some millisecond values wrong, so check every one in
    # a second and a spread of instants out to the year 2100.
    for ms in range(T0, T0 + 1000):
        assert mf.to_epoch_ms(mf.to_iso(ms)) == ms
    rng = random.Random(1)
    for _ in range(5000):
        ms = rng.randrange(0, 4_102_444_800_000)
        assert mf.to_epoch_ms(mf.to_iso(ms)) == ms


def test_to_epoch_ms_rejects_a_date_that_does_not_exist():
    with pytest.raises(ValueError):
        mf.to_epoch_ms("2026-13-45T04:15:00.000Z")


# --- experiment id, key, severity ---------------------------------------------

def test_experiment_id_carries_the_start_time_and_a_hex_suffix():
    assert re.fullmatch(r"exp-20261012T041500Z-[0-9a-f]{4}", mf.new_experiment_id(T0))


def test_experiment_id_defaults_to_now():
    assert re.fullmatch(r"exp-\d{8}T\d{6}Z-[0-9a-f]{4}", mf.new_experiment_id())


def test_generated_experiment_id_passes_the_schema():
    assert _errors(_normal(experiment_id=mf.new_experiment_id(T0))) == []


def test_experiment_id_does_not_draw_from_the_seeded_rng():
    # The fault params are resolved from a seeded RNG. If making an id consumed
    # from it, the same seed would stop giving the same params.
    random.seed(7)
    before = random.getstate()
    mf.new_experiment_id(T0)
    assert random.getstate() == before


def test_manifest_key_is_under_the_experiments_prefix():
    # The simulator role may only write under experiments/.
    assert mf.manifest_key(EXPERIMENT_ID) == KEY


@pytest.mark.parametrize("intensities, expected", [
    ([], None),            # no activations: a NORMAL run has no severity
    ([0], "LOW"),
    ([0.33], "LOW"),
    ([1 / 3], "MEDIUM"),   # a boundary belongs to the band above it
    ([0.5], "MEDIUM"),
    ([0.66], "MEDIUM"),
    ([2 / 3], "HIGH"),
    ([1], "HIGH"),
    ([0.1, 0.9, 0.4], "HIGH"),  # the highest intensity decides
    ([0.1, 0.4], "MEDIUM"),
])
def test_severity_is_thirds_of_the_highest_intensity(intensities, expected):
    assert mf.severity_for(intensities) == expected


def test_severity_for_accepts_a_generator():
    assert mf.severity_for(value for value in [0.2, 0.7]) == "HIGH"


def test_severity_is_relative_to_the_band_not_the_native_value():
    # Intensity is a position within the band it was drawn from, so severity
    # does not order runs across bands: 6.2x traffic at the bottom of the 6-8
    # band is LOW while 3.8x near the top of the 2-4 band is HIGH. Read `params`
    # to compare magnitudes.
    bottom_of_high_band = _activation(
        injector="traffic-spike", target=None, intensity=0.1,
        band={"parameter": "multiplier", "low": 6, "high": 8}, params={"multiplier": 6.2})
    top_of_low_band = _activation(
        injector="traffic-spike", target=None, intensity=0.9,
        band={"parameter": "multiplier", "low": 2, "high": 4}, params={"multiplier": 3.8})
    assert _running(incident_type="TRAFFIC_SPIKE", activations=[bottom_of_high_band])["severity"] == "LOW"
    assert _running(incident_type="TRAFFIC_SPIKE", activations=[top_of_low_band])["severity"] == "HIGH"


# --- building -------------------------------------------------------------------

def test_new_manifest_is_running_with_no_end_time():
    manifest = _running()
    assert _errors(manifest) == []
    assert manifest["status"] == "RUNNING"
    assert manifest["ended_at"] is None
    assert manifest["schema_version"] == "1.0"
    assert manifest["telemetry"] == {"bucket": BUCKET, "prefixes": ["raw/", "metrics/"]}


def test_activation_fault_id_is_the_experiment_id_and_its_index():
    # The same id names the fault-config item when the injector uses the control
    # plane, which is how the two records are matched up.
    assert mf.fault_id(EXPERIMENT_ID, 0) == f"{EXPERIMENT_ID}#0"
    assert _activation(0)["fault_id"] == f"{EXPERIMENT_ID}#0"
    assert _activation(3)["fault_id"] == f"{EXPERIMENT_ID}#3"


def test_activation_has_nothing_observed_until_it_is_measured():
    # `params` is what was asked for; `observed` is what the injector measured,
    # which is only known once the run is over.
    assert _activation()["observed"] == {}
    measured = _activation(observed={"throttled_requests": 412})
    assert measured["observed"] == {"throttled_requests": 412}
    assert _errors(_final(activations=[measured])) == []


def test_each_activation_gets_its_own_observed_dict():
    # A shared default dict would leak one activation's measurements into the next.
    first, second = _activation(0), _activation(1)
    first["observed"]["throttled_requests"] = 412
    assert second["observed"] == {}


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "ABORTED"])
def test_finalized_manifest_has_its_status_and_end_time(status):
    manifest = _final(status)
    assert _errors(manifest) == []
    assert manifest["status"] == status
    assert manifest["ended_at"] == "2026-10-12T04:30:00.000Z"


def test_finalize_does_not_change_the_manifest_it_is_given():
    # The runner still holds the RUNNING manifest; finalizing must not edit it
    # underneath.
    manifest = _running()
    snapshot = copy.deepcopy(manifest)
    mf.finalize(manifest, "COMPLETED", T0 + 15 * MINUTE)
    assert manifest == snapshot


def test_built_manifest_matches_the_documented_example():
    # The example in Docs/SIMULATOR_INTERFACE.md, field for field and in the
    # same key order.
    expected = {
        "schema_version": "1.0",
        "experiment_id": EXPERIMENT_ID,
        "status": "COMPLETED",
        "incident_type": "DEPENDENCY_FAILURE",
        "severity": "HIGH",
        "seed": 20261012,
        "created_by": "jake",
        "started_at": "2026-10-12T04:15:00.000Z",
        "ended_at": "2026-10-12T04:30:00.000Z",
        "workload": {"request_rate_rps": 20},
        "activations": [
            {
                "fault_id": f"{EXPERIMENT_ID}#0",
                "injector": "payment-failure",
                "target": "payment-service",
                "intensity": 0.8,
                "band": {"parameter": "timeout_ms", "low": 2500, "high": 3500},
                "params": {"timeout_ms": 3300},
                "observed": {},
                "active_from": "2026-10-12T04:20:00.000Z",
                "active_until": "2026-10-12T04:25:00.000Z",
            }
        ],
        "telemetry": {"bucket": BUCKET, "prefixes": ["raw/", "metrics/"]},
    }
    manifest = _final()
    assert manifest == expected
    assert list(manifest) == TOP_LEVEL_KEYS
    assert list(manifest["activations"][0]) == ACTIVATION_KEYS


def test_same_inputs_give_the_same_manifest():
    # M3-1's reproducibility requirement: same params -> same recorded config,
    # down to the bytes that are stored.
    assert _final() == _final()
    assert json.dumps(_final(), indent=2) == json.dumps(_final(), indent=2)


def test_normal_run_has_no_severity_and_no_activations():
    # Also what a no-op run records: nothing was injected, so nothing is claimed.
    manifest = _normal()
    assert _errors(manifest) == []
    assert manifest["severity"] is None
    assert manifest["activations"] == []


def test_confounder_is_a_single_fault_type_with_two_activations():
    # PROJECT_PLAN 6b: two faults co-occur without the run being a cascade. The
    # top-level incident_type carries the intended label, so the validator must
    # not insist that a single-fault type has exactly one activation.
    activations = [
        _activation(0, injector="traffic-spike", target=None),
        _activation(1, injector="lambda-degradation", target="product-service"),
    ]
    assert _errors(_final(incident_type="TRAFFIC_SPIKE", activations=activations)) == []


def test_cascade_records_overlapping_activations():
    # Overlapping windows are the "which faults are concurrently active" record
    # M4 labels CASCADING_FAILURE windows from.
    activations = [
        _activation(0, injector="traffic-spike", target=None,
                    active_from_ms=T0 + 3 * MINUTE, active_until_ms=T0 + 12 * MINUTE),
        _activation(1, injector="db-throttling", target="product-service",
                    active_from_ms=T0 + 5 * MINUTE, active_until_ms=T0 + 12 * MINUTE),
        _activation(2, injector="lambda-degradation", target="order-service",
                    active_from_ms=T0 + 7 * MINUTE, active_until_ms=T0 + 12 * MINUTE),
    ]
    assert _errors(_final(incident_type="CASCADING_FAILURE", activations=activations)) == []


# --- schema: what a manifest may contain ---------------------------------------

@pytest.mark.parametrize("where", (
    TOP_LEVEL_KEYS
    + [f"activations/0/{key}" for key in ACTIVATION_KEYS]
    + ["activations/0/band/parameter", "activations/0/band/low", "activations/0/band/high",
       "telemetry/bucket", "telemetry/prefixes"]
))
def test_every_key_is_required(where):
    # Nullable keys (severity, ended_at, target) still have to be present, the
    # same convention as telemetry: a reader never has to guess what a missing
    # key means.
    key = where.split("/")[-1]
    errors = _errors(_without(_final(), where))
    assert any(f"'{key}' is a required property" in error for error in errors)


@pytest.mark.parametrize("where", [
    "label",
    "activations/0/db_throttled",
    "activations/0/band/phase",
    "telemetry/started_at",
])
def test_unknown_keys_are_rejected(where):
    # additionalProperties:false at every level the framework owns, so the shape
    # M4 reads cannot drift one ad-hoc key at a time.
    key = where.split("/")[-1]
    errors = _errors(_changed(_final(), where, "x"))
    assert any(f"'{key}' was unexpected" in error for error in errors)


def test_params_observed_and_workload_are_open():
    # The objects the framework does not own: injector authors define `params`
    # and `observed`, the workload generator's owner defines `workload`.
    manifest = _final(
        workload={"request_rate_rps": 20, "mix": {"GET /products": 0.6}, "users": [1, 2]},
        activations=[_activation(
            params={"timeout_ms": 3300, "nested": {"anything": [True, None]}},
            observed={"throttled_requests": 412, "by_minute": [0, 130, 282]},
        )],
    )
    assert _errors(manifest) == []


@pytest.mark.parametrize("incident_type", [
    "TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION", "DEPENDENCY_FAILURE",
    "CASCADING_FAILURE",
])
def test_incident_types_are_the_plans_classes(incident_type):
    assert _errors(_final(incident_type=incident_type)) == []


def test_incident_types_constant_lists_the_six_labels():
    # The injector registry checks each injector's incident_type against this.
    assert mf.INCIDENT_TYPES == (
        "NORMAL", "TRAFFIC_SPIKE", "DATABASE_THROTTLING", "LAMBDA_DEGRADATION",
        "DEPENDENCY_FAILURE", "CASCADING_FAILURE")


@pytest.mark.parametrize("target", [
    "product-service", "cart-service", "order-service", "payment-service", None,
])
def test_target_is_a_telemetry_service_or_null(target):
    # Null is for a fault with no single service behind it, e.g. a traffic spike.
    assert _errors(_final(activations=[_activation(target=target)])) == []


@pytest.mark.parametrize("where, value", [
    ("schema_version", "2.0"),
    ("experiment_id", "042"),                        # a bare sequence number; M4's registry owns those
    ("experiment_id", "exp-20261012T041500Z-3F9A"),  # the suffix is lowercase hex
    ("status", "DONE"),
    ("incident_type", "CONFOUNDER"),                 # a confounder keeps a single-fault type
    ("severity", "CRITICAL"),
    ("seed", -1),
    ("seed", 2**32),                                 # numpy seeds stop at 2**32 - 1
    ("seed", 1.5),
    ("created_by", ""),
    ("started_at", "2026-10-12T04:15:00Z"),          # no milliseconds
    ("started_at", "2026-10-12T04:15:00.000+00:00"), # an offset where telemetry writes Z
    ("started_at", 1791778500000),                   # epoch ms belongs to the control plane
    ("ended_at", "2026-10-12T04:30:00Z"),
    ("workload", []),
    ("activations", {}),
    ("activations/0/fault_id", "0"),
    ("activations/0/injector", ""),
    ("activations/0/target", "anomalypulse-products"),  # a table, not a service
    ("activations/0/intensity", 1.2),
    ("activations/0/intensity", -0.1),
    ("activations/0/intensity", "0.8"),
    ("activations/0/band/parameter", ""),
    ("activations/0/band/low", "2500"),
    ("activations/0/params", [3300]),
    ("activations/0/observed", [412]),
    ("activations/0/active_from", "2026-10-12T04:20:00Z"),
    ("activations/0/active_until", 1791778800000),
    ("telemetry/bucket", ""),
    ("telemetry/prefixes", []),
    ("telemetry/prefixes", ["raw"]),                 # without the slash it would also match raw-other/
    ("telemetry/prefixes", ["raw/", "raw/"]),
])
def test_bad_values_are_rejected_and_the_error_names_the_field(where, value):
    errors = _errors(_changed(_final(), where, value))
    assert any(error.startswith(where) for error in errors)


def test_running_manifest_cannot_carry_an_end_time():
    errors = _errors(_changed(_final(), "status", "RUNNING"))
    assert any(error.startswith("ended_at") for error in errors)


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "ABORTED"])
def test_finished_manifest_needs_an_end_time(status):
    # ended_at is null exactly while RUNNING, so a manifest still at RUNNING
    # after its run is unmistakably one that died.
    errors = _errors(_changed(_final(status), "ended_at", None))
    assert any(error.startswith("ended_at") for error in errors)


def test_normal_run_cannot_record_an_activation():
    # A NORMAL label on a run that injected a fault would be wrong ground truth.
    errors = _errors(_final(incident_type="NORMAL"))
    assert any(error.startswith("activations") for error in errors)


def test_normal_run_cannot_carry_a_severity():
    errors = _errors(_changed(_normal(), "severity", "LOW"))
    assert any(error.startswith("severity") for error in errors)


def test_incident_run_needs_an_activation():
    errors = _errors(_final(activations=[]))
    assert any(error.startswith("activations") for error in errors)


def test_incident_run_needs_a_severity():
    errors = _errors(_changed(_final(), "severity", None))
    assert any(error.startswith("severity") for error in errors)


# --- rules the schema cannot express ---------------------------------------------

@pytest.mark.parametrize("ended_at_ms", [T0 - 1, T0])
def test_run_must_end_after_it_starts(ended_at_ms):
    assert "ended_at must be after started_at" in _errors(_final("ABORTED", ended_at_ms))


@pytest.mark.parametrize("active_until_ms", [T0 + 5 * MINUTE, T0 + 4 * MINUTE])
def test_fault_window_must_have_a_positive_length(active_until_ms):
    # The window is start-inclusive, end-exclusive, so equal ends mean the fault
    # was never active.
    activation = _activation(active_from_ms=T0 + 5 * MINUTE, active_until_ms=active_until_ms)
    assert _errors(_final(activations=[activation])) == [
        f"{EXPERIMENT_ID}#0: active_from must be before active_until"]


def test_fault_window_cannot_start_before_the_run():
    activation = _activation(active_from_ms=T0 - 1)
    assert _errors(_final(activations=[activation])) == [
        f"{EXPERIMENT_ID}#0: active_from is before started_at"]


def test_fault_window_may_fill_the_whole_run():
    activation = _activation(active_from_ms=T0, active_until_ms=T0 + 15 * MINUTE)
    assert _errors(_final(activations=[activation])) == []


def test_completed_run_cannot_have_a_fault_outlasting_it():
    activation = _activation(active_until_ms=T0 + 20 * MINUTE)
    assert _errors(_final("COMPLETED", activations=[activation])) == [
        f"{EXPERIMENT_ID}#0: active_until is after ended_at"]


@pytest.mark.parametrize("status", ["FAILED", "ABORTED"])
def test_interrupted_run_keeps_the_windows_it_planned(status):
    # A run stopped early still has to write its final manifest, or it would be
    # left at RUNNING with no record of how it ended. Its planned windows may
    # run past the moment it stopped, or not have started at all.
    stopped_mid_fault = _final(status, ended_at_ms=T0 + 7 * MINUTE)
    stopped_before_fault = _final(status, ended_at_ms=T0 + 1 * MINUTE)
    assert _errors(stopped_mid_fault) == []
    assert _errors(stopped_before_fault) == []


def test_running_manifest_may_plan_any_window_after_the_start():
    assert _errors(_running(activations=[_activation(active_until_ms=T0 + 60 * MINUTE)])) == []


def test_fault_id_must_belong_to_this_experiment():
    other = "exp-20261012T041500Z-ffff#0"
    manifest = _changed(_final(), "activations/0/fault_id", other)
    assert _errors(manifest) == [f"{other}: fault_id must start with {EXPERIMENT_ID}#"]


def test_fault_ids_must_be_unique():
    # Two activations sharing an id would also share one fault-config item.
    assert _errors(_final(activations=[_activation(0), _activation(0)])) == [
        "fault_id values must be unique"]


def test_band_cannot_be_upside_down():
    band = {"parameter": "timeout_ms", "low": 3500, "high": 2500}
    assert _errors(_final(activations=[_activation(band=band)])) == [
        f"{EXPERIMENT_ID}#0: band low is above band high"]


def test_band_may_be_a_single_value():
    band = {"parameter": "timeout_ms", "low": 3000, "high": 3000}
    assert _errors(_final(activations=[_activation(band=band)])) == []


def test_severity_must_agree_with_the_intensities():
    # Severity is derived, so a manifest whose severity was typed in or edited
    # by hand is caught here.
    assert _errors(_changed(_final(), "severity", "LOW")) == [
        "severity must be HIGH for the highest intensity"]


def test_timestamp_that_is_not_a_real_date_is_reported_not_raised():
    # The schema's pattern only checks the shape, so month 13 gets past it.
    errors = _errors(_changed(_final(), "started_at", "2026-13-45T04:15:00.000Z"))
    assert len(errors) == 1
    assert errors[0].startswith("timestamp is not a real date and time")


def test_every_broken_rule_is_reported_at_once():
    activation = _activation(active_from_ms=T0 - 1, band={"parameter": "x", "low": 2, "high": 1})
    assert len(_errors(_final(activations=[activation]))) == 2


# --- values JSON cannot hold ------------------------------------------------------

def test_decimal_in_params_is_a_validation_error():
    # boto3's DynamoDB resource returns every number as a Decimal, which
    # json.dumps cannot write. An injector that copies one into `params` gets a
    # validation error up front instead of a crash in the middle of the write.
    activation = _activation(params={"provisioned_rcu": Decimal("5")})
    errors = _errors(_final(activations=[activation]))
    assert len(errors) == 1
    assert errors[0].startswith("not JSON-serializable")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_validation_errors(value):
    # json.dumps would happily write these as NaN / Infinity, which is not JSON
    # and breaks every other reader.
    errors = _errors(_final(workload={"request_rate_rps": value}))
    assert len(errors) == 1
    assert errors[0].startswith("not JSON-serializable")


@pytest.mark.parametrize("value", [None, [], "manifest", 3, {}])
def test_things_that_are_not_manifests_are_rejected_without_raising(value):
    assert _errors(value) != []


# --- validate_or_raise --------------------------------------------------------------

def test_validate_or_raise_is_silent_for_a_valid_manifest():
    assert mf.validate_or_raise(_final()) is None


def test_validate_or_raise_lists_every_problem():
    activation = _activation(active_from_ms=T0 - 1, band={"parameter": "x", "low": 2, "high": 1})
    with pytest.raises(mf.ManifestError) as raised:
        mf.validate_or_raise(_final(activations=[activation]))
    message = str(raised.value)
    assert "active_from is before started_at" in message
    assert "band low is above band high" in message


def test_manifest_error_is_a_value_error():
    assert issubclass(mf.ManifestError, ValueError)


# --- writing --------------------------------------------------------------------------

def test_write_puts_the_manifest_at_its_key_and_returns_the_key(s3):
    client, stubber = s3
    manifest = _final()
    _expect_put(stubber, manifest)
    assert mf.write_manifest(client, BUCKET, manifest) == KEY
    stubber.assert_no_pending_responses()


def test_final_manifest_overwrites_the_running_one(s3):
    # Written twice per run to the same key: RUNNING at the start, so a run that
    # dies leaves evidence, then the final manifest over the top of it.
    client, stubber = s3
    running = _running()
    final = mf.finalize(running, "COMPLETED", T0 + 15 * MINUTE)
    _expect_put(stubber, running)
    _expect_put(stubber, final)
    assert mf.write_manifest(client, BUCKET, running) == KEY
    assert mf.write_manifest(client, BUCKET, final) == KEY
    stubber.assert_no_pending_responses()


def test_invalid_manifest_is_never_sent(s3):
    # Nothing is queued on the stubber, so a put_object here would raise a
    # stubbing error instead of the ManifestError we expect.
    client, stubber = s3
    with pytest.raises(mf.ManifestError):
        mf.write_manifest(client, BUCKET, _changed(_final(), "label", "x"))
    stubber.assert_no_pending_responses()


def test_unserializable_manifest_is_never_sent(s3):
    client, stubber = s3
    activation = _activation(params={"provisioned_rcu": Decimal("5")})
    with pytest.raises(mf.ManifestError):
        mf.write_manifest(client, BUCKET, _final(activations=[activation]))
    stubber.assert_no_pending_responses()
