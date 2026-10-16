"""Experiment runner (M3-1): one run, from taking the lock to the final manifest.

    plan -> activations -> lock -> RUNNING manifest -> start and stop each
    injector on schedule -> recovery -> observe -> final manifest -> release

Everything that can be checked is checked before the lock is taken. Once it is
taken, whatever happens next, the wind-down in run() stops every injector,
deletes the experiment's faults, records how the run ended and releases the lock.
"""
import random
import time
from dataclasses import dataclass

from simulator import injectors, manifest
from simulator.injectors import Context
from simulator.stack import Stack

# start() is called this much earlier than an injector's lead_ms asks for, so a
# control-plane fault still meets its minimum lead after the time the calls
# before it take.
START_SLACK_MS = 5_000

# The lock outlives the planned end by this much, which leaves time for
# observe() and the final manifest write. It is also how long a crashed run
# keeps blocking everyone else after it should have ended.
LOCK_MARGIN_S = 300

# A step that runs this much later than planned means the simulator stopped
# for a while. A sleeping laptop does this: its timers count only time awake,
# so the run carries on long after its plan (M3-2 saw one end 25 minutes late)
# and its manifest's windows are no longer what happened. Normal lateness is
# milliseconds.
MAX_LATE_MS = 10_000


class FellBehind(RuntimeError):
    """A step ran more than MAX_LATE_MS late, so the run is recorded as FAILED."""


@dataclass(frozen=True)
class Fault:
    """One fault in a plan. Times are in seconds."""
    injector: str                  # registry name
    offset_s: float                # from the start of the run to the window opening
    duration_s: float              # length of the window
    intensity: float | None = None
    band: dict | None = None
    target: str | None = None


@dataclass(frozen=True)
class Plan:
    """What one run does.

    A single incident has one fault. A cascade or a confounder has several,
    with windows that overlap; `incident_type` is what tells those two apart.
    """
    incident_type: str             # the intended label
    faults: tuple
    recovery_s: float              # after the last window closes
    seed: int
    created_by: str

    @classmethod
    def single(cls, injector: str, *, duration_s: float, baseline_s: float, recovery_s: float,
               seed: int, created_by: str, intensity: float | None = None,
               band: dict | None = None, target: str | None = None) -> "Plan":
        """A run of one injector: baseline, its window, recovery.

        The label is the injector's own incident_type, so a noop run is NORMAL.
        """
        fault = Fault(injector=injector, offset_s=baseline_s, duration_s=duration_s,
                      intensity=intensity, band=band, target=target)
        return cls(incident_type=injectors.get(injector).incident_type, faults=(fault,),
                   recovery_s=recovery_s, seed=seed, created_by=created_by)


class NoWorkload:
    """The workload a run gets when it is given none: it sends nothing.

    A real workload driver (M3-2) provides the same three methods. The runner
    calls only these; anything else on the object is between the workload and
    the injectors, which receive it as ctx.workload.
    """

    def describe(self) -> dict:
        """Its settings, recorded as the manifest's `workload`. Plain JSON."""
        return {}

    def start(self) -> None:
        """Begin sending baseline traffic. Returns quickly."""

    def stop(self) -> None:
        """Stop sending. Safe to call more than once, and without start()."""


@dataclass(frozen=True)
class _Draft:
    experiment_id: str
    started_at_ms: int
    ends_at_ms: int
    planned: list   # one (injector class, Activation) per fault
    running: dict   # the manifest as it is first written


def _now_ms() -> int:
    return int(time.time() * 1000)


def _start_at_ms(cls, activation) -> int:
    """When the runner calls start() for this activation."""
    if cls.lead_ms == 0:
        return activation.active_from_ms
    return activation.active_from_ms - cls.lead_ms - START_SLACK_MS


