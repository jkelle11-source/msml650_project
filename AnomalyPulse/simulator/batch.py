"""Batch runner (M4-1): run a queue of experiments unattended and register each one.

    python -m simulator run-batch --queue simulator/queues/<file>.json

Run it from AnomalyPulse/ on the runner host (Docs/DATASET.md), in tmux. The
queue file names the dataset and lists the experiments, each with its number
("slot"), phase, seed and what to inject. Rerunning the same command resumes:
accepted experiments are skipped.

Before the first run the batch refuses to start unless the stack has M4's
frozen configuration (stack_config.py) and matches what the dataset is pinned
to. Every run is then judged before it is accepted:

    the run      COMPLETED, at 20 rps
    the client   dropped nothing; no response for at most 0.1% of requests
    db-throttling  achieved_fraction >= 0.97, label_from set, a steady
                 throttle rate no more than 0.1 below 1 - 1/ratio, no
                 minute after label_from served more than 1.25x capacity,
                 and 30 s of the fault left past the minute holding label_from
    Products     no throttling outside a db-throttling window (CloudWatch),
                 checked once the run's metrics have settled, during the next run

A rejected run is rerun with the same seed, so it is the same experiment again,
up to max_attempts times.

A queue for the main dataset (v1) must also pass M4-3's evaluation-integrity
guard, integrity.check_entry: the phase's bands, run shape and endpoint mix.
"""
import hashlib
import json
import platform
import socket
import subprocess
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from simulator import cascade, dataset, integrity, manifest, runner, stack_config, workload
from simulator.lock import LockHeld

MAX_ATTEMPTS = 3
# This many rejected runs in a row means something is wrong with the setup,
# not with the runs, so the batch stops rather than spend every slot's attempts.
MAX_CONSECUTIVE_REJECTIONS = 3
MAX_ERROR_SHARE = 0.001
MIN_ACHIEVED_FRACTION = 0.97
# Once the burst bank is spent, DynamoDB serves its capacity and throttles the
# rest: 1 - 1/ratio of the reads (M3-3 measured within a point of it). A
# steady throttle rate more than this below it means the window ran on burst
# capacity, as M4-1's validation found at 2.0x. Above it is not a fault: the
# app's own reads rose after offered_ratio was measured (a traffic spike in the
# same cascade; M4-3's validation measured 0.48 where 1.63x gives 0.39).
MAX_THROTTLE_RATE_GAP = 0.10
# A spent bank serves only capacity. More than this x provisioned RCU over any
# minute after label_from means burst capacity came back inside the labeled
# stretch (once in M4-1's investigation, a second full bank ~3 minutes in).
MAX_STEADY_SERVED_FACTOR = 1.25
# The labeler counts db-throttling from label_from and excludes the minute that
# holds it as onset (Docs/LABELING.md), so a window is labeled only if the fault
# runs on this long past that minute. A 5-minute fault always does; the test
# set's 2-minute fault doesn't when label_from falls in a minute's first 18 s,
# about 3 runs in 10, and such a run is rerun (decided in M4-4's review, for M9-1).
MIN_LABELED_PAST_ONSET_MS = 30_000
# CloudWatch's DynamoDB metrics for a run's last minute are in by then.
SETTLE_S = 300
# The db-throttling injector's reads in flight when its window closes can take
# up to 5 s more (its read timeout), so its window is taken to end this much later.
_DB_WINDOW_TAIL_MS = 10_000
# Every M4 run, test set included (Milestones/M04_Dataset.md).
RPS = workload.DEFAULT_RPS
# Requests the client keeps open at once; past it, it drops new ones, and the run
# is rejected. The workload's default, 256, was not enough: a 7.9x spike into
# 4.45x throttling averaged ~130 open and peaked past 256 (M4-3's validation).
# Not higher: an open POST /orders keeps an Order and a Payment Lambda running,
# and the account runs 1,000 at once, shared by every function; past that,
# Lambda would refuse requests, a failure no injector caused.
MAX_IN_FLIGHT = 512

_KINDS = ("incident", "cascade", "plan")
_ENTRY_KEYS = {"slot", "phase", "seed", "mix", *_KINDS}
_INCIDENT_KEYS = {"injector", "intensity", "band", "target", "baseline_s", "duration_s", "recovery_s"}
_PLAN_KEYS = {"incident_type", "faults", "recovery_s"}
_FAULT_KEYS = {"injector", "offset_s", "duration_s", "intensity", "band", "target"}


