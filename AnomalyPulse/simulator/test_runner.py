import json
import time

import pytest
from boto3.dynamodb.conditions import Key

from simulator import control_plane as cp
from simulator import injectors
from simulator import manifest as mf
from simulator import runner
from simulator.injectors import ControlPlaneInjector, Injector
from simulator.lock import LockHeld
from simulator.runner import Fault, Plan

# Created by conftest's `aws` fixture.
BUCKET = "anomalypulse-telemetry-000000000000-us-east-2"
EXPERIMENTS_TABLE = "anomalypulse-experiments"
FAULT_CONFIG_TABLE = "anomalypulse-fault-config"

BAND = {"parameter": "added_latency_ms", "low": 200, "high": 500}
MINUTE = 60_000


# --- helpers ---------------------------------------------------------------

class FakeClock:
    # Time that only moves when the runner sleeps, so a 15-minute run takes no
    # time at all. It starts at the real time because the control plane checks
    # a fault's lead against the wall clock.
    def __init__(self):
        self.started_ms = self.now_ms = int(time.time() * 1000)
        self.before_sleep = None  # a test's hook, called with nothing, before each sleep

    def clock(self):
        return self.now_ms

    def sleep(self, seconds):
        if self.before_sleep is not None:
            self.before_sleep()
        self.now_ms += round(seconds * 1000)

    @property
    def elapsed_ms(self):
        return self.now_ms - self.started_ms


class FakeWorkload:
    def __init__(self):
        self.calls = []

    def describe(self):
        return {"request_rate_rps": 20}

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def _single(injector="test-recorder", **overrides):
    # Five minutes of baseline, a five-minute fault, five minutes of recovery.
    fields = {"duration_s": 300, "baseline_s": 300, "recovery_s": 300, "seed": 7,
              "created_by": "jake"}
    if injector != "noop":
        fields.update(intensity=0.5, band=BAND)
    fields.update(overrides)
    return Plan.single(injector, **fields)


def _stored_manifests(aws):
    # Every version of every manifest in the bucket, as (first written, last
    # written) per experiment. Each test runs one experiment.
    s3 = aws.client("s3")
    versions = s3.list_object_versions(Bucket=BUCKET, Prefix="experiments/").get("Versions", [])

    def read(version):
        body = s3.get_object(Bucket=BUCKET, Key=version["Key"], VersionId=version["VersionId"])["Body"]
        return json.loads(body.read())

    latest = [read(v) for v in versions if v["IsLatest"]]
    earlier = [read(v) for v in versions if not v["IsLatest"]]
    return earlier, latest


def _final_manifest(aws):
    _, latest = _stored_manifests(aws)
    assert len(latest) == 1
    return latest[0]


def _faults(aws):
    table = aws.resource("dynamodb").Table(FAULT_CONFIG_TABLE)
    items = []
    for service in cp.SERVICES:
        items += table.query(KeyConditionExpression=Key("service").eq(service),
                             ConsistentRead=True)["Items"]
    return items


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def fake():
    return FakeClock()


@pytest.fixture
def go(stack, fake):
    # runner.run against the mocked stack, on fake time, collecting the log.
    lines = []

    def _go(plan, **kwargs):
        return runner.run(plan, stack, clock=fake.clock, sleep=fake.sleep, log=lines.append,
                          **kwargs)

    _go.log = lines
    return _go


@pytest.fixture
def recorder(registry, fake):
    # An injector that injects nothing but notes when each step was called, and
    # can be told to fail a step or to report a time.
    @injectors.register
    class Recorder(Injector):
        name = "test-recorder"
        incident_type = "LAMBDA_DEGRADATION"
        parameter = "added_latency_ms"
        targets = ("order-service",)

        events = []     # (step, activation index, fake time relative to the run's start)
        fails = set()   # steps that raise
        returns = {}    # step -> what it returns
        contexts = []

        @classmethod
        def configure(cls, intensity, band, rng):
            return {"added_latency_ms": cls.native_value(intensity, band), "jitter": rng.random()}

        def _step(self, step):
            Recorder.events.append((step, self.activation.index, fake.elapsed_ms))
            Recorder.contexts.append(self.ctx)
            if step in Recorder.fails:
                raise RuntimeError(f"{step} failed")
            return Recorder.returns.get(step)

        def start(self):
            return self._step("start")

        def stop(self):
            return self._step("stop")

        def observe(self):
            result = self._step("observe")
            return result if "observe" in Recorder.returns else {}

    return Recorder


