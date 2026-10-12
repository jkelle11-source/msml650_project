import importlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from botocore.stub import Stubber

# fault_config builds its DynamoDB resource at import, which needs a region to
# resolve. Lambda always supplies one; locally we set it before the import, the
# same way the order and cart handler tests do.
os.environ["AWS_DEFAULT_REGION"] = "us-east-2"

# The reader ships inside the layer's python/ dir (that dir is what the Makefile
# copies into the Lambda layer). This test lives one level up so it is NOT
# packaged into the runtime layer; add python/ to the path to import it.
sys.path.insert(0, str(Path(__file__).parent / "python"))

import fault_config as fc

SERVICE = "payment-service"
TABLE = "anomalypulse-fault-config"


# --- helpers ---------------------------------------------------------------

def _item(fault_id="exp-1#0", start=1_000, end=2_000):
    # One fault activation as the reader hands it to a handler: plain Python
    # types, numbers as int/float. Windows are epoch milliseconds; the tiny
    # defaults just keep the boundary tests readable.
    return {
        "service": SERVICE,
        "fault_id": fault_id,
        "injector": "payment-failure",
        "params": {"latency_ms": 800, "failure_rate": 0.2},
        "active_from_ms": start,
        "active_until_ms": end,
        "expires_at": 1_800_000_000,
    }


def _wire_item(fault_id="exp-1#0", start=1_000, end=2_000):
    # The same activation in DynamoDB's wire format, as the Query API returns
    # it. boto3 deserializes every "N" into a Decimal, which is what _plain()
    # exists to undo.
    return {
        "service": {"S": SERVICE},
        "fault_id": {"S": fault_id},
        "injector": {"S": "payment-failure"},
        "params": {"M": {"latency_ms": {"N": "800"}, "failure_rate": {"N": "0.2"}}},
        "active_from_ms": {"N": str(start)},
        "active_until_ms": {"N": str(end)},
        "expires_at": {"N": "1800000000"},
    }


