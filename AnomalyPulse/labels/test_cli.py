"""The labeling CLI over a local copy of the dataset layout (no AWS)."""
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from labels.__main__ import LocalStore, label_dataset
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
        assert len(log) == 2 and len(failed) == 1
        files = sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("labels*.jsonl"))
        assert all("/tier2/labels/" in f for f in files)
        assert not (Path(root) / "datasets/d/tier1").exists()
        rows = [json.loads(x) for x in (base / "labels/experiment_id=exp-20261016T223852Z-0002/labels.jsonl")
                .read_text().splitlines()]
        assert {r["label"] for r in rows if r["included"]} == {"NORMAL", "TRAFFIC_SPIKE", "CASCADING_FAILURE"}
        log_lines = (base / "labels/labeling_log.jsonl").read_text().splitlines()
        assert json.loads(log_lines[-1])["not_labeled"][0]["error"].count("label_from")