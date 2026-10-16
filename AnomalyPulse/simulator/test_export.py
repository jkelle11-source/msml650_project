import json
from datetime import datetime, timedelta, timezone

import pytest

from simulator import dataset, export, manifest

ID = "exp-20261016T120000Z-ab12"


def _iso(moment: datetime) -> str:
    return manifest.to_iso(int(moment.timestamp() * 1000))


def _record(service, request_id, at, **extra):
    return {"timestamp": _iso(at), "request_id": request_id, "service": service, "endpoint": "/products",
            "http_method": "GET", "status_code": 200, "latency_ms": 12, "lambda_duration_ms": 3,
            "cold_start": False, "db_latency_ms": 5, "db_consumed_capacity": 0.5, "dependency": None,
            "dependency_latency_ms": None, "dependency_error": None, **extra}


@pytest.fixture
def run(stack):
    # A run that ends about now: S3 stamps the objects below with the real time,
    # and the export reads objects that landed during the run or just after.
    # Half past the minute, so the run's first and last minutes are partial.
    end = datetime.now(timezone.utc).replace(second=30, microsecond=0)
    start = end - timedelta(minutes=10)
    s3, bucket = stack.s3, stack.bucket
    final = {"experiment_id": ID, "started_at": _iso(start), "ended_at": _iso(end), "activations": []}
    s3.put_object(Bucket=bucket, Key=manifest.manifest_key(ID), Body=json.dumps(final).encode())

    def put_raw(service, name, records):
        key = f"raw/service={service}/dt={end.date().isoformat()}/part-{name}.json"
        s3.put_object(Bucket=bucket, Key=key, Body="\n".join(json.dumps(r) for r in records).encode())

    put_raw("product-service", "a", [_record("product-service", "r2", start + timedelta(minutes=2)),
                                     _record("product-service", "r1", start + timedelta(minutes=1)),
                                     _record("product-service", "early", start - timedelta(seconds=1))])
    # A batch the shipper delivered twice, and a line it could not parse.
    put_raw("product-service", "b", [_record("product-service", "r1", start + timedelta(minutes=1))])
    put_raw("order-service", "c", [_record("order-service", "r1", start + timedelta(minutes=3))])
    s3.put_object(Bucket=bucket, Key=f"raw/service=cart-service/dt={end.date().isoformat()}/part-d.json",
                  Body=b"not json\n" + json.dumps(_record("cart-service", "late", end)).encode())

    def put_metrics(service, stamp):
        key = f"metrics/service={service}/dt={stamp.date().isoformat()}/metrics-{stamp:%Y%m%dT%H%M%S%f}Z.json"
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(
            {"timestamp": _iso(stamp), "service": service, "metrics": {"lambda": {"cold_starts": 0}}}).encode())

    minute = start.replace(second=0)
    for stamp in (minute, minute + timedelta(minutes=1), end.replace(second=0) + timedelta(minutes=1),
                  end.replace(second=0) + timedelta(minutes=2)):
        put_metrics("product-service", stamp)

    registry = dataset.Registry(s3, bucket, "v1")
    registry.put_slot({**dataset.new_slot_record("v1", {"slot": 1, "phase": "train", "seed": 5}, "NORMAL"),
                       "status": "ACCEPTED", "experiment_id": ID, "generated_at": _iso(start),
                       "generation_date": start.date().isoformat(), "ended_at": _iso(end), "batch_id": "b"})
    registry.put_slot(dataset.new_slot_record("v1", {"slot": 2, "phase": "train", "seed": 6}, "NORMAL"))
    return {"start": start, "end": end}


def _lines(stack, key):
    body = stack.s3.get_object(Bucket=stack.bucket, Key=key)["Body"].read().decode()
    return [json.loads(line) for line in body.splitlines()]


def test_export_slices_dedupes_and_orders_the_runs_records(stack, run):
    assert export.export_dataset(stack.session, stack.bucket, "v1", log=lambda *_: None) == \
        {"exported": 1, "accepted": 1}
    records = _lines(stack, dataset.requests_key("v1", ID))
    assert [(r["service"], r["request_id"]) for r in records] == [
        ("product-service", "r1"), ("product-service", "r2"), ("order-service", "r1")]


def test_export_takes_the_snapshots_whose_minute_overlaps_the_run(stack, run):
    export.export_dataset(stack.session, stack.bucket, "v1", log=lambda *_: None)
    stamps = [s["timestamp"] for s in _lines(stack, dataset.metrics_key("v1", ID))]
    minute = run["start"].replace(second=0)
    end_minute = run["end"].replace(second=0)
    # A snapshot covers the minute before its stamp: the one stamped at the
    # start's minute is wholly before the run, the one after the end's minute
    # wholly after it.
    assert stamps == [_iso(minute + timedelta(minutes=1)), _iso(end_minute + timedelta(minutes=1))]


def test_export_copies_the_manifest_to_tier2_and_writes_the_index(stack, run):
    export.export_dataset(stack.session, stack.bucket, "v1", log=lambda *_: None)
    copied = json.loads(stack.s3.get_object(Bucket=stack.bucket, Key=dataset.manifest_key("v1", ID))
                        ["Body"].read())
    assert copied["experiment_id"] == ID
    index = _lines(stack, dataset.index_key("v1"))
    assert [(row["slot"], row["experiment_id"], row["phase"]) for row in index] == [(1, ID, "train")]


def test_export_skips_what_it_has_done_unless_forced(stack, run):
    quiet = {"log": lambda *_: None}
    export.export_dataset(stack.session, stack.bucket, "v1", **quiet)
    assert export.export_dataset(stack.session, stack.bucket, "v1", **quiet)["exported"] == 0
    assert export.export_dataset(stack.session, stack.bucket, "v1", force=True, **quiet)["exported"] == 1


def test_a_tier2_field_in_a_raw_record_stops_the_export(stack, run):
    key = f"raw/service=product-service/dt={run['end'].date().isoformat()}/part-leak.json"
    stack.s3.put_object(Bucket=stack.bucket, Key=key, Body=json.dumps(
        _record("product-service", "x", run["start"] + timedelta(minutes=4), error_type="DynamoDBThrottle")).encode())
    with pytest.raises(export.LeakError, match="error_type"):
        export.export_dataset(stack.session, stack.bucket, "v1", log=lambda *_: None)
    assert "Contents" not in stack.s3.list_objects_v2(Bucket=stack.bucket, Prefix="datasets/v1/tier1/")


def test_the_catalog_allows_the_telemetry_fields_and_no_tier2_field():
    allowed = export.allowed_fields()
    assert {"timestamp", "request_id", "latency_ms", "db_consumed_capacity"} <= allowed
    assert not {"error_type", "db_throttled", "incident_type", "severity", "fault_injection_params"} & allowed
