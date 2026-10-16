"""The dataset's S3 layout and its experiment registry (M4-1).

Everything lives under datasets/<name>/ in the telemetry bucket, which has no
lifecycle rule (raw/ and metrics/ expire after 90 days). Tier-1 and Tier-2 are
separate trees, so feature code can be given one and never the other:

    datasets/<name>/
      tier1/                          observable: may feed features
        requests/experiment_id=<id>/requests.jsonl   the run's request records (export.py)
        metrics/experiment_id=<id>/metrics.jsonl     the run's CloudWatch snapshots (export.py)
        windows/                                     one-minute windows (M4-4)
        features/                                    the feature matrix (M4-5)
      tier2/                          ground truth: never a feature
        experiments.jsonl                            one line per accepted experiment (export.py)
        manifests/experiment_id=<id>/manifest.json   the run's manifest (export.py)
        labels/                                      window labels (M4-4)
        registry/slots/<slot>.json                   one record per experiment number
        registry/batches/<batch_id>.json             each batch: stack config, runner, code
        registry/pins.json                           the stack and runner the dataset is pinned to

A window is keyed by (experiment_id, window start). An experiment's number
(its "slot", 1-1000) and phase are in the registry, not in any Tier-1 key.
"""
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from botocore.exceptions import ClientError

DEFAULT_DATASET = "v1"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

# Project plan section 17: 1-560 train, 561-700 validation, then the test set,
# generated on a separate day. The extreme-extrapolation slice is numbered
# after the test set and tagged "stress", so it is reported separately.
PHASES = ("train", "val", "test", "stress")
TRAIN_LAST = 560
VAL_LAST = 700

# A slot's life: PENDING until a run of it is judged; CHECKING while its last
# run waits for CloudWatch to settle; ACCEPTED, or EXHAUSTED when every attempt
# was rejected.
STATUSES = ("PENDING", "CHECKING", "ACCEPTED", "EXHAUSTED")
REGISTRY_VERSION = 1


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"dataset name must be lowercase letters, digits and hyphens, got {name!r}")
    return name


def phases_for(slot: int) -> tuple:
    """The phases experiment number `slot` may have."""
    if slot <= TRAIN_LAST:
        return ("train",)
    if slot <= VAL_LAST:
        return ("val",)
    return ("test", "stress")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_batch_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"batch-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


# --- keys ------------------------------------------------------------------

def root(dataset: str) -> str:
    return f"datasets/{check_name(dataset)}/"


def requests_key(dataset: str, experiment_id: str) -> str:
    return f"{root(dataset)}tier1/requests/experiment_id={experiment_id}/requests.jsonl"


def metrics_key(dataset: str, experiment_id: str) -> str:
    return f"{root(dataset)}tier1/metrics/experiment_id={experiment_id}/metrics.jsonl"


def manifests_prefix(dataset: str) -> str:
    return f"{root(dataset)}tier2/manifests/"


def manifest_key(dataset: str, experiment_id: str) -> str:
    return f"{manifests_prefix(dataset)}experiment_id={experiment_id}/manifest.json"


def index_key(dataset: str) -> str:
    return f"{root(dataset)}tier2/experiments.jsonl"


def _registry(dataset: str) -> str:
    return f"{root(dataset)}tier2/registry/"


def slot_key(dataset: str, slot: int) -> str:
    return f"{_registry(dataset)}slots/{slot:04d}.json"


def batch_key(dataset: str, batch_id: str) -> str:
    return f"{_registry(dataset)}batches/{batch_id}.json"


def pins_key(dataset: str) -> str:
    return f"{_registry(dataset)}pins.json"


# --- the registry ----------------------------------------------------------

def new_slot_record(dataset: str, entry: dict, incident_type: str) -> dict:
    """The record of an experiment number before any run of it."""
    return {
        "registry_version": REGISTRY_VERSION,
        "dataset": dataset,
        "slot": entry["slot"],
        "phase": entry["phase"],
        "incident_type": incident_type,
        "spec": entry,
        "status": "PENDING",
        # Set when a run is accepted. generation_date (UTC) is what the
        # temporal split keys off (M4-3).
        "experiment_id": None,
        "generated_at": None,
        "generation_date": None,
        "ended_at": None,
        "batch_id": None,
        "attempts": [],
        "updated_at": None,
    }


def index_row(record: dict) -> dict:
    """An accepted experiment as a line of experiments.jsonl."""
    return {key: record[key] for key in ("slot", "phase", "incident_type", "experiment_id",
                                         "generation_date", "generated_at", "ended_at", "batch_id")} \
        | {"seed": record["spec"]["seed"]}


class Registry:
    """The registry of one dataset, in S3. One writer at a time: the batch runner."""

    def __init__(self, s3, bucket: str, dataset: str):
        self.s3, self.bucket, self.dataset = s3, bucket, check_name(dataset)

    def _get(self, key: str):
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(body)

    def _put(self, key: str, value) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, ContentType="application/json",
                           Body=json.dumps(value, indent=2, allow_nan=False).encode())

    def _keys(self, prefix: str) -> list[str]:
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [obj["Key"] for obj in page.get("Contents", [])]
        return keys

    def _get_all(self, prefix: str) -> list:
        with ThreadPoolExecutor(max_workers=16) as pool:
            return [value for value in pool.map(self._get, self._keys(prefix)) if value is not None]

    def slots(self) -> dict[int, dict]:
        return {record["slot"]: record for record in self._get_all(f"{_registry(self.dataset)}slots/")}

    def slot(self, slot: int) -> dict | None:
        return self._get(slot_key(self.dataset, slot))

    def put_slot(self, record: dict) -> None:
        record["updated_at"] = utc_now_iso()
        self._put(slot_key(self.dataset, record["slot"]), record)

    def batches(self) -> list[dict]:
        return sorted(self._get_all(f"{_registry(self.dataset)}batches/"), key=lambda b: b["started_at"])

    def put_batch(self, record: dict) -> None:
        self._put(batch_key(self.dataset, record["batch_id"]), record)

    def pins(self) -> dict | None:
        return self._get(pins_key(self.dataset))

    def put_pins(self, pins: dict) -> None:
        self._put(pins_key(self.dataset), pins)
