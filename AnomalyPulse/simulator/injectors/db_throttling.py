"""DynamoDB-throttling injector (M3-3).

Produces real throttling on the Products table. The table sits at a fixed,
low provisioned read capacity; for the fault's window this injector reads it
directly, at `load_ratio` x provisioned RCU per second, on top of the
workload's own reads. The ratio is the knob (project plan section 5). The
throttle rate is whatever DynamoDB does, measured afterwards by observe().

The reads are strongly consistent BatchGetItem calls of up to 25 keys. Every
product item is under 4 KB, so each key costs exactly 1 RCU, and a few dozen
calls a second offer thousands of RCU. One GetItem per RCU would need a call
for every unit, more than one client process sustains at the top ratios.

A batch DynamoDB could partly serve returns the keys it couldn't read in
UnprocessedKeys; a batch it couldn't serve at all raises
ProvisionedThroughputExceededException. Both count as throttled reads, and
neither is retried, so the offered load stays what the ratio says.
"""
import math
import threading
import time
from datetime import datetime, timezone

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from simulator import manifest
from simulator.injectors import Injector, register

SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"

# Keys per BatchGetItem. DynamoDB allows 100, all distinct; Products has 25 items.
_BATCH_KEYS = 25
# BatchGetItem calls a second asked of one thread. A call takes tens of
# milliseconds from a laptop, a few from inside the region.
_CALLS_PER_WORKER = 10
_MAX_WORKERS = 32
# A worker this far behind its schedule skips ahead instead of bursting to
# catch up. The shortfall shows in observed["achieved_reads_per_s"].
_MAX_BEHIND_S = 1.0
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
        self._threads = []
        self._lock = threading.Lock()
        self._attempted = 0
        self._throttled = 0
        self._errors = 0
        self._first_throttle_ms = None
        self._started_s = None    # monotonic, when the load began
        self._stopped_s = None    # monotonic, when it ended

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

        calls_per_s = self._target_reads_per_s / len(keys)
        workers = max(1, min(_MAX_WORKERS, math.ceil(calls_per_s / _CALLS_PER_WORKER)))
        # No retries: boto3 would otherwise retry a throttled call, which hides
        # the throttle and slows the load down.
        client = self.ctx.session.client(
            "dynamodb",
            config=Config(retries={"total_max_attempts": 1},
                          max_pool_connections=max(workers, 10)),
        )

        self._stop_event.clear()
        self._started_s = time.monotonic()
        rate = calls_per_s / workers
        for n in range(workers):
            # Staggered, so the workers' calls spread across each interval.
            first_at = self._started_s + n / (rate * workers)
            thread = threading.Thread(target=self._worker, args=(client, request, rate, first_at),
                                      name=f"db-throttling-{n}", daemon=True)
            self._threads.append(thread)
            thread.start()
        return None

    def _worker(self, client, request, rate, next_at):
        interval = 1.0 / rate
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                self._stop_event.wait(next_at - now)
                continue
            if now - next_at > _MAX_BEHIND_S:
                next_at = now
            next_at += interval
            self._read(client, request)

    def _read(self, client, request):
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
        with self._lock:
            self._attempted += keys
            self._throttled += throttled
            self._errors += errors
            if throttled and self._first_throttle_ms is None:
                self._first_throttle_ms = int(time.time() * 1000)

    def stop(self) -> int | None:
        # Safe before start(), after a failed start(), and twice.
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)
        if self._threads and self._stopped_s is None:
            self._stopped_s = time.monotonic()
        self._threads = []
        return None

    def observe(self) -> dict:
        observed = {
            "table": self._table,
            "provisioned_rcu": self._provisioned_rcu,
            "target_reads_per_s": (None if self._target_reads_per_s is None
                                   else round(self._target_reads_per_s, 1)),
            "reads_attempted": self._attempted,
            "reads_throttled": self._throttled,
            "read_errors": self._errors,
            # When DynamoDB first refused a read. Burst capacity (up to 300 s of
            # unused capacity) can hold this off for minutes into the window.
            "first_throttle_at": (None if self._first_throttle_ms is None
                                  else manifest.to_iso(self._first_throttle_ms)),
        }
        if self._started_s is not None and self._stopped_s is not None:
            seconds = self._stopped_s - self._started_s
            if seconds > 0:
                observed["achieved_reads_per_s"] = round(self._attempted / seconds, 1)
        if self._attempted:
            observed["client_throttle_rate"] = round(self._throttled / self._attempted, 4)
        if self._table:
            observed.update(self._cloudwatch_counts())
        return observed

    def _cloudwatch_counts(self) -> dict:
        """The table's CloudWatch counts over the window's minutes, normal traffic included.

        That is the point: it is what a monitor would see. ThrottledRequests is
        published per operation, so it is summed over every operation that has
        it (the app's GetItem and Scan, and BatchGetItem from this injector). A
        BatchGetItem counts there only when all of its keys were throttled;
        ReadThrottleEvents counts each throttled key.
        """
        cloudwatch = self.ctx.session.client("cloudwatch")
        start = datetime.fromtimestamp(self.activation.active_from_ms / 1000, timezone.utc)
        start = start.replace(second=0, microsecond=0)
        end = datetime.fromtimestamp(self.activation.active_until_ms / 1000, timezone.utc)
        table = {"Name": "TableName", "Value": self._table}

        def total(metric, dimensions):
            points = cloudwatch.get_metric_statistics(
                Namespace="AWS/DynamoDB", MetricName=metric, Dimensions=dimensions,
                StartTime=start, EndTime=end, Period=60, Statistics=["Sum"],
            )["Datapoints"]
            return round(sum(point["Sum"] for point in points), 1)

        by_operation = {}
        pages = cloudwatch.get_paginator("list_metrics").paginate(
            Namespace="AWS/DynamoDB", MetricName="ThrottledRequests", Dimensions=[table])
        for page in pages:
            for metric in page["Metrics"]:
                names = {d["Name"]: d["Value"] for d in metric["Dimensions"]}
                if set(names) == {"TableName", "Operation"}:
                    by_operation[names["Operation"]] = total("ThrottledRequests", metric["Dimensions"])

        return {
            "cw_throttled_requests": round(sum(by_operation.values()), 1),
            "cw_throttled_requests_by_operation": dict(sorted(by_operation.items())),
            "cw_read_throttle_events": total("ReadThrottleEvents", [table]),
            "cw_consumed_read_capacity": total("ConsumedReadCapacityUnits", [table]),
        }
