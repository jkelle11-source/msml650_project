import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from simulator import batch, dataset, manifest, runner
from simulator.batch import BatchRefused
from simulator.lock import LockHeld

DAY = 86_400_000
NOOP = {"injector": "noop", "baseline_s": 60, "duration_s": 60, "recovery_s": 60}
THROTTLE = {"injector": "db-throttling", "intensity": 0.5,
            "band": {"parameter": "load_ratio", "low": 1.5, "high": 2.5},
            "baseline_s": 60, "duration_s": 120, "recovery_s": 60}
CONFIG = {
    "functions": {key: {"function_name": name, "memory_mb": 1769, "timeout_s": 10, "runtime": "python3.13",
                        "architectures": ["arm64"], "code_sha256": f"sha-{key}", "layers": ["layer:1"],
                        "runtime_version_arn": "rv:1"}
                  for key, name in batch.stack_config.FUNCTIONS.items()},
    "products_table": {"name": "anomalypulse-products-v2", "billing_mode": "PROVISIONED",
                       "read_capacity_units": 200, "write_capacity_units": 5, "scan_rcu": 0.5},
}
RUNNER = {"hostname": "runner", "python": "3.13", "platform": "linux", "ec2": {"instance_id": "i-0runner"}}


def _entry(slot, phase="train", kind="incident", spec=NOOP, **extra):
    return {"slot": slot, "phase": phase, "seed": 1000 + slot, kind: spec, **extra}


def _queue(tmp_path, entries, name="test"):
    path = tmp_path / f"queue-{len(list(tmp_path.iterdir()))}.json"
    path.write_text(json.dumps({"dataset": name, "experiments": entries}))
    return batch.load_queue(path)


class FakeClock:
    def __init__(self):
        self.now_ms = int(datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp() * 1000)

    def clock(self):
        return self.now_ms

    def sleep(self, seconds):
        self.now_ms += max(1, round(seconds * 1000))


class FakeRun:
    """Stands in for runner.run: a run takes its planned length on the fake clock.

    `outcomes[slot]` is a list, one dict per attempt, of how that run goes:
    raise (an exception), status, dropped, errors, achieved_fraction, label_from.
    """

    def __init__(self, stack, clock):
        self.stack, self.clock = stack, clock
        self.outcomes = {}
        self.seeds = []

    def __call__(self, plan, stack, *, workload, log, on_start):
        slot = plan.seed - 1000
        self.seeds.append(plan.seed)
        attempts = self.outcomes.get(slot, [])
        outcome = attempts.pop(0) if attempts else {}
        running = runner.preview(plan, workload=workload, now_ms=self.clock.now_ms)
        on_start(running["experiment_id"])
        self.clock.now_ms += round(batch.run_seconds(plan) * 1000)
        if "raise" in outcome:
            raise outcome["raise"]
        final = manifest.finalize(running, outcome.get("status", "COMPLETED"), self.clock.now_ms)
        for activation in final["activations"]:
            if activation["injector"] == "db-throttling":
                # THROTTLE asks for 2.0x, which throttles half the steady reads.
                activation["observed"] = {
                    "achieved_fraction": outcome.get("achieved_fraction", 1.0),
                    "label_from": None if outcome.get("label_from") is False else activation["active_from"],
                    "offered_ratio": 2.04,
                    "steady_throttle_rate": outcome.get("steady_throttle_rate", 0.51),
                    "provisioned_rcu": 200, "steady_peak_served_per_s": 192.5}
        manifest.write_manifest(stack.s3, stack.bucket, final)
        # What the client saw; the workload never really started.
        workload._sent = 10_000
        workload._dropped = outcome.get("dropped", 0)
        workload._results[("GET /products", "2xx")] = 10_000 - outcome.get("errors", 0)
        workload._results[("GET /products", "error")] = outcome.get("errors", 0)
        return final


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake_run(stack, clock):
    return FakeRun(stack, clock)