def _contains_decimal(value):
    # Decimal("800") == 800 is True, so equality alone cannot prove a value was
    # converted. Walk the structure and look at the actual types.
    if isinstance(value, Decimal):
        return True
    if isinstance(value, dict):
        return any(_contains_decimal(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_decimal(v) for v in value)
    return False


class FakeFetch:
    # Stands in for fc._fetch, the only function that talks to DynamoDB: records
    # each call and returns (or raises) whatever the test sets.
    def __init__(self):
        self.items = []
        self.error = None
        self.calls = []

    def __call__(self, service):
        self.calls.append(service)
        if self.error is not None:
            raise self.error
        return list(self.items)


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def advance(self, seconds):
        self.now += seconds


# --- fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def dynamodb(monkeypatch):
    # Stubber intercepts every call on the module's real DynamoDB client. A
    # queued response is validated against the service model and returned
    # without touching the network; a call with nothing queued raises. So no
    # test in this file can reach AWS, even by mistake.
    fc._cache.clear()
    monkeypatch.delenv("FAULT_CONFIG_TABLE", raising=False)
    with Stubber(fc._dynamodb.meta.client) as stubber:
        yield stubber
    fc._cache.clear()


@pytest.fixture
def fetch(monkeypatch):
    fake = FakeFetch()
    monkeypatch.setattr(fc, "_fetch", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    # Drives the cache's sense of elapsed time (time.monotonic) so a test can
    # step past the TTL without sleeping. The wall clock behind now_ms is left
    # alone. The TTL is pinned so these tests don't depend on the environment.
    fake = FakeClock()
    monkeypatch.setattr(fc.time, "monotonic", lambda: fake.now)
    monkeypatch.setattr(fc, "_TTL_SECONDS", 5.0)
    return fake


# --- _plain: DynamoDB numbers -> plain Python numbers -------------------------

@pytest.mark.parametrize("raw, expected", [
    (Decimal("800"), 800),
    (Decimal("0"), 0),
    (Decimal("1760000000000"), 1_760_000_000_000),  # an epoch-ms value survives intact
    (Decimal("0.2"), 0.2),
    (Decimal("-1.5"), -1.5),
])
def test_plain_converts_decimal_to_int_or_float(raw, expected):
    # Whole numbers become int and everything else float, so a handler can pass
    # a param straight to time.sleep() or json.dumps(), neither of which
    # accepts a Decimal.
    result = fc._plain(raw)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize("value", ["payment-failure", "", True, False, None, 7, 0.5])
def test_plain_passes_other_values_through_unchanged(value):
    # Only Decimals are touched. In particular a bool must stay a bool (True is
    # numerically 1, so a careless numeric check would turn it into an int).
    result = fc._plain(value)
    assert result == value
    assert type(result) is type(value)


def test_plain_walks_nested_dicts_and_lists():
    # `params` is a map and may nest further, so the conversion has to recurse.
    raw = {
        "injector": "payment-failure",
        "params": {
            "latency_ms": Decimal("800"),
            "steps": [Decimal("1"), Decimal("2.5"), {"deep": Decimal("3")}],
        },
    }
    result = fc._plain(raw)
    assert result == {
        "injector": "payment-failure",
        "params": {"latency_ms": 800, "steps": [1, 2.5, {"deep": 3}]},
    }
    assert not _contains_decimal(result)


# --- _fetch: the one function that talks to DynamoDB --------------------------

def test_fetch_returns_empty_without_calling_dynamodb_when_table_unset():
    # Local runs and the handler unit tests have no FAULT_CONFIG_TABLE. Nothing
    # is queued on the stubber, so a query here would raise instead.
    assert fc._fetch(SERVICE) == []


def test_fetch_queries_this_services_partition_of_the_configured_table(dynamodb, monkeypatch):
    # The table name is read when _fetch runs, not at import: it is only set
    # here, long after the module was loaded.
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    dynamodb.add_response(
        "query", {"Items": []},
        expected_params={"TableName": TABLE, "KeyConditionExpression": Key("service").eq(SERVICE)},
    )
    assert fc._fetch(SERVICE) == []
    dynamodb.assert_no_pending_responses()


def test_fetch_returns_items_with_plain_numbers(dynamodb, monkeypatch):
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    dynamodb.add_response("query", {"Items": [_wire_item()]})
    items = fc._fetch(SERVICE)
    assert items == [_item()]
    assert not _contains_decimal(items)
    json.dumps(items)  # would raise on a leftover Decimal


def test_fetch_does_not_filter_by_time(dynamodb, monkeypatch):
    # An activation that has already ended and one that is far in the future
    # both come back. Filtering here would break scheduled faults: an item
    # fetched before its window opens has to be in the cache to switch on later.
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    ended = _wire_item("ended#0", 0, 1)
    future = _wire_item("future#0", 9_999_999_999_998, 9_999_999_999_999)
    dynamodb.add_response("query", {"Items": [ended, future]})
    assert [item["fault_id"] for item in fc._fetch(SERVICE)] == ["ended#0", "future#0"]


def test_fetch_lets_dynamodb_errors_propagate(dynamodb, monkeypatch):
    # _fetch must raise rather than return []. The cache layer relies on the
    # exception to keep serving the previous items; swallowing it here would
    # silently switch every active fault off during a control-plane blip.
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    dynamodb.add_client_error("query", service_error_code="InternalServerError")
    with pytest.raises(ClientError):
        fc._fetch(SERVICE)


# --- client settings: a control-plane hiccup must be cheap --------------------

def test_client_fails_fast_and_never_retries():
    # This read sits on the request path of every service, so a slow or failing
    # control plane may cost about a second, once, and no more.
    # total_max_attempts counts the first try, so 1 means "no retries". The
    # similarly named max_attempts counts RETRIES: max_attempts=1 would allow a
    # second try and double the worst case.
    config = fc._dynamodb.meta.client.meta.config
    assert config.connect_timeout <= 1
    assert config.read_timeout <= 1
    assert config.retries["total_max_attempts"] == 1


# --- cache lifetime setting ---------------------------------------------------

def _ttl_after_import(env_value):
    # _TTL_SECONDS is read once at import (i.e. at cold start), so a new value
    # only shows up on a fresh import. Reload again afterwards, with the
    # environment restored, so later tests see the module as it was.
    try:
        with pytest.MonkeyPatch.context() as mp:
            if env_value is None:
                mp.delenv("FAULT_CONFIG_TTL_SECONDS", raising=False)
            else:
                mp.setenv("FAULT_CONFIG_TTL_SECONDS", env_value)
            return importlib.reload(fc)._TTL_SECONDS
    finally:
        importlib.reload(fc)


def test_ttl_defaults_to_five_seconds():
    assert _ttl_after_import(None) == 5.0


def test_ttl_is_configurable_through_the_environment():
    assert _ttl_after_import("30") == 30.0


# --- caching: at most one fetch per TTL per execution environment -------------

def test_calls_within_the_ttl_share_one_fetch(fetch, clock):
    fetch.items = [_item()]
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item()]
    clock.advance(4.9)
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item()]
    assert fetch.calls == [SERVICE]


