"""Workload generator (M3-2): the baseline traffic every experiment runs on.

Sends requests to the live API as an open-loop Poisson process: arrivals come
at `rps` on average whether or not earlier requests have finished, the way
independent users do. A closed loop (send, wait, send) would quietly slow down
when the service does, hiding exactly the latency shifts the experiments are
meant to produce.

The runner calls describe(), start() and stop(). Injectors reach the rest
through ctx.workload; the traffic spike uses set_rate_multiplier() and
outcomes().

Default endpoint mix, from project plan section 7:

    60% GET /products        15% GET /products/{id}      10% POST /cart
    10% GET /orders/{id}      5% POST /orders

(The plan writes "GET /orders"; the API's route is GET /orders/{id}, so the
generator reads back orders it created.)
"""
import http.client
import json
import random
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

ROUTES = ("GET /products", "GET /products/{id}", "POST /cart", "GET /orders/{id}", "POST /orders")
DEFAULT_MIX = dict(zip(ROUTES, (60, 15, 10, 10, 5)))
# The plan's "normal" rate (section 5: normal 20 requests/sec).
DEFAULT_RPS = 20
DEFAULT_USERS = 50

# The ids services/product/seed.py writes: prod-001 ... prod-025.
PRODUCT_IDS = tuple(f"prod-{i:03d}" for i in range(1, 26))

_API_URL_PARAM = "/anomalypulse/api/base-url"


