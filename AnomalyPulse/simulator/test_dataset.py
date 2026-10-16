import pytest

from simulator import dataset

ID = "exp-20261016T120000Z-ab12"


def test_tier1_and_tier2_are_separate_trees():
    tier1 = [dataset.requests_key("v1", ID), dataset.metrics_key("v1", ID)]
    tier2 = [dataset.manifest_key("v1", ID), dataset.index_key("v1"), dataset.slot_key("v1", 7),
             dataset.batch_key("v1", "batch-x"), dataset.pins_key("v1")]
    assert all(key.startswith("datasets/v1/tier1/") for key in tier1)
    assert all(key.startswith("datasets/v1/tier2/") for key in tier2)
    assert dataset.requests_key("v1", ID) == f"datasets/v1/tier1/requests/experiment_id={ID}/requests.jsonl"
    assert dataset.slot_key("v1", 7) == "datasets/v1/tier2/registry/slots/0007.json"


@pytest.mark.parametrize("name", ["", "V1", "v1/../x", "-v1", "v 1"])
def test_bad_dataset_names_are_refused(name):
    with pytest.raises(ValueError, match="dataset name"):
        dataset.root(name)


@pytest.mark.parametrize("slot, phases", [
    (1, ("train",)), (560, ("train",)), (561, ("val",)), (700, ("val",)),
    (701, ("test", "stress")), (1050, ("test", "stress")),
])
def test_phases_follow_the_plans_numbering(slot, phases):
    assert dataset.phases_for(slot) == phases


def test_the_registry_round_trips_through_s3(stack):
    registry = dataset.Registry(stack.s3, stack.bucket, "v1")
    assert registry.slots() == {}
    assert registry.slot(3) is None and registry.pins() is None

    entry = {"slot": 3, "phase": "train", "seed": 9, "incident": {"injector": "noop"}}
    record = dataset.new_slot_record("v1", entry, "NORMAL")
    registry.put_slot(record)
    registry.put_pins({"stack": {}, "runner_instance_id": None})
    registry.put_batch({"batch_id": "batch-b", "started_at": "2026-10-16T12:00:00.000Z"})
    registry.put_batch({"batch_id": "batch-a", "started_at": "2026-10-15T12:00:00.000Z"})

    assert registry.slots() == {3: record}
    assert registry.slot(3)["updated_at"] is not None
    assert registry.pins() == {"stack": {}, "runner_instance_id": None}
    assert [b["batch_id"] for b in registry.batches()] == ["batch-a", "batch-b"]
    # Another dataset's registry is a different tree.
    assert dataset.Registry(stack.s3, stack.bucket, "m4-1-validation").slots() == {}


def test_an_index_row_has_what_the_split_needs():
    record = {**dataset.new_slot_record("v1", {"slot": 3, "phase": "train", "seed": 9}, "NORMAL"),
              "status": "ACCEPTED", "experiment_id": ID, "generated_at": "2026-10-16T12:00:00.000Z",
              "generation_date": "2026-10-16", "ended_at": "2026-10-16T12:10:00.000Z", "batch_id": "b"}
    assert dataset.index_row(record) == {
        "slot": 3, "phase": "train", "incident_type": "NORMAL", "experiment_id": ID,
        "generation_date": "2026-10-16", "generated_at": "2026-10-16T12:00:00.000Z",
        "ended_at": "2026-10-16T12:10:00.000Z", "batch_id": "b", "seed": 9}
