"""A profit center's own aggregates into silver_aggregate rows (no database)."""

from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from ahi_silver import aggregate, catalog, profit_center
from ahi_silver.transform import Context, LoadInfo

ROOT = Path(__file__).resolve().parents[1]
CATALOG = catalog.load_plain(ROOT / "config" / "silver_aggregate_columns.csv")
PARTNERS = [("Accident Fund", "9928.42", "5593.17", "443.81"), ("CompSource Mutual", "2643.97", "1375.23", "0"),
            ("Convex UK", "0", "4036.57", "0")]


def _bronze() -> pl.DataFrame:
    """Two months of PC2030's summary as Bronze keeps it: text, with each row's sheet and month."""
    rows = []
    for sheet, month in (("January 2026", "2026-01"), ("March 2026", "2026-03")):
        for name, casualty, prop, workers in PARTNERS:
            total = round(float(casualty) + float(prop) + float(workers), 2)
            rows.append((name, casualty, prop, workers, str(total), "load-1", "summary.xlsx", sheet, month))
        sums = [round(sum(float(row[index]) for row in PARTNERS), 2) for index in (1, 2, 3)]
        rows.append(("TOTALS", *(str(value) for value in sums), str(round(sum(sums), 2)), "load-1", "summary.xlsx",
                     sheet, month))
    names = ["delegated_authority_partner", "casualty_treaty", "property_treaty", "workers_comp", "totals",
             *aggregate.LINEAGE]
    return pl.DataFrame(rows, schema={name: pl.String for name in names}, orient="row")


CONTEXT = Context(
    source_system="pc2030", source_table="ahi_bronze.ext_pc2030_data_agg",
    processed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    loads={"load-1": LoadInfo("PC2030_Summary_07312026.xlsx", datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
                              "2026-01", "2026-06", "PC2030", date(2026, 1, 1), date(2026, 6, 30), "YTD",
                              date(2026, 7, 31))},
)
HEADERS = {"casualty_treaty": "Casualty Treaty", "property_treaty": "Property Treaty", "workers_comp": "Workers Comp"}
SPREAD = aggregate.Spread(["casualty_treaty", "property_treaty", "workers_comp"], "premium", "product_line_name")
LOTL = profit_center.Lotl.from_rows([("2030", "Waypoint Programs", "Active")])


def _run(spread=SPREAD, mapping=(("delegated_authority_partner", "delegated_authority_partner"),)):
    return aggregate.transform(_bronze(), list(mapping), spread, CATALOG, "PC2030", LOTL, CONTEXT, HEADERS, extras=True)


def test_the_spread_columns_are_unpivoted_and_the_totals_left_out():
    rows, quality = _run()
    # 3 partners x 3 product lines x 2 months; the TOTALS rows and the totals column are not loaded.
    assert rows.height == 18 and quality["rollup_rows"] == 2 and quality["rows"] == 8
    assert set(rows["product_line_name"]) == {"Casualty Treaty", "Property Treaty", "Workers Comp"}
    assert "TOTALS" not in set(rows["delegated_authority_partner"])
    accident = rows.filter((pl.col("delegated_authority_partner") == "Accident Fund")
                           & (pl.col("product_line_name") == "Property Treaty") & (pl.col("reporting_month") == 3))
    assert float(accident["premium"][0]) == 5593.17
    # The figures as reported: their sum is the TOTALS the file gave, never doubled.
    assert round(quality["measures"]["premium"], 2) == round(2 * (9928.42 + 5593.17 + 443.81 + 2643.97 + 1375.23 + 4036.57), 2)


def test_each_row_is_dated_by_its_sheet_and_carries_the_profit_center_and_lineage():
    rows, _ = _run()
    first = rows.row(0, named=True)
    assert (first["reporting_start_date"], first["reporting_end_date"], first["reporting_period"]) == (
        date(2026, 1, 1), date(2026, 1, 31), "2026-01")
    assert (first["record_grain"], first["report_type"], first["agg_data_source"]) == (
        "AS_REPORTED", "SOURCE_AGGREGATE", "bronze_aggregate")
    assert (first["profit_center_number"], first["profit_center_name"]) == ("2030", "Waypoint Programs")
    assert (first["aggregation_dimension_1_name"], first["aggregation_dimension_1_value"]) == (
        "delegated_authority_partner", "Accident Fund")
    assert (first["aggregation_dimension_2_name"], first["aggregation_dimension_2_value"]) == (
        "product_line_name", "Casualty Treaty")
    assert first["aggregation_category"] == "DELEGATED_AUTHORITY_PARTNER x PRODUCT_LINE_NAME"
    assert (first["source_file"], first["source_sheet"], first["file_date"]) == (
        "PC2030_Summary_07312026.xlsx", "January 2026", date(2026, 7, 31))
    assert first["source_data_period_type"] == "DATE_RANGE" and first["_ingestion_id"] == "load-1"
    assert len(set(rows["business_key_hash"])) == rows.height


def test_without_a_named_dimension_the_headers_fill_a_generic_slot():
    rows, quality = _run(aggregate.Spread(SPREAD.columns, "premium", None, "line of business"))
    assert rows["product_line_name"].null_count() == rows.height
    assert set(rows["aggregation_dimension_2_name"]) == {"line of business"}
    assert quality["spread"]["dimension"] == "line of business"


def test_only_columns_the_load_does_not_fill_are_targets():
    names = {column.name for column in aggregate.targets(CATALOG)}
    assert {"delegated_authority_partner", "product_line_name", "premium", "policy_count"} <= names
    assert not names & {"record_grain", "source_table", "reporting_period", "row_hash", "ahi_aggregate_id"}
