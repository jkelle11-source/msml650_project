import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from botocore.exceptions import ProfileNotFound

from simulator import cli
from simulator import injectors
from simulator import manifest as mf
from simulator.injectors import Injector

# Created by conftest's `aws` fixture.
BUCKET = "anomalypulse-telemetry-000000000000-us-east-2"

# A no-op run short enough to do in real time: the CLI runs on the real clock.
QUICK_NOOP = ["start-incident", "--type", "noop", "--baseline", "0", "--duration", "0.05",
              "--recovery", "0"]


# --- helpers ---------------------------------------------------------------

def _manifest_in_bucket(aws):
    s3 = aws.client("s3")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix="experiments/").get("Contents", [])]
    assert len(keys) == 1
    return json.loads(s3.get_object(Bucket=BUCKET, Key=keys[0])["Body"].read())


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def no_aws(monkeypatch):
    # Any attempt to open an AWS session fails the test.
    def refuse(**kwargs):
        raise AssertionError("this command must not touch AWS")

    monkeypatch.setattr(cli.boto3, "Session", refuse)


@pytest.fixture
def asked():
    # The profile and region the CLI asked for when it opened its session.
    return {}


@pytest.fixture
def mocked(aws, stack, monkeypatch, asked):
    # The CLI opens its own session from --profile. Hand it the mocked one.
    def session(**kwargs):
        asked.update(kwargs)
        return aws

    monkeypatch.setattr(cli.boto3, "Session", session)
    return stack


@pytest.fixture
def test_fault(registry):
    @injectors.register
    class TestFault(Injector):
        name = "test-fault"
        incident_type = "LAMBDA_DEGRADATION"
        parameter = "added_latency_ms"
        targets = ("product-service", "order-service")

        @classmethod
        def configure(cls, intensity, band, rng):
            return {"added_latency_ms": cls.native_value(intensity, band), "jitter": rng.random()}

        def start(self):
            return None

        def stop(self):
            return None

    return TestFault


# --- start-incident --dry-run ---------------------------------------------------

