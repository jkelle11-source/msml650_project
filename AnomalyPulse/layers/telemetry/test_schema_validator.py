import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# The validator ships inside the layer's python/ dir (that dir is what the
# Makefile copies into the Lambda layer). This test lives one level up so it is
# NOT packaged into the runtime layer; add python/ to the path to import it.
sys.path.insert(0, str(Path(__file__).parent / "python"))

import schema_validator as sv

# Tier-2 ground-truth/diagnostic fields (PROJECT_PLAN Section 3). None of these
# may ever appear on the observable request record; additionalProperties:false
# in the schema is what enforces that boundary. Kept in sync with the service
# handler tests' TIER2_FIELDS.
TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}


# --- helpers ---------------------------------------------------------------

def _valid_order_record():
    # POST /orders: calls the payment dependency AND hits DynamoDB, so every
    # optional field is populated (the richest record shape).
    return {
        "timestamp": "2026-09-30T15:42:18.123Z",
        "request_id": "req-abc-123",
        "service": "order-service",
        "endpoint": "/orders",
        "http_method": "POST",
        "status_code": 201,
        "latency_ms": 184.2,
        "lambda_duration_ms": 180.0,
        "cold_start": False,
        "db_latency_ms": 32.0,
        "db_consumed_capacity": 2.0,
        "dependency": "payment-service",
        "dependency_latency_ms": 110.0,
        "dependency_error": False,
    }


def _valid_product_record():
    # GET /products: hits DynamoDB but makes no downstream call, so the
    # dependency.* fields are null (schema allows null there, not absence).
    return {
        "timestamp": "2026-09-30T15:42:18.123Z",
        "request_id": "req-prod-1",
        "service": "product-service",
        "endpoint": "/products",
        "http_method": "GET",
        "status_code": 200,
        "latency_ms": 12.5,
        "lambda_duration_ms": 10.0,
        "cold_start": False,
        "db_latency_ms": 4.0,
        "db_consumed_capacity": 0.5,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }


def _valid_payment_record():
    # POST /payments: no DB call and no downstream, so BOTH the db.* and
    # dependency.* fields are null. Proves null is accepted on every optional.
    return {
        "timestamp": "2026-09-30T15:42:18.123Z",
        "request_id": "req-pay-1",
        "service": "payment-service",
        "endpoint": "/payments",
        "http_method": "POST",
        "status_code": 200,
        "latency_ms": 45.0,
        "lambda_duration_ms": 44.0,
        "cold_start": True,
        "db_latency_ms": None,
        "db_consumed_capacity": None,
        "dependency": None,
        "dependency_latency_ms": None,
        "dependency_error": None,
    }


# --- the schema itself ------------------------------------------------------

def test_schema_is_well_formed():
    # Importing schema_validator already runs check_schema at module load, so a
    # malformed schema would fail cold start. Assert it explicitly too, so this
    # is a named proof point rather than an implicit side effect of import.
    Draft202012Validator.check_schema(sv.schema)


# --- valid records: one per service shape -----------------------------------

@pytest.mark.parametrize("record", [_valid_order_record(), _valid_product_record(), _valid_payment_record()],
                         ids=["order", "product", "payment"])
def test_valid_records_pass(record):
    valid, errors = sv.validate(record)
    assert valid is True
    assert errors == []


# --- the Feature/Label Boundary (the reason this contract exists) -----------

@pytest.mark.parametrize("tier2_field", sorted(TIER2_FIELDS))
def test_tier2_field_is_rejected(tier2_field):
    # A service owner who accidentally folds a Tier-2 ground-truth field into the
    # observable record must be rejected at the contract (PROJECT_PLAN Section 3).
    # additionalProperties:false is the leak-catcher.
    record = _valid_order_record()
    record[tier2_field] = "leaked"
    valid, errors = sv.validate(record)
    assert valid is False
    assert any("Additional properties" in e.message for e in errors)


def test_unknown_field_is_rejected():
    record = _valid_order_record()
    record["totally_unknown"] = 1
    valid, errors = sv.validate(record)
    assert valid is False


# --- correlation discipline: required fields --------------------------------

@pytest.mark.parametrize("missing_field", list(_valid_order_record().keys()))
def test_missing_required_field_is_rejected(missing_field):
    # Every field is required; emission rule is "never omit, emit null" so
    # presence (not non-null) is what's checked. Dropping any field fails.
    record = _valid_order_record()
    del record[missing_field]
    valid, errors = sv.validate(record)
    assert valid is False
    assert any(missing_field in e.message and "required" in e.message for e in errors)


def test_missing_timestamp_and_request_id_rejected():
    # Called out explicitly in the M2-1 Definition of Done: the two correlation
    # keys that make cross-service ordering reconstructable (PROJECT_PLAN Section 3).
    for key in ("timestamp", "request_id"):
        record = _valid_order_record()
        del record[key]
        valid, _ = sv.validate(record)
        assert valid is False, f"record missing {key} should be rejected"


# --- timestamp precision: pattern, not format -------------------------------

@pytest.mark.parametrize("bad_timestamp", [
    "2026-09-30T15:42:18Z",        # no milliseconds
    "2026-09-30T15:42:18.12Z",     # 2 digits, not 3
    "2026-09-30T15:42:18.123456Z", # microseconds
    "2026-09-30 15:42:18.123Z",    # space instead of T
    "2026-09-30T15:42:18.123",     # no trailing Z
])
def test_timestamp_millisecond_precision_enforced(bad_timestamp):
    # format:date-time is advisory only (unenforced without a FormatChecker); the
    # pattern regex is what pins exactly 3 fractional digits + Z. This guards that
    # the pattern, not format, is doing the work.
    record = _valid_order_record()
    record["timestamp"] = bad_timestamp
    valid, _ = sv.validate(record)
    assert valid is False


# --- a sampling of type/enum/range constraints ------------------------------

def test_unknown_service_enum_rejected():
    record = _valid_order_record()
    record["service"] = "billing-service"
    valid, _ = sv.validate(record)
    assert valid is False


def test_status_code_out_of_range_rejected():
    record = _valid_order_record()
    record["status_code"] = 42
    valid, _ = sv.validate(record)
    assert valid is False


def test_negative_latency_rejected():
    record = _valid_order_record()
    record["latency_ms"] = -1
    valid, _ = sv.validate(record)
    assert valid is False


# --- the return contract ----------------------------------------------------

def test_validate_returns_bool_and_list():
    # Uniform return type both ways: (bool, list). A caller can always iterate
    # the second element without a None check.
    valid, errors = sv.validate(_valid_order_record())
    assert isinstance(valid, bool) and isinstance(errors, list)
    valid, errors = sv.validate({})
    assert isinstance(valid, bool) and isinstance(errors, list)


def test_validate_or_raise_passes_silently_on_valid():
    assert sv.validate_or_raise(_valid_order_record()) is None


def test_validate_or_raise_raises_on_invalid():
    record = _valid_order_record()
    del record["timestamp"]
    with pytest.raises(ValidationError):
        sv.validate_or_raise(record)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
