from decimal import Decimal

import pytest

from simulator.lock import LockHeld

EXPERIMENTS_TABLE = "anomalypulse-experiments"  # created by conftest's `aws` fixture
MINE = "exp-20261012T041500Z-3f9a"
THEIRS = "exp-20261012T050000Z-0b1c"

# Epoch seconds. A run that starts at NOW and is planned to be over, margin
# included, by EXPIRES.
NOW = 1_791_778_500
EXPIRES = NOW + 1200


# --- helpers ---------------------------------------------------------------

def _stored(aws):
    # The lock item exactly as it sits in the table, or None.
    table = aws.resource("dynamodb").Table(EXPERIMENTS_TABLE)
    return table.get_item(Key={"pk": "LOCK"}, ConsistentRead=True).get("Item")


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def lock(stack):
    # Over moto's DynamoDB, which evaluates the condition expressions. These
    # tests are about what the conditions let through, so a stub that only
    # compares request parameters would prove nothing.
    return stack.lock


# --- acquire -------------------------------------------------------------------

def test_acquire_creates_the_lock_item(lock, aws):
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    assert _stored(aws) == {"pk": "LOCK", "experiment_id": MINE, "held_by": "jake",
                            "expires_at": Decimal(EXPIRES)}


def test_second_experiment_is_refused_while_the_lock_is_held(lock, aws):
    # One experiment at a time: two overlapping runs would put each other's
    # traffic and faults into both windows.
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    with pytest.raises(LockHeld):
        lock.acquire(THEIRS, "linu", EXPIRES + 600, now_s=NOW + 60)
    assert _stored(aws)["experiment_id"] == MINE  # the holder's item is untouched


def test_refusal_says_who_holds_the_lock_and_until_when(lock):
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    with pytest.raises(LockHeld) as raised:
        lock.acquire(THEIRS, "linu", EXPIRES + 600, now_s=NOW + 60)
    assert raised.value.holder == {"experiment_id": MINE, "held_by": "jake", "expires_at": EXPIRES}
    message = str(raised.value)
    assert MINE in message
    assert "started by jake" in message
    assert "2026-10-12 04:35:00 UTC" in message


def test_expired_lock_can_be_taken_over(lock, aws):
    # A run that crashed never releases. Its lock stops blocking everyone once
    # its expiry has passed.
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    lock.acquire(THEIRS, "linu", EXPIRES + 1800, now_s=EXPIRES + 1)
    assert _stored(aws) == {"pk": "LOCK", "experiment_id": THEIRS, "held_by": "linu",
                            "expires_at": Decimal(EXPIRES + 1800)}


def test_lock_is_still_held_at_the_second_it_expires(lock):
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    with pytest.raises(LockHeld):
        lock.acquire(THEIRS, "linu", EXPIRES + 1800, now_s=EXPIRES)


def test_acquire_uses_the_wall_clock_by_default(lock):
    # Held until long after today, then expired long before it.
    lock.acquire(MINE, "jake", 4_102_444_800)  # year 2100
    with pytest.raises(LockHeld):
        lock.acquire(THEIRS, "linu", 4_102_444_800)
    assert lock.release(MINE)
    lock.acquire(MINE, "jake", 946_684_800)  # year 2000
    lock.acquire(THEIRS, "linu", 4_102_444_800)


# --- release -------------------------------------------------------------------

def test_release_deletes_the_holders_lock(lock, aws):
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    assert lock.release(MINE) is True
    assert _stored(aws) is None
    lock.acquire(THEIRS, "linu", EXPIRES, now_s=NOW)  # free again


def test_release_leaves_a_lock_that_is_no_longer_ours(lock, aws):
    # Our run outlived its expiry and another experiment took the lock over.
    # Releasing must not delete theirs.
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    lock.acquire(THEIRS, "linu", EXPIRES + 1800, now_s=EXPIRES + 1)
    assert lock.release(MINE) is False
    assert _stored(aws)["experiment_id"] == THEIRS


def test_release_with_no_lock_held_reports_false(lock):
    assert lock.release(MINE) is False


# --- holder --------------------------------------------------------------------

def test_holder_is_none_when_the_lock_is_free(lock):
    assert lock.holder() is None


def test_holder_returns_plain_numbers(lock):
    # DynamoDB hands numbers back as Decimal; callers compare expires_at with
    # time.time() and format it as a date.
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    holder = lock.holder()
    assert holder == {"experiment_id": MINE, "held_by": "jake", "expires_at": EXPIRES}
    assert type(holder["expires_at"]) is int


def test_lock_held_without_a_readable_holder_still_has_a_message():
    # The holder released between our failed write and our read of who it was.
    assert "try again" in str(LockHeld(None))


def test_other_items_in_the_experiments_table_are_ignored(lock, aws):
    # The table's key is a generic `pk` so M4's registry rows can share it.
    table = aws.resource("dynamodb").Table(EXPERIMENTS_TABLE)
    table.put_item(Item={"pk": f"EXP#{THEIRS}", "phase": "train"})
    lock.acquire(MINE, "jake", EXPIRES, now_s=NOW)
    assert lock.release(MINE) is True
    assert table.get_item(Key={"pk": f"EXP#{THEIRS}"})["Item"] == {"pk": f"EXP#{THEIRS}", "phase": "train"}
