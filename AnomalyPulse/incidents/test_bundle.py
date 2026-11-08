"""The Lambda bundle (incidents/Makefile) has every genai module the Lambda imports. A module left out
imports fine in a checkout and fails only on AWS, on the first event. No AWS.

Run from AnomalyPulse/:  python3 -m pytest incidents/test_bundle.py -q
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENAI = HERE.parent / "genai"
LAMBDA = ("builder", "record", "core", "handler")     # what the Makefile copies from incidents/


def genai_imports(source):
    """The genai modules a source file imports: `from genai.x import ...` and `from genai import x, y`."""
    names = set(re.findall(r"^from genai\.(\w+) import", source, re.MULTILINE))
    for group in re.findall(r"^from genai import ([\w, ]+)$", source, re.MULTILINE):
        names |= {name.strip() for name in group.split(",")}
    return names


def test_the_bundle_has_every_genai_module_the_lambda_imports():
    copied = set(re.findall(r"\$\(ROOT\)/genai/(\w+)\.py", (HERE / "Makefile").read_text()))
    needed = set().union(*(genai_imports((HERE / f"{name}.py").read_text()) for name in LAMBDA))
    todo = set(needed)
    while todo:                                        # and what those import in turn
        more = genai_imports((GENAI / f"{todo.pop()}.py").read_text()) - needed
        needed |= more
        todo |= more
    assert needed == {"contract", "affected", "self_check"}
    assert needed <= copied, f"incidents/Makefile doesn't copy genai/{sorted(needed - copied)}"


def test_imports_are_found_in_both_forms():
    assert genai_imports("from genai import affected, contract\nfrom genai.self_check import kind\n") == {
        "affected", "contract", "self_check"}