class BatchRefused(RuntimeError):
    """The batch did not start. Nothing was run or registered."""


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- the queue ---------------------------------------------------------------

@dataclass(frozen=True)
class Queue:
    dataset: str
    entries: tuple          # the experiments, in the order to run them
    incident_types: dict    # slot -> the run's label
    sha256: str
    path: str | None = None


def _keys(what: str, value, allowed: set, required: set) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    if set(value) - allowed or required - set(value):
        raise ValueError(f"{what} takes {', '.join(sorted(allowed))} "
                         f"(required: {', '.join(sorted(required))}); got {', '.join(sorted(value))}")
    return value


def plan_for(entry: dict, created_by: str) -> runner.Plan:
    """The runner.Plan for one queue entry. Raises ValueError for one that cannot run."""
    seed = entry["seed"]
    if "incident" in entry:
        spec = _keys(f"slot {entry['slot']} incident", entry["incident"], _INCIDENT_KEYS,
                     {"injector", "baseline_s", "duration_s", "recovery_s"})
        return runner.Plan.single(
            spec["injector"], duration_s=spec["duration_s"], baseline_s=spec["baseline_s"],
            recovery_s=spec["recovery_s"], seed=seed, created_by=created_by,
            intensity=spec.get("intensity"), band=spec.get("band"), target=spec.get("target"))
    if "cascade" in entry:
        return cascade.build_plan(**cascade.load_spec(entry["cascade"]), seed=seed, created_by=created_by)
    spec = _keys(f"slot {entry['slot']} plan", entry["plan"], _PLAN_KEYS, _PLAN_KEYS)
    if not isinstance(spec["faults"], list):
        raise ValueError(f"slot {entry['slot']}: plan faults must be a list")
    faults = tuple(runner.Fault(**_keys(f"slot {entry['slot']} fault", f, _FAULT_KEYS,
                                        {"injector", "offset_s", "duration_s"}))
                   for f in spec["faults"])
    return runner.Plan(incident_type=spec["incident_type"], faults=faults,
                       recovery_s=spec["recovery_s"], seed=seed, created_by=created_by)


def workload_for(entry: dict, base_url: str | None = None) -> workload.Workload:
    """The run's traffic: always 20 rps; the endpoint mix can change (the test set's shifted mix)."""
    mix = entry.get("mix")
    if mix is not None and (not isinstance(mix, list) or len(mix) != len(workload.ROUTES)):
        raise ValueError(f"slot {entry['slot']}: mix is {len(workload.ROUTES)} weights, in the order "
                         + ", ".join(workload.ROUTES))
    return workload.Workload(rps=RPS, mix=None if mix is None else dict(zip(workload.ROUTES, mix)),
                             seed=entry["seed"], base_url=base_url, max_in_flight=MAX_IN_FLIGHT)


def run_seconds(plan: runner.Plan) -> float:
    return max(f.offset_s + f.duration_s for f in plan.faults) + plan.recovery_s


def _check_entry(entry) -> None:
    _keys("each experiment", entry, _ENTRY_KEYS, {"slot", "phase", "seed"})
    slot, phase, seed = entry["slot"], entry["phase"], entry["seed"]
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 1:
        raise ValueError(f"slot must be a whole number, 1 or more, got {slot!r}")
    if phase not in dataset.phases_for(slot):
        raise ValueError(f"slot {slot}: phase must be {' or '.join(dataset.phases_for(slot))} "
                         f"(1-{dataset.TRAIN_LAST} train, {dataset.TRAIN_LAST + 1}-{dataset.VAL_LAST} val, "
                         f"then test or stress), got {phase!r}")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**32:
        raise ValueError(f"slot {slot}: seed must be a whole number from 0 to 2^32-1")
    if sum(kind in entry for kind in _KINDS) != 1:
        raise ValueError(f"slot {slot}: give exactly one of {', '.join(_KINDS)}")


