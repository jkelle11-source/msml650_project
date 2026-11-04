"""The window builder (M8-1; Docs/PIPELINE.md). Run once a minute by a schedule, it
turns the Aggregator's per-minute record files into what the rest of the system reads:

- each minute's health numbers for the dashboard, under pipeline/health/
- each minute's 106-column feature window, built by build_matrix as training's were,
  under pipeline/windows/, and announced on the bus (WindowReady) when no cell is empty

It decides what to build from what is in the bucket, not from the clock, so a late,
missed or doubled run builds the same thing."""
import json, os, boto3

from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError

from features.build_matrix import build_matrix
from features.request_features import SERVICES, percentile
from features import windows

BUCKET = os.environ["BUCKET_NAME"]
BUS_NAME = os.environ["BUS_NAME"]
s3 = boto3.client("s3")
events = boto3.client("events")

def minute_name(w: datetime) -> str:
    """'2026-11-04T07:04:00.000Z': the features' name for the window that starts at w."""
    return w.strftime("%Y-%m-%dT%H:%M:%S.") + f"{w.microsecond // 1000:03d}Z"

def _minute_path(w: datetime) -> str:
    """'dt=2026-11-04/minute=07-04': the date and minute parts of every pipeline/ key."""
    return f"dt={w:%Y-%m-%d}/minute={w:%H-%M}"

def records_prefix(w: datetime) -> str:
    """'pipeline/records/dt=2026-11-04/minute=07-04/': where the Aggregator files the minute's records."""
    return f"pipeline/records/{_minute_path(w)}/"

def health_key(w: datetime) -> str:
    """'pipeline/health/dt=2026-11-04/minute=07-04.json'"""
    return f"pipeline/health/{_minute_path(w)}.json"

def window_key(w: datetime) -> str:
    """'pipeline/windows/dt=2026-11-04/minute=07-04.json'"""
    return f"pipeline/windows/{_minute_path(w)}.json"

def snapshot_key(service: str, w: datetime) -> str:
    """'metrics/service=product-service/dt=2026-11-04/metrics-20261104T070500000000Z.json':
    the metrics extractor's snapshot for the window that starts at w. It is stamped with
    the window's end, and its date folder is the stamp's, so 23:59's is filed under the next day."""
    stamp = w + timedelta(minutes=1)
    return f"metrics/service={service}/dt={stamp:%Y-%m-%d}/metrics-{stamp:%Y%m%dT%H%M%S%fZ}.json"

def _numbers(recs: list[dict]) -> dict:
    """{"requests_per_min", "error_rate", "p95_latency_ms"} of these records; the last two None if there are none."""
    requests_per_min = len(recs)
    
    error_rate = sum(1 for r in recs if 500 <= r["status_code"] <= 599 
                     ) / requests_per_min if requests_per_min else None
    
    p95_latency_ms = percentile([r["latency_ms"] for r in recs], 95)
    
    return {"requests_per_min": requests_per_min,
            "error_rate": error_rate,
            "p95_latency_ms": p95_latency_ms}
    
def _read_records(prefix: str) -> list[dict]:
    """Every record in the files under prefix. The Aggregator writes one file per minute per
    run, one JSON record per line, so a minute is usually several files."""
    records = []

    # A listing comes back in pages of up to 1,000 files; a minute with no files has no "Contents".
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket = BUCKET,
                                                             Prefix = prefix):

        for item in page.get("Contents", []):
            raw_obj = s3.get_object(Bucket = BUCKET, Key = item["Key"])["Body"].read().decode()
            split_obj = raw_obj.split("\n")
            records.extend(json.loads(obj) for obj in split_obj if obj.strip())
            
    return records

