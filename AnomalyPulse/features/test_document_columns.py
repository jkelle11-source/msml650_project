"""Tests for the feature-matrix documentation generator (M4-5)."""
import pytest

from features import document_columns as dc
from features import guard


def test_every_column_has_a_definition():
    for col in dc.columns():
        dc.describe(col)


def test_undocumented_column_is_refused():
    with pytest.raises(ValueError):
        dc.describe("mystery_column")


def test_table_has_one_row_per_column():
    assert dc.render().count("\n| `") == len(dc.columns())


def test_documented_columns_pass_the_boundary_guard():
    guard.check_columns(dc.columns())


def test_the_committed_doc_is_current():
    # Docs/FEATURE_MATRIX.md is generated; regenerate it with python -m features.document_columns.
    assert dc.OUT.read_text() == dc.render()


def test_throttle_count_is_described_as_a_cloudwatch_count():
    definition, source = dc.describe("product_throttled_requests")
    assert "CloudWatch" in definition and "CloudWatch" in source