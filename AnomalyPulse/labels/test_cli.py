"""The labeling CLI over a local copy of the dataset layout (no AWS)."""
import io
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from labels.__main__ import LocalStore, _check_s3, label_dataset, main
from labels.test_policy import activation, run, slot11


def _dataset(root, runs):
    base = Path(root) / "datasets/d/tier2"
    (base).mkdir(parents=True)
    lines = []
    for slot, m in runs:
        m = {**m, "experiment_id": f"exp-20261016T22385{slot}Z-000{slot}"}
        d = base / f"manifests/experiment_id={m['experiment_id']}"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(m))
        lines.append(json.dumps({"slot": slot, "experiment_id": m["experiment_id"]}))
    (base / "experiments.jsonl").write_text("\n".join(lines) + "\n")
    return base


def test_writes_labels_and_a_log_under_tier2_only():
    with tempfile.TemporaryDirectory() as root:
        bad = activation(0, "db-throttling", 180, 480, observed={"label_from": None})
        base = _dataset(root, [(1, run("NORMAL", [])), (2, slot11()),
                               (3, run("DATABASE_THROTTLING", [bad]))])
        log, failed = label_dataset("d", LocalStore(root), out=io.StringIO())
        assert len(log) == 3 and len(failed) == 1
        files = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("labels*.jsonl"))
        assert all("/tier2/labels/" in f for f in files)
        assert not (Path(root) / "datasets/d/tier1").exists()
        rows = [json.loads(x) for x in (base / "labels/experiment_id=exp-20261016T223852Z-0002/labels.jsonl")
                .read_text().splitlines()]
        assert {r["label"] for r in rows if r["included"]} == {"NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"}
        log_lines = [json.loads(x) for x in (base / "labels/labeling_log.jsonl").read_text().splitlines()]
        # One line per experiment, all of one shape, so the log loads as a table.
        assert [x["slot"] for x in log_lines] == [1, 2, 3]
        assert len({tuple(sorted(x)) for x in log_lines}) == 1
        assert log_lines[0]["error"] is None and log_lines[1]["error"] is None
        assert "label_from" in log_lines[2]["error"] and log_lines[2]["windows"] == 0


def test_only_v1_labels_go_to_s3():
    _check_s3("datasets/v1/tier2/labels/experiment_id=x/labels.jsonl")
    with pytest.raises(ValueError, match="never stored"):
        _check_s3("datasets/m4-1-validation/tier2/labels/experiment_id=x/labels.jsonl")
    with pytest.raises(ValueError, match="tier2/labels"):
        _check_s3("datasets/v1/tier1/labels.jsonl")
    # Refused before any S3 client is made.
    with pytest.raises(SystemExit):
        main(["--dataset", "m4-1-validation", "--bucket", "some-bucket"])