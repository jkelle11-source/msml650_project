"""Tests for the dashboard read API's handler (M7-2; the live source's wiring, M8-5)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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


@pytest.mark.parametrize("source", [None, "Live", "Mock", "LIVE", ""])
def test_any_source_but_mock_or_live_is_a_503_and_never_mock_data(source):
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


# ---- live (M8-5) ------------------------------------------------------------------------------
# respond takes the live reader as `read`; these pass stand-ins, so no AWS is reached. The live source's
# own reads are tested in test_live_source.py.

LIVE_INPUTS = mock_source.read("incident", NOW)       # a stand-in for what live_source.read returns


def reads(inputs=LIVE_INPUTS):
    calls = []

    def read():
        calls.append(1)
        return inputs
    return read, calls


def failing(error):
    def read():
        raise error
    return read


def test_live_serves_the_builder_s_reply_for_the_live_inputs():
    read, calls = reads()
    response = handler.respond({}, "live", NOW, read=read)
    assert response["statusCode"] == 200 and response["headers"] == JSON
    assert body(response) == builder.build_dashboard(*LIVE_INPUTS, NOW)
    assert calls == [1]


def test_live_with_a_null_or_other_query_string_still_serves_live():
    for event in ({"queryStringParameters": None}, {"queryStringParameters": {"cache": "1"}}):
        assert handler.respond(event, "live", NOW, read=reads()[0])["statusCode"] == 200


def test_live_with_no_incident_serves_healthy():
    response = handler.respond({}, "live", NOW, read=reads(mock_source.read("healthy", NOW))[0])
    assert body(response)["health"]["status"] == "HEALTHY" and body(response)["incident"] is None


def test_live_refuses_a_scenario_without_reading_anything():
    read, calls = reads()
    response = handler.respond({"queryStringParameters": {"scenario": "incident"}}, "live", NOW, read=read)
    assert response["statusCode"] == 400 and response["headers"] == JSON
    assert "?source=mock" in body(response)["error"]
    assert calls == []


@pytest.mark.parametrize("error", [RuntimeError("Incidents table unreachable"), KeyError("window_start"),
                                   ValueError("bad manifest")])
def test_a_failed_live_read_is_a_503_with_its_reason_and_never_mock_data(error, capsys):
    response = handler.respond({}, "live", NOW, read=failing(error))
    assert response["statusCode"] == 503 and response["headers"] == JSON
    assert list(body(response)) == ["error"]
    assert type(error).__name__ in body(response)["error"]
    logged = json.loads(capsys.readouterr().out)
    assert logged["level"] == "ERROR" and logged["error"] == type(error).__name__


def test_a_live_reply_json_can_t_write_is_a_503_not_a_crash():
    # A Decimal left unconverted from DynamoDB: json.dumps can't write it.
    window, incident, manifest = LIVE_INPUTS
    unconverted = {**window, "system_p95_latency_ms": Decimal("260")}
    response = handler.respond({}, "live", NOW, read=reads((unconverted, incident, manifest))[0])
    assert response["statusCode"] == 503 and "TypeError" in body(response)["error"]


@pytest.mark.parametrize("unset", [("INCIDENTS_TABLE",), ("TELEMETRY_BUCKET",), ("INCIDENTS_TABLE", "TELEMETRY_BUCKET")])
def test_handler_live_without_its_table_or_bucket_is_a_503_naming_them(monkeypatch, unset):
    monkeypatch.setenv("DASHBOARD_SOURCE", "live")
    monkeypatch.setenv("INCIDENTS_TABLE", "anomalypulse-incidents")
    monkeypatch.setenv("TELEMETRY_BUCKET", "anomalypulse-telemetry")
    for name in unset:
        monkeypatch.delenv(name)
    response = handler.handler({}, None)
    assert response["statusCode"] == 503
    assert all(name in body(response)["error"] for name in unset)


def test_handler_live_reads_through_live_source_with_the_configured_names(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_SOURCE", "live")
    monkeypatch.setenv("INCIDENTS_TABLE", "anomalypulse-incidents")
    monkeypatch.setenv("TELEMETRY_BUCKET", "anomalypulse-telemetry")
    monkeypatch.setattr(handler, "_aws", lambda name: (f"table:{name}", "s3-client"))
    monkeypatch.setattr(handler.live_source, "read",
                        lambda table, s3, bucket: seen.update(table=table, s3=s3, bucket=bucket) or LIVE_INPUTS)
    response = handler.handler({}, None)
    assert response["statusCode"] == 200
    assert seen == {"table": "table:anomalypulse-incidents", "s3": "s3-client", "bucket": "anomalypulse-telemetry"}
