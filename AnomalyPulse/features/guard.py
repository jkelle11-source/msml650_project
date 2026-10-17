"""Feature/Label Boundary guard (M4-5).

Fails the build if a Tier-2 name (or a join key / label name) shows up as a
column of the feature matrix. The Tier-2 names come from the field catalog, so
nobody has to keep a second list in sync.
"""
import json
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "infra" / "shared" / "field_catalog.json"

# Names that are not Tier-2 fields in the catalog but must never be feature
# columns: join keys (docs/DATASET.md: "a key for joining, never a column"),
# the label, and the fault-injection parameters.
EXTRA_FORBIDDEN = frozenset({
    "experiment_id", "slot", "phase", "label",
    "intensity", "band", "params",
    "multiplier", "timeout_ms", "added_latency_ms",
})


class BoundaryViolation(Exception):
    """A forbidden name reached the feature matrix."""


def tier2_names(catalog_path=_CATALOG_PATH):
    """Every field the catalog marks tier2_ground_truth."""
    catalog = json.loads(Path(catalog_path).read_text())
    names = {name for name, info in catalog["fields"].items()
             if info.get("tier") == "tier2_ground_truth"}
    if not names:
        # An empty list would let everything pass, so refuse to continue.
        raise ValueError("field catalog lists no Tier-2 fields")
    return names


def forbidden_names(catalog_path=_CATALOG_PATH):
    return tier2_names(catalog_path) | EXTRA_FORBIDDEN


def check_columns(columns, catalog_path=_CATALOG_PATH):
    """Raise BoundaryViolation if any column is a forbidden name.

    Exact whole-name match (ignoring case). Never a substring search: the
    legitimate Tier-1 counts throttled_requests / read_throttle_events must pass,
    while the Tier-2 db_throttled must not.
    """
    forbidden = {name.lower() for name in forbidden_names(catalog_path)}
    bad = sorted(str(c) for c in columns if str(c).lower() in forbidden)
    if bad:
        raise BoundaryViolation(f"forbidden columns in the feature matrix: {', '.join(bad)}")