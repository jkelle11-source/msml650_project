"""The dashboard read API's Lambda (M7-2, M8-5): GET /dashboard returns the contract's object.

The only code here that reads the clock or the environment. DASHBOARD_SOURCE picks where the inputs come
from: "mock" serves the contract's scenarios (?scenario=, default healthy); "live" reads the newest health,
the open incident and its run's manifest (live_source.py) from the Incidents table and the telemetry bucket
named by INCIDENTS_TABLE and TELEMETRY_BUCKET. Anything else answers 503, and so does a live read that fails:
never mock data in place of live. CORS is set on the HTTP API in the stack's template, not here: API Gateway
ignores a function's own CORS headers when the API has a CORS configuration.
"""
import json
import os
from datetime import datetime, timezone
from functools import lru_cache

import live_source
import mock_source
from builder import build_dashboard

DEFAULT_SCENARIO = "healthy"
LIVE_SETTINGS = ("INCIDENTS_TABLE", "TELEMETRY_BUCKET")


def _response(status_code: int, payload: dict) -> dict:
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(payload)}


@lru_cache(maxsize=1)
def _aws(table_name: str):
    """The Incidents table and an S3 client, made once per warm Lambda. boto3 is imported only here, so the
    mock path needs nothing beyond the standard library."""
    import boto3
    return boto3.resource("dynamodb").Table(table_name), boto3.client("s3")


def read_live() -> tuple:
    """The live inputs for build_dashboard: (window, incident, manifest)."""
    missing = [name for name in LIVE_SETTINGS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"{' and '.join(missing)} not set")
    table, s3 = _aws(os.environ["INCIDENTS_TABLE"])
    return live_source.read(table, s3, os.environ["TELEMETRY_BUCKET"])


def _live(now: datetime, read) -> dict:
    # The live API ignores ?scenario= (Docs/DASHBOARD_CONTRACT.md, "The call").
    try:
        body = json.dumps(build_dashboard(*read(), now))
    except Exception as e:  # noqa: BLE001 - any failure is a 503 with its reason, which the console shows
        print(json.dumps({"level": "ERROR", "message": "live read failed", "error": type(e).__name__,
                          "detail": str(e)}))
        return _response(503, {"error": f"live read failed: {type(e).__name__}: {e}"})
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": body}


def respond(event: dict, source: str | None, now: datetime, read=read_live) -> dict:
    """The HTTP response to one GET /dashboard. `read` gives the live inputs (tests pass their own)."""
    # An HTTP API event has no queryStringParameters key, or None, when the URL has no query string.
    params = event.get("queryStringParameters") or {}
    if source == "live":
        return _live(now, read)
    if source != "mock":
        # Never fall back to mock: a live deploy missing its setting would show made-up incidents as real.
        return _response(503, {"error": f"DASHBOARD_SOURCE is {source!r}; this build serves 'mock' or 'live'"})
    try:
        inputs = mock_source.read(params.get("scenario", DEFAULT_SCENARIO), now)
    except ValueError as e:
        return _response(400, {"error": str(e)})
    return _response(200, build_dashboard(*inputs, now))


def handler(event, context):
    return respond(event, os.environ.get("DASHBOARD_SOURCE"), datetime.now(timezone.utc))
