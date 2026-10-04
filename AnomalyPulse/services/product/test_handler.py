"""Local unit tests for the Product handler. No AWS or boto3 needed (jsonschema is).

Run from this folder:  python -m unittest -v test_handler
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAYER_DIR = os.path.join(_HERE, "..", "..", "layers", "telemetry", "python")
_CATALOG = os.path.join(_HERE, "..", "..", "infra", "shared", "field_catalog.json")

# The validator ships in the anomalypulse-telemetry Lambda layer; locally it is
# only importable once the layer's python/ dir is on sys.path.
sys.path.insert(0, os.path.abspath(_LAYER_DIR))

import handler as product  # noqa: E402
from schema_validator import validate  # noqa: E402

# Read the field lists from Jake's contract files so these tests can't drift from them.
with open(os.path.join(_LAYER_DIR, "telemetry_schema.json")) as f:
    SCHEMA_FIELDS = set(json.load(f)["required"])
with open(_CATALOG) as f:
    TIER2_FIELDS = {
        name for name, meta in json.load(f)["fields"].items()
        if meta["tier"] == "tier2_ground_truth"
    }

PRODUCTS = {
    "prod-002": {"id": "prod-002", "name": "Keyboard", "price": Decimal("89.00"), "stock": Decimal(75)},
    "prod-001": {"id": "prod-001", "name": "Mouse", "price": Decimal("24.99"), "stock": Decimal(150)},
}


def _event(resource, method="GET", path_params=None):
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": path_params,
        "requestContext": {"requestId": "req-123"},
    }


class FakeTable:
    def __init__(self, items, pages=1, fail=False, capacity=True):
        self.items = items
        self.pages = pages
        self.fail = fail
        self.capacity = capacity  # False => responses omit ConsumedCapacity

    def scan(self, ReturnConsumedCapacity=None, ExclusiveStartKey=None):
        if self.fail:
            raise RuntimeError("boom")
        values = list(self.items.values())
        # Split into `pages` pages to exercise pagination.
        start = ExclusiveStartKey["page"] if ExclusiveStartKey else 0
        size = -(-len(values) // self.pages)
        page = {"Items": values[start * size:(start + 1) * size]}
        if self.capacity:
            page["ConsumedCapacity"] = {"CapacityUnits": 0.5}
        if start + 1 < self.pages:
            page["LastEvaluatedKey"] = {"page": start + 1}
        return page

    def get_item(self, Key, ReturnConsumedCapacity=None):
        if self.fail:
            raise RuntimeError("boom")
        page = {}
        if self.capacity:
            page["ConsumedCapacity"] = {"CapacityUnits": 0.5}
        if Key["id"] in self.items:
            page["Item"] = self.items[Key["id"]]
        return page


class ProductHandlerTest(unittest.TestCase):
    def invoke(self, event, table=None, context=None):
        table = table or FakeTable(PRODUCTS)
        out = io.StringIO()
        with mock.patch.object(product, "_table", return_value=table), redirect_stdout(out):
            resp = product.handler(event, context)
        log_lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(log_lines), 1, "exactly one log line per request")
        return resp, json.loads(resp["body"]), json.loads(log_lines[0])

    def test_list_products(self):
        resp, body, _ = self.invoke(_event("/products"))
        self.assertEqual(resp["statusCode"], 200)
        self.assertIsNone(body["error"])
        self.assertEqual([p["id"] for p in body["data"]], ["prod-001", "prod-002"])
        self.assertEqual(body["data"][0]["price"], 24.99)
        self.assertEqual(body["data"][0]["stock"], 150)

    def test_list_products_follows_pagination(self):
        resp, body, log = self.invoke(_event("/products"), FakeTable(PRODUCTS, pages=2))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(log["db_consumed_capacity"], 1.0)

    def test_list_products_empty_table(self):
        resp, body, _ = self.invoke(_event("/products"), FakeTable({}))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["data"], [])

    def test_get_product(self):
        resp, body, _ = self.invoke(_event("/products/{id}", path_params={"id": "prod-002"}))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["data"]["name"], "Keyboard")
        self.assertIsNone(body["error"])

    def test_get_product_not_found(self):
        resp, body, _ = self.invoke(_event("/products/{id}", path_params={"id": "nope"}))
        self.assertEqual(resp["statusCode"], 404)
        self.assertIsNone(body["data"])
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_get_product_missing_id(self):
        resp, body, _ = self.invoke(_event("/products/{id}"))
        self.assertEqual(resp["statusCode"], 400)
        self.assertEqual(body["error"]["code"], "BAD_REQUEST")

    def test_unhandled_route(self):
        resp, body, log = self.invoke(_event("/products", method="DELETE"))
        self.assertEqual(resp["statusCode"], 501)
        self.assertEqual(body["error"]["code"], "NOT_IMPLEMENTED")
        self.assertIsNone(log["db_latency_ms"])

    def test_db_failure_returns_envelope_not_trace(self):
        resp, body, log = self.invoke(_event("/products"), FakeTable(PRODUCTS, fail=True))
        self.assertEqual(resp["statusCode"], 500)
        self.assertEqual(body["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("boom", resp["body"])
        self.assertEqual(log["status_code"], 500)
        # The failure-path log line must stay Tier-1 only and never carry the
        # exception text (the cause is Tier-2 ground truth).
        self.assertFalse(set(log) & TIER2_FIELDS)
        self.assertNotIn("boom", json.dumps(log))

    def test_log_line_matches_schema(self):
        _, _, log = self.invoke(_event("/products/{id}", path_params={"id": "prod-001"}))
        self.assertEqual(set(log), SCHEMA_FIELDS)
        self.assertFalse(set(log) & TIER2_FIELDS)
        self.assertEqual(log["service"], "product-service")
        self.assertEqual(log["endpoint"], "/products/{id}")
        self.assertEqual(log["http_method"], "GET")
        self.assertEqual(log["request_id"], "req-123")
        self.assertEqual(log["status_code"], 200)
        self.assertEqual(log["db_consumed_capacity"], 0.5)
        self.assertIsNotNone(log["db_latency_ms"])
        self.assertIsNone(log["dependency"])
        self.assertRegex(log["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_records_validate_on_every_path(self):
        # Every response path must emit a record that passes the shared validator
        # (M2-2 DoD: "Sample of Product records validates clean").
        cases = {
            "list 200": (_event("/products"), None, 200),
            "get 200": (_event("/products/{id}", path_params={"id": "prod-001"}), None, 200),
            "get 404": (_event("/products/{id}", path_params={"id": "nope"}), None, 404),
            "get 400": (_event("/products/{id}"), None, 400),
            "unrouted 501": (_event("/products", method="DELETE"), None, 501),
            "db error 500": (_event("/products"), FakeTable(PRODUCTS, fail=True), 500),
        }
        for name, (event, table, status) in cases.items():
            with self.subTest(name):
                _, _, log = self.invoke(event, table)
                ok, errors = validate(log)
                self.assertTrue(ok, errors and errors[0].message)
                self.assertEqual(log["status_code"], status)
                self.assertFalse(set(log) & TIER2_FIELDS)

    def test_db_fields_null_without_db_call(self):
        _, _, log = self.invoke(_event("/products/{id}"))  # 400 before any DB call
        self.assertIsNone(log["db_latency_ms"])
        self.assertIsNone(log["db_consumed_capacity"])
        self.assertTrue(validate(log)[0])

    def test_timings_are_non_negative(self):
        _, _, log = self.invoke(_event("/products"))
        self.assertGreaterEqual(log["latency_ms"], 0)
        self.assertGreaterEqual(log["lambda_duration_ms"], 0)
        self.assertGreaterEqual(log["db_latency_ms"], 0)

    def test_request_id_falls_back_to_lambda_context(self):
        event = _event("/products")
        event["requestContext"] = {}
        _, _, log = self.invoke(event, context=SimpleNamespace(aws_request_id="lambda-456"))
        self.assertEqual(log["request_id"], "lambda-456")

    def test_request_id_never_null(self):
        event = _event("/products")
        del event["requestContext"]
        _, _, log = self.invoke(event)
        self.assertTrue(log["request_id"])
        self.assertTrue(validate(log)[0])

    def test_invalid_record_is_dropped_not_raised(self):
        # PUT isn't in the schema's http_method enum, so the record is invalid:
        # the request must still get its response, and the invalid record must
        # not be printed (it carries no request_id, so it isn't shipped to S3).
        resp, body, log = self.invoke(_event("/products", method="PUT"))
        self.assertEqual(resp["statusCode"], 501)
        self.assertEqual(body["error"]["code"], "NOT_IMPLEMENTED")
        self.assertEqual(set(log), {"telemetry_error"})

    def test_telemetry_crash_does_not_fail_request(self):
        with mock.patch.object(product, "validate", side_effect=RuntimeError("validator down")):
            resp, body, log = self.invoke(_event("/products/{id}", path_params={"id": "prod-001"}))
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["data"]["id"], "prod-001")
        self.assertEqual(set(log), {"telemetry_error"})

    def test_response_has_cors_header(self):
        # The dashboard is a cross-origin browser client, so every response
        # (including error envelopes) must carry Access-Control-Allow-Origin.
        resp, _, _ = self.invoke(_event("/products"))
        self.assertEqual(resp["headers"]["Access-Control-Allow-Origin"], "*")
        err, _, _ = self.invoke(_event("/products/{id}", path_params={"id": "nope"}))
        self.assertEqual(err["headers"]["Access-Control-Allow-Origin"], "*")

    def test_cold_start_only_first_invocation(self):
        product._cold_start = True
        _, _, first = self.invoke(_event("/products"))
        _, _, second = self.invoke(_event("/products"))
        self.assertTrue(first["cold_start"])
        self.assertFalse(second["cold_start"])

    def test_malformed_event_drops_telemetry_without_failing_request(self):
        # An event missing resource/httpMethod is still served (501), but the
        # record it would emit has null endpoint/http_method, fails the schema,
        # and is dropped -- a served request that ships no telemetry. This
        # documents that silent-loss behaviour so a regression that instead
        # shipped a half-null record would fail here.
        resp, body, log = self.invoke({"requestContext": {"requestId": "req-x"}})
        self.assertEqual(resp["statusCode"], 501)
        self.assertEqual(body["error"]["code"], "NOT_IMPLEMENTED")
        self.assertEqual(set(log), {"telemetry_error"})

    def test_db_capacity_null_when_dynamodb_omits_it(self):
        # If a DynamoDB response carries no ConsumedCapacity, the record must
        # still validate with db_consumed_capacity=null, while db_latency_ms is
        # set because a call did happen.
        _, _, log = self.invoke(_event("/products"), FakeTable(PRODUCTS, capacity=False))
        self.assertIsNone(log["db_consumed_capacity"])
        self.assertIsNotNone(log["db_latency_ms"])
        self.assertGreaterEqual(log["db_latency_ms"], 0)
        self.assertTrue(validate(log)[0])

    def test_db_latency_sums_across_pages(self):
        # Two scan pages -> two timed DB calls -> db_latency_ms is their sum.
        # perf_counter is stubbed (start, end per call) so the sum is exact.
        ticks = iter([0.0, 1.0, 1.0, 3.0])  # call 1: 0->1s, call 2: 1->3s
        with mock.patch.object(product.time, "perf_counter", lambda: next(ticks)):
            _, _, log = self.invoke(_event("/products"), FakeTable(PRODUCTS, pages=2))
        self.assertEqual(log["db_latency_ms"], 3000.0)  # 1000ms + 2000ms
        self.assertEqual(log["db_consumed_capacity"], 1.0)  # 0.5 + 0.5

    def test_json_default_rejects_unserializable(self):
        # Decimal is handled (covered by test_list_products); anything else must
        # raise rather than silently coerce or crash the response.
        with self.assertRaises(TypeError):
            product._json_default(object())


if __name__ == "__main__":
    unittest.main()
