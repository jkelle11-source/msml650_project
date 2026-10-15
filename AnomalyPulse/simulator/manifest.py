"""Experiment manifest (M3-1): build, validate and write.

A manifest is the ground-truth record of one simulator run. It is stored at
experiments/<experiment_id>/manifest.json in the telemetry bucket and is pure
Tier-2: nothing in it may ever reach the feature matrix.

The manifest holds ISO timestamps in telemetry's format, while the rest of the
simulator (the lock, the fault control plane) works in epoch milliseconds.
to_iso() and to_epoch_ms() are the one place the two meet.
"""
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

_schema = json.loads((Path(__file__).parent / "manifest_schema.json").read_text())
Draft202012Validator.check_schema(_schema)
_validator = Draft202012Validator(_schema)

SCHEMA_VERSION = _schema["properties"]["schema_version"]["const"]
INCIDENT_TYPES = tuple(_schema["properties"]["incident_type"]["enum"])
TELEMETRY_PREFIXES = ("raw/", "metrics/")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ManifestError(ValueError):
    """A manifest failed validation. Nothing has been written when this is raised."""


# --- time ------------------------------------------------------------------

def to_iso(epoch_ms: int) -> str:
    """Epoch milliseconds -> telemetry's timestamp format (UTC, ms precision, Z)."""
    # timedelta arithmetic keeps this exact; dividing by 1000 would go through a float.
    moment = _EPOCH + timedelta(milliseconds=epoch_ms)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_epoch_ms(iso: str) -> int:
    """Telemetry-format timestamp -> epoch milliseconds.

    Raises ValueError for a string that is not a real date and time.
    """
    moment = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (moment - _EPOCH) // timedelta(milliseconds=1)


# --- building --------------------------------------------------------------

def new_experiment_id(now_ms: int | None = None) -> str:
    """exp-<UTC start time>-<4 hex>, e.g. exp-20261012T041500Z-3f9a."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    stamp = (_EPOCH + timedelta(milliseconds=now_ms)).strftime("%Y%m%dT%H%M%SZ")
    # secrets, not random: the suffix must not draw from the seeded RNG that
    # resolves fault params, or the same seed would stop giving the same params.
    return f"exp-{stamp}-{secrets.token_hex(2)}"


def manifest_key(experiment_id: str) -> str:
    return f"experiments/{experiment_id}/manifest.json"


def fault_id(experiment_id: str, index: int) -> str:
    """Id of the experiment's `index`-th activation (0, 1, ...).

    It is also the `fault_id` of the fault-config item when the injector uses
    the control plane, so the two records can be matched.
    """
    return f"{experiment_id}#{index}"


def severity_for(intensities) -> str | None:
    """Severity from the highest intensity, by thirds of the 0-1 range.

    None when there are no intensities, which is a NORMAL run.
    """
    intensities = list(intensities)
    if not intensities:
        return None
    highest = max(intensities)
    if highest < 1 / 3:
        return "LOW"
    if highest < 2 / 3:
        return "MEDIUM"
    return "HIGH"


def build_activation(experiment_id: str, index: int, *, injector: str, target: str | None,
                     intensity: float, band: dict, params: dict,
                     active_from_ms: int, active_until_ms: int,
                     observed: dict | None = None) -> dict:
    """One entry for the manifest's `activations` list.

    `params` is what was asked for and repeats from the seed; `observed` is what
    the injector measured and does not. It is empty until the run ends.
    """
    return {
        "fault_id": fault_id(experiment_id, index),
        "injector": injector,
        "target": target,
        "intensity": intensity,
        "band": band,
        "params": params,
        "observed": {} if observed is None else observed,
        "active_from": to_iso(active_from_ms),
        "active_until": to_iso(active_until_ms),
    }


def build_manifest(*, experiment_id: str, incident_type: str, seed: int, created_by: str,
                   started_at_ms: int, workload: dict, activations: list[dict],
                   bucket: str, prefixes=TELEMETRY_PREFIXES) -> dict:
    """The manifest as it is first written: status RUNNING, no end time yet.

    `incident_type` is the intended experiment-level label. It is not checked
    against the number of activations, because a confounder run is a
    single-fault type with two of them. Severity is derived, never passed in.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "status": "RUNNING",
        "incident_type": incident_type,
        "severity": severity_for(a["intensity"] for a in activations),
        "seed": seed,
        "created_by": created_by,
        "started_at": to_iso(started_at_ms),
        "ended_at": None,
        "workload": workload,
        "activations": list(activations),
        "telemetry": {"bucket": bucket, "prefixes": list(prefixes)},
    }


