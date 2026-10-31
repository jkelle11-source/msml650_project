"""The dashboard read API's Lambda (M7-2): GET /dashboard returns the contract's object.

The only code here that reads the clock or the environment. DASHBOARD_SOURCE picks where the inputs come
from: "mock" serves the contract's scenarios (?scenario=, default healthy); "live" arrives in M8-5.
CORS is set on the HTTP API in the stack's template, not here: API Gateway ignores a function's own CORS
headers when the API has a CORS configuration.
"""
import json
import os
from datetime import datetime, timezone

import mock_source
from builder import build_dashboard

DEFAULT_SCENARIO = "healthy"


def _response(status_code: int, payload: dict) -> dict:
    return {"statusCode": status_code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(payload)}


def respond(event: dict, source: str | None, now: datetime) -> dict:
    """The HTTP response to one GET /dashboard."""
    if source != "mock":
        # Never fall back to mock: a live deploy missing its setting would show made-up incidents as real.
        return _response(503, {"error": f"DASHBOARD_SOURCE is {source!r}; this build serves only 'mock'"})
    # An HTTP API event has no queryStringParameters key, or None, when the URL has no query string.
    scenario = (event.get("queryStringParameters") or {}).get("scenario", DEFAULT_SCENARIO)
    try:
        inputs = mock_source.read(scenario, now)
    except ValueError as e:
        return _response(400, {"error": str(e)})
    return _response(200, build_dashboard(*inputs, now))


def handler(event, context):
    return respond(event, os.environ.get("DASHBOARD_SOURCE"), datetime.now(timezone.utc))
