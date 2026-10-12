import json
import random
import time

import pytest

from simulator import cascade, cli, injectors
from simulator import manifest as mf
from simulator import runner
from simulator.cascade import Stage
from simulator.injectors import Context, Injector
from simulator.runner import Plan

BAND = {"parameter": "added_latency_ms", "low": 200, "high": 500}
RETRY_BAND = {"parameter": "retries_per_failure", "low": 0.5, "high": 2.5}
NOW_MS = 1_790_000_000_000      # a fixed start, so previews are comparable


# --- helpers ---------------------------------------------------------------

@pytest.fixture
def fakes(registry):
    # Stand-ins for the injectors of M3-2/3/4, which this ticket is blocked on.
    # They inject nothing and note what they were asked to do.
    def make(name, incident_type, parameter, targets=()):
        body = {
            "name": name, "incident_type": incident_type, "parameter": parameter,
            "targets": targets,
            "configure": classmethod(lambda cls, intensity, band, rng:
                                     {parameter: cls.native_value(intensity, band)}),
            "start": lambda self: None,
            "stop": lambda self: None,
        }
        injectors.register(type("Fake", (Injector,), body))

    make("test-spike", "TRAFFIC_SPIKE", "multiplier")
    make("test-throttle", "DATABASE_THROTTLING", "load_ratio")
    make("test-lambda", "LAMBDA_DEGRADATION", "added_latency_ms", ("order-service",))


def _stages(**overrides):
    return [
        Stage("test-spike", 0.5, {"parameter": "multiplier", "low": 2, "high": 4}),
        Stage("test-throttle", 0.5, {"parameter": "load_ratio", "low": 1.5, "high": 2.5},
              onset_delay_s=60),
        Stage("test-lambda", 0.5, BAND, target="order-service", onset_delay_s=60),
        Stage("retry-amplification", 0.5, RETRY_BAND, onset_delay_s=60),
    ]


def _plan(stages=None, **overrides):
    fields = {"baseline_s": 300, "cascade_s": 600, "recovery_s": 300, "seed": 7,
              "created_by": "melisa"}
    fields.update(overrides)
    return cascade.build_plan(_stages() if stages is None else stages, **fields)


def _offsets(plan):
    return [(f.injector, f.offset_s, f.duration_s) for f in plan.faults]


# --- building the plan -----------------------------------------------------

def test_stages_begin_one_after_another_and_end_together(fakes):
    plan = _plan()
    assert plan.incident_type == "CASCADING_FAILURE"
    assert _offsets(plan) == [
        ("test-spike", 300, 600), ("test-throttle", 360, 540),
        ("test-lambda", 420, 480), ("retry-amplification", 480, 420),
    ]
    assert plan.recovery_s == 300


def test_a_stage_can_end_before_the_cascade_does(fakes):
    stages = _stages()
    stages[1] = Stage(**{**stages[1].__dict__, "duration_s": 200})
    assert _offsets(_plan(stages))[1] == ("test-throttle", 360, 200)


def test_jitter_moves_onsets_but_the_same_seed_repeats_it(fakes):
    stages = [Stage(**{**s.__dict__, "onset_jitter_s": 20}) for s in _stages()]
    assert _offsets(_plan(stages, seed=1)) == _offsets(_plan(stages, seed=1))
    assert _offsets(_plan(stages, seed=1)) != _offsets(_plan(stages, seed=2))


def test_jitter_never_reorders_the_stages(fakes):
    stages = [Stage(**{**s.__dict__, "onset_delay_s": 1, "onset_jitter_s": 50}) for s in _stages()]
    for seed in range(50):
        onsets = [f.offset_s for f in _plan(stages, seed=seed).faults]
        assert onsets == sorted(onsets)


def test_jitter_does_not_change_what_a_fault_resolves(fakes):
    # The injector params come from the runner's own per-activation RNG.
    plain = runner.preview(_plan(seed=7), now_ms=NOW_MS)
    jittered = runner.preview(_plan([Stage(**{**s.__dict__, "onset_jitter_s": 20})
                                     for s in _stages()], seed=7), now_ms=NOW_MS)
    assert [a["params"] for a in plain["activations"]] == [a["params"] for a in jittered["activations"]]


