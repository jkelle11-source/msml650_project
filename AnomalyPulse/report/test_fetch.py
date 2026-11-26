"""Tests for report/fetch.py, with a fake S3 client: no AWS."""
import io
import json

from report import fetch

RUNS = [
    {"experiment_id": "exp-b", "slot": 702, "phase": "test", "incident_type": "DATABASE_THROTTLING"},
    {"experiment_id": "exp-a", "slot": 701, "phase": "test", "incident_type": "NORMAL"},
    {"experiment_id": "exp-t", "slot": 12, "phase": "train", "incident_type": "TRAFFIC_SPIKE"},
    {"experiment_id": "exp-s", "slot": 1051, "phase": "stress", "incident_type": "TRAFFIC_SPIKE"},
]
FAULT = {"fault_id": "exp-b#0", "injector": "db-throttling", "target": "products", "intensity": 0.8,
         "band": [2.5, 3.5], "params": {}, "observed": None,
         "active_from": "2026-11-13T22:45:43.000Z", "active_until": "2026-11-13T22:50:43.000Z"}


class FakeS3:
    def __init__(self, objects):
        self.objects, self.keys = objects, []

    def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        return {"Body": io.BytesIO(self.objects[Key].encode())}


def objects():
    registry = "".join(json.dumps(r) + "\n" for r in RUNS)
    out = {fetch.REGISTRY: registry}
    for r in RUNS:
        activations = [FAULT] if r["experiment_id"] == "exp-b" else []
        out[fetch.manifest_key(r["experiment_id"])] = json.dumps(
            {"experiment_id": r["experiment_id"], "started_at": "2026-11-13T22:42:28.000Z", "activations": activations})
    return out


def test_the_manifest_key_is_the_simulator_s():
    import pytest
    manifest = pytest.importorskip("simulator.manifest")
    assert fetch.manifest_key("exp-1") == manifest.manifest_key("exp-1")


def test_only_test_and_stress_runs_are_read():
    s3 = FakeS3(objects())
    runs = fetch.registry(s3, "bucket")
    assert [r["experiment_id"] for r in runs] == ["exp-b", "exp-a", "exp-s"]


def test_manifests_keep_the_fault_times_and_sort_by_slot():
    s3 = FakeS3(objects())
    items = fetch.manifests(s3, "bucket", fetch.registry(s3, "bucket"))
    assert [m["slot"] for m in items] == [701, 702, 1051]
    throttling = items[1]
    assert throttling["labeled_type"] == "DATABASE_THROTTLING" and throttling["phase"] == "test"
    assert throttling["faults"] == [{k: FAULT[k] for k in fetch.FAULT_KEYS}]
    assert items[0]["faults"] == []
    assert ("bucket", "experiments/exp-b/manifest.json") in s3.keys