def test_cache_refreshes_once_the_ttl_has_passed(fetch, clock):
    fetch.items = [_item("exp-1#0")]
    fc.active_faults(SERVICE, now_ms=1_500)
    fetch.items = [_item("exp-2#0")]
    clock.advance(5.1)
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item("exp-2#0")]
    assert len(fetch.calls) == 2


def test_fault_free_answer_is_cached_too(fetch, clock):
    # "No faults" is the normal state. It must be cached like any other answer,
    # otherwise NORMAL traffic would query DynamoDB on every request while
    # incident traffic would not -- a difference between the two that the
    # classifier could learn.
    assert fc.active_faults(SERVICE) == []
    assert fc.active_faults(SERVICE) == []
    assert len(fetch.calls) == 1


def test_each_service_has_its_own_cache_entry(fetch, clock):
    # In Lambda a function only ever asks for itself, but the cache is keyed by
    # service so one service's faults can never be served to another.
    fetch.items = [_item()]
    assert fc.active_faults("payment-service", now_ms=1_500) == [_item()]
    fetch.items = []
    assert fc.active_faults("order-service", now_ms=1_500) == []
    assert fetch.calls == ["payment-service", "order-service"]
    assert fc.active_faults("payment-service", now_ms=1_500) == [_item()]
    assert len(fetch.calls) == 2


# --- failure handling: the control plane must never hurt a request ------------

def test_failed_refresh_keeps_serving_the_previous_items(fetch, clock):
    # The cached items still carry their own windows, so serving them through an
    # outage is safe: they keep switching on and off at the right times.
    fetch.items = [_item()]
    fc.active_faults(SERVICE, now_ms=1_500)
    fetch.error = RuntimeError("control plane down")
    clock.advance(5.1)
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item()]
    assert len(fetch.calls) == 2


def test_failed_first_fetch_returns_empty(fetch, clock):
    fetch.error = RuntimeError("control plane down")
    assert fc.active_faults(SERVICE, now_ms=1_500) == []


def test_failed_refresh_is_not_retried_until_the_ttl_passes_again(fetch, clock):
    # A failed fetch still resets the timer. Without that, a broken control
    # plane would be retried on EVERY request, each one waiting out the client
    # timeouts -- turning a control-plane problem into a latency incident.
    fetch.error = RuntimeError("control plane down")
    fc.active_faults(SERVICE)
    clock.advance(1)
    fc.active_faults(SERVICE)
    assert len(fetch.calls) == 1
    clock.advance(5)
    fc.active_faults(SERVICE)
    assert len(fetch.calls) == 2


def test_recovers_once_the_control_plane_is_back(fetch, clock):
    fetch.error = RuntimeError("control plane down")
    assert fc.active_faults(SERVICE, now_ms=1_500) == []
    fetch.error = None
    fetch.items = [_item()]
    clock.advance(5.1)
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item()]


def test_failed_refresh_logs_one_json_line_naming_only_the_exception_class(fetch, clock, capsys):
    # Exactly one line per failed refresh, valid JSON, with no request_id: the
    # shipper's subscription filter forwards lines that have one, and this is
    # not a telemetry record. The message is left out because AWS error
    # messages carry ARNs and account ids.
    fetch.error = RuntimeError("arn:aws:dynamodb:us-east-2:123456789012:table/anomalypulse-fault-config")
    fc.active_faults(SERVICE)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"fault_config_error": "RuntimeError"}


# --- the time window -----------------------------------------------------------

@pytest.mark.parametrize("now_ms, expected_active", [
    (999, False),    # one millisecond before the window opens
    (1_000, True),   # start is inclusive
    (1_500, True),
    (1_999, True),   # last millisecond of the window
    (2_000, False),  # end is exclusive
    (2_001, False),
])
def test_fault_applies_only_inside_its_window(fetch, now_ms, expected_active):
    fetch.items = [_item(start=1_000, end=2_000)]
    assert fc.active_faults(SERVICE, now_ms=now_ms) == ([_item()] if expected_active else [])