def load_queue(path) -> Queue:
    """Read and check a queue file. Every entry is resolved as a run would be, without AWS."""
    text = Path(path).read_text()
    data = json.loads(text)
    _keys("a queue", data, {"dataset", "experiments"}, {"dataset", "experiments"})
    name = dataset.check_name(data["dataset"])
    if not isinstance(data["experiments"], list) or not data["experiments"]:
        raise ValueError("experiments must be a non-empty list")

    incident_types = {}
    for entry in data["experiments"]:
        _check_entry(entry)
        if entry["slot"] in incident_types:
            raise ValueError(f"slot {entry['slot']} is listed twice")
        try:
            plan = plan_for(entry, created_by="queue-check")
            runner.preview(plan, workload=workload_for(entry))
            if name == integrity.GUARDED_DATASET:
                integrity.check_entry(entry)
        except (ValueError, LookupError, TypeError) as e:
            raise ValueError(f"slot {entry['slot']}: {e}") from None
        incident_types[entry["slot"]] = plan.incident_type
    return Queue(name, tuple(data["experiments"]), incident_types,
                 hashlib.sha256(text.encode()).hexdigest(), str(path))


# --- judging a run -------------------------------------------------------------

def labeled_past_onset_ms(activation: dict) -> int:
    """How long a db-throttling activation runs past the minute that holds its label_from."""
    label_from = manifest.to_epoch_ms(activation["observed"]["label_from"])
    first_window = -(-label_from // 60_000) * 60_000     # the first minute not overlapping the onset
    return manifest.to_epoch_ms(activation["active_until"]) - first_window


def run_problems(final: dict, client: dict) -> list[str]:
    """Why a finished run can't go in the dataset, from its manifest and what the client saw."""
    problems = []
    if final["status"] != "COMPLETED":
        problems.append(f"the run ended {final['status']}")
    if final["workload"].get("request_rate_rps") != RPS:
        problems.append(f"it ran at {final['workload'].get('request_rate_rps')} rps, not {RPS}")

    sent, dropped = client["sent"], client["dropped"]
    errors = client["responses"].get("error", 0)
    if dropped:
        problems.append(f"the client dropped {dropped} requests (max_in_flight reached)")
    if not sent:
        problems.append("the client sent nothing")
    elif errors > MAX_ERROR_SHARE * sent:
        problems.append(f"{errors} of {sent} requests ({errors / sent:.2%}) got no response")

    for activation in final["activations"]:
        if activation["injector"] != "db-throttling":
            continue
        observed = activation["observed"]
        if observed.get("label_from") is None:
            problems.append("db-throttling never throttled (label_from is null)")
        elif (left := labeled_past_onset_ms(activation)) < MIN_LABELED_PAST_ONSET_MS:
            problems.append(f"db-throttling ran {max(left, 0) / 1000:g} s past the minute holding label_from, "
                            f"under the {MIN_LABELED_PAST_ONSET_MS // 1000} s that labels a window")
        fraction = observed.get("achieved_fraction")
        if fraction is None or fraction < MIN_ACHIEVED_FRACTION:
            problems.append(f"db-throttling sent {fraction} of its target read rate, "
                            f"below {MIN_ACHIEVED_FRACTION}")
        # offered_ratio counts the app's reads too; without CloudWatch, the asked-for ratio.
        ratio = observed.get("offered_ratio") or activation["params"]["load_ratio"]
        rate = observed.get("steady_throttle_rate")
        expected = 1 - 1 / ratio
        if rate is None or expected - rate > MAX_THROTTLE_RATE_GAP:
            problems.append(f"db-throttling throttled {rate} of its steady reads; {ratio:g}x should "
                            f"throttle about {expected:.2f} (did the window run on burst capacity?)")
        peak, rcu = observed.get("steady_peak_served_per_s"), observed.get("provisioned_rcu")
        if peak is None or rcu is None or peak > MAX_STEADY_SERVED_FACTOR * rcu:
            problems.append(f"DynamoDB served db-throttling {peak} reads/s over a minute after label_from, "
                            f"more than {MAX_STEADY_SERVED_FACTOR}x its {rcu} RCU "
                            f"(did burst capacity come back?)")
    return problems


def _minute_ms(when) -> int:
    return int(when.timestamp() * 1000) if isinstance(when, datetime) else int(when)


def products_throttling(cloudwatch, table: str, final: dict) -> dict:
    """Products' ThrottledRequests and ReadThrottleEvents in the run's minutes, outside db-throttling.

    A minute that overlaps a db-throttling window is left out: that throttling
    is the incident. Everything else should be zero; a traffic spike must not
    throttle by itself (Docs/DB_THROTTLING_INJECTOR.md).
    """
    start_ms = manifest.to_epoch_ms(final["started_at"]) // 60_000 * 60_000
    end_ms = manifest.to_epoch_ms(final["ended_at"])
    windows = [(manifest.to_epoch_ms(a["active_from"]),
                manifest.to_epoch_ms(a["active_until"]) + _DB_WINDOW_TAIL_MS)
               for a in final["activations"] if a["injector"] == "db-throttling"]
    since = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    until = datetime.fromtimestamp(end_ms / 1000, timezone.utc)

    def total(metric, dimensions):
        points = cloudwatch.get_metric_statistics(
            Namespace="AWS/DynamoDB", MetricName=metric, Dimensions=dimensions,
            StartTime=since, EndTime=until, Period=60, Statistics=["Sum"])["Datapoints"]
        return sum(p["Sum"] for p in points
                   if not any(_minute_ms(p["Timestamp"]) < b and a < _minute_ms(p["Timestamp"]) + 60_000
                              for a, b in windows))

    table_dimension = [{"Name": "TableName", "Value": table}]
    # ThrottledRequests is published per operation only (the metrics extractor
    # sums it the same way). Not RecentlyActive: a resumed batch may check a
    # run from hours ago.
    throttled, token = 0, None
    while True:
        page = cloudwatch.list_metrics(Namespace="AWS/DynamoDB", MetricName="ThrottledRequests",
                                       Dimensions=table_dimension, **({"NextToken": token} if token else {}))
        for metric in page.get("Metrics", []):
            if {d["Name"] for d in metric["Dimensions"]} == {"TableName", "Operation"}:
                throttled += total("ThrottledRequests", metric["Dimensions"])
        token = page.get("NextToken")
        if not token:
            break
    return {"throttled_requests": throttled,
            "read_throttle_events": total("ReadThrottleEvents", table_dimension)}


# --- where and what runs it --------------------------------------------------

def describe_runner(timeout_s: float = 1.0) -> dict:
    """The machine running the batch. `ec2` is None anywhere but an EC2 instance."""
    ec2 = None
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=timeout_s).read().decode()
        document = json.loads(urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token}), timeout=timeout_s).read())
        # Not accountId: nothing written back should carry it.
        ec2 = {key: document[source] for key, source in (
            ("instance_id", "instanceId"), ("instance_type", "instanceType"), ("image_id", "imageId"),
            ("availability_zone", "availabilityZone"))}
    except (OSError, ValueError, KeyError):
        pass
    return {"hostname": socket.gethostname(), "python": platform.python_version(),
            "platform": platform.platform(), "ec2": ec2}


