"""Experiment lock (M3-1).

Telemetry is tied to an experiment by time alone, so two runs that overlap on
the shared stack put each other's traffic and faults into both windows, and the
labels are silently wrong. The lock is one item in the experiments table that a
run creates when it starts and deletes when it ends.

It is advisory: it stops a second simulator run, not someone curling the API.
"""
import time
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr

_KEY = {"pk": "LOCK"}


class LockHeld(Exception):
    """Another experiment holds the lock. `holder` is its lock item, if it could be read."""

    def __init__(self, holder: dict | None):
        self.holder = holder
        if holder is None:
            message = "another experiment held the lock a moment ago; try again"
        else:
            expires = datetime.fromtimestamp(holder["expires_at"], tz=timezone.utc)
            message = (f"experiment {holder['experiment_id']} is already running, started by "
                       f"{holder['held_by']}; its lock expires at {expires:%Y-%m-%d %H:%M:%S} UTC")
        super().__init__(message)


class Lock:
    def __init__(self, table):
        """`table` is a boto3 DynamoDB Table resource for the experiments table."""
        self._table = table

    def acquire(self, experiment_id: str, held_by: str, expires_at_s: int,
                now_s: int | None = None) -> None:
        """Take the lock, or raise LockHeld.

        `expires_at_s` (epoch seconds) is when a run that crashed without
        releasing stops blocking everyone else; set it past the planned end.
        """
        if now_s is None:
            now_s = int(time.time())
        try:
            # One conditional write: DynamoDB checks and writes as a single
            # server-side operation, so of two simultaneous callers exactly one
            # wins. Reading first and writing after would let both through.
            self._table.put_item(
                Item={**_KEY, "experiment_id": experiment_id, "held_by": held_by,
                      "expires_at": expires_at_s},
                ConditionExpression=Attr("pk").not_exists() | Attr("expires_at").lt(now_s),
            )
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            raise LockHeld(self.holder()) from None

    def release(self, experiment_id: str) -> bool:
        """Give the lock back. False if it was no longer this experiment's.

        That happens when the run outlived its expiry and another experiment
        took the lock over; the condition keeps us from deleting theirs.
        """
        try:
            self._table.delete_item(
                Key=_KEY, ConditionExpression=Attr("experiment_id").eq(experiment_id))
        except self._table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def holder(self) -> dict | None:
        """The current lock item (experiment_id, held_by, expires_at), or None if free."""
        item = self._table.get_item(Key=_KEY, ConsistentRead=True).get("Item")
        if item is None:
            return None
        return {
            "experiment_id": item["experiment_id"],
            "held_by": item["held_by"],
            "expires_at": int(item["expires_at"]),  # DynamoDB hands numbers back as Decimal
        }