@pytest.fixture
def go(stack, clock, fake_run):
    # run_batch against the mocked stack on fake time. `throttles[slot]` is what
    # CloudWatch reports for that slot's runs (default: nothing).
    lines, throttles, configs = [], {}, [CONFIG]

    def throttle_check(final):
        return throttles.get(final["seed"] - 1000, {"throttled_requests": 0, "read_throttle_events": 0})

    def _go(queue, **kwargs):
        options = dict(read_config=lambda session: configs[-1], runner_info=RUNNER,
                       code_info={"commit": "abc123", "dirty": False}, base_url="https://api.invalid",
                       run=fake_run, throttle_check=throttle_check, clock=clock.clock, sleep=clock.sleep,
                       log=lines.append)
        options.update(kwargs)
        return batch.run_batch(queue, stack, **options)

    _go.log, _go.throttles, _go.configs = lines, throttles, configs
    return _go


def _registry(stack, name="test"):
    return dataset.Registry(stack.s3, stack.bucket, name)


# --- the queue -----------------------------------------------------------------

def test_the_validation_queue_in_the_repo_loads():
    queue = batch.load_queue(Path(batch.__file__).parent / "queues" / "m4-1-validation.json")
    assert queue.dataset == "m4-1-validation"
    assert len(queue.entries) >= 10       # M4-1's Definition of Done
    assert set(queue.incident_types.values()) == set(manifest.INCIDENT_TYPES)
    # The run shape M4 chose: 3 min baseline, 5 min fault, 2 min recovery.
    assert {batch.run_seconds(batch.plan_for(e, "x")) for e in queue.entries} == {600}


@pytest.mark.parametrize("entries, message", [
    ([_entry(1), _entry(1)], "listed twice"),
    ([_entry(600, phase="train")], "phase must be val"),
    ([_entry(701, phase="val")], "phase must be test or stress"),
    ([{**_entry(1), "rps": 40}], "takes"),
    ([{**_entry(1), "cascade": {}}], "exactly one of"),
    ([{**_entry(1), "seed": -1}], "seed"),
    ([_entry(1, spec={**THROTTLE, "band": {"parameter": "multiplier", "low": 1, "high": 2}})], "slot 1"),
    ([_entry(1, mix=[1, 2])], "weights"),
])
def test_a_bad_queue_is_refused_before_anything_runs(tmp_path, entries, message):
    with pytest.raises(ValueError, match=message):
        _queue(tmp_path, entries)


def test_a_confounder_plan_keeps_its_single_fault_label(tmp_path):
    plan = {"incident_type": "TRAFFIC_SPIKE", "recovery_s": 60, "faults": [
        {"injector": "traffic-spike", "offset_s": 60, "duration_s": 120, "intensity": 0.5,
         "band": {"parameter": "multiplier", "low": 4, "high": 6}},
        {"injector": "lambda-degradation", "offset_s": 60, "duration_s": 120, "intensity": 0.5,
         "target": "order-service", "band": {"parameter": "added_latency_ms", "low": 500, "high": 800}}]}
    queue = _queue(tmp_path, [_entry(701, phase="test", kind="plan", spec=plan)])
    assert queue.incident_types == {701: "TRAFFIC_SPIKE"}


def test_every_run_is_sent_at_20_rps():
    assert batch.workload_for(_entry(1, mix=[1, 1, 1, 1, 1])).describe()["request_rate_rps"] == 20


# --- judging a run -------------------------------------------------------------

def _final(status="COMPLETED", rps=20, activations=()):
    return {"status": status, "workload": {"request_rate_rps": rps}, "activations": list(activations)}


def _client(sent=10_000, dropped=0, errors=0):
    return {"sent": sent, "dropped": dropped, "responses": {"2xx": sent - errors, "error": errors}}


