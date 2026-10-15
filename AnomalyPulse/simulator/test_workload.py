import json
import statistics
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import workload as wl
from simulator.workload import DEFAULT_MIX, ROUTES, Workload


def _quiet(*args):
    pass


# --- a local stand-in for the API ------------------------------------------------------------

class _FakeApi(BaseHTTPRequestHandler):
    # Answers the five routes the way the real services do, and records what it got.
    received = None
    lock = threading.Lock()

    def _record(self, body):
        with self.lock:
            self.received.append((self.command, self.path, body))

    def _reply(self, status, data):
        payload = json.dumps({"data": data, "error": None}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._record(None)
        self._reply(200, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        if self.path.endswith("/orders"):
            self._reply(201, {"order_id": f"order-{len(self.received)}"})
        else:
            self._reply(200, body)

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    handler = type("Api", (_FakeApi,), {"received": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/dev", handler.received
    server.shutdown()
    server.server_close()


# --- configuration ---------------------------------------------------------------------------

def test_describe_is_plain_json_with_a_normalized_mix():
    described = Workload(rps=20, seed=1).describe()
    assert json.loads(json.dumps(described, allow_nan=False)) == described
    assert described["request_rate_rps"] == 20
    assert described["users"] == 50
    assert described["mix"] == {"GET /products": 0.6, "GET /products/{id}": 0.15,
                                "POST /cart": 0.1, "GET /orders/{id}": 0.1, "POST /orders": 0.05}
    assert sum(described["mix"].values()) == pytest.approx(1)


def test_default_mix_matches_the_project_plan():
    assert DEFAULT_MIX == {"GET /products": 60, "GET /products/{id}": 15, "POST /cart": 10,
                           "GET /orders/{id}": 10, "POST /orders": 5}


def test_a_partial_mix_leaves_other_routes_out():
    load = Workload(rps=20, mix={"GET /products": 3, "POST /cart": 1})
    assert load.mix["GET /products"] == 0.75
    assert load.mix["POST /orders"] == 0
    routes = {load._next_request()[0] for _ in range(500)}
    assert routes == {"GET /products", "POST /cart"}


@pytest.mark.parametrize("kwargs, problem", [
    ({"rps": 0}, "rps must be a positive number"),
    ({"rps": -5}, "rps must be a positive number"),
    ({"rps": True}, "rps must be a positive number"),
    ({"rps": 20, "users": 0}, "users must be a whole number"),
    ({"rps": 20, "mix": {"GET /nope": 1}}, "unknown route"),
    ({"rps": 20, "mix": {"GET /products": 0}}, "not all 0"),
    ({"rps": 20, "mix": {"GET /products": -1, "POST /cart": 2}}, "0 or more"),
])
def test_bad_configuration_is_refused(kwargs, problem):
    with pytest.raises(ValueError, match=problem):
        Workload(**kwargs)


def test_constructing_touches_nothing():
    # The CLI builds the workload for a dry run, so construction makes no call.
    load = Workload(rps=20)
    assert load.base_url is None
    assert load.requests_sent() == 0


# --- what it sends ---------------------------------------------------------------------------

def _sequence(seed, n=200):
    load = Workload(rps=20, seed=seed)
    return [load._next_request() for _ in range(n)]


def test_same_seed_sends_the_same_sequence():
    assert _sequence(7) == _sequence(7)
    assert _sequence(7) != _sequence(8)


def test_the_sequence_does_not_depend_on_when_orders_come_back():
    # How soon the first POST /orders answers is up to the API. The routes,
    # users and items drawn after it must not move with that timing.
    def sequence(first_order_at):
        load = Workload(rps=20, seed=7, mix={"POST /orders": 1, "GET /orders/{id}": 1})
        drawn = []
        for i in range(200):
            if i == first_order_at:
                load._order_ids.append("order-1")
            route, _, _, body = load._next_request()
            drawn.append((route, body))
        return drawn

    assert sequence(3) == sequence(50)


def test_route_frequencies_follow_the_mix():
    load = Workload(rps=20, seed=3)
    counts = Counter(load._next_request()[0] for _ in range(20_000))
    for route, weight in DEFAULT_MIX.items():
        assert counts[route] / 20_000 == pytest.approx(weight / 100, abs=0.01)


def test_requests_are_shaped_for_each_service():
    load = Workload(rps=20, users=5, seed=2)
    seen = {}
    for _ in range(2000):
        route, method, path, body = load._next_request()
        seen.setdefault(route, (method, path, body))
    assert seen["GET /products"] == ("GET", "/products", None)
    assert seen["GET /products/{id}"][1] in {f"/products/{p}" for p in wl.PRODUCT_IDS}
    cart = seen["POST /cart"][2]
    assert set(cart) == {"user_id", "item_id", "quantity"}
    assert cart["item_id"] in wl.PRODUCT_IDS and cart["user_id"].startswith("user-000")
    order = seen["POST /orders"][2]
    assert set(order) == {"customer_id", "items"}
    assert {"product_id", "quantity", "price"} == set(order["items"][0])
    assert seen["GET /orders/{id}"][1].startswith("/orders/")


def test_users_spread_over_the_configured_count():
    load = Workload(rps=20, users=3, mix={"POST /cart": 1}, seed=1)
    users = {load._next_request()[3]["user_id"] for _ in range(300)}
    assert users == {"user-0000", "user-0001", "user-0002"}


def test_arrival_gaps_average_one_over_the_rate():
    load = Workload(rps=20, seed=1)
    assert statistics.mean(load._gap_s() for _ in range(20_000)) == pytest.approx(1 / 20, rel=0.05)


def test_the_multiplier_scales_the_rate():
    load = Workload(rps=20, seed=1)
    load.set_rate_multiplier(10)
    assert load.current_rps == 200
    assert statistics.mean(load._gap_s() for _ in range(20_000)) == pytest.approx(1 / 200, rel=0.05)
    load.set_rate_multiplier(1)
    assert load.current_rps == 20


@pytest.mark.parametrize("bad", [0, -1, "2", None, True])
def test_bad_multiplier_is_refused(bad):
    with pytest.raises(ValueError):
        Workload(rps=20).set_rate_multiplier(bad)


# --- running against an HTTP server ----------------------------------------------------------

def test_start_without_a_base_url_is_refused():
    with pytest.raises(RuntimeError, match="no base_url"):
        Workload(rps=20, log=_quiet).start()


def test_stop_is_safe_without_start_and_twice():
    load = Workload(rps=20, log=_quiet)
    load.stop()
    load.stop()


def test_it_sends_traffic_and_reads_back_created_orders(api):
    base_url, received = api
    load = Workload(rps=200, base_url=base_url, seed=4, log=_quiet,
                    mix={"POST /orders": 1, "GET /orders/{id}": 1})
    load.start()
    time.sleep(1.5)
    load.stop()
    load.stop()   # repeat is harmless

    assert load.requests_sent() > 50
    assert len(received) == load.requests_sent()
    assert all(path.startswith("/dev/") for _, path, _ in received)
    reads = [path for method, path, _ in received if method == "GET"]
    # After the first order exists, reads use ids the API returned.
    assert any(path.startswith("/dev/orders/order-") for path in reads)
    assert "dropped 0" in load.summary()
    seen = load.outcomes()
    assert seen["sent"] == load.requests_sent() and seen["dropped"] == 0
    # The fake API answers 201 to a POST and 200 to a GET.
    assert seen["responses"] == {"2xx": len(received)}
    assert json.loads(json.dumps(seen)) == seen


def test_it_stops_sending_when_stopped(api):
    base_url, received = api
    load = Workload(rps=100, base_url=base_url, log=_quiet)
    load.start()
    time.sleep(0.5)
    load.stop()
    count = len(received)
    time.sleep(0.3)
    assert len(received) == count


def test_rate_roughly_matches_rps_and_the_multiplier(api):
    base_url, _ = api
    load = Workload(rps=40, base_url=base_url, mix={"GET /products": 1}, log=_quiet, seed=5)
    load.start()
    time.sleep(2)
    baseline = load.requests_sent()
    load.set_rate_multiplier(4)
    time.sleep(2)
    spiked = load.requests_sent() - baseline
    load.stop()
    # Loose bounds: this is wall-clock timing on a shared machine.
    assert 40 <= baseline <= 130          # ~80 expected
    assert spiked >= 2 * baseline         # ~320 expected


def test_unreachable_api_is_counted_not_raised():
    load = Workload(rps=50, base_url="http://127.0.0.1:9", timeout_s=0.5, log=_quiet)
    load.start()
    time.sleep(0.5)
    load.stop()
    assert load.requests_sent() > 0
    assert "error" in load.summary()
    assert set(load.outcomes()["responses"]) == {"error"}


def test_api_base_url_reads_ssm():
    class Ssm:
        def get_parameter(self, Name):
            assert Name == "/anomalypulse/api/base-url"
            return {"Parameter": {"Value": "https://abc.execute-api.us-east-2.amazonaws.com/dev/"}}

    class Session:
        def client(self, name):
            assert name == "ssm"
            return Ssm()

    assert wl.api_base_url(Session()) == "https://abc.execute-api.us-east-2.amazonaws.com/dev"


def test_routes_are_the_api_routes():
    assert ROUTES == ("GET /products", "GET /products/{id}", "POST /cart", "GET /orders/{id}",
                      "POST /orders")


# --- retries (M3-5's retry amplification) ------------------------------------------------------

_REQUEST = ("GET /products", "GET", "/products", None)


def test_retries_are_off_by_default():
    load = Workload(rps=20, seed=1, log=_quiet)
    with load._lock:
        load._queue_retry(_REQUEST, None)
    assert load._retry_queue == []
    assert load.retry_stats() == {"requests_retried": 0, "retries_sent": 0,
                                  "retries_dropped": 0, "retries_cancelled": 0}


def test_a_whole_number_of_retries_is_queued_one_at_a_time():
    load = Workload(rps=20, seed=1, retry_delay_s=1.0, log=_quiet)
    load.set_retry_policy(2)
    before = time.monotonic()
    with load._lock:
        load._queue_retry(_REQUEST, None)            # the arrival failed
    (due, _, request, left), = load._retry_queue
    assert request == _REQUEST and left == 1        # one more after this one
    assert due >= before + 1.0
    load._retry_queue.clear()
    with load._lock:
        load._queue_retry(_REQUEST, 1)              # the first retry failed too
        load._queue_retry(_REQUEST, 0)              # the last one failed: nothing more
    assert [entry[3] for entry in load._retry_queue] == [0]
    assert load.retry_stats()["requests_retried"] == 1


def test_a_fraction_is_an_extra_retry_that_often():
    load = Workload(rps=20, seed=1, log=_quiet)
    load.set_retry_policy(2.5)
    budgets = Counter()
    for _ in range(2000):
        with load._lock:
            load._queue_retry(_REQUEST, None)
            (_, _, _, left), = load._retry_queue
            load._retry_queue.clear()
        budgets[left + 1] += 1
    assert set(budgets) == {2, 3}
    assert 0.45 < budgets[3] / 2000 < 0.55


def test_retries_do_not_change_the_request_sequence():
    plain = _sequence(7)
    load = Workload(rps=20, seed=7, log=_quiet)
    load.set_retry_policy(1.5)
    drawn = []
    for _ in range(200):
        drawn.append(load._next_request())
        with load._lock:
            load._queue_retry(_REQUEST, None)
    assert drawn == plain


def test_turning_retries_off_cancels_the_waiting_ones():
    load = Workload(rps=20, seed=1, log=_quiet)
    load.set_retry_policy(3)
    with load._lock:
        load._queue_retry(_REQUEST, None)
        load._queue_retry(_REQUEST, None)
    load.set_retry_policy(0)
    assert load._retry_queue == []
    assert load.retry_stats()["retries_cancelled"] == 2
    with load._lock:
        load._queue_retry(_REQUEST, None)           # off: a new failure is not retried
    assert load._retry_queue == []


@pytest.mark.parametrize("bad", [-1, True, "2", float("nan"), float("inf"), None])
def test_bad_retry_policy_is_refused(bad):
    with pytest.raises(ValueError, match="retries_per_failure"):
        Workload(rps=20, log=_quiet).set_retry_policy(bad)


class _FailingCart(_FakeApi):
    # POST /cart always answers 503, and GET /products/{id} 404; everything else succeeds.
    def do_GET(self):
        self._record(None)
        self._reply(404 if "/products/" in self.path else 200, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        self._reply(503 if self.path.endswith("/cart") else 201, {"order_id": "o"})


def _run_against_failing_cart(mix, seconds=1.5):
    handler = type("Api", (_FailingCart,), {"received": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        load = Workload(rps=40, seed=3, retry_delay_s=0.05, log=_quiet, mix=mix,
                        base_url=f"http://127.0.0.1:{server.server_address[1]}")
        load.set_retry_policy(2)
        load.start()
        time.sleep(seconds)
        load.set_retry_policy(0)
        time.sleep(0.3)
        load.stop()
    finally:
        server.shutdown()
        server.server_close()
    return load, handler.received


def test_failed_requests_are_resent_until_their_retries_run_out():
    # Every POST /cart answers 503, so each new one is sent once and retried twice.
    load, received = _run_against_failing_cart({"POST /cart": 1})
    stats = load.retry_stats()
    assert stats["requests_retried"] > 20
    # Never more than two each. Turning retries off cuts the last few short: those
    # waiting are cancelled, and those in flight then are not followed up.
    assert stats["retries_sent"] <= 2 * stats["requests_retried"]
    assert stats["retries_sent"] >= 2 * stats["requests_retried"] - 10
    assert stats["retries_dropped"] == 0
    # Retries are real requests: the API got them, and the client counts them as sent.
    assert load.outcomes()["sent"] == len(received)
    assert len(received) >= stats["requests_retried"] + stats["retries_sent"]


def test_client_errors_and_successes_are_not_resent():
    # GET /products/{id} answers 404 and GET /products 200: nothing to retry.
    load, received = _run_against_failing_cart({"GET /products/{id}": 1, "GET /products": 1})
    assert len(received) > 20
    assert load.retry_stats() == {"requests_retried": 0, "retries_sent": 0,
                                  "retries_dropped": 0, "retries_cancelled": 0}