@pytest.mark.parametrize("overrides, problem", [
    ({"stages": []}, "at least two"),
    ({"stages": [Stage("test-spike", 0.5, {"parameter": "multiplier", "low": 2, "high": 4})]},
     "at least two"),
    ({"cascade_s": 0}, "needs a length"),
    ({"baseline_s": -1}, "negative"),
    ({"cascade_s": 100}, "would begin"),                  # the third stage starts at 120
    ({"baseline_s": 900, "cascade_s": 900}, "past the limit"),
])
def test_build_plan_refuses_a_cascade_that_cannot_work(fakes, overrides, problem):
    stages = overrides.pop("stages", None)
    with pytest.raises(ValueError, match=problem):
        _plan(stages, **overrides)


def test_the_cost_limit_can_be_raised_on_purpose(fakes):
    assert _plan(baseline_s=900, cascade_s=900, max_run_s=7200).faults


def test_a_stage_cannot_outlast_the_cascade(fakes):
    stages = _stages()
    stages[1] = Stage(**{**stages[1].__dict__, "duration_s": 5000})
    with pytest.raises(ValueError, match="would end"):
        _plan(stages)


def test_stages_that_never_overlap_are_not_a_cascade(fakes):
    stages = [Stage("test-spike", 0.5, {"parameter": "multiplier", "low": 2, "high": 4},
                    duration_s=50),
              Stage("test-throttle", 0.5, {"parameter": "load_ratio", "low": 1.5, "high": 2.5},
                    onset_delay_s=100)]
    with pytest.raises(ValueError, match="no two stages overlap"):
        _plan(stages)


def test_noop_is_not_a_stage(fakes):
    stages = _stages() + [Stage("noop")]
    with pytest.raises(ValueError, match="not a cascade stage"):
        _plan(stages)


def test_an_unknown_injector_says_what_is_registered(fakes):
    with pytest.raises(LookupError, match="no injector named"):
        _plan([Stage("nonsense"), Stage("test-spike")])


def test_the_preview_is_a_valid_manifest_with_overlapping_windows(fakes):
    preview = runner.preview(_plan(), now_ms=NOW_MS)
    mf.validate_or_raise(preview)
    assert preview["incident_type"] == "CASCADING_FAILURE"
    assert [a["injector"] for a in preview["activations"]] == [
        "test-spike", "test-throttle", "test-lambda", "retry-amplification"]
    assert len({a["active_until"] for a in preview["activations"]}) == 1


# --- specs -----------------------------------------------------------------

def test_a_spec_becomes_the_arguments_of_build_plan(fakes):
    spec = {"baseline_s": 300, "cascade_s": 600, "recovery_s": 300,
            "stages": [{"injector": "test-spike", "intensity": 0.5,
                        "band": {"parameter": "multiplier", "low": 2, "high": 4}},
                       {"injector": "test-throttle", "intensity": 0.5, "onset_delay_s": 60,
                        "band": {"parameter": "load_ratio", "low": 1.5, "high": 2.5}}]}
    plan = cascade.build_plan(**cascade.load_spec(spec), seed=1, created_by="melisa")
    assert _offsets(plan) == [("test-spike", 300, 600), ("test-throttle", 360, 540)]


def test_a_spec_is_read_from_a_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"baseline_s": 1, "cascade_s": 2, "recovery_s": 3,
                                "stages": [{"injector": "x"}]}))
    assert cascade.load_spec(path)["stages"] == [Stage("x")]


@pytest.mark.parametrize("spec", [
    [],
    {"baseline_s": 1, "cascade_s": 2, "stages": []},                                # key missing
    {"baseline_s": 1, "cascade_s": 2, "recovery_s": 3, "stages": [], "extra": 1},   # key unknown
    {"baseline_s": 1, "cascade_s": 2, "recovery_s": 3, "stages": [{}]},
    {"baseline_s": 1, "cascade_s": 2, "recovery_s": 3,
     "stages": [{"injector": "x", "onset_delay": 5}]},                               # misspelled
])
def test_load_spec_rejects_what_it_would_have_to_guess_at(spec):
    with pytest.raises(ValueError):
        cascade.load_spec(spec)


# --- the concurrency timeline ----------------------------------------------

def _manifest(*windows):
    # (injector, from, until) in seconds, as a manifest's activations.
    base = NOW_MS
    return {"activations": [
        {"fault_id": f"exp#{i}", "injector": name,
         "active_from": mf.to_iso(base + start * 1000), "active_until": mf.to_iso(base + end * 1000)}
        for i, (name, start, end) in enumerate(windows)]}