def finalize(manifest: dict, status: str, ended_at_ms: int) -> dict:
    """A copy of `manifest` with its final status and end time.

    `status` is COMPLETED, FAILED or ABORTED. The dict passed in is not changed.
    """
    return {**manifest, "status": status, "ended_at": to_iso(ended_at_ms)}


# --- validating ------------------------------------------------------------

def _dumps(manifest) -> str:
    # allow_nan=False: json would otherwise write NaN / Infinity, which is not
    # JSON and breaks every other reader.
    return json.dumps(manifest, indent=2, allow_nan=False)


def _describe(error) -> str:
    where = "/".join(str(part) for part in error.absolute_path)
    return f"{where}: {error.message}" if where else error.message


def _cross_field_errors(manifest: dict) -> list[str]:
    """The rules the schema cannot express. Assumes the manifest passed the schema."""
    experiment_id = manifest["experiment_id"]
    activations = manifest["activations"]

    try:
        started = to_epoch_ms(manifest["started_at"])
        ended = None if manifest["ended_at"] is None else to_epoch_ms(manifest["ended_at"])
        windows = [(to_epoch_ms(a["active_from"]), to_epoch_ms(a["active_until"]))
                   for a in activations]
    except ValueError as e:
        # The schema's pattern only checks the shape, so 2026-13-45 gets this far.
        return [f"timestamp is not a real date and time: {e}"]

    errors = []
    if ended is not None and ended <= started:
        errors.append("ended_at must be after started_at")

    for activation, (active_from, active_until) in zip(activations, windows):
        fault_id = activation["fault_id"]
        if not fault_id.startswith(experiment_id + "#"):
            errors.append(f"{fault_id}: fault_id must start with {experiment_id}#")
        if active_from >= active_until:
            errors.append(f"{fault_id}: active_from must be before active_until")
        if active_from < started:
            errors.append(f"{fault_id}: active_from is before started_at")
        # Only a COMPLETED run promises that its faults ended inside it. A FAILED
        # or ABORTED run keeps the windows it planned, which can run past ended_at.
        if manifest["status"] == "COMPLETED" and active_until > ended:
            errors.append(f"{fault_id}: active_until is after ended_at")
        if activation["band"]["low"] > activation["band"]["high"]:
            errors.append(f"{fault_id}: band low is above band high")

    fault_ids = [a["fault_id"] for a in activations]
    if len(set(fault_ids)) != len(fault_ids):
        errors.append("fault_id values must be unique")

    expected = severity_for(a["intensity"] for a in activations)
    if manifest["severity"] != expected:
        errors.append(f"severity must be {expected} for the highest intensity")

    # Project plan section 3: CASCADING_FAILURE means two or more faults active at
    # once. A run whose faults never overlap (or that has only one) is not a cascade.
    if manifest["incident_type"] == "CASCADING_FAILURE" and not any(
            a_from < b_until and b_from < a_until
            for i, (a_from, a_until) in enumerate(windows)
            for b_from, b_until in windows[i + 1:]):
        errors.append("a CASCADING_FAILURE run needs two or more faults active at the same time")

    return errors


def validate(manifest):
    """(True, []) for a valid manifest, else (False, [one message per problem])."""
    try:
        _dumps(manifest)
    except (TypeError, ValueError) as e:
        # e.g. a Decimal that came back from DynamoDB and went into `params`.
        return (False, [f"not JSON-serializable: {e}"])

    errors = [_describe(error) for error in _validator.iter_errors(manifest)]
    if len(errors) == 0:
        errors = _cross_field_errors(manifest)
    return (len(errors) == 0, errors)


def validate_or_raise(manifest):
    valid, errors = validate(manifest)
    if not valid:
        raise ManifestError("invalid manifest: " + "; ".join(errors))


# --- writing ---------------------------------------------------------------

def write_manifest(s3_client, bucket: str, manifest: dict) -> str:
    """Validate `manifest`, then write it to its key in `bucket`. Returns the key.

    Called twice per run: once with the RUNNING manifest and once with the
    finalized one, which overwrites the first.
    """
    validate_or_raise(manifest)
    key = manifest_key(manifest["experiment_id"])
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=_dumps(manifest).encode("utf-8"),
        ContentType="application/json",
    )
    return key