def _throttling(fraction=1.0, label_from="2026-10-16T12:01:05.000Z", rate=0.75, offered=4.0, peak=192.5):
    return {"injector": "db-throttling", "params": {"load_ratio": 4.0},
            "observed": {"achieved_fraction": fraction, "label_from": label_from,
                         "steady_throttle_rate": rate, "offered_ratio": offered,
                         "provisioned_rcu": 200, "steady_peak_served_per_s": peak}}


@pytest.mark.parametrize("final, client, problem", [
    (_final(status="FAILED"), _client(), "ended FAILED"),
    (_final(rps=5), _client(), "not 20"),
    (_final(), _client(dropped=1), "dropped 1"),
    (_final(), _client(errors=11), "got no response"),
    (_final(), _client(sent=0), "sent nothing"),
    (_final(activations=[_throttling(fraction=0.96)]), _client(), "below 0.97"),
    (_final(activations=[_throttling(fraction=None)]), _client(), "below 0.97"),
    (_final(activations=[_throttling(label_from=None)]), _client(), "never throttled"),
    # M4-1's validation at 4.0x: 53% throttled where 75% is expected.
    (_final(activations=[_throttling(rate=0.5261)]), _client(), "burst capacity"),
    (_final(activations=[_throttling(rate=0.9)]), _client(), "should throttle about 0.75"),
    (_final(activations=[_throttling(rate=None)]), _client(), "throttled None"),
    # Without CloudWatch's offered_ratio, the asked-for ratio: 4.0x, so 0.4 is far off.
    (_final(activations=[_throttling(rate=0.4, offered=None)]), _client(), "4x should throttle"),
    # M4-1's 10x probe: a second full bank ~3 minutes in, served at ~1,000 RCU/s.
    (_final(activations=[_throttling(peak=325.0)]), _client(), "did burst capacity come back"),
    (_final(activations=[_throttling(peak=None)]), _client(), "served db-throttling None reads/s"),
])
def test_run_problems(final, client, problem):
    assert any(problem in p for p in batch.run_problems(final, client))


def test_a_good_run_has_no_problems():
    # 10 errors in 10,000 is exactly 0.1%, and 250 reads/s is exactly 1.25x 200 RCU: both allowed.
    assert batch.run_problems(_final(activations=[_throttling(fraction=0.97, peak=250.0)]),
                              _client(errors=10)) == []


class FakeCloudWatch:
    def __init__(self, points):
        self.points = points   # (metric, operation or None) -> [(minute iso, sum)]

    def list_metrics(self, **kwargs):
        return {"Metrics": [{"Dimensions": [{"Name": "TableName", "Value": "t"},
                                            {"Name": "Operation", "Value": op}]}
                            for metric, op in self.points if metric == kwargs["MetricName"] and op]}

    def get_metric_statistics(self, MetricName, Dimensions, StartTime, EndTime, **kwargs):
        op = next((d["Value"] for d in Dimensions if d["Name"] == "Operation"), None)
        return {"Datapoints": [
            {"Timestamp": datetime.fromtimestamp(manifest.to_epoch_ms(at) / 1000, timezone.utc), "Sum": n}
            for at, n in self.points.get((MetricName, op), [])]}


def test_products_throttling_counts_only_minutes_outside_db_throttling():
    final = {"started_at": "2026-10-16T12:00:30.000Z", "ended_at": "2026-10-16T12:10:30.000Z",
             "activations": [{"injector": "db-throttling", "active_from": "2026-10-16T12:03:30.000Z",
                              "active_until": "2026-10-16T12:08:30.000Z"}]}
    cloudwatch = FakeCloudWatch({
        # 12:03 and 12:08 overlap the window, so only 12:01 and 12:09 count.
        ("ThrottledRequests", "Scan"): [("2026-10-16T12:01:00.000Z", 2), ("2026-10-16T12:03:00.000Z", 50),
                                        ("2026-10-16T12:08:00.000Z", 40)],
        ("ThrottledRequests", "GetItem"): [("2026-10-16T12:09:00.000Z", 1)],
        ("ReadThrottleEvents", None): [("2026-10-16T12:05:00.000Z", 900)],
    })
    assert batch.products_throttling(cloudwatch, "t", final) == {"throttled_requests": 3,
                                                                  "read_throttle_events": 0}