def _secs(iso):
    return (mf.to_epoch_ms(iso) - NOW_MS) // 1000


def test_the_timeline_separates_single_fault_windows_from_multi_fault_ones():
    timeline = cascade.concurrency_timeline(
        _manifest(("spike", 300, 900), ("throttle", 360, 900), ("lambda", 420, 900)))
    assert [(_secs(s["from"]), _secs(s["until"]), s["count"]) for s in timeline] == [
        (300, 360, 1), (360, 420, 2), (420, 900, 3)]
    assert timeline[0]["injectors"] == ["spike"]
    assert timeline[2]["injectors"] == ["spike", "throttle", "lambda"]


def test_multi_fault_windows_start_when_the_second_fault_does():
    windows = cascade.multi_fault_windows(
        _manifest(("spike", 300, 900), ("throttle", 360, 900), ("lambda", 420, 900)))
    assert [(_secs(a), _secs(b)) for a, b in windows] == [(360, 900)]


def test_a_gap_between_two_multi_fault_stretches_makes_two_windows():
    windows = cascade.multi_fault_windows(
        _manifest(("a", 0, 100), ("b", 50, 100), ("c", 200, 300), ("d", 250, 300)))
    assert [(_secs(a), _secs(b)) for a, b in windows] == [(50, 100), (250, 300)]


def test_one_fault_alone_never_makes_a_multi_fault_window():
    assert cascade.multi_fault_windows(_manifest(("spike", 0, 100))) == []
    assert cascade.multi_fault_windows({"activations": []}) == []


def test_excluded_injectors_are_not_counted():
    manifest = _manifest(("spike", 0, 100), ("retry-amplification", 50, 100))
    assert len(cascade.multi_fault_windows(manifest)) == 1
    assert cascade.multi_fault_windows(manifest, exclude_injectors=("retry-amplification",)) == []


def test_the_planned_cascade_has_a_single_fault_stretch_first(fakes):
    preview = runner.preview(_plan(), now_ms=NOW_MS)
    timeline = cascade.concurrency_timeline(preview)
    assert [s["count"] for s in timeline] == [1, 2, 3, 4]
    assert [_secs(a) for a, _ in cascade.multi_fault_windows(preview)] == [360]


def test_the_timeline_reads_as_text(fakes):
    text = cascade.describe_timeline(runner.preview(_plan(), now_ms=NOW_MS))
    assert "1 active: test-spike\n" in text + "\n"
    assert "<- multi-fault" in text
    assert cascade.describe_timeline({"activations": []}) == "no faults were active"


# --- retry amplification ---------------------------------------------------

class RetryWorkload:
    def __init__(self):
        self.policies = []

    def set_retry_policy(self, retries_per_failure):
        self.policies.append(retries_per_failure)

    def retry_stats(self):
        return {"retries_sent": 12}


def _retry(workload):
    cls = injectors.get("retry-amplification")
    activation = cls.plan("exp-x", 0, active_from_ms=0, active_until_ms=1000,
                          rng=random.Random(7), intensity=0.5, band=RETRY_BAND)
    ctx = Context(experiment_id="exp-x", session=None, control_plane=None, workload=workload)
    return cls(ctx, activation)


def test_retry_amplification_resolves_a_rate_inside_its_band():
    assert _retry(RetryWorkload()).activation.params == {"retries_per_failure": 1.5}


def test_retry_amplification_is_reproducible():
    assert _retry(RetryWorkload()).activation.params == _retry(RetryWorkload()).activation.params


def test_a_negative_retry_rate_is_refused():
    cls = injectors.get("retry-amplification")
    with pytest.raises(ValueError, match="cannot be negative"):
        cls.plan("exp-x", 0, active_from_ms=0, active_until_ms=1000, rng=random.Random(7),
                 intensity=0, band={"parameter": "retries_per_failure", "low": -1, "high": 1})


def test_start_turns_retries_on_and_stop_turns_them_off():
    workload = RetryWorkload()
    injector = _retry(workload)
    injector.start()
    injector.stop()
    injector.stop()
    assert workload.policies == [1.5, 0, 0]


def test_stop_is_safe_before_start_and_with_no_workload():
    workload = RetryWorkload()
    _retry(workload).stop()
    assert workload.policies == [0]
    _retry(None).stop()
    _retry(runner.NoWorkload()).stop()


