"""Fault control plane, simulator side (M3-1).

The service handlers read fault activations from the anomalypulse-fault-config
table (layers/telemetry/python/fault_config.py). This module is the other half:
it writes and deletes those items. One item is one fault, applied by one service
for one time window.

A fault is scheduled, not switched on. Every Lambda execution environment caches
its service's items for a few seconds, so an item written MIN_LEAD_MS before its
window opens is in every cache by then and the fault begins everywhere at the
same instant. The window also closes on its own, so a simulator that crashes
cannot leave a service broken.
"""
import time
from decimal import Decimal

from boto3.dynamodb.conditions import Key

# The table's partition key. Same values as telemetry's `service` field.
SERVICES = ("product-service", "cart-service", "order-service", "payment-service")

# How far ahead of its window an item has to be written. The reader's cache
# lives 5 s (FAULT_CONFIG_TTL_SECONDS); the other 5 s covers its eventually
# consistent read, one failed refresh, and clock skew between the laptop and
# Lambda.
MIN_LEAD_MS = 10_000

# expires_at is DynamoDB's TTL attribute, in epoch SECONDS. Deletion is lazy (it
# can take days), so this only garbage-collects after a crashed run. It is not
# what ends a fault; the window is.
EXPIRY_GRACE_SECONDS = 3600


def _to_dynamo(value):
    """Recursively turn floats into Decimals.

    boto3's DynamoDB resource refuses a float. The reader's _plain() undoes
    this, so a handler gets back the number the injector resolved.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _item(*, service: str, fault_id: str, injector: str, params: dict,
          active_from_ms: int, active_until_ms: int) -> dict:
    return {
        "service": service,
        "fault_id": fault_id,
        "injector": injector,
        "params": _to_dynamo(params),
        "active_from_ms": active_from_ms,
        "active_until_ms": active_until_ms,
        "expires_at": active_until_ms // 1000 + EXPIRY_GRACE_SECONDS,
    }


class ControlPlane:
    def __init__(self, table):
        """`table` is a boto3 DynamoDB Table resource for the fault-config table."""
        self._table = table

    def schedule(self, *, service: str, fault_id: str, injector: str, params: dict,
                 active_from_ms: int, active_until_ms: int, now_ms: int | None = None) -> None:
        """Write one fault. `service` applies it while active_from_ms <= now < active_until_ms.

        Raises ValueError, and writes nothing, for a service no handler reads or
        a window that opens less than MIN_LEAD_MS from now. Either would give a
        run whose manifest claims a fault the application never saw as described.
        """
        if service not in SERVICES:
            raise ValueError(f"unknown service {service!r}; expected one of {', '.join(SERVICES)}")
        if active_from_ms >= active_until_ms:
            raise ValueError(f"{fault_id}: active_from_ms must be before active_until_ms")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        lead_ms = active_from_ms - now_ms
        if lead_ms < MIN_LEAD_MS:
            raise ValueError(f"{fault_id}: a fault must be scheduled at least {MIN_LEAD_MS} ms "
                             f"before it starts; this one starts in {lead_ms} ms")
        self._table.put_item(Item=_item(
            service=service, fault_id=fault_id, injector=injector, params=params,
            active_from_ms=active_from_ms, active_until_ms=active_until_ms,
        ))

    def cancel(self, service: str, fault_id: str) -> None:
        """Delete one fault. Safe to repeat, and safe for a fault that was never written.

        The fault stops being applied once each execution environment's cache
        has refreshed, so within a few seconds.
        """
        self._table.delete_item(Key={"service": service, "fault_id": fault_id})

    def cancel_experiment(self, experiment_id: str) -> int:
        """Delete every fault of one experiment. Returns how many were deleted."""
        # The '#' ends the id, so one experiment's prefix cannot match another's.
        return self._cancel(prefix=f"{experiment_id}#")

    def cancel_all(self) -> int:
        """Delete every fault in the table. Returns how many were deleted."""
        return self._cancel(prefix=None)

    def _cancel(self, prefix: str | None) -> int:
        deleted = 0
        for service in SERVICES:
            condition = Key("service").eq(service)
            if prefix is not None:
                condition = condition & Key("fault_id").begins_with(prefix)
            # ConsistentRead: a fault written a moment ago must not be missed.
            query = {
                "KeyConditionExpression": condition,
                "ProjectionExpression": "fault_id",
                "ConsistentRead": True,
            }
            while True:
                page = self._table.query(**query)
                for item in page["Items"]:
                    self.cancel(service, item["fault_id"])
                    deleted += 1
                if "LastEvaluatedKey" not in page:
                    break
                query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return deleted