def describe_code() -> dict:
    """The commit the simulator runs from, and whether its files differ from it."""
    here = Path(__file__).parent

    def git(*args):
        return subprocess.run(["git", *args], cwd=here, capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()
    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", "."))}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _instance_id(runner_info: dict):
    return (runner_info.get("ec2") or {}).get("instance_id")


# --- the batch -----------------------------------------------------------------

class _Batch:
    def __init__(self, queue, stack, registry, *, batch_id, created_by, max_attempts, base_url, run,
                 throttle_check, clock, sleep, log):
        self.queue, self.stack, self.registry = queue, stack, registry
        self.batch_id, self.created_by, self.max_attempts = batch_id, created_by, max_attempts
        self.base_url, self.run, self.throttle_check = base_url, run, throttle_check
        self.clock, self.sleep, self.log = clock, sleep, log
        self.runs = 0
        self.accepted = []           # (slot, experiment_id, ended_at) accepted in this batch

    def refuse_if_out_of_order(self, records: dict) -> None:
        """Training is generated first; the test set later, on a separate day (plan section 6b).

        In the main dataset, training and validation also run in slot order: a
        queue waits until every slot before its first is accepted or exhausted,
        so the daily queues go one after another and validation comes last
        (Docs/EVALUATION_INTEGRITY.md, "Building a queue").
        """
        groups = {"test" if e["phase"] in ("test", "stress") else "train" for e in self.queue.entries}
        if len(groups) > 1:
            raise BatchRefused("a batch is either training/validation or test/stress, not both: the test set "
                               "is generated on a separate day")
        accepted = [r for r in records.values() if r["status"] == "ACCEPTED"]
        if groups == {"train"}:
            tested = sorted(r["slot"] for r in accepted if r["phase"] in ("test", "stress"))
            if tested:
                raise BatchRefused(f"test experiments are already generated (slots {tested[:5]}...); "
                                   f"training and validation are generated before the test set")
            if self.queue.dataset == integrity.GUARDED_DATASET:
                first = min(e["slot"] for e in self.queue.entries)
                unfinished = [s for s in range(1, first)
                              if records.get(s, {}).get("status") not in ("ACCEPTED", "EXHAUSTED")]
                if unfinished:
                    raise BatchRefused(
                        f"{len(unfinished)} of slots 1-{first - 1} aren't finished (the first is slot "
                        f"{unfinished[0]}); dataset {self.queue.dataset}'s training and validation run in slot "
                        f"order, so finish the queue holding slot {unfinished[0]} first")
        else:
            trained = [r["generation_date"] for r in accepted if r["phase"] in ("train", "val")]
            today = manifest.to_iso(self.clock())[:10]
            if trained and today <= max(trained):
                raise BatchRefused(f"training runs were generated on {max(trained)}; the test set is "
                                   f"generated on a later day (today is {today} UTC)")

    def _attempt_run(self, entry: dict, attempt: dict) -> None:
        """Run one experiment and judge what can be judged at once. Fills in `attempt`."""
        load = None

        def started(experiment_id):
            attempt["experiment_id"] = experiment_id
        try:
            plan = plan_for(entry, self.created_by)
            load = workload_for(entry, self.base_url)
            final = self.run(plan, self.stack, workload=load, log=self.log, on_start=started)
        except (LockHeld, KeyboardInterrupt):
            raise
        except Exception as e:
            attempt.update(status="FAILED", reasons=[f"{type(e).__name__}: {e}"],
                           client=None if load is None else load.outcomes())
            return
        client = load.outcomes()
        attempt.update(status=final["status"], started_at=final["started_at"], ended_at=final["ended_at"],
                       client=client, reasons=run_problems(final, client))
        if not attempt["reasons"]:
            attempt.update(verdict="checking", settles_at=manifest.to_iso(
                manifest.to_epoch_ms(final["ended_at"]) + SETTLE_S * 1000))

    def settle(self, record: dict) -> bool | None:
        """Finish judging a CHECKING slot from CloudWatch.

        True if it was accepted, False if rejected, None if CloudWatch could not
        be read; it is tried again a minute later rather than ending the batch.
        """
        attempt = record["attempts"][-1]
        try:
            final = json.loads(self.stack.s3.get_object(
                Bucket=self.stack.bucket, Key=manifest.manifest_key(attempt["experiment_id"]))["Body"].read())
            counts = self.throttle_check(final)
        except Exception as e:
            attempt["settles_at"] = manifest.to_iso(self.clock() + 60_000)
            self.log(f"  warning: slot {record['slot']}: could not check CloudWatch "
                     f"({type(e).__name__}: {e}); trying again at {attempt['settles_at']}")
            return None
        attempt["products_throttling"] = counts
        slot = record["slot"]
        if any(counts.values()):
            attempt.update(verdict="rejected", reasons=[
                f"Products was throttled outside any db-throttling window: "
                f"{counts['throttled_requests']:g} ThrottledRequests, "
                f"{counts['read_throttle_events']:g} ReadThrottleEvents"])
            self.log(f"  slot {slot}: rejected {attempt['experiment_id']}: {attempt['reasons'][0]}")
            return False
        attempt["verdict"] = "accepted"
        record.update(status="ACCEPTED", experiment_id=attempt["experiment_id"],
                      generated_at=attempt["started_at"], generation_date=attempt["started_at"][:10],
                      ended_at=attempt["ended_at"], batch_id=attempt["batch_id"])
        self.registry.put_slot(record)
        self.accepted.append((slot, attempt["experiment_id"], attempt["ended_at"]))
        self.log(f"  slot {slot}: accepted {attempt['experiment_id']}")
        return True

    def _retry_or_exhaust(self, record: dict, work: deque, retry: bool) -> None:
        if retry and len(record["attempts"]) < self.max_attempts:
            record["status"] = "PENDING"
            work.append(record["spec"])
        else:
            record["status"] = "PENDING" if len(record["attempts"]) < self.max_attempts else "EXHAUSTED"
        self.registry.put_slot(record)

    def loop(self, records: dict) -> str | None:
        """Run the queue. Returns why the batch stopped early, or None if it got through it."""
        work = deque(e for e in self.queue.entries
                     if records.get(e["slot"], {}).get("status") not in ("ACCEPTED", "CHECKING", "EXHAUSTED"))
        # Every CHECKING slot in the registry, this queue's or an earlier batch's.
        checking = {slot: r for slot, r in records.items() if r["status"] == "CHECKING"}
        rejected_in_a_row, stop_reason = 0, None

        while True:
            for slot in [s for s, r in checking.items()
                         if manifest.to_epoch_ms(r["attempts"][-1]["settles_at"]) <= self.clock()]:
                verdict = self.settle(checking[slot])
                if verdict is not None:
                    record = checking.pop(slot)
                    if verdict is False:
                        self._retry_or_exhaust(record, work, retry=stop_reason is None)

            if stop_reason is not None or not work:
                if not checking:
                    return stop_reason
                due = min(manifest.to_epoch_ms(r["attempts"][-1]["settles_at"]) for r in checking.values())
                self.log(f"waiting until {manifest.to_iso(due)} for CloudWatch to settle "
                         f"(slots {sorted(checking)})")
                while self.clock() < due:
                    self.sleep((due - self.clock()) / 1000)
                continue

            entry = work.popleft()
            slot = entry["slot"]
            # Read fresh: the record may have changed since the batch started.
            record = self.registry.slot(slot) or dataset.new_slot_record(
                self.queue.dataset, entry, self.queue.incident_types[slot])
            if record["status"] in ("ACCEPTED", "CHECKING", "EXHAUSTED"):
                continue
            if len(record["attempts"]) >= self.max_attempts:
                self._retry_or_exhaust(record, work, retry=False)
                continue

            self.runs += 1
            self.log(f"[run {self.runs}, {len(work)} queued after it] slot {slot} ({record['phase']}) "
                     f"{record['incident_type']}, attempt {len(record['attempts']) + 1}")
            attempt = {"experiment_id": None, "batch_id": self.batch_id, "status": None,
                       "verdict": "rejected", "reasons": [], "started_at": None, "ended_at": None,
                       "settles_at": None, "client": None}
            try:
                self._attempt_run(entry, attempt)
            except LockHeld as e:
                # Another run is on the stack. Nothing ran; nothing is recorded.
                stop_reason = f"the experiment lock is held: {e}"
                work.appendleft(entry)
                continue
            except KeyboardInterrupt:
                attempt.update(status="ABORTED", reasons=["the batch was interrupted"])
                record["attempts"].append(attempt)
                self.registry.put_slot(record)
                raise
            record["attempts"].append(attempt)
            if attempt["verdict"] == "checking":
                rejected_in_a_row = 0
                record["status"] = "CHECKING"
                checking[slot] = record
                self.registry.put_slot(record)
                self.log(f"  slot {slot}: {attempt['experiment_id']} passed its run checks; "
                         f"CloudWatch check after {attempt['settles_at']}")
            else:
                rejected_in_a_row += 1
                self._retry_or_exhaust(record, work, retry=True)
                self.log(f"  slot {slot}: rejected {attempt['experiment_id']}: {'; '.join(attempt['reasons'])}")
                if rejected_in_a_row >= MAX_CONSECUTIVE_REJECTIONS:
                    stop_reason = f"{rejected_in_a_row} runs in a row were rejected; check the setup"


def run_batch(queue: Queue, stack, *, created_by: str | None = None, max_attempts: int = MAX_ATTEMPTS,
              repin: bool = False, read_config=stack_config.read, runner_info=None, code_info=None,
              base_url: str | None = None, run=runner.run, throttle_check=None,
              clock=_now_ms, sleep=time.sleep, log=print) -> dict:
    """Run every experiment in `queue` that the registry hasn't accepted. Returns the batch record.

    Raises BatchRefused, before anything runs, when the stack or the queue is
    not fit for the dataset. KeyboardInterrupt is recorded and re-raised.
    """
    registry = dataset.Registry(stack.s3, stack.bucket, queue.dataset)
    records = registry.slots()
    for entry in queue.entries:
        record = records.get(entry["slot"])
        if record is not None and record["spec"] != entry:
            raise BatchRefused(f"slot {entry['slot']} is registered with a different spec; an experiment "
                               f"number means one experiment (registry: {record['spec']})")

    batch = _Batch(queue, stack, registry, batch_id=dataset.new_batch_id(),
                   created_by=created_by or f"m4-batch:{queue.dataset}", max_attempts=max_attempts,
                   base_url=base_url, run=run, throttle_check=throttle_check,
                   clock=clock, sleep=sleep, log=log)
    batch.refuse_if_out_of_order(records)

    holder = stack.lock.holder()
    if holder is not None and holder["expires_at"] * 1000 > clock():
        raise BatchRefused(str(LockHeld(holder)))
    config = read_config(stack.session)
    problems = stack_config.problems(config)
    if problems:
        raise BatchRefused("the stack is not M4's frozen configuration: " + "; ".join(problems))
    runner_info = describe_runner() if runner_info is None else runner_info
    code_info = describe_code() if code_info is None else code_info
    if batch.base_url is None:
        batch.base_url = workload.api_base_url(stack.session)
    if batch.throttle_check is None:
        cloudwatch = stack.session.client("cloudwatch")
        table = config["products_table"]["name"]
        batch.throttle_check = lambda final: products_throttling(cloudwatch, table, final)

    pins = registry.pins()
    if pins is not None and not repin:
        changed = stack_config.changes(pins["stack"], config)
        if _instance_id(runner_info) != pins["runner_instance_id"]:
            changed.append(f"runner instance: {pins['runner_instance_id']} -> {_instance_id(runner_info)}")
        if changed:
            raise BatchRefused(f"dataset {queue.dataset} is pinned to another stack or runner (since "
                               f"{pins['batch_id']}): " + "; ".join(changed)
                               + ". Runs on either side of a change are not comparable; pass --repin "
                                 "only if that is intended")
    if pins is None or repin:
        registry.put_pins({"pinned_at": manifest.to_iso(clock()), "batch_id": batch.batch_id,
                           "stack": config, "runner_instance_id": _instance_id(runner_info)})

    record = {
        "batch_id": batch.batch_id, "dataset": queue.dataset, "status": "RUNNING", "stop_reason": None,
        "started_at": manifest.to_iso(clock()), "ended_at": None, "created_by": batch.created_by,
        "queue": {"path": queue.path, "sha256": queue.sha256, "slots": [e["slot"] for e in queue.entries]},
        "max_attempts": max_attempts, "repinned": bool(repin and pins is not None),
        "stack": config, "runner": runner_info, "code": code_info, "results": None,
    }
    registry.put_batch(record)
    log(f"{batch.batch_id}: dataset {queue.dataset}, {len(queue.entries)} experiments, "
        f"commit {code_info.get('commit')}{' (modified)' if code_info.get('dirty') else ''}")

    try:
        record["stop_reason"] = batch.loop(records)
        record["status"] = "FINISHED" if record["stop_reason"] is None else "STOPPED"
    except KeyboardInterrupt:
        record["status"] = "ABORTED"
        raise
    except BaseException as e:
        record.update(status="FAILED", stop_reason=f"{type(e).__name__}: {e}")
        raise
    finally:
        record["ended_at"] = manifest.to_iso(clock())
        record["results"] = {
            "runs": batch.runs,
            "accepted_in_batch": [{"slot": s, "experiment_id": x, "ended_at": t}
                                  for s, x, t in batch.accepted],
            "queue_slots": None,
        }
        try:
            now = registry.slots()
            statuses = {e["slot"]: now.get(e["slot"], {}).get("status", "PENDING") for e in queue.entries}
            record["results"]["queue_slots"] = {status: sorted(s for s, v in statuses.items() if v == status)
                                                for status in dataset.STATUSES}
            registry.put_batch(record)
        except Exception as e:
            # Don't hide the exception that ended the batch, if there was one.
            log(f"warning: could not record the end of {batch.batch_id}: {type(e).__name__}: {e}")
        counts = ", ".join(f"{status.lower()} {len(slots)}"
                           for status, slots in (record["results"]["queue_slots"] or {}).items())
        log(f"{batch.batch_id} {record['status']}: {batch.runs} runs; queue: {counts or 'unknown'}"
            + (f"; {record['stop_reason']}" if record["stop_reason"] else ""))
    return record