def test_start_fails_loudly_when_the_workload_cannot_retry():
    with pytest.raises(RuntimeError, match="cannot retry"):
        _retry(runner.NoWorkload()).start()
    with pytest.raises(RuntimeError, match="cannot retry"):
        _retry(None).start()


def test_what_the_retries_did_is_observed():
    assert _retry(RetryWorkload()).observe() == {"retries_sent": 12}
    assert _retry(runner.NoWorkload()).observe() == {}


# --- running a cascade (against the mocked stack) --------------------------

class FakeClock:
    def __init__(self):
        self.now_ms = int(time.time() * 1000)

    def clock(self):
        return self.now_ms

    def sleep(self, seconds):
        self.now_ms += round(seconds * 1000)


class CascadeWorkload(RetryWorkload):
    def describe(self):
        return {"request_rate_rps": 20}

    def start(self):
        pass

    def stop(self):
        pass


def test_a_cascade_runs_start_to_finish_and_records_its_overlaps(fakes, stack):
    clock, workload = FakeClock(), CascadeWorkload()
    final = runner.run(_plan(), stack, workload=workload, clock=clock.clock, sleep=clock.sleep,
                       log=lambda line: None)

    assert final["status"] == "COMPLETED"
    assert final["incident_type"] == "CASCADING_FAILURE"
    assert [a["injector"] for a in final["activations"]] == [
        "test-spike", "test-throttle", "test-lambda", "retry-amplification"]
    # Retries were on during the run, and are off again after it.
    assert workload.policies[0] == 1.5 and workload.policies[-1] == 0
    assert final["activations"][3]["observed"] == {"retries_sent": 12}
    # Single-fault stretch first, then multi-fault.
    assert [s["count"] for s in cascade.concurrency_timeline(final)] == [1, 2, 3, 4]


def test_a_failed_cascade_still_switches_the_retries_off(fakes, stack):
    clock, workload = FakeClock(), CascadeWorkload()

    def boom(seconds):
        clock.sleep(seconds)
        if clock.now_ms > start_ms + 450_000:       # during the third stage
            raise RuntimeError("boom")

    start_ms = clock.now_ms
    with pytest.raises(RuntimeError):
        runner.run(_plan(), stack, workload=workload, clock=clock.clock, sleep=boom,
                   log=lambda line: None)
    assert workload.policies[-1] == 0


# --- the command line ------------------------------------------------------

def _spec_file(tmp_path, **overrides):
    spec = {"baseline_s": 300, "cascade_s": 600, "recovery_s": 300,
            "stages": [{"injector": "test-spike", "intensity": 0.5,
                        "band": {"parameter": "multiplier", "low": 2, "high": 4}},
                       {"injector": "test-throttle", "intensity": 0.5, "onset_delay_s": 60,
                        "band": {"parameter": "load_ratio", "low": 1.5, "high": 2.5}}]}
    spec.update(overrides)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return str(path)


@pytest.fixture
def no_aws(monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("a dry run must not touch AWS")

    monkeypatch.setattr(cli.boto3, "Session", refuse)


def test_a_dry_run_prints_the_manifest_and_the_concurrency(fakes, no_aws, tmp_path, capsys):
    assert cli.main(["start-cascade", "--spec", _spec_file(tmp_path), "--seed", "7",
                     "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"incident_type": "CASCADING_FAILURE"' in out
    assert "planned concurrency:" in out and "<- multi-fault" in out


def test_a_dry_run_with_the_same_seed_gives_the_same_params(fakes, no_aws, tmp_path, capsys):
    argv = ["start-cascade", "--spec", _spec_file(tmp_path), "--seed", "7", "--dry-run"]

    def params():
        cli.main(argv)
        text = capsys.readouterr().out.split("\nplanned concurrency")[0]
        return [a["params"] for a in json.loads(text)["activations"]]

    assert params() == params()


@pytest.mark.parametrize("make, message", [
    (lambda tmp: ["--spec", str(tmp / "missing.json")], "missing.json"),
    (lambda tmp: ["--spec", _spec_file(tmp, cascade_s=10)], "would begin"),
    (lambda tmp: ["--spec", _spec_file(tmp), "--max-run", "100"], "past the limit"),
])
def test_a_bad_cascade_is_refused_before_aws(fakes, no_aws, tmp_path, capsys, make, message):
    assert cli.main(["start-cascade", *make(tmp_path), "--dry-run"]) == 1
    assert message in capsys.readouterr().err
