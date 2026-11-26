"""Transactions or a profit center's own aggregates: told apart by shape (no database)."""

import polars as pl

from ahi_bronze import grain

PARTNERS = [("Accident Fund", 99284023.2, 55931627.87, 4438051.29), ("CompSource Mutual", 26439703.31, 13752290.91, 2075397.81),
            ("Convex UK", 0, 4036571.14, 0), ("Hastings", 6538901.26, 0, 925172.35)]


def _summary(months=("January 2026", "March 2026")) -> pl.DataFrame:
    """Premium per partner and product line, a month to a sheet, with a TOTALS column and
    a TOTALS row per sheet -- as the cleaner leaves PC2030's Waypoint summary (all text)."""
    rows = []
    for month in months:
        for name, casualty, prop, workers in PARTNERS:
            rows.append((month, name, casualty, prop, workers, casualty + prop + workers))
        sums = [sum(row[index] for row in PARTNERS) for index in (1, 2, 3)]
        rows.append((month, "TOTALS", *sums, sum(sums)))
    columns = ["source_sheet", "delegated_authority_partner", "casualty_treaty", "property_treaty", "workers_comp", "totals"]
    return pl.DataFrame([[str(value) for value in row] for row in rows], schema={name: pl.String for name in columns},
                        orient="row")


OWN = ["delegated_authority_partner", "casualty_treaty", "property_treaty", "workers_comp", "totals"]


def test_a_summary_with_totals_and_no_transactions_is_aggregated():
    found = grain.detect(_summary(), OWN, keyed=False, dated=False, group="source_sheet")
    assert found.aggregated
    assert found.total_column == "totals" and found.parts == ["casualty_treaty", "property_treaty", "workers_comp"]
    assert found.total_rows == [4, 9]  # each sheet's TOTALS row
    assert found.dimensions == ["delegated_authority_partner"] and len(found.measures) == 4
    assert all(signal["ok"] for signal in found.signals)


def test_a_policy_number_or_row_dates_mean_transactions():
    keyed = grain.detect(_summary(), OWN, keyed=True, dated=False, group="source_sheet")
    dated = grain.detect(_summary(), OWN, keyed=False, dated=True, group="source_sheet")
    assert not keyed.aggregated and not dated.aggregated
    assert next(s for s in keyed.signals if s["id"] == "no_key")["ok"] is False


def test_amounts_without_any_total_are_not_taken_for_a_summary():
    frame = _summary().drop("totals").filter(pl.col("delegated_authority_partner") != "TOTALS")
    found = grain.detect(frame, OWN[:-1], keyed=False, dated=False, group="source_sheet")
    assert not found.aggregated and found.total_column is None and found.total_rows == []


def test_a_sheet_with_one_record_keeps_it_and_takes_the_last_row_as_the_total():
    frame = pl.DataFrame({"name": ["A", "B", "C", "TOTALS"], "x": ["5", "0", "0", "5"], "y": ["2", "0", "0", "2"]})
    assert grain.total_rows(frame, ["x", "y"]) == [3]


def test_numbers_read_with_thousands_separators_and_text_is_null():
    assert grain.numbers(pl.Series(["1,250.5", "0", "n/a", None])).to_list() == [1250.5, 0.0, None, None]
