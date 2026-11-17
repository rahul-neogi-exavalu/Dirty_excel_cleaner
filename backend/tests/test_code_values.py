"""Code values (ZIP+4, hyphenated ids) must never be blanked by a numeric read."""

import polars as pl
import pytest

from ahi_clean import coerce, contracts
from conftest import HEADER, one, records


@pytest.mark.parametrize(
    "values",
    [
        ["75230", "78759", "59808-1658", "75231"],
        ["1200", "1350", "12-345", "990"],
        ["10", "20", "7/1", "30"],
    ],
)
def test_a_column_holding_a_code_with_separators_stays_text(values):
    result = coerce.coerce_column("zip", values)
    assert result.series.dtype == pl.String
    assert result.series.to_list() == values
    assert not result.failures


@pytest.mark.parametrize("values", [["1 234", "5 678", "910"], ["-5", "12", "7.5"]])
def test_ordinary_numbers_are_still_numbers(values):
    assert coerce.coerce_column("amount", values).series.dtype in (pl.Int64, pl.Float64)


@pytest.mark.parametrize(
    "values",
    [
        ["9906-78-32", "9906-78-32", "9906-79-21", "620756", "9906-79-21"],  # ids shaped like dates
        ["$250,000", "$1,000,0", "$500,000", "$1,500,0", "$750,000"],          # amounts cut short
    ],
)
def test_a_column_is_kept_as_text_rather_than_blank_a_value(values):
    result = coerce.coerce_column("c", values)
    assert result.series.dtype == pl.String
    assert result.series.to_list() == values
    assert not result.failures


def test_a_missing_marker_may_still_be_blanked():
    result = coerce.coerce_column("amount", ["10.5", "NULL", "20.25", "3.5"])
    assert result.series.dtype == pl.Float64
    assert result.series.null_count() == 1


def test_a_blanked_value_with_digits_is_a_violation():
    result = one([HEADER] + records(6))
    result.trace["coercion_failures"] = [
        {"column": "zip", "value": "59808-1658", "reason": "not numeric"},
    ]
    assert contracts.VALUE_NULLED in {v.check for v in contracts.check_result(result)}


@pytest.mark.parametrize("value", ["NULL", "0", "N/A"])
def test_blanking_a_missing_marker_is_not_a_violation(value):
    result = one([HEADER] + records(6))
    result.trace["coercion_failures"] = [{"column": "d", "value": value, "reason": "unparseable date"}]
    assert contracts.VALUE_NULLED not in {v.check for v in contracts.check_result(result)}
