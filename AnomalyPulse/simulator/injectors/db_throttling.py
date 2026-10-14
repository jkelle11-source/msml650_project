from datetime import datetime, timezone
import math
import threading
import time

from botocore.config import Config
from botocore.exceptions import ClientError

from simulator.injectors import Injector, register

SSM_TABLE_PARAM = "/anomalypulse/tables/products/name"

_READS_PER_WORKER = 40
_MAX_WORKERS = 32
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
        self._stop_event = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self._attempted = 0
        self._throttled = 0

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
        reads_per_s = self.activation.params["load_ratio"] * self._provisioned_rcu
        workers = max(1, min(_MAX_WORKERS, math.ceil(reads_per_s / _READS_PER_WORKER)))

        client = self.ctx.session.client(
            "dynamodb",
            config=Config(retries={"total_max_attempts": 1},
                          max_pool_connections=max(workers, 10)),
        )
        page = client.scan(TableName=self._table, Limit=25, ProjectionExpression="id")
        keys = [{"id": item["id"]} for item in page.get("Items", [])]
        if not keys:
            raise RuntimeError(f"{self._table} has no items to read; seed it first")

        self._stop_event.clear()
        for n in range(workers):
            thread = threading.Thread(
                target=self._worker, args=(client, keys, reads_per_s / workers, n), daemon=True)
            self._threads.append(thread)
            thread.start()
        return None

    def _worker(self, client, keys, rate, n):
        interval = 1.0 / rate
        next_at = time.monotonic()
        i = n
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                self._stop_event.wait(next_at - now)
                continue
            next_at += interval
            key = keys[i % len(keys)]
            i += 1
            self._read(client, key)

    def _read(self, client, key):
        throttled = False
        try:
            client.get_item(TableName=self._table, Key=key, ConsistentRead=True)
        except ClientError as error:
            throttled = error.response["Error"]["Code"] in _THROTTLE_CODES
        with self._lock:
            self._attempted += 1
            self._throttled += throttled

    def stop(self) -> int | None:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)
        self._threads = []
        return None

    def observe(self) -> dict:
        observed = {
            "table": self._table,
            "provisioned_rcu": self._provisioned_rcu,
            "reads_attempted": self._attempted,
            "reads_throttled_client": self._throttled,
        }
        if self._attempted:
            observed["client_throttle_rate"] = round(self._throttled / self._attempted, 4)

        if self._table:
            start = datetime.fromtimestamp(self.activation.active_from_ms / 1000, timezone.utc)
            end = datetime.fromtimestamp(self.activation.active_until_ms / 1000, timezone.utc)
            start = start.replace(second=0, microsecond=0)
            cloudwatch = self.ctx.session.client("cloudwatch")

            def total(metric, extra_dimensions=()):
                points = cloudwatch.get_metric_statistics(
                    Namespace="AWS/DynamoDB",
                    MetricName=metric,
                    Dimensions=[{"Name": "TableName", "Value": self._table}, *extra_dimensions],
                    StartTime=start,
                    EndTime=end,
                    Period=60,
                    Statistics=["Sum"],
                )["Datapoints"]
                return round(sum(point["Sum"] for point in points), 1)

            observed["cw_throttled_requests"] = total(
                "ThrottledRequests", [{"Name": "Operation", "Value": "GetItem"}])
            observed["cw_read_throttle_events"] = total("ReadThrottleEvents")
        return observed