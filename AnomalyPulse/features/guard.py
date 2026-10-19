"""Feature/Label Boundary guard (M4-5).

Fails the build if a Tier-2 name (or a join key / label name) shows up in a
column of the feature matrix. The names come from the field catalog, the
manifest schema and the injector registry, so nobody has to keep a second list
in sync.
"""
import json
from pathlib import Path

from simulator import injectors

_ROOT = Path(__file__).resolve().parent.parent
_CATALOG_PATH = _ROOT / "infra" / "shared" / "field_catalog.json"
_MANIFEST_SCHEMA_PATH = _ROOT / "simulator" / "manifest_schema.json"

# Names that are not Tier-2 fields in the catalog but must never be in a feature
# column: join keys (docs/DATASET.md: "a key for joining, never a column"), the
# label, the run's seed and bounds, and when DB-throttling labeling starts.
EXTRA_FORBIDDEN = frozenset({
    "experiment_id", "slot", "phase", "label", "seed", "started_at", "ended_at", "label_from",
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


def fault_names(schema_path=_MANIFEST_SCHEMA_PATH):
    """A fault activation's fields (injector, intensity, band, params, observed, ...) and
    every injector's native parameter (multiplier, load_ratio, timeout_ms, ...)."""
    schema = json.loads(Path(schema_path).read_text())
    fields = set(schema["$defs"]["activation"]["properties"])
    params = {injectors.get(name).parameter for name in injectors.names()} - {None}
    if not fields or not params:
        raise ValueError("manifest schema or injector registry lists no fault fields")
    return fields | params


def forbidden_names(catalog_path=_CATALOG_PATH):
    return tier2_names(catalog_path) | fault_names() | EXTRA_FORBIDDEN


def _words(name):
    return str(name).lower().split("_")


def _contains(column_words, name_words):
    n = len(name_words)
    return any(column_words[i:i + n] == name_words for i in range(len(column_words) - n + 1))


def check_columns(columns, catalog_path=_CATALOG_PATH):
    """Raise BoundaryViolation if any column holds a forbidden name as whole words.

    Every real column has a service prefix or a temporal suffix (product_..., ..._change),
    so a leak would be named like one: product_db_throttled and order_error_type_count fail.
    Whole words split on "_", ignoring case, never a substring search: the legitimate
    Tier-1 counts product_throttled_requests / read_throttle_events must pass, and
    db_throttled is not a run of their words.
    """
    forbidden = [_words(name) for name in forbidden_names(catalog_path)]
    bad = sorted(str(c) for c in columns if any(_contains(_words(c), f) for f in forbidden))
    if bad:
        raise BoundaryViolation(f"forbidden columns in the feature matrix: {', '.join(bad)}")
