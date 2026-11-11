"""Each Lambda bundle a Makefile builds has every module of this repository its code imports. A module left
out imports fine in a checkout and fails only on AWS, on the first event. No AWS.

Adding a Lambda, or a module a Lambda runs, means one row in BUILDS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_bundle.py -q
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]          # AnomalyPulse/
PACKAGES = ("genai", "incidents")

# Each build: its Makefile, and the files its Lambda runs, as (folder, module); the imports are followed
# from these. A folder that isn't a package (dashboard/) is bundled flat, at the bundle's top level.
BUILDS = {
    "anomalypulse-incidents": ("incidents/Makefile",
                               [("incidents", m) for m in ("builder", "record", "core", "handler")]),
    # M8-5's live source reads stored records through incidents.record.to_incident (Docs/INCIDENT_FEED.md),
    # so the bundle carries it before dashboard/handler.py imports it.
    "anomalypulse-dashboard": ("dashboard/Makefile",
                               [("dashboard", m) for m in ("handler", "builder", "mock_source", "live_source")]
                               + [("incidents", "record")]),
}


def imports(source, folder):
    """The (folder, module) pairs a source file in `folder` imports from this repository: `from genai.x
    import ...` and `from genai import x, y` (likewise incidents), and in a flat folder, `import x` or
    `from x import ...` of a file beside it."""
    found = set()
    for package in PACKAGES:
        found |= {(package, m) for m in re.findall(rf"^\s*from {package}\.(\w+) import", source, re.MULTILINE)}
        for group in re.findall(rf"^\s*from {package} import ([\w, ]+?)\s*(?:#.*)?$", source, re.MULTILINE):
            found |= {(package, name.strip()) for name in group.split(",")}
    if folder not in PACKAGES:
        for plain, frm in re.findall(r"^\s*(?:import (\w+)\s*(?:#.*)?$|from (\w+) import)", source, re.MULTILINE):
            module = plain or frm
            if module not in PACKAGES and (ROOT / folder / f"{module}.py").exists():
                found.add((folder, module))
    return found


def needed(entries):
    """Every (folder, module) the entries import, followed through each module that is in the repository,
    plus each package's __init__."""
    out, todo = set(entries), list(entries)
    while todo:
        folder, module = todo.pop()
        path = ROOT / folder / f"{module}.py"
        if not path.exists():           # incidents/incident.py: copied from ml/diagnosis/ at build time
            continue
        for found in imports(path.read_text(), folder) - out:
            out.add(found)
            todo.append(found)
    return out | {(folder, "__init__") for folder, _ in out if folder in PACKAGES}


def copied(makefile):
    """The (folder, module) pairs a Makefile copies: each `$(ROOT)/<folder>/<module>.py` it reads, and each
    `$(ARTIFACTS_DIR)/<folder>/<module>.py` it writes (incident.py, renamed on the way)."""
    text = (ROOT / makefile).read_text()
    return (set(re.findall(r"\$\(ROOT\)/(\w+)/(\w+)\.py", text))
            | set(re.findall(r"\$\(ARTIFACTS_DIR\)/(\w+)/(\w+)\.py", text)))


@pytest.mark.parametrize("name", sorted(BUILDS))
def test_each_bundle_has_every_module_its_code_imports(name):
    makefile, entries = BUILDS[name]
    missing = needed(entries) - copied(makefile)
    assert not missing, f"{makefile} doesn't copy {sorted(f'{f}/{m}.py' for f, m in missing)}"


def test_the_bundles_need_what_they_are_known_to_need():
    # A check on the walk itself: these are the modules each bundle is known to need (2026-11-09; the
    # dashboard's live_source, M8-5).
    assert needed(BUILDS["anomalypulse-incidents"][1]) == {
        ("incidents", m) for m in ("__init__", "builder", "record", "core", "handler", "incident")} | {
        ("genai", m) for m in ("__init__", "contract", "affected", "self_check")}
    assert needed(BUILDS["anomalypulse-dashboard"][1]) == {
        ("dashboard", m) for m in ("handler", "builder", "mock_source", "live_source")} | {
        ("incidents", m) for m in ("__init__", "record", "builder")} | {
        ("genai", m) for m in ("__init__", "contract", "affected", "self_check")}


def test_imports_are_found_in_every_form():
    source = ("from genai import affected, contract\nfrom genai.self_check import kind\n"
              "    from incidents import incident   # a comment\nimport mock_source  # another\n"
              "from builder import build_dashboard\nimport json\n")
    assert imports(source, "incidents") == {("genai", "affected"), ("genai", "contract"), ("genai", "self_check"),
                                            ("incidents", "incident")}
    assert imports(source, "dashboard") == {("genai", "affected"), ("genai", "contract"), ("genai", "self_check"),
                                            ("incidents", "incident"), ("dashboard", "mock_source"),
                                            ("dashboard", "builder")}
