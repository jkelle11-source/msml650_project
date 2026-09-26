"""Local unit tests for the Product handler. No AWS or boto3 needed.

Run from this folder:  python -m unittest -v test_handler
"""
import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

import handler as product

SCHEMA_FIELDS = {
    "timestamp", "request_id", "service", "endpoint", "http_method",
    "status_code", "latency_ms", "lambda_duration_ms", "cold_start",
    "db_latency_ms", "db_consumed_capacity",
    "dependency", "dependency_latency_ms", "dependency_error",
}
TIER2_FIELDS = {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"}

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
    def __init__(self, items, pages=1, fail=False):
        self.items = items
        self.pages = pages
        self.fail = fail

    def scan(self, ReturnConsumedCapacity=None, ExclusiveStartKey=None):
        if self.fail:
            raise RuntimeError("boom")
        values = list(self.items.values())
        # Split into `pages` pages to exercise pagination.
        start = ExclusiveStartKey["page"] if ExclusiveStartKey else 0
        size = -(-len(values) // self.pages)
        page = {
            "Items": values[start * size:(start + 1) * size],
            "ConsumedCapacity": {"CapacityUnits": 0.5},
        }
        if start + 1 < self.pages:
            page["LastEvaluatedKey"] = {"page": start + 1}
        return page

    def get_item(self, Key, ReturnConsumedCapacity=None):
        if self.fail:
            raise RuntimeError("boom")
        page = {"ConsumedCapacity": {"CapacityUnits": 0.5}}
        if Key["id"] in self.items:
            page["Item"] = self.items[Key["id"]]
        return page


class ProductHandlerTest(unittest.TestCase):
    def invoke(self, event, table=None):
        table = table or FakeTable(PRODUCTS)
        out = io.StringIO()
        with mock.patch.object(product, "_table", return_value=table), redirect_stdout(out):
            resp = product.handler(event, None)
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

    def test_cold_start_only_first_invocation(self):
        product._cold_start = True
        _, _, first = self.invoke(_event("/products"))
        _, _, second = self.invoke(_event("/products"))
        self.assertTrue(first["cold_start"])
        self.assertFalse(second["cold_start"])


if __name__ == "__main__":
    unittest.main()
