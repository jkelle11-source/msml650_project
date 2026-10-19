"""The features CLI over a local copy of the dataset layout (no AWS)."""
import io
import json
from pathlib import Path

import pytest

from features import build_matrix as bm
from features import guard
from features.__main__ import LocalStore, _check_s3, build_dataset, main
from features.request_features import SERVICES
from features.test_build_matrix import REC, SNAP

STARTED, ENDED = "2026-10-16T03:19:54.000Z", "2026-10-16T03:24:54.000Z"
W = ["2026-10-16T03:%02d:00.000Z" % m for m in range(19, 25)]   # 03:19 and 03:24 are partial


def _labels():
    reasons = {W[0]: "partial_window", W[1]: "startup", W[5]: "partial_window"}
    return [{"window_start": w, "label": None if w in reasons else "NORMAL", "included": w not in reasons,
             "excluded_reason": reasons.get(w)} for w in W]


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _dataset(root, eids=("exp-a", "exp-b"), labels=None, skip_snapshot=None):
    base = Path(root) / "datasets/d"
    _write(base / "tier2/experiments.jsonl", [{"experiment_id": e, "slot": i} for i, e in enumerate(eids)])
    for eid in eids:
        manifest = base / f"tier2/manifests/experiment_id={eid}/manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"experiment_id": eid, "started_at": STARTED, "ended_at": ENDED}))
        rows = (labels or {}).get(eid, _labels())
        if rows is not None:
            _write(base / f"tier2/labels/experiment_id={eid}/labels.jsonl", rows)
        _write(base / f"tier1/requests/experiment_id={eid}/requests.jsonl",
               [dict(REC, service=s, timestamp=f"2026-10-16T03:{m}:30.000Z") for m in range(20, 24) for s in SERVICES])
        _write(base / f"tier1/metrics/experiment_id={eid}/metrics.jsonl",
               [dict(SNAP, service=s, timestamp=f"2026-10-16T03:{m}:00.000Z") for m in range(21, 25) for s in SERVICES
                if (m, s) != skip_snapshot])
    return base


def _features(base, eid):
    return [json.loads(line) for line in (base / f"tier1/features/experiment_id={eid}/features.jsonl").open()]


def _log(base):
    return [json.loads(line) for line in (base / "tier1/features/feature_log.jsonl").open()]


def test_builds_every_run_and_logs_it(tmp_path):
    base = _dataset(tmp_path)
    assert main(["--dataset", "d", "--root", str(tmp_path)]) == 0
    for eid in ("exp-a", "exp-b"):
        rows = _features(base, eid)
        assert [r["window_start"] for r in rows] == W[1:5]       # complete windows only, startup included
        assert all(list(r) == ["window_start"] + bm.columns() for r in rows)
    assert [(line["experiment_id"], line["windows"], line["error"]) for line in _log(base)] == [
        ("exp-a", 4, None), ("exp-b", 4, None)]


def test_the_log_holds_no_tier2_field(tmp_path):
    base = _dataset(tmp_path)
    main(["--dataset", "d", "--root", str(tmp_path)])
    for line in _log(base):
        assert set(line) == {"experiment_id", "windows", "missing_snapshot", "error"}   # no slot, no label


def test_a_run_with_no_labels_is_logged_and_the_rest_are_built(tmp_path):
    base = _dataset(tmp_path, labels={"exp-a": None})
    out = io.StringIO()
    _, failed = build_dataset("d", LocalStore(tmp_path), out=out)
    assert [line["experiment_id"] for line in failed] == ["exp-a"]
    assert "python -m labels" in failed[0]["error"]
    assert not (base / "tier1/features/experiment_id=exp-a").exists()
    assert len(_features(base, "exp-b")) == 4
    assert main(["--dataset", "d", "--root", str(tmp_path)]) == 1


def test_a_run_whose_labels_miss_a_window_is_not_written(tmp_path):
    base = _dataset(tmp_path, labels={"exp-a": [r for r in _labels() if r["window_start"] != W[3]]})
    _, failed = build_dataset("d", LocalStore(tmp_path), out=io.StringIO())
    assert "orphans" in failed[0]["error"] and W[3] in failed[0]["error"]
    assert not (base / "tier1/features/experiment_id=exp-a").exists()


def test_a_minute_without_a_snapshot_is_logged_and_its_columns_are_empty(tmp_path):
    base = _dataset(tmp_path, skip_snapshot=(22, "product-service"))     # the snapshot for 03:21
    build_dataset("d", LocalStore(tmp_path), out=io.StringIO())
    assert _log(base)[0]["missing_snapshot"] == [W[2]]
    row = next(r for r in _features(base, "exp-a") if r["window_start"] == W[2])
    assert row["product_throttled_requests"] is None
    assert row["cart_throttled_requests"] == 0


def test_rerunning_rewrites(tmp_path):
    base = _dataset(tmp_path)
    main(["--dataset", "d", "--root", str(tmp_path)])
    first = _features(base, "exp-a")
    assert main(["--dataset", "d", "--root", str(tmp_path)]) == 0
    assert _features(base, "exp-a") == first


def test_label_values_never_reach_the_features(tmp_path):
    # Only which windows the labels include is read: relabeling a window changes no feature.
    base = _dataset(tmp_path)
    main(["--dataset", "d", "--root", str(tmp_path)])
    before = _features(base, "exp-a")
    relabeled = [dict(r, label="TRAFFIC_SPIKE") if r["included"] else r for r in _labels()]
    _dataset(tmp_path, labels={"exp-a": relabeled})
    main(["--dataset", "d", "--root", str(tmp_path)])
    assert _features(base, "exp-a") == before


def test_a_forbidden_column_stops_the_whole_build(tmp_path, monkeypatch):
    base = _dataset(tmp_path)
    real = bm.request_features

    def leaky(records):
        rows = real(records)
        for row in rows.values():
            row["product_db_throttled"] = True
        return rows

    monkeypatch.setattr(bm, "request_features", leaky)
    with pytest.raises(guard.BoundaryViolation):
        main(["--dataset", "d", "--root", str(tmp_path)])
    assert not (base / "tier1/features").exists()


def test_local_store_writes_only_under_tier1_features(tmp_path):
    with pytest.raises(ValueError):
        LocalStore(tmp_path).write("datasets/d/tier2/labels/experiment_id=e/labels.jsonl", "")


def test_only_v1_features_go_to_s3():
    _check_s3("datasets/v1/tier1/features/experiment_id=e/features.jsonl")
    with pytest.raises(ValueError):
        _check_s3("datasets/m4-1-validation/tier1/features/experiment_id=e/features.jsonl")
    with pytest.raises(ValueError):
        _check_s3("datasets/v1/tier2/labels/experiment_id=e/labels.jsonl")
    with pytest.raises(SystemExit):
        main(["--dataset", "m4-1-validation", "--bucket", "b"])