def test_a_db_throttling_window_covers_its_reads_still_in_flight():
    # The window closes exactly on 12:08:00; its last reads can come back in 12:08.
    final = {"started_at": "2026-10-16T12:00:30.000Z", "ended_at": "2026-10-16T12:10:30.000Z",
             "activations": [{"injector": "db-throttling", "active_from": "2026-10-16T12:03:00.000Z",
                              "active_until": "2026-10-16T12:08:00.000Z"}]}
    cloudwatch = FakeCloudWatch({("ReadThrottleEvents", None): [("2026-10-16T12:08:00.000Z", 25),
                                                                ("2026-10-16T12:09:00.000Z", 1)]})
    assert batch.products_throttling(cloudwatch, "t", final)["read_throttle_events"] == 1


def test_without_db_throttling_every_minute_counts():
    final = {"started_at": "2026-10-16T12:00:30.000Z", "ended_at": "2026-10-16T12:10:30.000Z",
             "activations": []}
    cloudwatch = FakeCloudWatch({("ReadThrottleEvents", None): [("2026-10-16T12:05:00.000Z", 4)]})
    assert batch.products_throttling(cloudwatch, "t", final)["read_throttle_events"] == 4


# --- the batch -----------------------------------------------------------------

def test_a_batch_runs_checks_and_registers_every_experiment(tmp_path, go, stack):
    queue = _queue(tmp_path, [_entry(1), _entry(2, spec=THROTTLE), _entry(561, phase="val")])
    record = go(queue)

    assert record["status"] == "FINISHED"
    assert record["results"]["queue_slots"]["ACCEPTED"] == [1, 2, 561]
    slots = _registry(stack).slots()
    assert slots[2]["incident_type"] == "DATABASE_THROTTLING"
    assert slots[561]["phase"] == "val"
    for record_ in slots.values():
        assert record_["generation_date"] == "2026-10-16"
        assert record_["experiment_id"] == record_["attempts"][-1]["experiment_id"]
        assert record_["attempts"][-1]["verdict"] == "accepted"
        assert record_["attempts"][-1]["client"]["sent"] == 10_000


def test_the_batch_record_holds_the_stack_runner_and_code(tmp_path, go, stack):
    record = go(_queue(tmp_path, [_entry(1)]))
    stored = _registry(stack).batches()
    assert [b["batch_id"] for b in stored] == [record["batch_id"]]
    assert stored[0]["stack"] == CONFIG
    assert stored[0]["runner"] == RUNNER
    assert stored[0]["code"]["commit"] == "abc123"
    assert stored[0]["queue"]["slots"] == [1]
    assert _registry(stack).pins()["runner_instance_id"] == "i-0runner"


def test_rerunning_the_same_queue_resumes(tmp_path, go, fake_run):
    queue = _queue(tmp_path, [_entry(1), _entry(2)])
    go(queue)
    assert len(fake_run.seeds) == 2
    record = go(queue)
    assert len(fake_run.seeds) == 2            # nothing ran again
    assert record["results"]["queue_slots"]["ACCEPTED"] == [1, 2]


def test_a_rejected_run_is_rerun_with_the_same_seed(tmp_path, go, fake_run, stack):
    fake_run.outcomes[1] = [{"errors": 500}]
    go(_queue(tmp_path, [_entry(1), _entry(2)]))
    assert fake_run.seeds == [1001, 1002, 1001]
    attempts = _registry(stack).slot(1)["attempts"]
    assert [a["verdict"] for a in attempts] == ["rejected", "accepted"]
    assert "got no response" in attempts[0]["reasons"][0]


