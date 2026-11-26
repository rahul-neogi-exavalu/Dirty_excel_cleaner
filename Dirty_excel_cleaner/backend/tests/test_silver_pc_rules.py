"""Per-profit-center rules (filters, date formats, lookups, translations, derivations,
constants), without a database. The cases are the July 2026 notebooks' logic."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from ahi_silver import aggregate, catalog, profit_center, rules, transform
from ahi_silver.rules import Rule, RuleError

ROOT = Path(__file__).resolve().parents[1]
CATALOG = catalog.load(ROOT / "config" / "silver_columns.csv")
AGG_CATALOG = catalog.load_plain(ROOT / "config" / "silver_aggregate_columns.csv")
ALLOWED = rules.allowed_columns(CATALOG, AGG_CATALOG)
LOTL = profit_center.Lotl.from_rows([("2041", "Pearl", "Active"), ("2007", "CPro", "Active"),
                                     ("2002", "EE Hall", "Active"), ("2022", "Equity Partners", "Active"),
                                     ("0792", "ARMR", "Active"), ("0692", "Direct Bill", "Active")])


def _frame(pc: str, **columns) -> tuple[pl.DataFrame, transform.Context]:
    height = len(next(iter(columns.values())))
    frame = pl.DataFrame({**{name: pl.Series(name, values, dtype=pl.String) for name, values in columns.items()},
                          "_ingestion_id": ["i1"] * height, "_source_file": [f"{pc}_July.xlsx"] * height,
                          "_source_sheet": ["Sheet1"] * height})
    context = transform.Context(f"pc{pc[2:]}", f"bronze.ext_{pc.lower()}_data", None,
                                {"i1": transform.LoadInfo(f"{pc}_July.xlsx", pc_id=pc)})
    return frame, context


def _rule(key, kind, spec, column=None, pc="PC2041", **scope) -> Rule:
    return rules.parse({"key": key, "kind": kind, "spec": spec, "column": column, **scope}, ALLOWED, pc)


# --- validation ---------------------------------------------------------------------


def test_a_rule_writes_only_columns_its_target_has():
    with pytest.raises(RuleError, match="not a transaction column"):
        _rule("pc2041.x", "constant", {"value": "x"}, "record_grain", target="transaction")
    # An aggregate-only column is fine for an aggregate rule.
    assert _rule("pc2041.grain", "constant", {"value": "CARRIER"}, "record_grain", target="aggregate")
    with pytest.raises(RuleError, match="not an aggregate column"):
        _rule("pc2041.hash", "constant", {"value": "x"}, "row_hash", target="aggregate")


def test_a_filter_reads_only_bronze_and_ops_are_checked():
    with pytest.raises(RuleError, match="use src: columns"):
        _rule("pc2041.f", "filter", {"keep": {"op": "eq", "args": ["silver:policy_status", "x"]}})
    with pytest.raises(RuleError, match="unknown op"):
        _rule("pc2041.d", "derive", {"expr": {"op": "eval", "args": ["1"]}}, "premium")
    with pytest.raises(RuleError, match="lpad needs width"):
        _rule("pc2041.d", "derive", {"expr": {"op": "lpad", "args": ["src:pc"]}}, "profit_center_number")
    with pytest.raises(RuleError, match="exactly one of 'keep' or 'drop'"):
        _rule("pc2041.f", "filter", {})


def test_every_seed_file_parses_against_the_catalogs():
    files = sorted((ROOT / "config" / "pc_rules").glob("*.yaml"))
    assert files, "no seed files"
    keys = set()
    for path in files:
        for rule in rules.load_seed(path, ALLOWED):
            assert (rule.profit_center, rule.key) not in keys
            keys.add((rule.profit_center, rule.key))
            assert rule.source_ref, f"{rule.key}: say which notebook it came from"


# --- transaction rules ----------------------------------------------------------------


def test_retention_keeps_issued_rows_and_asserts_the_renewal():
    frame, context = _frame("PC2041", status=["Issued", "Lost", "ISSUED"], pol=["P1", "P2", "P3"])
    ruleset = [
        _rule("pc2041.issued", "filter", {"keep": {"op": "eq", "args": ["src:status", "issued"]}}),
        _rule("pc2041.detail", "constant", {"value": "Renewal"}, "transaction_detail"),
        _rule("pc2041.flag", "constant", {"value": True}, "renewal_flag"),
    ]
    silver, quality = transform.transform(frame, [("pol", "policy_number")], CATALOG, "PC2041", LOTL, context,
                                          rules=ruleset)
    assert silver["policy_number"].to_list() == ["P1", "P3"]
    assert set(silver["transaction_detail"]) == {"Renewal"} and set(silver["renewal_flag"]) == {True}
    assert quality["rows_filtered"] == 1 and quality["rows"] == 2
    stats = {stat["key"]: stat for stat in quality["rules"]}
    assert stats["pc2041.issued"]["rows_dropped"] == 1
    assert stats["pc2041.flag"]["rows_changed"] == 2
    assert "renewal_flag" not in quality["unmapped_silver_columns"]


def test_a_rule_on_a_column_the_file_lacks_changes_nothing_and_says_so():
    # The July Preferra file has no 'Renewal Sales Cycle Status': the filter must not drop
    # every row, and a derivation must not overwrite mapped values with NULL.
    frame, context = _frame("PC2041", pol=["P1", "P2"], pct=["0.1", "0.2"])
    ruleset = [
        _rule("pc2041.issued", "filter", {"keep": {"op": "eq", "args": ["src:Renewal Sales Cycle Status", "ISSUED"]}}),
        _rule("pc2041.pct", "derive", {"expr": {"op": "div_safe", "args": ["src:Commission", "silver:premium"]}},
              "commission_pct"),
    ]
    silver, quality = transform.transform(frame, [("pol", "policy_number"), ("pct", "commission_pct")], CATALOG,
                                          "PC2041", LOTL, context, rules=ruleset)
    assert silver.height == 2 and quality["rows_filtered"] == 0
    assert silver["commission_pct"].to_list() == [Decimal("0.100000"), Decimal("0.200000")]
    stats = {stat["key"]: stat for stat in quality["rules"]}
    assert stats["pc2041.issued"]["missing_sources"] == ["Renewal Sales Cycle Status"]
    assert stats["pc2041.pct"]["missing_sources"] == ["Commission"] and stats["pc2041.pct"]["rows_changed"] == 0


def test_a_translation_maps_codes_and_keeps_unmatched_values_without_a_default():
    frame, context = _frame("PC2041", result=["successful", "lapsed", None])
    rule = _rule("pc2041.status", "translate", {"source": "src:Result", "cases": [
        {"when": "successful", "then": "Renewed"}], "default": "Not Renewed"}, "policy_status")
    silver, _ = transform.transform(frame, [], CATALOG, "PC2041", LOTL, context, rules=[rule])
    # The notebook: null stays null; 'successful' -> Renewed; anything else -> Not Renewed.
    assert silver["policy_status"].to_list() == ["Renewed", "Not Renewed", None]

    keep = _rule("pc2041.status2", "translate", {"source": "src:Result", "cases": [
        {"when": "successful", "then": "Renewed"}]}, "policy_status")
    silver, _ = transform.transform(frame, [("result", "policy_status")], CATALOG, "PC2041", LOTL, context,
                                    rules=[keep])
    assert silver["policy_status"].to_list() == ["Renewed", "lapsed", None]


def test_a_rule_applies_only_to_its_own_profit_center_and_file():
    frame = pl.DataFrame({"tran": ["RWL", "RWL"], "_ingestion_id": ["a", "b"],
                          "_source_file": ["CPro July Billing Register.xlsx", "Other.xlsx"],
                          "_source_sheet": ["S", "S"]})
    context = transform.Context("pc2007", "bronze.ext_pc2007_x", None, {
        "a": transform.LoadInfo("CPro July Billing Register.xlsx", pc_id="PC2007"),
        "b": transform.LoadInfo("Other.xlsx", pc_id="PC2007")})
    rule = _rule("pc2007.rwl", "translate", {"source": "src:Tran", "match": "equals",
                                             "cases": [{"when": "RWL", "then": True}], "default": False},
                 "renewal_flag", pc="PC2007", scope_file_like="*billing register*")
    silver, quality = transform.transform(frame, [], CATALOG, "PC2007", LOTL, context, rules=[rule])
    assert silver["renewal_flag"].to_list() == [True, None]
    assert quality["rules"][0]["rows_in_scope"] == 1

    other_pc = _rule("pc2041.rwl", "constant", {"value": True}, "renewal_flag")
    silver, quality = transform.transform(frame, [], CATALOG, "PC2007", LOTL, context, rules=[other_pc])
    assert silver["renewal_flag"].null_count() == 2 and quality["rules"] == []


def test_derived_ratios_guard_a_zero_premium_and_fees_follow_the_line_of_business():
    frame, context = _frame("PC0692", prem=["1000", "0", "(500)"], mga=["150", "10", "-50"],
                            lob=["Policy Fee", "GL", "Policy Fee"], fee=["25", "30", "$1,000.00"])
    ruleset = [
        _rule("pc0692.pct", "derive", {"expr": {"op": "div_safe", "args": ["src:mga", "silver:premium"]}},
              "commission_pct", pc="PC0692"),
        _rule("pc0692.fees", "derive", {"expr": {"op": "when", "cases": [
            {"if": {"op": "eq", "args": ["src:lob", "Policy Fee"]}, "then": "src:fee"}], "else": 0}},
            "policy_fees", pc="PC0692"),
    ]
    silver, quality = transform.transform(frame, [("prem", "premium")], CATALOG, "PC0692", LOTL, context,
                                          rules=ruleset)
    assert silver["commission_pct"].to_list() == [Decimal("0.150000"), None, Decimal("0.100000")]
    assert silver["policy_fees"].to_list() == [Decimal("25.00"), Decimal("0.00"), Decimal("1000.00")]
    assert all(stat["unreadable"] == 0 for stat in quality["rules"])


def test_a_day_first_file_is_read_day_first_only_where_the_rule_applies():
    frame, context = _frame("PC2001", eff=["05/07/2026", "31/12/2026", "2026-01-02"])
    rule = _rule("pc2001.eff", "date_format", {"formats": ["dd/MM/yyyy"]}, "policy_effective_date", pc="PC2001")
    silver, quality = transform.transform(frame, [("eff", "policy_effective_date")], CATALOG, "PC2001", LOTL,
                                          context, rules=[rule])
    assert silver["policy_effective_date"].to_list() == [date(2026, 7, 5), date(2026, 12, 31), date(2026, 1, 2)]
    assert silver["policy_effective_month"].to_list() == [7, 12, 1]  # derived after the rule
    assert "policy_effective_date" not in quality["invalid_values"]  # 31/12/2026 is read now
    without, quality = transform.transform(frame, [("eff", "policy_effective_date")], CATALOG, "PC2001", LOTL,
                                           context)
    assert without["policy_effective_date"].to_list()[:2] == [date(2026, 5, 7), None]


def test_a_rule_given_number_is_completed_from_the_lotl_and_changes_the_hash():
    frame, context = _frame("PC2002", pcname=["Equity Partners", "EE Hall", "Unknown"], pol=["A", "B", "C"])
    rule = _rule("pc2002.number", "translate", {"source": "src:pcname", "cases": [
        {"when": "equity partners", "then": "2022"}, {"when": "ee hall", "then": "2002"}]},
        "profit_center_number", pc="PC2002")
    mapping = [("pcname", "profit_center_name"), ("pol", "policy_number")]
    silver, quality = transform.transform(frame, mapping, CATALOG, "PC2002", LOTL, context, rules=[rule],
                                          extras=True)
    assert silver["profit_center_number"].to_list() == ["2022", "2002", None]
    # The LOTL agrees with the rule, so the numbers are kept rather than corrected.
    assert silver["_pc_status"].to_list()[:2] == ["kept", "kept"]

    # A business-key column set by a rule is part of the hash.
    carrier = _rule("pc2002.carrier", "constant", {"value": "Arrowhead"}, "insurance_company_name", pc="PC2002")
    plain, _ = transform.transform(frame, mapping, CATALOG, "PC2002", LOTL, context, rules=[rule])
    ruled, _ = transform.transform(frame, mapping, CATALOG, "PC2002", LOTL, context, rules=[rule, carrier])
    assert all(a != b for a, b in zip(plain["business_key_hash"], ruled["business_key_hash"]))


def test_an_inline_lookup_fills_addresses_with_a_default_and_fill_null_keeps_source_values():
    frame, context = _frame("PC2041", agency=["Acme - Travis Cutter", "Other", "Chip Treadway Co"],
                            addr=[None, "1 Main St", None])
    rule = _rule("pc2041.addr", "lookup", {
        "source": "inline", "key": "src:agency", "match": "contains", "mode": "fill_null",
        "columns": {"producer_billing_address": "address", "producer_billing_city": "city"},
        "entries": [{"key": "Travis Cutter", "address": "220 Emerson Place STE 200", "city": "Davenport"},
                    {"key": "Chip Treadway", "address": "1300 S Main Street", "city": "Tulsa"}]},
        pc="PC2041")
    silver, _ = transform.transform(frame, [("addr", "producer_billing_address")], CATALOG, "PC2041", LOTL, context,
                                    rules=[rule])
    assert silver["producer_billing_address"].to_list() == ["220 Emerson Place STE 200", "1 Main St",
                                                            "1300 S Main Street"]
    assert silver["producer_billing_city"].to_list() == ["Davenport", None, "Tulsa"]


def test_a_bronze_table_lookup_reads_the_rows_the_service_hands_over():
    frame, context = _frame("PC2007", broker=["ACME INS ", "nobody"])
    rule = _rule("pc2007.broker", "lookup", {
        "source": "bronze_table", "table": "ext_pc2007_brokers", "key": "src:broker", "key_to": "broker",
        "columns": {"producer_billing_city": "city"}, "default": {"producer_billing_city": "Trent Woods"}},
        pc="PC2007")
    lookups = {"pc2007.broker": [{"broker": "Acme Ins", "city": "Raleigh"}, {"broker": "acme ins", "city": "dup"}]}
    silver, _ = transform.transform(frame, [], CATALOG, "PC2007", LOTL, context, rules=[rule], lookups=lookups)
    assert silver["producer_billing_city"].to_list() == ["Raleigh", "Trent Woods"]


def test_a_source_column_is_found_by_its_header_on_a_fresh_frame():
    # Bronze stores snake_case names; the rule names the header the file wrote.
    frame = pl.DataFrame({"decpagetypecode": ["REN", "NEW"], "ab_db": ["AB", "DB"], "renewal_sales_cycle_status": ["x", "y"]})
    headers = {"decpagetypecode": "DecpageTypeCode", "ab_db": "AB/DB",
               "renewal_sales_cycle_status": "Renewal Sales Cycle Status"}
    for name, column in (("DecpageTypeCode", "decpagetypecode"), ("AB/DB", "ab_db"),
                         ("Renewal Sales Cycle Status ", "renewal_sales_cycle_status"), ("Nowhere", None)):
        assert rules.Env(frame, {}, frame.height, headers)._find(name) == column


@pytest.mark.parametrize("expr, expected", [
    ({"op": "lpad", "args": [{"op": "regex_extract", "args": ["src:pc"], "pattern": r"^(\d+)"}], "width": 4},
     ["0094", "8022"]),
    ({"op": "add_months", "args": ["src:eff"], "months": 12}, [date(2027, 7, 1), date(2027, 2, 28)]),
    ({"op": "concat", "args": ["src:a", "src:b"], "sep": " "}, ["x y", "x"]),
    ({"op": "null_if", "args": ["src:a"], "values": ["N/A"]}, ["x", "x"]),
    ({"op": "mul", "args": ["src:amt", 0.15]}, [15.0, None]),
])
def test_expressions(expr, expected):
    frame = pl.DataFrame({"pc": ["94 Dayton", "8022 Equinox"], "eff": ["2026-07-01", "2026-02-28"],
                          "a": ["x", "x"], "b": ["y", None], "amt": ["100", "abc"]})
    values = rules.evaluate(expr, rules.Env(frame, {}, frame.height)).to_list()
    assert values == expected


# --- aggregate rules ---------------------------------------------------------------------


def test_aggregate_rules_set_the_grain_drop_blank_agencies_and_rehash():
    names = ["producer_agency_name", "premium", *aggregate.LINEAGE]
    frame = pl.DataFrame([("Agency A", "100", "i1", "f.xlsx", "S", None), ("", "5", "i1", "f.xlsx", "S", None),
                          ("Agency B", "50", "i1", "f.xlsx", "S", None)],
                         schema={name: pl.String for name in names}, orient="row")
    context = transform.Context("pc0792", "bronze.ext_pc0792_agency", None,
                                {"i1": transform.LoadInfo("f.xlsx", pc_id="PC0792")})
    ruleset = [
        _rule("pc0792.blank", "filter", {"drop": {"op": "is_null", "args": ["src:producer_agency_name"]}},
              pc="PC0792", target="aggregate"),
        _rule("pc0792.grain", "constant", {"value": "AGENT"}, "record_grain", pc="PC0792", target="aggregate"),
        _rule("pc0792.source", "constant", {"value": "EXTERNAL_FILE"}, "agg_data_source", pc="PC0792",
              target="aggregate"),
    ]
    mapping = [("producer_agency_name", "producer_agency_name"), ("premium", "premium")]
    rows, quality = aggregate.transform(frame, mapping, None, AGG_CATALOG, "PC0792", LOTL, context, rules=ruleset)
    assert rows["producer_agency_name"].to_list() == ["Agency A", "Agency B"]
    assert set(rows["record_grain"]) == {"AGENT"} and set(rows["agg_data_source"]) == {"EXTERNAL_FILE"}
    assert quality["rows_filtered"] == 1 and quality["rows_loaded"] == 2
    plain, _ = aggregate.transform(frame, mapping, None, AGG_CATALOG, "PC0792", LOTL, context)
    assert set(plain["record_grain"]) == {"AS_REPORTED"}
    # Same figures, same keys: only rows the filter kept, hashed the same way.
    kept = plain.filter(pl.col("producer_agency_name").is_not_null())
    assert rows["business_key_hash"].to_list() == kept["business_key_hash"].to_list()


def test_transaction_rules_do_not_touch_aggregates():
    rule = _rule("pc0792.flag", "constant", {"value": True}, "renewal_flag", pc="PC0792", target="transaction")
    assert rules.select([rule], rules.AGGREGATE, "bronze.x") == []
    assert rules.select([rule], rules.TRANSACTION, "bronze.x") == [rule]
