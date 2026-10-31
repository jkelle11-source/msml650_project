"""Tests for the dashboard read API's handler (M7-2)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import builder
import handler
import mock_source

NOW = datetime(2026, 10, 30, 14, 34, 5, tzinfo=timezone.utc)
JSON = {"Content-Type": "application/json"}


def body(response):
    return json.loads(response["body"])


def test_no_query_string_serves_the_healthy_scenario():
    response = handler.respond({}, "mock", NOW)
    assert response["statusCode"] == 200
    assert response["headers"] == JSON
    assert body(response)["health"]["status"] == "HEALTHY"
    assert body(response)["health"]["window_start"] == "2026-10-30T14:33:00.000Z"


def test_a_null_query_string_serves_the_healthy_scenario_too():
    assert body(handler.respond({"queryStringParameters": None}, "mock", NOW))["health"]["status"] == "HEALTHY"


def test_a_query_string_without_scenario_serves_healthy():
    response = handler.respond({"queryStringParameters": {"cache": "1"}}, "mock", NOW)
    assert body(response)["health"]["status"] == "HEALTHY"


def test_the_scenario_parameter_picks_the_scenario():
    response = handler.respond({"queryStringParameters": {"scenario": "incident"}}, "mock", NOW)
    assert response["statusCode"] == 200
    assert body(response)["incident"]["comparison"] == {"detection_latency_s": 69.0}


def test_the_body_is_the_builder_s_reply_as_json_text():
    response = handler.respond({"queryStringParameters": {"scenario": "cascade_early"}}, "mock", NOW)
    assert isinstance(response["body"], str)
    assert body(response) == builder.build_dashboard(*mock_source.read("cascade_early", NOW), NOW)


def test_an_unknown_scenario_is_a_400_naming_the_valid_ones():
    response = handler.respond({"queryStringParameters": {"scenario": "nope"}}, "mock", NOW)
    assert response["statusCode"] == 400
    assert response["headers"] == JSON
    assert "'nope'" in body(response)["error"]
    assert "cascade_early" in body(response)["error"]


@pytest.mark.parametrize("source", [None, "live", "Mock", ""])
def test_any_source_but_mock_is_a_503_and_never_mock_data(source):
    response = handler.respond({}, source, NOW)
    assert response["statusCode"] == 503
    assert response["headers"] == JSON
    assert list(body(response)) == ["error"]
    assert repr(source) in body(response)["error"]


def test_handler_reads_the_source_from_the_environment_and_the_real_clock(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SOURCE", "mock")
    before = datetime.now(timezone.utc)
    response = handler.handler({}, None)
    after = datetime.now(timezone.utc)
    assert response["statusCode"] == 200
    # The mock's window is the minute before the real now, so it is never stale.
    assert body(response)["health"]["status"] == "HEALTHY"
    # A clock without a time zone would write times with no Z, which parse as naive and can't be compared
    # with an aware one. generated_at is truncated to the millisecond, hence the 1 ms allowance.
    generated = datetime.fromisoformat(body(response)["generated_at"])
    assert before - timedelta(milliseconds=1) <= generated <= after


def test_handler_without_the_setting_refuses(monkeypatch):
    monkeypatch.delenv("DASHBOARD_SOURCE", raising=False)
    assert handler.handler({}, None)["statusCode"] == 503