def test_a_db_throttling_run_that_fell_short_is_rerun(tmp_path, go, fake_run, stack):
    fake_run.outcomes[1] = [{"achieved_fraction": 0.9}, {"label_from": False}]
    go(_queue(tmp_path, [_entry(1, spec=THROTTLE)]))
    assert [a["verdict"] for a in _registry(stack).slot(1)["attempts"]] == ["rejected", "rejected", "accepted"]


def test_a_db_throttling_window_that_ran_on_burst_is_rerun(tmp_path, go, fake_run, stack):
    # M4-1's validation at 2.0x: 5% of the steady reads throttled, not ~50%.
    fake_run.outcomes[1] = [{"steady_throttle_rate": 0.0512}]
    go(_queue(tmp_path, [_entry(1, spec=THROTTLE)]))
    attempts = _registry(stack).slot(1)["attempts"]
    assert [a["verdict"] for a in attempts] == ["rejected", "accepted"]
    assert "run on burst capacity" in attempts[0]["reasons"][0]


def test_a_slot_is_given_up_after_max_attempts(tmp_path, go, fake_run, stack):
    fake_run.outcomes[1] = [{"dropped": 3}] * 2
    record = go(_queue(tmp_path, [_entry(1), _entry(2)]), max_attempts=2)
    assert _registry(stack).slot(1)["status"] == "EXHAUSTED"
    assert record["results"]["queue_slots"]["EXHAUSTED"] == [1]
    assert record["status"] == "FINISHED"


def test_throttling_outside_db_throttling_rejects_the_run_once_cloudwatch_settles(tmp_path, go, fake_run,
                                                                                    stack, clock):
    go.throttles[1] = {"throttled_requests": 4, "read_throttle_events": 0}
    started = clock.now_ms
    go(_queue(tmp_path, [_entry(1)]), max_attempts=2)
    attempts = _registry(stack).slot(1)["attempts"]
    assert [a["verdict"] for a in attempts] == ["rejected", "rejected"]
    assert "Products was throttled" in attempts[0]["reasons"][0]
    assert attempts[0]["products_throttling"]["throttled_requests"] == 4
    # Each verdict waited for CloudWatch to settle after its run.
    assert clock.now_ms - started >= 2 * (180 + batch.SETTLE_S) * 1000


def test_the_cloudwatch_check_of_one_run_waits_for_the_next(tmp_path, go, clock):
    go(_queue(tmp_path, [_entry(1), _entry(2), _entry(3)]))
    log = "\n".join(go.log)
    # Slot 1 is judged after slot 2 has run (a run is 3 min; it settles 5 min after).
    assert log.index("slot 1: accepted") > log.index("slot 2 (train)")


def test_a_cloudwatch_error_is_retried_not_fatal(tmp_path, go, stack):
    calls = []

    def flaky(final):
        calls.append(final["experiment_id"])
        if len(calls) == 1:
            raise RuntimeError("throttled by CloudWatch")
        return {"throttled_requests": 0, "read_throttle_events": 0}

    record = go(_queue(tmp_path, [_entry(1)]), throttle_check=flaky)
    assert record["results"]["queue_slots"]["ACCEPTED"] == [1]
    assert len(calls) == 2


def test_the_batch_stops_after_three_rejections_in_a_row(tmp_path, go, fake_run, stack):
    for slot in (1, 2, 3):
        fake_run.outcomes[slot] = [{"status": "FAILED"}]
    record = go(_queue(tmp_path, [_entry(s) for s in (1, 2, 3, 4)]))
    assert record["status"] == "STOPPED"
    assert "3 runs in a row" in record["stop_reason"]
    assert fake_run.seeds == [1001, 1002, 1003]
    assert _registry(stack).slot(1)["status"] == "PENDING"


def test_a_run_that_raises_is_recorded_with_its_experiment_id(tmp_path, go, fake_run, stack):
    fake_run.outcomes[1] = [{"raise": runner.FellBehind("the machine slept")}]
    go(_queue(tmp_path, [_entry(1)]))
    first = _registry(stack).slot(1)["attempts"][0]
    assert first["status"] == "FAILED"
    assert first["experiment_id"].startswith("exp-")
    assert first["reasons"] == ["FellBehind: the machine slept"]