def test_window_is_checked_on_every_call_not_when_fetched(fetch, clock):
    # The property the control-plane design rests on: an item fetched BEFORE its
    # window opens switches on at the right millisecond straight from the cache,
    # and off again, with no further fetch. That is what makes onset simultaneous
    # across execution environments however old each one's cache is.
    fetch.items = [_item(start=1_000, end=2_000)]
    assert fc.active_faults(SERVICE, now_ms=500) == []
    assert fc.active_faults(SERVICE, now_ms=1_000) == [_item()]
    assert fc.active_faults(SERVICE, now_ms=2_000) == []
    assert len(fetch.calls) == 1


def test_overlapping_faults_are_all_returned(fetch, clock):
    # A cascade: two activations on one service whose windows overlap.
    first = _item("exp-1#0", 1_000, 3_000)
    second = _item("exp-1#1", 2_000, 4_000)
    fetch.items = [first, second]
    assert fc.active_faults(SERVICE, now_ms=1_500) == [first]
    assert fc.active_faults(SERVICE, now_ms=2_500) == [first, second]
    assert fc.active_faults(SERVICE, now_ms=3_500) == [second]


def test_now_defaults_to_the_current_time_in_epoch_milliseconds(fetch):
    # Windows are epoch MILLISECONDS. If the default clock were in seconds,
    # "now" would be about 1000x too small: the live window below would look
    # like the far future, and the one that ended a minute ago would look
    # current.
    now_ms = int(time.time() * 1000)
    live = _item("live#0", now_ms - 60_000, now_ms + 60_000)
    ended = _item("ended#0", 0, now_ms - 60_000)
    fetch.items = [live, ended]
    assert fc.active_faults(SERVICE) == [live]


# --- never raises ---------------------------------------------------------------

@pytest.mark.parametrize("bad_item", [
    {"service": SERVICE, "fault_id": "exp-1#0"},
    dict(_item(), active_from_ms=None),
    dict(_item(), active_until_ms="soon"),
], ids=["no_window_fields", "null_start", "non_numeric_end"])
def test_malformed_item_returns_empty_instead_of_raising(fetch, bad_item):
    # Same rule as telemetry: this code runs inside every request, so it must
    # never be the reason a request fails.
    fetch.items = [bad_item]
    assert fc.active_faults(SERVICE, now_ms=1_500) == []


def test_unexpected_internal_error_returns_empty(monkeypatch):
    # Last line of defence: even a bug in the cache layer must not reach the
    # handler.
    def boom(service):
        raise RuntimeError("bug in the cache layer")

    monkeypatch.setattr(fc, "_items", boom)
    assert fc.active_faults(SERVICE) == []


# --- end to end through the (stubbed) DynamoDB client ---------------------------

def test_end_to_end_active_fault_is_read_from_dynamodb(dynamodb, monkeypatch):
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    dynamodb.add_response("query", {"Items": [_wire_item("exp-1#0", 1_000, 2_000),
                                              _wire_item("exp-1#1", 5_000, 6_000)]})
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item("exp-1#0", 1_000, 2_000)]
    # Only one response was queued, so this second answer came from the cache.
    assert fc.active_faults(SERVICE, now_ms=5_500) == [_item("exp-1#1", 5_000, 6_000)]
    dynamodb.assert_no_pending_responses()


def test_end_to_end_dynamodb_failure_returns_empty_and_logs(dynamodb, monkeypatch, capsys):
    monkeypatch.setenv("FAULT_CONFIG_TABLE", TABLE)
    dynamodb.add_client_error("query", service_error_code="InternalServerError")
    assert fc.active_faults(SERVICE, now_ms=1_500) == []
    (line,) = capsys.readouterr().out.splitlines()
    assert set(json.loads(line)) == {"fault_config_error"}


def test_end_to_end_no_table_configured_is_silent(capsys):
    # With no FAULT_CONFIG_TABLE the reader is simply off: no faults, no AWS
    # call (the stubber has nothing queued) and no error line on every refresh.
    assert fc.active_faults(SERVICE) == []
    assert capsys.readouterr().out == ""


# --- the Feature/Label Boundary -------------------------------------------------

def test_reading_active_faults_prints_nothing(fetch, capsys):
    # Fault items are Tier-2 ground truth (injector name, injected magnitudes).
    # A handler's stdout goes to CloudWatch, so the reader must stay silent on
    # the happy path and never echo what it fetched.
    fetch.items = [_item()]
    assert fc.active_faults(SERVICE, now_ms=1_500) == [_item()]
    assert capsys.readouterr().out == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