def _entries(planned, actual_from=None, actual_until=None, observed=None) -> list[dict]:
    """The manifest's `activations`. A NORMAL injector injects nothing, so it has no entry."""
    actual_from, actual_until, observed = actual_from or {}, actual_until or {}, observed or {}
    return [
        activation.manifest_entry(observed=observed.get(index),
                                  active_from_ms=actual_from.get(index),
                                  active_until_ms=actual_until.get(index))
        for index, (cls, activation) in enumerate(planned)
        if cls.incident_type != "NORMAL"
    ]


def _draft(plan: Plan, started_at_ms: int, bucket: str, workload) -> _Draft:
    """Resolve a plan into activations and the RUNNING manifest. Touches nothing."""
    if not plan.faults:
        raise ValueError("a plan needs at least one fault; a NORMAL run uses the noop injector")
    if plan.recovery_s < 0:
        raise ValueError("recovery cannot be negative")

    experiment_id = manifest.new_experiment_id(started_at_ms)
    planned = []
    for index, fault in enumerate(plan.faults):
        cls = injectors.get(fault.injector)
        if cls.needs_workload and isinstance(workload, NoWorkload):
            raise ValueError(f"{cls.name}: is applied through the workload, so the run needs one")
        if fault.offset_s < 0:
            raise ValueError(f"{cls.name}: its window cannot open before the run starts")
        active_from_ms = started_at_ms + round(fault.offset_s * 1000)
        activation = cls.plan(
            experiment_id, index,
            active_from_ms=active_from_ms,
            active_until_ms=active_from_ms + round(fault.duration_s * 1000),
            # Each activation draws from its own RNG, so adding a fault to a
            # plan cannot shift the values another one resolves.
            rng=random.Random(f"{plan.seed}:{index}"),
            intensity=fault.intensity, band=fault.band, target=fault.target,
        )
        if _start_at_ms(cls, activation) < started_at_ms:
            needed_s = (cls.lead_ms + START_SLACK_MS) / 1000
            raise ValueError(f"{cls.name}: needs at least {needed_s:g} s between the start of "
                             f"the run and its window")
        planned.append((cls, activation))

    running = manifest.build_manifest(
        experiment_id=experiment_id, incident_type=plan.incident_type, seed=plan.seed,
        created_by=plan.created_by, started_at_ms=started_at_ms, workload=workload.describe(),
        activations=_entries(planned), bucket=bucket,
    )
    manifest.validate_or_raise(running)
    return _Draft(
        experiment_id=experiment_id,
        started_at_ms=started_at_ms,
        ends_at_ms=max(a.active_until_ms for _, a in planned) + round(plan.recovery_s * 1000),
        planned=planned,
        running=running,
    )


def preview(plan: Plan, *, workload=None, now_ms: int | None = None) -> dict:
    """The manifest a run of `plan` would start with, without touching AWS.

    Same seed, same params: this is how to check an injector's configure()
    before spending a real run on it.
    """
    workload = NoWorkload() if workload is None else workload
    return _draft(plan, _now_ms() if now_ms is None else now_ms, "dry-run", workload).running


def _timeline(planned) -> list[tuple]:
    """Every start and stop as (epoch ms, activation index, step), in the order to make them."""
    steps = []
    for index, (cls, activation) in enumerate(planned):
        steps.append((_start_at_ms(cls, activation), index, "start"))
        steps.append((activation.active_until_ms, index, "stop"))
    return sorted(steps)


def _sleep_until(at_ms: int, clock, sleep, what: str) -> None:
    # A loop, because a sleep may return early.
    while (remaining_ms := at_ms - clock()) > 0:
        sleep(remaining_ms / 1000)
    late_ms = clock() - at_ms
    if late_ms > MAX_LATE_MS:
        raise FellBehind(f"{what} came {late_ms / 1000:.0f} s late, so this run is not what its "
                         f"manifest says. The machine probably slept: run the simulator under "
                         f"`caffeinate -is`. The run is recorded as FAILED, which M4 skips.")


def _reported_time(value, step: str):
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(f"{step}() must return epoch milliseconds or None, got {value!r}")
    return value


