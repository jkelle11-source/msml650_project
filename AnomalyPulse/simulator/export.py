"""Export accepted experiments into the dataset layout (M4-1).

    python -m simulator export --dataset v1

For each experiment the registry has accepted, it writes (dataset.py has the layout):

    tier1/requests/experiment_id=<id>/requests.jsonl   every request record of the run,
        from raw/, with started_at <= timestamp < ended_at, deduplicated by
        (service, request_id) and sorted by time
    tier1/metrics/experiment_id=<id>/metrics.jsonl     the CloudWatch snapshots from metrics/
        whose minute overlaps the run
    tier2/manifests/experiment_id=<id>/manifest.json   the run's manifest, written last

and then tier2/experiments.jsonl, one line per accepted experiment. It is
idempotent: an experiment whose manifest copy exists is skipped unless --force.

Each request record is checked against the field catalog: a field that is not
correlation or Tier-1 stops the export, so tier1/ can never hold a near-label.

run-batch exports its dataset when it ends, once the shipper has landed the last
run's records. Don't export by hand while a batch is running on the same host:
it competes with the workload for the CPU.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.config import Config

from simulator import dataset, manifest
from simulator.telemetry_report import SERVICES

# The shipper lands a record within a couple of minutes (Docs/SIMULATOR_INTERFACE.md);
# objects that landed up to this long after the run are read for its records.
LANDING_MARGIN = timedelta(minutes=10)
WORKERS = 32

_CATALOG = Path(__file__).parent.parent / "infra" / "shared" / "field_catalog.json"


def allowed_fields() -> set:
    """The fields a Tier-1 record may hold: correlation and tier1_observable in the catalog."""
    fields = json.loads(_CATALOG.read_text())["fields"]
    return {name for name, field in fields.items() if field["tier"] in ("correlation", "tier1_observable")}


class LeakError(RuntimeError):
    """A raw record holds a field that may not reach tier1/."""


def _days(start: datetime, end: datetime) -> list[str]:
    return [(start.date() + timedelta(days=d)).isoformat() for d in range((end.date() - start.date()).days + 1)]


def _parse(iso: str) -> datetime:
    return datetime.fromtimestamp(manifest.to_epoch_ms(iso) / 1000, timezone.utc)


class Exporter:
    def __init__(self, s3, bucket: str, name: str, *, workers: int = WORKERS, log=print):
        self.s3, self.bucket, self.name, self.log = s3, bucket, dataset.check_name(name), log
        self.workers = workers
        self.allowed = allowed_fields()
        self._listings = {}   # prefix -> [(LastModified, key)]

    def _listing(self, prefix: str) -> list:
        # Listed once per export: a day's raw/ prefix holds tens of thousands of objects.
        if prefix not in self._listings:
            found = []
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
                found += [(obj["LastModified"], obj["Key"]) for obj in page.get("Contents", [])]
            self._listings[prefix] = found
        return self._listings[prefix]

    def _bodies(self, keys: list[str]) -> list[str]:
        def get(key):
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(get, keys))

    def requests(self, start: datetime, end: datetime) -> tuple[list[dict], int]:
        """The run's request records, deduplicated and in time order, and how many duplicates were dropped."""
        landed_by = end + LANDING_MARGIN
        keys = [key for service in SERVICES for day in _days(start, landed_by)
                for modified, key in self._listing(f"raw/service={service}/dt={day}/")
                if start <= modified <= landed_by]
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        rows, seen, duplicates = [], set(), 0
        for body in self._bodies(keys):
            for line in body.splitlines():
                try:
                    record = json.loads(line)
                    at_ms = manifest.to_epoch_ms(record["timestamp"])
                except (ValueError, KeyError, TypeError):
                    continue
                if not start_ms <= at_ms < end_ms:
                    continue
                extra = set(record) - self.allowed
                if extra:
                    raise LeakError(f"a {record.get('service')} record holds {sorted(extra)}, which is not "
                                    f"correlation or Tier-1 in {_CATALOG.name}; nothing more is exported")
                request_id = record.get("request_id")
                if request_id is not None:
                    # The shipper delivers at least once (Docs/TELEMETRY_CONTRACT.md).
                    if (record.get("service"), request_id) in seen:
                        duplicates += 1
                        continue
                    seen.add((record.get("service"), request_id))
                rows.append((at_ms, record.get("service") or "", request_id or "", record))
        rows.sort(key=lambda row: row[:3])
        return [row[3] for row in rows], duplicates

    def metrics(self, start: datetime, end: datetime) -> list[dict]:
        """The CloudWatch snapshots whose minute overlaps the run.

        A snapshot is stamped with the end of the minute it covers
        (metrics_extractor), so one stamped T covers [T - 1 min, T).
        """
        keys = []
        for service in SERVICES:
            for day in _days(start, end + timedelta(minutes=1)):
                for _, key in self._listing(f"metrics/service={service}/dt={day}/"):
                    try:
                        stamp = datetime.strptime(key.rsplit("metrics-", 1)[1], "%Y%m%dT%H%M%S%fZ.json")
                    except (IndexError, ValueError):
                        continue
                    stamp = stamp.replace(tzinfo=timezone.utc)
                    if start < stamp and stamp - timedelta(minutes=1) < end:
                        keys.append(key)
        snapshots = [json.loads(body) for body in self._bodies(keys)]
        return sorted(snapshots, key=lambda s: (s.get("timestamp", ""), s.get("service", "")))

    def _put_lines(self, key: str, rows: list[dict]) -> None:
        body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode(), ContentType="application/x-ndjson")

    def experiment(self, experiment_id: str) -> dict:
        """Export one run. Its manifest copy is written last, so a half-done export is redone."""
        body = self.s3.get_object(Bucket=self.bucket, Key=manifest.manifest_key(experiment_id))["Body"].read()
        run = json.loads(body)
        start, end = _parse(run["started_at"]), _parse(run["ended_at"])
        records, duplicates = self.requests(start, end)
        snapshots = self.metrics(start, end)
        self._put_lines(dataset.requests_key(self.name, experiment_id), records)
        self._put_lines(dataset.metrics_key(self.name, experiment_id), snapshots)
        self.s3.put_object(Bucket=self.bucket, Key=dataset.manifest_key(self.name, experiment_id),
                           Body=body, ContentType="application/json")
        counts = {"records": len(records), "duplicates_dropped": duplicates, "metric_snapshots": len(snapshots)}
        self.log(f"  {experiment_id}: {counts['records']} records ({duplicates} duplicates dropped), "
                 f"{len(snapshots)} metric snapshots"
                 + ("  <- no records: did the shipper land them?" if not records else ""))
        return counts

    def exported(self) -> set:
        prefix = dataset.manifests_prefix(self.name)
        keys = [key for _, key in self._listing(prefix)]
        del self._listings[prefix]   # it changes as this export runs
        return {key[len(prefix):].split("/", 1)[0].removeprefix("experiment_id=") for key in keys}


def export_dataset(session, bucket: str, name: str, *, force: bool = False, log=print) -> dict:
    """Export every accepted experiment not yet exported, then rewrite experiments.jsonl."""
    s3 = session.client("s3", config=Config(max_pool_connections=WORKERS))
    exporter = Exporter(s3, bucket, name, log=log)
    accepted = sorted((r for r in dataset.Registry(s3, bucket, name).slots().values()
                       if r["status"] == "ACCEPTED"), key=lambda r: r["slot"])
    done = set() if force else exporter.exported()
    todo = [r for r in accepted if r["experiment_id"] not in done]
    log(f"exporting {len(todo)} of {len(accepted)} accepted experiments in dataset {name}")
    for record in sorted(todo, key=lambda r: r["generated_at"]):
        exporter.experiment(record["experiment_id"])
    exporter._put_lines(dataset.index_key(name), [dataset.index_row(r) for r in accepted])
    log(f"wrote s3://{bucket}/{dataset.index_key(name)} ({len(accepted)} experiments)")
    return {"exported": len(todo), "accepted": len(accepted)}