@pytest.fixture
def control_plane_fault(registry):
    @injectors.register
    class ControlPlaneFault(ControlPlaneInjector):
        name = "test-control-plane-fault"
        incident_type = "DEPENDENCY_FAILURE"
        parameter = "timeout_ms"
        targets = ("payment-service",)

        @classmethod
        def configure(cls, intensity, band, rng):
            return {"timeout_ms": cls.native_value(intensity, band), "failure_rate": 0.2}

    return ControlPlaneFault


# --- a no-op run, end to end ---------------------------------------------------

def test_noop_run_writes_a_running_manifest_then_a_completed_one(go, aws):
    # M3-1's first Definition-of-Done item, short of the real stack: the no-op
    # goes through the whole run and leaves a valid manifest.
    final = go(_single("noop"))
    earlier, latest = _stored_manifests(aws)
    assert [m["status"] for m in earlier] == ["RUNNING"]
    assert latest == [final]
    assert mf.validate(final) == (True, [])
    assert final["status"] == "COMPLETED"
    assert final["experiment_id"] == earlier[0]["experiment_id"]


def test_noop_run_is_recorded_as_normal_with_no_activations(go):
    final = go(_single("noop"))
    assert final["incident_type"] == "NORMAL"
    assert final["severity"] is None
    assert final["activations"] == []
    assert final["telemetry"] == {"bucket": BUCKET, "prefixes": ["raw/", "metrics/"]}


def test_run_lasts_baseline_plus_fault_plus_recovery(go, fake):
    final = go(_single("noop", baseline_s=120, duration_s=60, recovery_s=30))
    assert fake.elapsed_ms == 210_000
    assert mf.to_epoch_ms(final["started_at"]) == fake.started_ms
    assert mf.to_epoch_ms(final["ended_at"]) == fake.started_ms + 210_000


def test_experiment_id_carries_the_start_time(go, fake):
    final = go(_single("noop"))
    assert final["experiment_id"].startswith(mf.new_experiment_id(fake.started_ms)[:-4])


def test_run_records_who_started_it_and_the_seed(go):
    final = go(_single("noop", seed=20261012, created_by="rachel"))
    assert (final["seed"], final["created_by"]) == (20261012, "rachel")


# --- the lock around a run ------------------------------------------------------

def test_lock_is_held_for_the_run_and_released_after(go, stack, fake):
    seen = []
    fake.before_sleep = lambda: seen.append(stack.lock.holder())
    final = go(_single("noop", created_by="jake"))
    assert seen and all(holder is not None for holder in seen)
    assert seen[0]["experiment_id"] == final["experiment_id"]
    assert seen[0]["held_by"] == "jake"
    assert stack.lock.holder() is None


def test_lock_expires_a_margin_after_the_planned_end(go, stack, fake):
    # Long enough for observe() and the final write; short enough that a crashed
    # run does not block the stack for long.
    seen = []
    fake.before_sleep = lambda: seen.append(stack.lock.holder())
    go(_single("noop"))  # 15 minutes
    assert seen[0]["expires_at"] == fake.started_ms // 1000 + 900 + runner.LOCK_MARGIN_S


