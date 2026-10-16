"""DynamoDB-throttling injector (M3-3).

Produces real throttling on the Products table. The table sits at a fixed,
low provisioned read capacity; for the fault's window this injector reads it
directly, at `load_ratio` x provisioned RCU per second, on top of the
workload's own reads. The ratio is the knob (project plan section 5). The
throttle rate is whatever DynamoDB does, measured afterwards by observe().

The reads are strongly consistent BatchGetItem calls of up to 25 keys. Every
product item is under 4 KB, so each key costs exactly 1 RCU, and a few dozen
calls a second offer thousands of RCU. They are sent open-loop, like the
workload's requests: a scheduler thread hands each call to a worker pool at
its planned time, whether or not earlier calls have come back, so slow calls
don't lower the offered load. If the pool is full, the call is dropped and
counted rather than sent late. The calls take turns over _CLIENTS clients,
each with its own connections: over the one to three connections a single
client uses from inside the region, DynamoDB spends burst capacity at only
about half the surge's rate.

Burst capacity. DynamoDB banks up to 300 s of unused capacity, so a load above
capacity only throttles once that bank is spent: 300 / (r - 1) seconds at
ratio r, whatever the capacity. (M3-3's validation saw no throttling in 5
minutes at 1.75x, and the first throttle 174 s in at 3x and 80 s in at 5.5x.)
So the window opens with a surge at _SURGE_RATIO x capacity, which spends a
full bank in about half a minute; from then on it holds the band's ratio. The
surge ends on DynamoDB's own answer, not on a prediction, so it works however
full the bank is (burst is best-effort): when the reads it served over the
last _SPENT_WINDOW_S seconds come to no more than a spent bank serves. A
throttled read is not that answer, since DynamoDB can refuse reads within
seconds of a full bank. M4 labels the window DATABASE_THROTTLING from
observed["label_from"], 5 s after the bank was spent, and observe() reports
the reads served after that, so a run where burst capacity came back can be
rerun.

A batch DynamoDB could partly serve returns the keys it couldn't read in
UnprocessedKeys; a batch it couldn't serve at all raises
ProvisionedThroughputExceededException. Both count as throttled reads, and
neither is retried, so the offered load stays what the schedule says.
"""
import math
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from simulator import manifest
from simulator.injectors import Injector, register

SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"

# Keys per BatchGetItem. DynamoDB allows 100, all distinct; Products has 25 items.
_BATCH_KEYS = 25
# Calls outstanding at once. More than this and a call is dropped and counted.
_MAX_IN_FLIGHT = 64
# The calls go out over this many clients in turn, each with its own
# connections. From inside the region one client needs only one to three
# connections, and over those DynamoDB spent the bank at only ~950 RCU/s, in a
# seesaw of full and refused seconds; over 16 clients it served the surge in
# full, as it did a laptop's dozens of connections (M4-1's investigation).
_CLIENTS = 16
# The surge's rate, as a multiple of capacity, and the longest it may last.
_SURGE_RATIO = 10
_SURGE_MAX_S = 150
# The bank is spent when the reads DynamoDB served the surge over the last
# _SPENT_WINDOW_S whole seconds average no more than _SPENT_FACTOR x provisioned
# capacity. Ten seconds, not one: DynamoDB can serve burst in a seesaw, a second
# or two in full and then none (M4-1's investigation).
_SPENT_WINDOW_S = 10
_SPENT_FACTOR = 1.5
# observe() reports the most DynamoDB served over any this many whole seconds
# after label_from (M4 labels by the minute), and the served reads in buckets
# of _RECORD_BUCKET_S.
_PEAK_WINDOW_S = 60
_RECORD_BUCKET_S = 10
# The scheduler sleeps at most this long at a time, so a phase change takes
# effect at most this late.
_MAX_WAIT_S = 0.05
# A scheduler this far behind (the machine stalled) skips ahead instead of
# bursting to catch up. The shortfall shows in observed["achieved_fraction"].
_MAX_BEHIND_S = 1.0
# Minutes before the window that measure the application's own reads.
_APP_BASELINE_MIN = 3
# M4 labels the window from this long after the first throttle. When the
# burst bank runs out, the app briefly meets the surge's throttling before the
# load drops to the band's ratio, and its retries take up to 3.15 s; M3-3's
# validation saw 3 failed requests in that instant at 1.75x, none after it.
_LABEL_SETTLE_S = 5
_THROTTLE_CODES = {"ProvisionedThroughputExceededException", "ThrottlingException"}
_JOIN_TIMEOUT_S = 5


