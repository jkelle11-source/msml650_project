import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator import cli, injectors
from simulator import manifest as mf
from simulator import runner
from simulator.injectors import Context
from simulator.injectors.traffic_spike import TrafficSpike

EXPERIMENT_ID = "exp-20261012T041500Z-3f9a"
T0 = 1_791_778_500_000
START, END = T0 + 300_000, T0 + 600_000
BAND = {"parameter": "multiplier", "low": 2, "high": 4}


class FakeWorkload:
    def __init__(self):
        self.multipliers = []
        self.sent = 0

    def describe(self):
        return {"request_rate_rps": 20}

    def start(self):
        pass

    def stop(self):
        pass

    def set_rate_multiplier(self, m):
        self.multipliers.append(m)

    def requests_sent(self):
        return self.sent


def _activation(intensity=0.5, band=BAND, seed=7):
    return TrafficSpike.plan(EXPERIMENT_ID, 0, active_from_ms=START, active_until_ms=END,
                             rng=random.Random(seed), intensity=intensity, band=band)


def _spike(workload):
    ctx = Context(experiment_id=EXPERIMENT_ID, session=None, control_plane=None, workload=workload)
    return TrafficSpike(ctx, _activation())


# --- registration and configure --------------------------------------------------------------

def test_it_is_registered_as_a_traffic_spike():
    cls = injectors.get("traffic-spike")
    assert cls is TrafficSpike
    assert cls.incident_type == "TRAFFIC_SPIKE"
    assert cls.parameter == "multiplier"
    assert cls.targets == ()


@pytest.mark.parametrize("intensity, band, multiplier", [
    (0, BAND, 2), (1, BAND, 4), (0.5, BAND, 3),
    (0.25, {"parameter": "multiplier", "low": 6, "high": 8}, 6.5),
    (0.8, {"parameter": "multiplier", "low": 8, "high": 10}, 9.6),
])
def test_multiplier_comes_from_the_band(intensity, band, multiplier):
    assert _activation(intensity, band).params == {"multiplier": multiplier}


def test_same_seed_same_params():
    assert _activation(seed=1).params == _activation(seed=1).params


def test_a_multiplier_of_one_or_less_is_refused():
    with pytest.raises(ValueError, match="above 1"):
        _activation(0, {"parameter": "multiplier", "low": 1, "high": 2})
    with pytest.raises(ValueError, match="above 1"):
        _activation(1, {"parameter": "multiplier", "low": 0.2, "high": 0.5})


def test_activation_has_no_target_and_fits_the_manifest():
    activation = _activation()
    entry = activation.manifest_entry()
    assert entry["target"] is None
    manifest = mf.build_manifest(
        experiment_id=EXPERIMENT_ID, incident_type="TRAFFIC_SPIKE", seed=7, created_by="josh",
        started_at_ms=T0, workload={"request_rate_rps": 20}, activations=[entry], bucket="b")
    assert mf.validate(manifest) == (True, [])


# --- start / stop / observe ------------------------------------------------------------------

def test_start_multiplies_and_stop_restores():
    load = FakeWorkload()
    spike = _spike(load)
    assert spike.start() is None
    load.sent = 900
    assert spike.stop() is None
    assert load.multipliers == [3, 1]


def test_stop_is_safe_before_start_and_twice():
    load = FakeWorkload()
    spike = _spike(load)
    spike.stop()
    spike.stop()
    assert load.multipliers == [1, 1]
    assert spike.observe() == {}


def test_stop_twice_keeps_the_first_end():
    load = FakeWorkload()
    spike = _spike(load)
    spike.start()
    load.sent = 600
    spike.stop()
    load.sent = 5000   # the second stop must not stretch the window it measured
    spike.stop()
    assert spike.observe()["requests_sent"] == 600


def test_observe_reports_what_was_sent(monkeypatch):
    clock = iter([100.0, 110.0])
    monkeypatch.setattr("simulator.injectors.traffic_spike.time.monotonic", lambda: next(clock))
    load = FakeWorkload()
    spike = _spike(load)
    spike.start()
    load.sent = 600
    spike.stop()
    observed = spike.observe()
    assert observed == {"requests_sent": 600, "achieved_rps": 60.0}
    assert json.loads(json.dumps(observed)) == observed


def test_start_without_a_workload_explains():
    spike = _spike(runner.NoWorkload())
    with pytest.raises(RuntimeError, match="needs a workload"):
        spike.start()
    spike.stop()   # still safe


# --- through the runner (mocked stack) -------------------------------------------------------

def test_a_run_spikes_inside_its_window_and_records_it(stack):
    load = FakeWorkload()
    plan = runner.Plan.single("traffic-spike", duration_s=300, baseline_s=300, recovery_s=300,
                              seed=7, created_by="josh", intensity=0.5, band=BAND)
    now = [T0]

    def sleep(seconds):
        now[0] += round(seconds * 1000)

    final = runner.run(plan, stack, workload=load, clock=lambda: now[0], sleep=sleep,
                       log=lambda *a: None)
    assert final["status"] == "COMPLETED"
    assert final["incident_type"] == "TRAFFIC_SPIKE"
    assert final["workload"] == {"request_rate_rps": 20}
    [activation] = final["activations"]
    assert activation["params"] == {"multiplier": 3}
    assert activation["target"] is None
    # Spiked once at the window, restored once at its end.
    assert load.multipliers == [3, 1]


# --- the CLI ---------------------------------------------------------------------------------

@pytest.fixture
def no_aws(monkeypatch):
    def refuse(**kwargs):
        raise AssertionError("this command must not touch AWS")

    monkeypatch.setattr(cli.boto3, "Session", refuse)


def test_dry_run_records_the_workload_and_multiplier(no_aws, capsys):
    assert cli.main(["start-incident", "--type", "traffic-spike", "--intensity", "0.5",
                     "--band", "2", "4", "--duration", "300", "--rps", "20", "--seed", "7",
                     "--dry-run"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["incident_type"] == "TRAFFIC_SPIKE"
    assert manifest["activations"][0]["params"] == {"multiplier": 3}
    assert manifest["workload"]["request_rate_rps"] == 20
    assert manifest["workload"]["mix"]["GET /products"] == 0.6


def test_dry_run_takes_a_custom_mix_and_users(no_aws, capsys):
    cli.main(["start-incident", "--type", "noop", "--duration", "60", "--rps", "10",
              "--users", "5", "--mix", "50", "20", "10", "10", "10", "--dry-run"])
    workload = json.loads(capsys.readouterr().out)["workload"]
    assert workload["users"] == 5
    assert workload["mix"]["GET /products"] == 0.5


def test_spike_without_rps_is_refused(no_aws, capsys):
    assert cli.main(["start-incident", "--type", "traffic-spike", "--intensity", "0.5",
                     "--band", "2", "4", "--duration", "300", "--dry-run"]) == 1
    assert "needs --rps" in capsys.readouterr().err


def test_workload_options_without_rps_are_refused(no_aws, capsys):
    assert cli.main(["start-incident", "--type", "noop", "--duration", "60", "--users", "5",
                     "--dry-run"]) == 1
    assert "need --rps" in capsys.readouterr().err


def test_bad_rps_is_refused(no_aws, capsys):
    assert cli.main(["start-incident", "--type", "noop", "--duration", "60", "--rps", "0",
                     "--dry-run"]) == 1
    assert "rps must be a positive number" in capsys.readouterr().err


def test_noop_without_rps_is_unchanged(no_aws, capsys):
    cli.main(["start-incident", "--type", "noop", "--duration", "60", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["workload"] == {}