def test_dry_run_prints_the_manifest_and_touches_no_aws(no_aws, capsys):
    assert cli.main(["start-incident", "--type", "noop", "--duration", "60", "--dry-run"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert mf.validate(manifest) == (True, [])
    assert manifest["status"] == "RUNNING"
    assert manifest["incident_type"] == "NORMAL"


def test_dry_run_resolves_an_injectors_params(no_aws, capsys, test_fault):
    # How an injector owner checks configure() without spending a real run.
    assert cli.main(["start-incident", "--type", "test-fault", "--intensity", "0.5",
                     "--band", "200", "500", "--duration", "300", "--target", "order-service",
                     "--seed", "7", "--dry-run"]) == 0
    output = capsys.readouterr().out
    manifest = json.loads(output)
    [activation] = manifest["activations"]
    assert manifest["incident_type"] == "LAMBDA_DEGRADATION"
    assert manifest["seed"] == 7
    assert activation["target"] == "order-service"
    assert activation["intensity"] == 0.5
    # The band's parameter comes from the injector.
    assert activation["band"] == {"parameter": "added_latency_ms", "low": 200, "high": 500}
    assert activation["params"]["added_latency_ms"] == 350
    # Whole numbers typed on the command line are recorded as whole numbers.
    assert '"low": 200,' in output
    assert '"high": 500' in output and '"high": 500.0' not in output


def test_same_seed_gives_the_same_params_through_the_cli(no_aws, capsys, test_fault):
    command = ["start-incident", "--type", "test-fault", "--intensity", "0.5", "--band", "200", "500",
               "--duration", "300", "--target", "order-service", "--seed", "7", "--dry-run"]
    cli.main(command)
    first = json.loads(capsys.readouterr().out)
    cli.main(command)
    again = json.loads(capsys.readouterr().out)
    assert first["activations"][0]["params"] == again["activations"][0]["params"]


def test_seed_is_chosen_and_recorded_when_none_is_given(no_aws, capsys):
    # Without --seed the run is still repeatable, because the seed it picked is
    # in the manifest.
    command = ["start-incident", "--type", "noop", "--duration", "60", "--dry-run"]
    cli.main(command)
    first = json.loads(capsys.readouterr().out)["seed"]
    cli.main(command)
    second = json.loads(capsys.readouterr().out)["seed"]
    assert 0 <= first < 2**32
    assert first != second


def test_default_timeline_is_five_minutes_each_side_of_the_fault(no_aws, capsys, test_fault):
    cli.main(["start-incident", "--type", "test-fault", "--intensity", "0.5", "--band", "200", "500",
              "--duration", "300", "--target", "order-service", "--dry-run"])
    manifest = json.loads(capsys.readouterr().out)
    started = mf.to_epoch_ms(manifest["started_at"])
    [activation] = manifest["activations"]
    assert mf.to_epoch_ms(activation["active_from"]) - started == 300_000
    assert mf.to_epoch_ms(activation["active_until"]) - started == 600_000


def test_created_by_defaults_to_the_current_user(no_aws, capsys, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "melisa")
    cli.main(["start-incident", "--type", "noop", "--duration", "60", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["created_by"] == "melisa"
    cli.main(["start-incident", "--type", "noop", "--duration", "60", "--dry-run",
              "--created-by", "josh"])
    assert json.loads(capsys.readouterr().out)["created_by"] == "josh"


# --- start-incident: requests that are refused -------------------------------------

@pytest.mark.parametrize("arguments, problem", [
    (["--type", "traffic-spke", "--duration", "60"], "no injector named 'traffic-spke'; registered: "),
    (["--type", "test-fault", "--duration", "60"],
     "test-fault needs --intensity and --band LOW HIGH \\(the band is in added_latency_ms\\)"),
    (["--type", "test-fault", "--duration", "60", "--intensity", "0.5"], "needs --intensity and --band"),
    (["--type", "test-fault", "--duration", "60", "--intensity", "1.5", "--band", "200", "500",
      "--target", "order-service"], "intensity must be a number from 0 to 1"),
    (["--type", "test-fault", "--duration", "60", "--intensity", "0.5", "--band", "500", "200",
      "--target", "order-service"], "band low is above band high"),
    (["--type", "test-fault", "--duration", "60", "--intensity", "0.5", "--band", "200", "500"],
     "choose a target from product-service, order-service"),
    (["--type", "noop", "--duration", "60", "--intensity", "0.5"], "takes no --intensity or --band"),
    (["--type", "noop", "--duration", "0"], "window must end after it starts"),
    (["--type", "noop", "--duration", "60", "--baseline", "-5"], "cannot open before the run starts"),
    (["--type", "noop", "--duration", "60", "--seed", "-1"], "seed"),
])
def test_bad_request_is_refused_with_a_message_and_no_aws(no_aws, capsys, test_fault, arguments, problem):
    # Exit code 1, the reason on stderr, and nothing touched: every one of
    # these is caught before a session is opened.
    assert cli.main(["start-incident", *arguments]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert re.search(problem, captured.err)


def test_missing_required_option_is_an_argparse_error(no_aws, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["start-incident", "--type", "noop"])
    assert raised.value.code == 2
    assert "--duration" in capsys.readouterr().err


def test_duration_must_be_a_number(no_aws, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["start-incident", "--type", "noop", "--duration", "five"])
    assert raised.value.code == 2
    assert "invalid number value: 'five'" in capsys.readouterr().err


# --- start-incident: a real (mocked) run ---------------------------------------------

def test_noop_runs_end_to_end_through_the_cli(mocked, aws, capsys):
    # M3-1's first Definition-of-Done item against the mocked stack: through the
    # CLI, a valid manifest lands at experiments/<experiment_id>/manifest.json.
    assert cli.main(QUICK_NOOP) == 0
    manifest = _manifest_in_bucket(aws)
    assert mf.validate(manifest) == (True, [])
    assert manifest["status"] == "COMPLETED"
    assert manifest["incident_type"] == "NORMAL"
    assert mocked.lock.holder() is None
    output = capsys.readouterr().out
    assert manifest["experiment_id"] in output
    assert f"s3://{BUCKET}/experiments/{manifest['experiment_id']}/manifest.json" in output
    assert output.strip().endswith("COMPLETED")


def test_cli_uses_the_sim_profile_by_default(mocked, asked):
    # The simulator role limits what a buggy injector can do, so it is the
    # default rather than whatever profile happens to be active.
    cli.main(QUICK_NOOP)
    assert asked == {"profile_name": "sim", "region_name": "us-east-2"}


def test_cli_passes_the_profile_and_region_it_is_given(mocked, asked):
    cli.main([*QUICK_NOOP, "--profile", "dev", "--region", "us-west-2"])
    assert asked == {"profile_name": "dev", "region_name": "us-west-2"}


def test_run_is_refused_while_another_holds_the_lock(mocked, aws, capsys):
    mocked.lock.acquire("exp-20261012T041500Z-0b1c", "linu", int(time.time()) + 600)
    assert cli.main(QUICK_NOOP) == 1
    error = capsys.readouterr().err
    assert "error: experiment exp-20261012T041500Z-0b1c is already running, started by linu" in error
    assert aws.client("s3").list_objects_v2(Bucket=BUCKET).get("Contents") is None


def test_unconfigured_profile_is_explained(monkeypatch, capsys):
    def no_such_profile(**kwargs):
        raise ProfileNotFound(profile=kwargs["profile_name"])

    monkeypatch.setattr(cli.boto3, "Session", no_such_profile)
    assert cli.main(QUICK_NOOP) == 1
    error = capsys.readouterr().err
    assert "could not reach the stack with AWS profile 'sim'" in error
    assert "could not be found" in error


def test_stack_that_is_not_deployed_is_explained(mocked, aws, capsys):
    aws.client("ssm").delete_parameter(Name="/anomalypulse/tables/fault-config/name")
    assert cli.main(QUICK_NOOP) == 1
    error = capsys.readouterr().err
    assert "SSM parameters not found: /anomalypulse/tables/fault-config/name" in error


def test_ctrl_c_exits_with_130(mocked, monkeypatch, capsys):
    def interrupted(plan, stack):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.runner, "run", interrupted)
    assert cli.main(QUICK_NOOP) == 130
    assert "interrupted" in capsys.readouterr().err


def test_failure_during_a_run_is_not_swallowed(mocked, monkeypatch):
    # A bug in an injector must show its traceback, not a one-line "error:".
    def broken(plan, stack):
        raise KeyError("multiplier")

    monkeypatch.setattr(cli.runner, "run", broken)
    with pytest.raises(KeyError):
        cli.main(QUICK_NOOP)


# --- cleanup --------------------------------------------------------------------------

def _schedule_a_fault(stack, experiment_id="exp-20261012T041500Z-0b1c"):
    now = int(time.time() * 1000)
    stack.control_plane.schedule(
        service="payment-service", fault_id=f"{experiment_id}#0", injector="payment-failure",
        params={"timeout_ms": 3300}, active_from_ms=now + 60_000, active_until_ms=now + 120_000)


def test_cleanup_with_nothing_to_clean(mocked, capsys):
    assert cli.main(["cleanup"]) == 0
    assert capsys.readouterr().out == "deleted 0 fault(s)\nno lock was held\n"


def test_cleanup_after_a_dead_run_deletes_its_faults_and_lock(mocked, capsys):
    # The lock's expiry has passed, so the run that held it is dead.
    mocked.lock.acquire("exp-20261012T041500Z-0b1c", "linu", int(time.time()) - 10,
                        now_s=int(time.time()) - 1000)
    _schedule_a_fault(mocked)
    assert cli.main(["cleanup"]) == 0
    assert capsys.readouterr().out == ("deleted 1 fault(s)\n"
                                       "released the lock of exp-20261012T041500Z-0b1c\n")
    assert mocked.lock.holder() is None
    assert mocked.control_plane.cancel_all() == 0


def test_cleanup_refuses_while_a_run_may_still_be_going(mocked, capsys):
    # Deleting a live run's faults and lock would silently ruin its labels.
    mocked.lock.acquire("exp-20261012T041500Z-0b1c", "linu", int(time.time()) + 600)
    _schedule_a_fault(mocked)
    assert cli.main(["cleanup"]) == 1
    error = capsys.readouterr().err
    assert "already running, started by linu" in error
    assert "--force" in error
    assert mocked.lock.holder()["experiment_id"] == "exp-20261012T041500Z-0b1c"
    assert mocked.control_plane.cancel_all() == 1  # the fault was still there


def test_cleanup_force_clears_a_live_lock(mocked, capsys):
    mocked.lock.acquire("exp-20261012T041500Z-0b1c", "linu", int(time.time()) + 600)
    _schedule_a_fault(mocked)
    assert cli.main(["cleanup", "--force"]) == 0
    assert mocked.lock.holder() is None
    assert mocked.control_plane.cancel_all() == 0


# --- python -m simulator ------------------------------------------------------------------

def test_package_runs_as_a_module():
    # The documented way to start it: `python -m simulator ...` from AnomalyPulse/.
    anomalypulse = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "simulator", "start-incident", "--type", "noop", "--duration", "60",
         "--dry-run"],
        cwd=anomalypulse, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["incident_type"] == "NORMAL"