@register
class DbThrottle(Injector):
    name = "db-throttling"
    incident_type = "DATABASE_THROTTLING"
    parameter = "load_ratio"
    targets = ("product-service",)

    @classmethod
    def configure(cls, intensity, band, rng):
        return {"load_ratio": round(cls.native_value(intensity, band), 3)}

    def __init__(self, ctx, activation):
        super().__init__(ctx, activation)
        self._table = None
        self._provisioned_rcu = None
        self._target_reads_per_s = None
        self._stop_event = threading.Event()
        self._scheduler = None
        self._pool = None
        self._lock = threading.Lock()
        self._in_flight = 0
        self._counts = {phase: {"attempted": 0, "throttled": 0, "errors": 0}
                        for phase in ("surge", "steady")}
        self._calls_dropped = 0
        self._served = defaultdict(int)  # reads served, by whole second since the load began
        self._first_throttle_ms = None
        self._bank_spent_ms = None   # epoch ms, when the surge saw the bank spent
        self._started_s = None       # monotonic, when the load began
        self._surge_ended_s = None   # monotonic, when the steady ratio took over
        self._surge_end = None       # "bank_spent" or "time_limit"
        self._stopped_s = None       # monotonic, when the load ended

    def start(self) -> int | None:
        ssm = self.ctx.session.client("ssm")
        self._table = ssm.get_parameter(Name=SSM_TABLE_PARAM)["Parameter"]["Value"]

        dynamodb = self.ctx.session.client("dynamodb")
        description = dynamodb.describe_table(TableName=self._table)["Table"]
        rcu = description["ProvisionedThroughput"]["ReadCapacityUnits"]
        if not rcu:
            raise RuntimeError(
                f"{self._table} is not in PROVISIONED mode, so it cannot be throttled"
            )
        self._provisioned_rcu = rcu
        self._target_reads_per_s = self.activation.params["load_ratio"] * rcu

        page = dynamodb.scan(TableName=self._table, Limit=_BATCH_KEYS,
                             ProjectionExpression="#id", ExpressionAttributeNames={"#id": "id"})
        keys = [{"id": item["id"]} for item in page.get("Items", [])]
        if not keys:
            raise RuntimeError(f"{self._table} has no items to read; seed it first")
        request = {self._table: {"Keys": keys, "ConsistentRead": True,
                                 "ProjectionExpression": "#id",
                                 "ExpressionAttributeNames": {"#id": "id"}}}

        # No retries: boto3 would otherwise retry a throttled call, which hides
        # the throttle and changes the load. Short timeouts bound stop().
        config = Config(retries={"total_max_attempts": 1}, connect_timeout=2, read_timeout=5,
                        max_pool_connections=_MAX_IN_FLIGHT)
        clients = [self.ctx.session.client("dynamodb", config=config) for _ in range(_CLIENTS)]
        surge_calls = max(_SURGE_RATIO * rcu, self._target_reads_per_s) / len(keys)
        steady_calls = self._target_reads_per_s / len(keys)

        self._stop_event.clear()
        self._pool = ThreadPoolExecutor(max_workers=_MAX_IN_FLIGHT, thread_name_prefix="db-throttling")
        self._started_s = time.monotonic()
        self._scheduler = threading.Thread(
            target=self._schedule, args=(clients, request, surge_calls, steady_calls),
            name="db-throttling-scheduler", daemon=True)
        self._scheduler.start()
        return None

    def _schedule(self, clients, request, surge_calls, steady_calls):
        next_at = time.monotonic()
        calls = 0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if self._surge_ended_s is None:
                if self._bank_spent(now):
                    self._bank_spent_ms = int(time.time() * 1000)
                    self._end_surge(now, "bank_spent")
                elif now - self._started_s >= _SURGE_MAX_S:
                    self._end_surge(now, "time_limit")
            if now < next_at:
                self._stop_event.wait(min(next_at - now, _MAX_WAIT_S))
                continue
            if now - next_at > _MAX_BEHIND_S:
                next_at = now
            phase = "surge" if self._surge_ended_s is None else "steady"
            next_at += 1.0 / (surge_calls if phase == "surge" else steady_calls)
            self._dispatch(clients[calls % len(clients)], request, phase)
            calls += 1

    def _bank_spent(self, now):
        """Whether DynamoDB served no more than a spent bank would over the last _SPENT_WINDOW_S whole seconds."""
        elapsed = int(now - self._started_s)
        if elapsed < _SPENT_WINDOW_S:
            return False
        with self._lock:
            served = sum(self._served.get(s, 0) for s in range(elapsed - _SPENT_WINDOW_S, elapsed))
        return served <= _SPENT_FACTOR * self._provisioned_rcu * _SPENT_WINDOW_S

    def _end_surge(self, now, why):
        self._surge_ended_s, self._surge_end = now, why

    def _dispatch(self, client, request, phase):
        with self._lock:
            if self._in_flight >= _MAX_IN_FLIGHT:
                self._calls_dropped += 1
                return
            self._in_flight += 1
        self._pool.submit(self._call, client, request, phase)

    def _call(self, client, request, phase):
        try:
            self._read(client, request, phase)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _read(self, client, request, phase="steady"):
        keys = len(request[self._table]["Keys"])
        throttled = errors = 0
        try:
            response = client.batch_get_item(RequestItems=request)
            unprocessed = response.get("UnprocessedKeys", {}).get(self._table, {})
            throttled = len(unprocessed.get("Keys", []))
        except ClientError as error:
            if error.response["Error"]["Code"] in _THROTTLE_CODES:
                throttled = keys
            else:
                errors = keys
        except BotoCoreError:
            # No answer at all (a timeout, a dropped connection): not a throttle.
            errors = keys
        now = time.monotonic()
        with self._lock:
            counts = self._counts[phase]
            counts["attempted"] += keys
            counts["throttled"] += throttled
            counts["errors"] += errors
            if self._started_s is not None:
                self._served[int(now - self._started_s)] += keys - throttled - errors
            if throttled and self._first_throttle_ms is None:
                self._first_throttle_ms = int(time.time() * 1000)

    def stop(self) -> int | None:
        # Safe before start(), after a failed start(), and twice.
        self._stop_event.set()
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            scheduler.join(timeout=_JOIN_TIMEOUT_S)
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            if self._stopped_s is None:
                self._stopped_s = time.monotonic()
        return None

    def observe(self) -> dict:
        surge, steady = self._counts["surge"], self._counts["steady"]
        attempted = surge["attempted"] + steady["attempted"]
        throttled = surge["throttled"] + steady["throttled"]
        observed = {
            "table": self._table,
            "provisioned_rcu": self._provisioned_rcu,
            "target_reads_per_s": _round(self._target_reads_per_s),
            "reads_attempted": attempted,
            "reads_throttled": throttled,
            "read_errors": surge["errors"] + steady["errors"],
            "calls_dropped": self._calls_dropped,
            # When DynamoDB first refused a read. Not when the bank ran out:
            # it can refuse some within seconds of a full one.
            "first_throttle_at": (None if self._first_throttle_ms is None
                                  else manifest.to_iso(self._first_throttle_ms)),
            # When the surge saw the bank spent: DynamoDB served it only its
            # capacity. Null when the surge reached its time limit first.
            "bank_spent_at": (None if self._bank_spent_ms is None
                              else manifest.to_iso(self._bank_spent_ms)),
            # Where M4 starts labeling DATABASE_THROTTLING: 5 s after the bank
            # was spent, once the surge's aftermath has settled. Null when it
            # never was; M4 reruns such a run.
            "label_from": (None if self._bank_spent_ms is None
                           else manifest.to_iso(self._bank_spent_ms + _LABEL_SETTLE_S * 1000)),
            "surge_end": self._surge_end,
            "surge_s": None,
            "steady_reads_per_s": None,
            "achieved_fraction": None,
            "steady_throttle_rate": None,
            # The reads DynamoDB served per second, in 10 s buckets from the
            # load's start, and the most it served over any 60 s after
            # label_from: its capacity, less the app's reads, while the bank
            # stays spent. More means burst capacity came back.
            "served_per_s_by_10s": self._served_by_bucket(),
            "steady_peak_served_per_s": self._steady_peak(),
            "clients": _CLIENTS,
        }
        if attempted:
            observed["client_throttle_rate"] = round(throttled / attempted, 4)
        if self._started_s is not None and self._surge_ended_s is not None:
            observed["surge_s"] = round(self._surge_ended_s - self._started_s, 1)
            if self._stopped_s is not None and self._stopped_s > self._surge_ended_s:
                rate = steady["attempted"] / (self._stopped_s - self._surge_ended_s)
                observed["steady_reads_per_s"] = round(rate, 1)
                observed["achieved_fraction"] = round(rate / self._target_reads_per_s, 3)
            if steady["attempted"]:
                observed["steady_throttle_rate"] = round(steady["throttled"] / steady["attempted"], 4)
        if self._table:
            observed.update(self._cloudwatch_counts())
            app = observed["app_reads_per_s"]
            if app is not None and observed["steady_reads_per_s"] is not None:
                observed["offered_ratio"] = round(
                    (observed["steady_reads_per_s"] + app) / self._provisioned_rcu, 3)
        return observed

    def _whole_seconds(self):
        """How many whole seconds of the load have all their served reads counted."""
        if self._started_s is None or self._stopped_s is None:
            return 0
        return int(self._stopped_s - self._started_s)

    def _served_by_bucket(self):
        end = self._whole_seconds()
        buckets = [range(b, min(b + _RECORD_BUCKET_S, end)) for b in range(0, end, _RECORD_BUCKET_S)]
        return [round(sum(self._served.get(s, 0) for s in bucket) / len(bucket), 1) for bucket in buckets]

    def _steady_peak(self):
        if self._bank_spent_ms is None or self._surge_ended_s is None:
            return None
        first = math.ceil(self._surge_ended_s - self._started_s + _LABEL_SETTLE_S)
        seconds = [self._served.get(s, 0) for s in range(first, self._whole_seconds())]
        if not seconds:
            return None
        span = min(_PEAK_WINDOW_S, len(seconds))
        return round(max(sum(seconds[i:i + span]) for i in range(len(seconds) - span + 1)) / span, 1)

    def _cloudwatch_counts(self) -> dict:
        """The table's CloudWatch counts over the window's minutes, normal traffic included.

        That is the point: it is what a monitor would see. ThrottledRequests is
        published per operation, so it is summed over every operation that has
        it (the app's GetItem and Scan, and BatchGetItem from this injector). A
        BatchGetItem counts there only when all of its keys were throttled;
        ReadThrottleEvents counts each throttled key. app_reads_per_s is the
        table's consumed RCU in the minutes before the window, which the
        offered ratio adds to the injector's own reads.
        """
        cloudwatch = self.ctx.session.client("cloudwatch")
        start = datetime.fromtimestamp(self.activation.active_from_ms / 1000, timezone.utc)
        start = start.replace(second=0, microsecond=0)
        end = datetime.fromtimestamp(self.activation.active_until_ms / 1000, timezone.utc)
        table = {"Name": "TableName", "Value": self._table}

        def points(metric, dimensions, since, until):
            return cloudwatch.get_metric_statistics(
                Namespace="AWS/DynamoDB", MetricName=metric, Dimensions=dimensions,
                StartTime=since, EndTime=until, Period=60, Statistics=["Sum"],
            )["Datapoints"]

        def total(metric, dimensions):
            return round(sum(p["Sum"] for p in points(metric, dimensions, start, end)), 1)

        by_operation = {}
        pages = cloudwatch.get_paginator("list_metrics").paginate(
            Namespace="AWS/DynamoDB", MetricName="ThrottledRequests", Dimensions=[table])
        for page in pages:
            for metric in page["Metrics"]:
                names = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
                if set(names) == {"TableName", "Operation"}:
                    by_operation[names["Operation"]] = total("ThrottledRequests", metric["Dimensions"])

        before = points("ConsumedReadCapacityUnits", [table],
                        start - timedelta(minutes=_APP_BASELINE_MIN), start)
        app = round(sum(p["Sum"] for p in before) / (60 * len(before)), 1) if before else None

        return {
            "cw_throttled_requests": round(sum(by_operation.values()), 1),
            "cw_throttled_requests_by_operation": dict(sorted(by_operation.items())),
            "cw_read_throttle_events": total("ReadThrottleEvents", [table]),
            "cw_consumed_read_capacity": total("ConsumedReadCapacityUnits", [table]),
            "app_reads_per_s": app,
        }


def _round(value):
    return None if value is None else round(value, 1)