def test_second_run_is_refused_and_touches_nothing(go, stack, aws, fake):
    stack.lock.acquire("exp-20261012T041500Z-0b1c", "linu", fake.started_ms // 1000 + 600,
                       now_s=fake.started_ms // 1000)
    with pytest.raises(LockHeld, match="started by linu"):
        go(_single("noop"))
    assert _stored_manifests(aws) == ([], [])
    assert stack.lock.holder()["experiment_id"] == "exp-20261012T041500Z-0b1c"
    assert fake.elapsed_ms == 0


def test_losing_the_lock_mid_run_is_reported(go, stack, aws, fake):
    # The run outlived its lock and another experiment took it. Ours still
    # finishes and must leave their lock alone, but the overlap is called out.
    table = aws.resource("dynamodb").Table(EXPERIMENTS_TABLE)
    fake.before_sleep = lambda: table.put_item(Item={
        "pk": "LOCK", "experiment_id": "exp-20261012T050000Z-0b1c", "held_by": "linu",
        "expires_at": fake.started_ms // 1000 + 7200})
    final = go(_single("noop"))
    assert final["status"] == "COMPLETED"
    assert stack.lock.holder()["experiment_id"] == "exp-20261012T050000Z-0b1c"
    assert any("another experiment took the lock" in line for line in go.log)


# --- the timeline -----------------------------------------------------------------

def test_injector_is_started_as_its_window_opens_and_stopped_as_it_closes(go, recorder):
    go(_single())
    assert recorder.events == [
        ("start", 0, 5 * MINUTE),
        ("stop", 0, 10 * MINUTE),
        ("observe", 0, 15 * MINUTE),  # after the recovery period
    ]


def test_activation_is_recorded_with_its_planned_window(go, fake, recorder):
    final = go(_single())
    assert final["incident_type"] == "LAMBDA_DEGRADATION"
    assert final["severity"] == "MEDIUM"
    [activation] = final["activations"]
    assert activation["fault_id"] == f"{final['experiment_id']}#0"
    assert activation["injector"] == "test-recorder"
    assert activation["target"] == "order-service"
    assert activation["band"] == BAND
    assert activation["params"]["added_latency_ms"] == 350
    assert activation["active_from"] == mf.to_iso(fake.started_ms + 5 * MINUTE)
    assert activation["active_until"] == mf.to_iso(fake.started_ms + 10 * MINUTE)
    assert activation["observed"] == {}


def test_times_reported_by_start_and_stop_replace_the_plan(go, fake, recorder):
    # Some mechanisms take effect late. The manifest records when the fault was
    # really active, which is what the window labels are computed from.
    recorder.returns = {"start": fake.started_ms + 5 * MINUTE + 4000,
                        "stop": fake.started_ms + 10 * MINUTE - 2000}
    [activation] = go(_single())["activations"]
    assert activation["active_from"] == mf.to_iso(fake.started_ms + 5 * MINUTE + 4000)
    assert activation["active_until"] == mf.to_iso(fake.started_ms + 10 * MINUTE - 2000)


def test_what_observe_returns_is_recorded(go, recorder):
    recorder.returns = {"observe": {"throttled_requests": 412}}
    [activation] = go(_single())["activations"]
    assert activation["observed"] == {"throttled_requests": 412}


def test_running_manifest_has_the_plan_and_nothing_observed(go, aws, recorder):
    recorder.returns = {"observe": {"throttled_requests": 412}}
    final = go(_single())
    [running], _ = _stored_manifests(aws)
    assert running["status"] == "RUNNING"
    assert running["ended_at"] is None
    assert running["activations"][0]["params"] == final["activations"][0]["params"]
    assert running["activations"][0]["observed"] == {}


def test_cascade_runs_each_fault_on_its_own_window(go, fake, recorder):
    # Two activations with staggered, overlapping windows: the record M4 labels
    # CASCADING_FAILURE windows from.
    plan = Plan(incident_type="CASCADING_FAILURE", recovery_s=120, seed=7, created_by="jake", faults=(
        Fault("test-recorder", offset_s=60, duration_s=240, intensity=0.2, band=BAND),
        Fault("test-recorder", offset_s=120, duration_s=180, intensity=0.9, band=BAND),
    ))
    final = go(plan)
    assert recorder.events == [
        ("start", 0, 1 * MINUTE), ("start", 1, 2 * MINUTE),
        ("stop", 0, 5 * MINUTE), ("stop", 1, 5 * MINUTE),
        ("observe", 0, 7 * MINUTE), ("observe", 1, 7 * MINUTE),
    ]
    assert [a["fault_id"][-2:] for a in final["activations"]] == ["#0", "#1"]
    assert final["severity"] == "HIGH"  # the highest intensity decides
    assert fake.elapsed_ms == 7 * MINUTE


# --- control-plane faults -----------------------------------------------------------

def test_control_plane_fault_is_in_the_table_well_before_its_window(go, aws, fake, control_plane_fault):
    # Every Lambda execution environment caches its faults for a few seconds, so
    # the item has to be there at least MIN_LEAD_MS before the window opens for
    # the fault to begin everywhere at once.
    seen = []
    fake.before_sleep = lambda: seen.append((fake.elapsed_ms, _faults(aws)))
    final = go(_single("test-control-plane-fault", band={"parameter": "timeout_ms", "low": 2500, "high": 3500}))

    in_table_from = min(at for at, items in seen if items)
    assert in_table_from <= 5 * MINUTE - cp.MIN_LEAD_MS
    [item] = next(items for at, items in seen if items)
    assert item["service"] == "payment-service"
    assert item["fault_id"] == final["activations"][0]["fault_id"]
    assert item["injector"] == "test-control-plane-fault"
    assert item["params"] == {"timeout_ms": 3000, "failure_rate": cp._to_dynamo(0.2)}
    assert item["active_from_ms"] == fake.started_ms + 5 * MINUTE
    assert item["active_until_ms"] == fake.started_ms + 10 * MINUTE


def test_control_plane_fault_is_gone_after_the_run(go, aws, control_plane_fault):
    go(_single("test-control-plane-fault", band={"parameter": "timeout_ms", "low": 2500, "high": 3500}))
    assert _faults(aws) == []


def test_fault_that_cannot_be_scheduled_in_time_is_refused_before_anything_starts(
        go, aws, stack, control_plane_fault):
    plan = _single("test-control-plane-fault", baseline_s=10,
                   band={"parameter": "timeout_ms", "low": 2500, "high": 3500})
    with pytest.raises(ValueError, match="needs at least 15 s between the start of the run"):
        go(plan)
    assert _stored_manifests(aws) == ([], [])
    assert stack.lock.holder() is None


def test_fault_left_behind_by_a_broken_stop_is_still_deleted(go, aws, registry):
    # The backstop: whatever an injector's stop() did or did not do, no fault of
    # this experiment is left in the table when the run ends.
    @injectors.register
    class Leaky(ControlPlaneInjector):
        name = "test-leaky"
        incident_type = "DEPENDENCY_FAILURE"
        parameter = "timeout_ms"
        targets = ("payment-service",)

        @classmethod
        def configure(cls, intensity, band, rng):
            return {"timeout_ms": cls.native_value(intensity, band)}

        def stop(self):
            return None

    go(_single("test-leaky", band={"parameter": "timeout_ms", "low": 2500, "high": 3500}))
    assert _faults(aws) == []


# --- checked before the lock is taken -------------------------------------------------

@pytest.mark.parametrize("plan, problem", [
    (Plan("NORMAL", (), 300, 7, "jake"), "a plan needs at least one fault"),
    (Plan("NORMAL", (Fault("noop", offset_s=0, duration_s=60),), -1, 7, "jake"), "recovery cannot be negative"),
    (Plan("NORMAL", (Fault("noop", offset_s=-5, duration_s=60),), 0, 7, "jake"), "cannot open before the run starts"),
    (Plan("NORMAL", (Fault("noop", offset_s=0, duration_s=0),), 0, 7, "jake"), "window must end after it starts"),
])
def test_plan_that_makes_no_sense_is_refused(go, aws, stack, plan, problem):
    with pytest.raises(ValueError, match=problem):
        go(plan)
    assert _stored_manifests(aws) == ([], [])
    assert stack.lock.holder() is None


def test_unknown_injector_is_refused(go):
    with pytest.raises(LookupError, match="no injector named 'traffic-spke'"):
        go(Plan("TRAFFIC_SPIKE", (Fault("traffic-spke", offset_s=0, duration_s=60),), 0, 7, "jake"))


def test_label_that_contradicts_the_faults_is_refused(go, aws, stack, recorder):
    # A NORMAL label on a run that injects a fault would be wrong ground truth.
    plan = Plan("NORMAL", (Fault("test-recorder", offset_s=60, duration_s=60, intensity=0.5, band=BAND),),
                60, 7, "jake")
    with pytest.raises(mf.ManifestError):
        go(plan)
    assert recorder.events == []
    assert _stored_manifests(aws) == ([], [])
    assert stack.lock.holder() is None


def test_seed_outside_32_bits_is_refused(go):
    with pytest.raises(mf.ManifestError, match="seed"):
        go(_single("noop", seed=2**32))


# --- when a run goes wrong ----------------------------------------------------------

def test_failed_start_is_recorded_and_everything_is_wound_down(go, aws, stack, recorder):
    recorder.fails = {"start"}
    with pytest.raises(RuntimeError, match="start failed"):
        go(_single())

    final = _final_manifest(aws)
    assert mf.validate(final) == (True, [])
    assert final["status"] == "FAILED"
    assert final["ended_at"] is not None
    assert len(final["activations"]) == 1  # the plan is kept; the status says it was not completed
    assert [step for step, _, _ in recorder.events] == ["start", "stop"]  # stop() still called
    assert stack.lock.holder() is None
    assert any(line.endswith("FAILED") for line in go.log)


def test_ctrl_c_is_recorded_as_aborted(go, aws, stack, fake, recorder):
    def interrupt_during_the_fault():
        if fake.elapsed_ms >= 5 * MINUTE:
            raise KeyboardInterrupt

    fake.before_sleep = interrupt_during_the_fault
    with pytest.raises(KeyboardInterrupt):
        go(_single())

    final = _final_manifest(aws)
    assert final["status"] == "ABORTED"
    assert mf.to_epoch_ms(final["ended_at"]) == fake.started_ms + 5 * MINUTE
    assert [step for step, _, _ in recorder.events] == ["start", "stop"]
    assert stack.lock.holder() is None


def _doze_once(fake, after_ms, for_ms):
    # The machine sleeps once: a sleeping laptop's timers stop, so the run's
    # clock jumps ahead while it waits for its next step.
    state = {"dozed": False}

    def doze():
        if fake.elapsed_ms >= after_ms and not state["dozed"]:
            state["dozed"] = True
            fake.now_ms += for_ms

    return doze


def test_a_run_that_slept_through_its_recovery_fails_instead_of_completing(go, aws, stack, fake,
                                                                          recorder):
    # What M3-2 saw: the laptop slept after the fault, and the run ended 25
    # minutes late but called itself COMPLETED.
    fake.before_sleep = _doze_once(fake, after_ms=10 * MINUTE, for_ms=25 * MINUTE)
    with pytest.raises(runner.FellBehind, match="the end of the recovery came 1500 s late"):
        go(_single())

    final = _final_manifest(aws)
    assert final["status"] == "FAILED"
    assert [step for step, _, _ in recorder.events] == ["start", "stop"]
    assert stack.lock.holder() is None
    assert any(line.endswith("FAILED") for line in go.log)


def test_a_run_that_slept_during_its_fault_fails_and_still_stops_the_fault(go, aws, stack, fake,
                                                                           recorder):
    fake.before_sleep = _doze_once(fake, after_ms=5 * MINUTE, for_ms=3 * MINUTE)
    with pytest.raises(runner.FellBehind, match="stop test-recorder came 180 s late"):
        go(_single())

    assert _final_manifest(aws)["status"] == "FAILED"
    # The late stop is not made on schedule, but the wind-down still makes it.
    assert [step for step, _, _ in recorder.events] == ["start", "stop"]
    assert stack.lock.holder() is None


def test_a_step_a_few_seconds_late_is_fine(go, aws, fake):
    fake.before_sleep = _doze_once(fake, after_ms=0, for_ms=runner.MAX_LATE_MS)
    assert go(_single("noop"))["status"] == "COMPLETED"


def test_aborted_control_plane_fault_is_deleted(go, aws, fake, control_plane_fault):
    # Aborting must not leave the fault to run out its window on the live stack.
    def interrupt_during_the_fault():
        if fake.elapsed_ms >= 5 * MINUTE:
            raise KeyboardInterrupt

    fake.before_sleep = interrupt_during_the_fault
    with pytest.raises(KeyboardInterrupt):
        go(_single("test-control-plane-fault", band={"parameter": "timeout_ms", "low": 2500, "high": 3500}))
    assert _faults(aws) == []


def test_failure_while_winding_down_does_not_stop_the_rest(go, aws, stack, recorder):
    # start() fails, and then stop() fails too. The manifest is still finalized
    # and the lock is still released; the stop failure is logged.
    recorder.fails = {"start", "stop"}
    with pytest.raises(RuntimeError, match="start failed"):
        go(_single())
    assert _final_manifest(aws)["status"] == "FAILED"
    assert stack.lock.holder() is None
    assert any("stopping test-recorder failed" in line for line in go.log)


def test_every_injector_is_stopped_when_one_fails(go, recorder):
    recorder.fails = {"start"}
    plan = Plan(incident_type="CASCADING_FAILURE", recovery_s=60, seed=7, created_by="jake", faults=(
        Fault("test-recorder", offset_s=60, duration_s=120, intensity=0.2, band=BAND),
        Fault("test-recorder", offset_s=90, duration_s=120, intensity=0.9, band=BAND),
    ))
    with pytest.raises(RuntimeError):
        go(plan)
    assert [(step, index) for step, index, _ in recorder.events] == [
        ("start", 0), ("stop", 0), ("stop", 1)]


@pytest.mark.parametrize("step, value", [("start", 2.5), ("stop", True), ("observe", [412])])
def test_step_that_returns_the_wrong_thing_fails_the_run(go, aws, stack, recorder, step, value):
    recorder.returns = {step: value}
    with pytest.raises(TypeError, match=f"{step}\\(\\) must return"):
        go(_single())
    assert _final_manifest(aws)["status"] == "FAILED"
    assert stack.lock.holder() is None


def test_run_that_fails_in_its_first_instant_is_still_recorded(go, aws, stack, fake, recorder):
    # No baseline, so start() is called, and fails, at the very moment the run
    # began. The manifest needs an end after its start to be valid; without one
    # the run would be left looking as if it were still RUNNING.
    recorder.fails = {"start"}
    with pytest.raises(RuntimeError):
        go(_single(baseline_s=0))
    assert fake.elapsed_ms == 0
    final = _final_manifest(aws)
    assert final["status"] == "FAILED"
    assert mf.validate(final) == (True, [])
    assert stack.lock.holder() is None


def test_failed_run_keeps_what_it_planned(go, aws, recorder):
    recorder.fails = {"stop"}
    with pytest.raises(RuntimeError):
        go(_single())
    [running], [final] = _stored_manifests(aws)
    assert final["activations"] == running["activations"]


# --- the workload ---------------------------------------------------------------------

def test_workload_is_started_stopped_and_recorded(go, recorder):
    workload = FakeWorkload()
    final = go(_single(), workload=workload)
    assert workload.calls[0] == "start"
    assert workload.calls[-1] == "stop"
    assert final["workload"] == {"request_rate_rps": 20}
    # Injectors reach the workload through their context; a traffic spike
    # changes its rate that way.
    assert all(ctx.workload is workload for ctx in recorder.contexts)


def test_workload_is_stopped_when_the_run_fails(go, recorder):
    recorder.fails = {"start"}
    workload = FakeWorkload()
    with pytest.raises(RuntimeError):
        go(_single(), workload=workload)
    assert workload.calls == ["start", "stop"]


def test_run_without_a_workload_records_none(go):
    assert go(_single("noop"))["workload"] == {}


# --- reproducibility --------------------------------------------------------------------

def test_same_plan_and_seed_resolve_the_same_params(recorder):
    # M3-1: same params -> same recorded config. Two runs differ only in their
    # ids and times.
    first = runner.preview(_single(seed=42), now_ms=1_791_778_500_000)
    again = runner.preview(_single(seed=42), now_ms=1_791_779_500_000)
    other = runner.preview(_single(seed=43), now_ms=1_791_778_500_000)
    assert first["activations"][0]["params"] == again["activations"][0]["params"]
    assert first["activations"][0]["params"] != other["activations"][0]["params"]
    for key in ("incident_type", "severity", "seed", "created_by", "workload"):
        assert first[key] == again[key]


def test_what_one_fault_draws_does_not_change_what_another_resolves(recorder):
    # Each activation draws from its own RNG, derived from the seed and its
    # index. With one RNG shared down the list, the second fault's params would
    # depend on how many values the first one happened to draw: here none (the
    # noop) against one (the recorder).
    quiet = Fault("noop", offset_s=60, duration_s=60)
    drawing = Fault("test-recorder", offset_s=60, duration_s=60, intensity=0.2, band=BAND)
    second = Fault("test-recorder", offset_s=60, duration_s=60, intensity=0.9, band=BAND)
    after_quiet = runner.preview(Plan("LAMBDA_DEGRADATION", (quiet, second), 60, 7, "jake"))
    after_drawing = runner.preview(Plan("LAMBDA_DEGRADATION", (drawing, second), 60, 7, "jake"))
    assert after_quiet["activations"][0]["fault_id"].endswith("#1")  # the noop has no entry
    assert after_quiet["activations"][0]["params"] == after_drawing["activations"][1]["params"]


def test_two_faults_in_one_plan_draw_different_values(recorder):
    fault = Fault("test-recorder", offset_s=60, duration_s=60, intensity=0.5, band=BAND)
    manifest = runner.preview(Plan("LAMBDA_DEGRADATION", (fault, fault), 60, 7, "jake"))
    first, second = manifest["activations"]
    assert first["params"]["jitter"] != second["params"]["jitter"]


def test_preview_is_the_manifest_the_run_would_start_with():
    # No fixtures: preview needs no AWS at all.
    manifest = runner.preview(_single("noop"), now_ms=1_791_778_500_000)
    assert mf.validate(manifest) == (True, [])
    assert manifest["status"] == "RUNNING"
    assert manifest["telemetry"]["bucket"] == "dry-run"


def test_single_takes_its_label_from_the_injector(recorder):
    assert _single("noop").incident_type == "NORMAL"
    assert _single("test-recorder").incident_type == "LAMBDA_DEGRADATION"