def _exists(key: str) -> bool:
    """True if the object is there. Only a 404 means it isn't; any other error (a 403 above all)
    is raised, never read as missing, or the same window would be rebuilt and announced every run."""
    try:
        s3.head_object(Bucket = BUCKET, Key = key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise

def _read_json(key: str) -> dict | None:
    """The JSON object at key, or None if there is none (as _exists: only a missing key is None)."""
    try:
        body = s3.get_object(Bucket = BUCKET, Key = key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body)

def _announce(window: dict, key: str) -> None:
    """One WindowReady event on the bus, holding the window in the Diagnosis Lambda's input shape."""
    response = events.put_events(Entries = [{
        "EventBusName": BUS_NAME,
        "Source": "anomalypulse.pipeline",
        "DetailType": "WindowReady",
        "Detail": json.dumps({"windows": [window], "key": key}),
    }])
    # As in the forwarder: a failed entry doesn't raise, PutEvents reports it.
    if response["FailedEntryCount"]:
        codes = sorted({e["ErrorCode"] for e in response["Entries"] if "ErrorCode" in e})
        raise RuntimeError(f"PutEvents: the WindowReady event failed {codes}")

def dedupe(records: list[dict]) -> list[dict]:
    """The records without repeats: the first of each (service, request_id), in their order."""
    seen = set()
    no_dupes = []
    
    for r in records:
        
        if (r["service"], r["request_id"]) not in seen:
            seen.add((r["service"], r["request_id"]))
            no_dupes.append(r)
            
    return no_dupes

def health(records: list[dict], window_start: str) -> dict:
    """The dashboard's numbers for one minute, from its records."""
    no_dupes = dedupe(records)

    # Only the minute's own records: a file could hold another minute's.
    filtered = [r for r in no_dupes
                if windows.request_window(r["timestamp"]) == window_start]

    # Payment is reached only through Order, so counting it would count each checkout
    # twice (Docs/DASHBOARD_CONTRACT.md); it still gets its own entry below.
    system_dict = _numbers([r for r in filtered if r["service"] != "payment-service"])
    
    service_dict = [{"service": s, **_numbers([r for r in filtered if r["service"] == s])}
                    for s in SERVICES]
    
    return {"window_start": window_start, **system_dict, "services": service_dict}

def build_window(records: list[dict], snapshots: list[dict], window_start: str) -> dict | None:
    """{"window_start": W, <the 106 columns>} from the records and snapshots of W-2..W; None if W has neither."""
    # build_matrix as training ran it, guard included, so the live path stays Tier-1 only.
    # The temporal columns look back 2 windows, so W-2..W gives exactly the row a build
    # over the whole stream would (all 1,540 validation windows, 2026-11-04).
    no_dupes = dedupe(records)
    matrix = build_matrix(no_dupes, snapshots)
    return {"window_start": window_start, **matrix[window_start]} if window_start in matrix else None
    
def is_complete(window: dict) -> bool:
    """True if no column is empty: the only window the Diagnosis Lambda scores."""
    return all(v is not None for k, v in window.items() if k != "window_start")

def run(now: datetime) -> dict:
    """One pass; {"health": [names], "windows": [names], "announced": [names]}."""
    # The minute now is in. It isn't over, so nothing is built for it.
    current = now.replace(second = 0, microsecond = 0)

    errors = []
    built = {"health": [], "windows": [], "announced": []}

    # Health: the last 3 minutes, rebuilt every run. The newest is then never more than
    # 2 minutes old, whatever second the schedule runs at (the dashboard says NO_DATA after
    # 3), and records still in flight on the first pass are counted on the next.
    for k in range(3, 0, -1):
        
        try:
            w = current - timedelta(minutes = k)
            records = _read_records(records_prefix(w))
                
            health_report = health(records, minute_name(w))
            
            s3.put_object(Bucket = BUCKET,
                        Key = health_key(w),
                        Body = json.dumps(health_report))
            
            built["health"].append(minute_name(w))
            
        except Exception as e:
            errors.append(f"health {minute_name(w)}: {e!r}")
    
    # Windows: each of the last 10 minutes, oldest first, is built once, when the metrics
    # extractor's snapshots for it are in (about 3½ minutes after it starts).
    for k in range(10, 0, -1):
        w = current - timedelta(minutes = k)
        name = minute_name(w)
        try:
            if _exists(window_key(w)):
                continue    # built on an earlier run, and announced then if complete

            own = [_read_json(snapshot_key(s, w)) for s in SERVICES]
            if any(snap is None for snap in own):
                continue    # not every snapshot is in yet: a later run builds it

            # The two minutes before it too: the temporal columns look back over them.
            records, snapshots = [], list(own)
            for back in (2, 1, 0):
                m = w - timedelta(minutes = back)
                records += _read_records(records_prefix(m))
                if back:
                    found = (_read_json(snapshot_key(s, m)) for s in SERVICES)
                    snapshots += [snap for snap in found if snap is not None]

            window = build_window(records, snapshots, name)
            if window is None:
                continue    # can't happen once its snapshots exist; never announce nothing

            # Announce first, then write the file: the file marks the window done, so a
            # failed announcement leaves no file and the next run tries again.
            if is_complete(window):
                _announce(window, window_key(w))
                built["announced"].append(name)
            s3.put_object(Bucket = BUCKET, Key = window_key(w), Body = json.dumps(window))
            built["windows"].append(name)

        except Exception as e:
            errors.append(f"window {name}: {e!r}")

    # Every minute was tried; report the failures together. The next run tries them again.
    if errors:
        raise RuntimeError(f"{len(errors)} failed: " + "; ".join(errors))
    return built


def handler(event, context) -> dict:
    """Run once a minute by the schedule: build whatever is due."""
    return run(datetime.now(timezone.utc))