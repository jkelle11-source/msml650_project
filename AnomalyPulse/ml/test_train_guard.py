"""A training job refuses test and stress runs on its own (M9-1), whatever the launcher put in a
channel. Every job script loads its runs through train.load_runs. No AWS.

Run from AnomalyPulse/:  python -m pytest ml/test_train_guard.py -q
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
sys.path.insert(0, str(Path(__file__).parent / "job"))

import train  # noqa: E402
import train_incident_rf  # noqa: E402
from ml.test_train_incident_rf import make_root, write_run  # noqa: E402

TRAINING_RUN = "exp-20261024T112908Z-a9bf"      # a v1-val run
TEST_RUN = "exp-20261113T020000Z-1c2d"


def test_a_training_run_loads(tmp_path):
    write_run(tmp_path, "train", TRAINING_RUN)
    runs = train.load_runs(tmp_path / "input" / "data" / "train")
    assert [r[0] for r in runs] == [TRAINING_RUN]


@pytest.mark.parametrize("channel", ["train", "validation"])
def test_a_test_run_in_either_channel_is_refused(tmp_path, channel):
    write_run(tmp_path, channel, TRAINING_RUN)
    write_run(tmp_path, channel, TEST_RUN)
    with pytest.raises(train.TrainError, match=f"run {TEST_RUN} started after training's last day"):
        train.load_runs(tmp_path / "input" / "data" / channel)


def test_training_s_last_day_is_allowed_and_the_next_refused():
    train.check_not_test("exp-20261025T235959Z-0000")
    with pytest.raises(train.TrainError):
        train.check_not_test("exp-20261026T000000Z-0000")


@pytest.mark.parametrize("bad", ["exp-2026-11-13-1c2d", "exp-20261113T020000Z", "exp-20261113T020000Z-XYZW"])
def test_an_id_that_starts_like_a_run_but_isnt_one_is_refused(bad):
    with pytest.raises(train.TrainError, match="not a run id"):
        train.check_not_test(bad)


def test_the_guard_dates_agree_with_the_training_set():
    # v1-val, the last training queue, finished on 2026-10-25 (Docs/TRAINING_SET.md, "Status").
    status = (Path(__file__).resolve().parents[2] / "Docs" / "TRAINING_SET.md").read_text()
    assert "| v1-val | `batch-20261024T110907Z-1bf3` | 2026-10-24 11:09 (finished 10-25 10:44)" in status
    assert train.LAST_TRAINING_DAY == "20261025"


def test_a_job_script_stops_on_a_test_run(tmp_path):
    root = make_root(tmp_path)
    write_run(tmp_path, "train", TEST_RUN)
    with pytest.raises(train.TrainError, match=TEST_RUN):
        train_incident_rf.run(root)