def api_base_url(session) -> str:
    """The deployed API's base URL, from SSM (the simulator role can read it)."""
    value = session.client("ssm").get_parameter(Name=_API_URL_PARAM)["Parameter"]["Value"]
    return value.rstrip("/")


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Workload:
    """Open-loop baseline traffic against the AnomalyPulse API.

    Configuration is fixed at construction and recorded by describe(). Nothing
    is sent, and no AWS or network call is made, until start().
    """

    def __init__(self, *, rps: float, users: int = DEFAULT_USERS, mix: dict | None = None, seed: int = 0,
                 base_url: str | None = None, timeout_s: float = 10, max_in_flight: int = 256,
                 log=print):
        mix = dict(DEFAULT_MIX if mix is None else mix)
        if not _is_number(rps) or rps <= 0:
            raise ValueError(f"workload: rps must be a positive number, got {rps!r}")
        if not isinstance(users, int) or isinstance(users, bool) or users < 1:
            raise ValueError(f"workload: users must be a whole number, 1 or more, got {users!r}")
        unknown = set(mix) - set(ROUTES)
        if unknown:
            raise ValueError(f"workload: unknown route(s) {sorted(unknown)}; routes are {list(ROUTES)}")
        if any(not _is_number(w) or w < 0 for w in mix.values()) or sum(mix.values()) <= 0:
            raise ValueError("workload: mix weights must be numbers, 0 or more, not all 0")
        if not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError("workload: max_in_flight must be a whole number, 1 or more")

        total = sum(mix.values())
        self.rps = rps
        self.users = users
        self.mix = {route: mix.get(route, 0) / total for route in ROUTES}
        self.seed = seed
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_s = timeout_s
        self.max_in_flight = max_in_flight
        self._log = log

        # The request sequence comes from its own seeded RNG, so the same seed
        # sends the same routes, users and items in the same order.
        self._rng = random.Random(f"{seed}:workload")
        self._routes = [r for r in ROUTES if self.mix[r] > 0]
        self._weights = [self.mix[r] for r in self._routes]

        self._lock = threading.Lock()
        self._multiplier = 1.0
        self._sent = 0
        self._dropped = 0
        self._in_flight = 0
        self._results = Counter()            # (route, status class) -> count
        self._order_ids = deque(maxlen=1000)  # ids POST /orders created, for GET /orders/{id}

        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._local = threading.local()

    # --- what the runner calls -------------------------------------------------

    def describe(self) -> dict:
        """The settings, recorded as the manifest's `workload`. Plain JSON."""
        return {
            "generator": "open-loop-poisson",
            "request_rate_rps": self.rps,
            "users": self.users,
            "mix": dict(self.mix),
            "max_in_flight": self.max_in_flight,
        }

    def start(self) -> None:
        """Begin sending. Returns at once; the requests go from background threads."""
        if self._thread is not None:
            return
        if not self.base_url:
            raise RuntimeError("workload: no base_url; set it (api_base_url(session)) before start()")
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.max_in_flight, thread_name_prefix="workload")
        self._thread = threading.Thread(target=self._schedule, name="workload-scheduler", daemon=True)
        self._thread.start()
        self._log(f"  workload: {self.rps:g} rps, {self.users} users, "
                  f"mix {', '.join(f'{r} {p:.0%}' for r, p in self.mix.items() if p)}")

    def stop(self) -> None:
        """Stop sending and wait for requests in flight. Safe to repeat, and without start()."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join()
        pool, self._pool = self._pool, None
        if pool is None:
            return
        pool.shutdown(wait=True, cancel_futures=True)
        self._log(f"  workload: {self.summary()}")

    # --- what injectors use ----------------------------------------------------

    def set_rate_multiplier(self, multiplier: float) -> None:
        """Scale the arrival rate: 1 is baseline, 10 is ten times the baseline rps."""
        if not _is_number(multiplier) or multiplier <= 0:
            raise ValueError(f"workload: rate multiplier must be a positive number, got {multiplier!r}")
        with self._lock:
            self._multiplier = float(multiplier)

    @property
    def current_rps(self) -> float:
        with self._lock:
            return self.rps * self._multiplier

    def requests_sent(self) -> int:
        """How many requests have been sent so far (not counting dropped ones)."""
        with self._lock:
            return self._sent

    def outcomes(self) -> dict:
        """What the client has seen so far: requests sent, dropped, and responses by class.

        A response is counted when it comes back. "error" is a request that got
        no HTTP response (a timeout or a reset). A request Lambda throttles
        leaves no telemetry record, so these counts are the only place it shows.
        """
        with self._lock:
            responses = Counter()
            for (_, status_class), n in self._results.items():
                responses[status_class] += n
            return {"sent": self._sent, "dropped": self._dropped, "responses": dict(responses)}

    def summary(self) -> str:
        seen = self.outcomes()
        parts = ", ".join(f"{k} {n}" for k, n in sorted(seen["responses"].items()))
        return f"sent {seen['sent']} ({parts or 'no responses'}), dropped {seen['dropped']}"

    # --- internals ---------------------------------------------------------------

    def _gap_s(self) -> float:
        """Seconds to the next arrival: exponential, so arrivals form a Poisson process."""
        return self._rng.expovariate(self.current_rps)

    def _next_request(self) -> tuple:
        """(route, method, path, body) for the next request. Draws only from the seeded RNG.

        Each route draws the same values every time, whatever the API has
        answered so far, so the same seed gives the same sequence of routes,
        users and items. Only which existing order a GET /orders/{id} reads
        depends on the API.
        """
        rng = self._rng
        route = rng.choices(self._routes, self._weights)[0]
        user = f"user-{rng.randrange(self.users):04d}"
        product = rng.choice(PRODUCT_IDS)
        if route == "GET /products":
            return route, "GET", "/products", None
        if route == "GET /products/{id}":
            return route, "GET", f"/products/{product}", None
        if route == "POST /cart":
            return route, "POST", "/cart", {"user_id": user, "item_id": product,
                                            "quantity": rng.randint(1, 3)}
        if route == "POST /orders":
            return route, "POST", "/orders", {
                "customer_id": user,
                "items": [{"product_id": product, "quantity": rng.randint(1, 3),
                           "price": round(rng.uniform(5, 250), 2)}],
            }
        # GET /orders/{id}: read back an order this run created. Until the first
        # one exists (about the first second of a run) the id is unknown, so the
        # read is a 404, as it would be for a user with a stale link.
        pick = rng.random()
        # Drawn even when an order exists: a draw that depended on whether the
        # first order had come back yet would shift the rest of the sequence.
        unknown_id = uuid.UUID(int=rng.getrandbits(128))
        with self._lock:
            order_id = self._order_ids[int(pick * len(self._order_ids))] if self._order_ids else None
        return route, "GET", f"/orders/{order_id or unknown_id}", None

    def _schedule(self) -> None:
        next_at = time.monotonic()
        while not self._stop.is_set():
            next_at += self._gap_s()
            delay = next_at - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            request = self._next_request()
            with self._lock:
                if self._in_flight >= self.max_in_flight:
                    # The client can't keep up. Dropping keeps the arrival rate
                    # honest instead of letting a backlog build and burst later.
                    self._dropped += 1
                    continue
                self._in_flight += 1
                self._sent += 1
            self._pool.submit(self._send, *request)

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            url = urlsplit(self.base_url)
            cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
            conn = cls(url.netloc, timeout=self.timeout_s)
            self._local.conn = conn
            self._local.prefix = url.path
        return conn

    def _send(self, route, method, path, body) -> None:
        status_class = "error"
        try:
            # One keep-alive connection per worker thread, so a request doesn't
            # pay a new TLS handshake each time.
            conn = self._connection()
            payload = None if body is None else json.dumps(body)
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, self._local.prefix + path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            status_class = f"{response.status // 100}xx"
            if route == "POST /orders" and response.status == 201:
                order_id = ((json.loads(data) or {}).get("data") or {}).get("order_id")
                if order_id:
                    with self._lock:
                        self._order_ids.append(order_id)
        except Exception:
            # A client-side failure (timeout, reset). Drop the connection so the
            # next request on this thread opens a fresh one.
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
            self._local.conn = None
        finally:
            with self._lock:
                self._in_flight -= 1
                self._results[(route, status_class)] += 1