def test_a_held_lock_stops_the_batch_without_recording_a_run(tmp_path, go, fake_run, stack):
    fake_run.outcomes[2] = [{"raise": LockHeld(None)}]
    record = go(_queue(tmp_path, [_entry(1), _entry(2), _entry(3)]))
    assert record["status"] == "STOPPED"
    assert "lock" in record["stop_reason"]
    assert _registry(stack).slot(2) is None
    assert _registry(stack).slot(1)["status"] == "ACCEPTED"   # still judged before stopping


def test_ctrl_c_records_the_run_and_the_batch_as_aborted(tmp_path, go, fake_run, stack):
    fake_run.outcomes[1] = [{"raise": KeyboardInterrupt()}]
    with pytest.raises(KeyboardInterrupt):
        go(_queue(tmp_path, [_entry(1)]))
    assert _registry(stack).slot(1)["attempts"][0]["status"] == "ABORTED"
    assert _registry(stack).batches()[0]["status"] == "ABORTED"


# --- refusals ---------------------------------------------------------------------

def test_refuses_a_stack_that_is_not_frozen(tmp_path, go, fake_run):
    go.configs.append({**CONFIG, "products_table": {**CONFIG["products_table"], "scan_rcu": 2.0}})
    with pytest.raises(BatchRefused, match="costs 2 RCU"):
        go(_queue(tmp_path, [_entry(1)]))
    assert fake_run.seeds == []


def test_refuses_a_deploy_since_the_dataset_was_pinned_unless_repinned(tmp_path, go, fake_run, stack):
    go(_queue(tmp_path, [_entry(1)]))
    functions = dict(CONFIG["functions"])
    functions["order-service"] = {**functions["order-service"], "code_sha256": "sha-new"}
    go.configs.append({**CONFIG, "functions": functions})
    queue = _queue(tmp_path, [_entry(1), _entry(2)])
    with pytest.raises(BatchRefused, match="functions.order-service.code_sha256"):
        go(queue)
    record = go(queue, repin=True)
    assert record["repinned"] is True
    assert _registry(stack).pins()["stack"]["functions"]["order-service"]["code_sha256"] == "sha-new"


def test_refuses_another_runner(tmp_path, go):
    go(_queue(tmp_path, [_entry(1)]))
    with pytest.raises(BatchRefused, match="runner instance: i-0runner -> None"):
        go(_queue(tmp_path, [_entry(2)]), runner_info={**RUNNER, "ec2": None})


def test_refuses_a_changed_spec_for_a_registered_slot(tmp_path, go):
    go(_queue(tmp_path, [_entry(1)]))
    with pytest.raises(BatchRefused, match="different spec"):
        go(_queue(tmp_path, [{**_entry(1), "seed": 7}]))


def test_refuses_a_held_lock(tmp_path, go, stack, clock):
    stack.lock.acquire("exp-20261016T115900Z-abcd", "josh", expires_at_s=clock.now_ms // 1000 + 600)
    with pytest.raises(BatchRefused, match="already running"):
        go(_queue(tmp_path, [_entry(1)]))


def test_training_and_test_never_share_a_batch(tmp_path, go):
    with pytest.raises(BatchRefused, match="not both"):
        go(_queue(tmp_path, [_entry(1), _entry(701, phase="test")]))


def test_the_test_set_is_generated_on_a_later_day_than_training(tmp_path, go, clock):
    go(_queue(tmp_path, [_entry(1)]))
    test_queue = _queue(tmp_path, [_entry(701, phase="test")])
    with pytest.raises(BatchRefused, match="later day"):
        go(test_queue)
    clock.now_ms += DAY
    assert go(test_queue)["results"]["queue_slots"]["ACCEPTED"] == [701]
    with pytest.raises(BatchRefused, match="before the test set"):
        go(_queue(tmp_path, [_entry(2)]))