def run(plan: Plan, stack: Stack, *, workload=None, clock=_now_ms, sleep=time.sleep,
        log=print, on_start=None) -> dict:
    """Run one experiment and return its final manifest.

    Raises before touching anything if the plan is invalid, and LockHeld if
    another experiment is running. Once the lock is taken, a failure or Ctrl-C
    still winds the run down and records it as FAILED or ABORTED before the
    exception carries on up. FellBehind is such a failure: a step that ran
    far later than planned, as after the machine slept.

    `on_start`, if given, is called with the experiment id once the lock is
    held, so a caller (M4's batch runner) knows which manifest a run that
    then fails has written.
    """
    workload = NoWorkload() if workload is None else workload
    draft = _draft(plan, clock(), stack.bucket, workload)
    experiment_id = draft.experiment_id

    stack.lock.acquire(experiment_id, plan.created_by,
                       expires_at_s=draft.ends_at_ms // 1000 + LOCK_MARGIN_S,
                       now_s=draft.started_at_ms // 1000)
    log(f"{experiment_id} {plan.incident_type}, seed {plan.seed}, "
        f"until {manifest.to_iso(draft.ends_at_ms)}")
    log(f"  manifest: s3://{stack.bucket}/{manifest.manifest_key(experiment_id)}")

    instances, stopped = [], set()
    actual_from, actual_until, observed = {}, {}, {}
    status = "FAILED"
    try:
        if on_start is not None:
            on_start(experiment_id)
        manifest.write_manifest(stack.s3, stack.bucket, draft.running)
        ctx = Context(experiment_id=experiment_id, session=stack.session,
                      control_plane=stack.control_plane, workload=workload)
        instances = [cls(ctx, activation) for cls, activation in draft.planned]
        workload.start()

        for at_ms, index, step in _timeline(draft.planned):
            _sleep_until(at_ms, clock, sleep, f"{step} {instances[index].name}")
            log(f"  {step} {instances[index].name}")
            if step == "start":
                actual_from[index] = _reported_time(instances[index].start(), step)
            else:
                actual_until[index] = _reported_time(instances[index].stop(), step)
                stopped.add(index)
        _sleep_until(draft.ends_at_ms, clock, sleep, "the end of the recovery")

        for index, instance in enumerate(instances):
            observed[index] = instance.observe()
            if not isinstance(observed[index], dict):
                raise TypeError(f"{instance.name}: observe() must return a dict, "
                                f"got {type(observed[index]).__name__}")
        workload.stop()

        final = manifest.finalize(
            {**draft.running,
             "activations": _entries(draft.planned, actual_from, actual_until, observed)},
            "COMPLETED", clock())
        manifest.write_manifest(stack.s3, stack.bucket, final)
        status = "COMPLETED"
        return final
    except KeyboardInterrupt:
        status = "ABORTED"
        raise
    finally:
        def attempt(what, action):
            # One step failing must not stop the others: the lock still has to
            # be released and the faults deleted.
            try:
                return action()
            except Exception as e:
                log(f"  warning: {what} failed: {type(e).__name__}: {e}")

        for index, instance in enumerate(instances):
            if index not in stopped:
                attempt(f"stopping {instance.name}", instance.stop)
        attempt("stopping the workload", workload.stop)
        attempt("deleting leftover faults",
                lambda: stack.control_plane.cancel_experiment(experiment_id))
        if status != "COMPLETED":
            # The planned windows are kept as they were; the status says the
            # run did not get through them. The +1 keeps the end after the
            # start for a run that failed in its first millisecond.
            ended_at_ms = max(clock(), draft.started_at_ms + 1)
            attempt("writing the final manifest", lambda: manifest.write_manifest(
                stack.s3, stack.bucket, manifest.finalize(draft.running, status, ended_at_ms)))
        if attempt("releasing the lock", lambda: stack.lock.release(experiment_id)) is False:
            log("  warning: another experiment took the lock before this one finished, "
                "so the two overlap")
        log(f"{experiment_id} {status}")
