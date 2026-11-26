"""A rule's logic as SQL and back (ahi_silver.rule_sql): the SQL view never changes a rule."""

from __future__ import annotations

import glob
from pathlib import Path

import polars as pl
import pytest
import yaml

from ahi_silver import rule_sql, rules
from ahi_silver.rule_sql import NoSql, SqlError, canonical, from_sql, to_sql

SEEDS = sorted(glob.glob(str(Path(__file__).resolve().parents[1] / "config" / "pc_rules" / "*.yaml")))


def _seed_rules():
    for path in SEEDS:
        for rule in (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("rules") or []:
            yield rule


def _columns(node, found: set):
    if isinstance(node, str) and node.startswith("src:"):
        found.add(node[4:])
    elif isinstance(node, dict):
        for value in node.values():
            _columns(value, found)
    elif isinstance(node, list):
        for value in node:
            _columns(value, found)
    return found


def _frame(spec: dict) -> pl.DataFrame:
    """Rows of the kinds of values the rules read, in every column the spec names."""
    samples = ["Policy Fee", "renewal", "RENEWAL 2026", "New", "", None, "12.5", "-3", "0", "2026-07-01",
               "07/01/2026", "N/A", "abc123", "Rwl", "REN"]
    return pl.DataFrame({name: samples for name in sorted(_columns(spec, set()))} or {"_": samples})


def test_every_seed_rule_with_logic_has_an_exact_sql_form():
    counts = {}
    for rule in _seed_rules():
        kind, spec = rule["kind"], rule.get("spec") or {}
        if kind not in rule_sql.SQL_KINDS:
            with pytest.raises(NoSql):
                to_sql(kind, spec)
            continue
        text = to_sql(kind, spec)
        assert canonical(kind, from_sql(kind, text, spec)) == canonical(kind, spec), rule["key"]
        counts[kind] = counts.get(kind, 0) + 1
    assert counts == {"constant": 32, "derive": 17, "translate": 9, "filter": 4}


def test_the_sql_runs_exactly_as_the_json_does():
    """Not just equal on paper: each derive and filter, run by the engine from its JSON
    and from its SQL read back, gives the same value on every row."""
    for rule in _seed_rules():
        kind, spec = rule["kind"], rule.get("spec") or {}
        if kind not in (rules.DERIVE, rules.FILTER):
            continue
        again = from_sql(kind, to_sql(kind, spec), spec)
        frame = _frame(spec)
        if kind == rules.DERIVE:
            before, after = spec["expr"], again["expr"]
        else:
            before = spec.get("keep", {"op": "not", "args": [spec.get("drop")]})
            after = again.get("keep", {"op": "not", "args": [again.get("drop")]})
        run = [rules.evaluate(node, rules.Env(frame, None, frame.height)).to_list() for node in (before, after)]
        assert run[0] == run[1], rule["key"]


def test_a_derive_rule_reads_as_sql():
    spec = {"expr": {"op": "when", "else": 0, "cases": [
        {"if": {"op": "eq", "args": ["src:LineOfBusiness", "Policy Fee"]}, "then": "src:PolicyFees"}]}}
    text = to_sql("derive", spec)
    assert text == ("CASE\n  WHEN src.\"LineOfBusiness\" = 'Policy Fee' THEN src.\"PolicyFees\"\n  ELSE 0\nEND")
    assert from_sql("derive", text) == spec


@pytest.mark.parametrize("text, expected", [
    ("src.\"Premium\" * 0.1", {"op": "mul", "args": ["src:Premium", 0.1]}),
    ("Premium / silver.policy_count", {"op": "div_safe", "args": ["src:Premium", "silver:policy_count"]}),
    ("a + b + c", {"op": "add", "args": ["src:a", "src:b", "src:c"]}),
    ("(a + b) * c", {"op": "mul", "args": [{"op": "add", "args": ["src:a", "src:b"]}, "src:c"]}),
    ("COALESCE(a, 'none')", {"op": "coalesce", "args": ["src:a", "none"]}),
    ("NULLIF(NULLIF(a, 'N/A'), '-')", {"op": "null_if", "args": ["src:a"], "values": ["N/A", "-"]}),
    ("a || ' ' || b", {"op": "concat", "args": ["src:a", " ", "src:b"]}),
    ("CONCAT_WS(' | ', a, b)", {"op": "concat", "args": ["src:a", "src:b"], "sep": " | "}),
    ("d + INTERVAL '3 months'", {"op": "add_months", "args": ["src:d"], "months": 3}),
    ("d - INTERVAL '1 year'", {"op": "add_months", "args": ["src:d"], "months": -12}),
    ("EXTRACT(YEAR FROM d)", {"op": "year", "args": ["src:d"]}),
    ("CAST(a AS NUMERIC(12, 2))", {"op": "to_number", "args": ["src:a"]}),
    ("a::text", {"op": "to_text", "args": ["src:a"]}),
    ("ROUND(a)", {"op": "round", "args": ["src:a"], "digits": 0}),
    ("LPAD(a, 5, '0')", {"op": "lpad", "args": ["src:a"], "width": 5, "fill": "0"}),
    ("REGEXP_REPLACE(a, '(\\d+)-x', '\\1', 'g')",
     {"op": "regex_replace", "args": ["src:a"], "pattern": "(\\d+)-x", "replacement": "$1"}),
    ("a BETWEEN 1 AND 5", {"op": "and", "args": [{"op": "ge", "args": ["src:a", 1]}, {"op": "le", "args": ["src:a", 5]}]}),
    ("a ILIKE '%fee%' OR a ILIKE '%tax%'", {"op": "contains", "args": ["src:a"], "value": ["fee", "tax"]}),
    ("a LIKE 'Pol%'", {"op": "starts_with", "args": ["src:a"], "value": "Pol", "case_insensitive": False}),
    ("a NOT IN ('x', 2)", {"op": "not", "args": [{"op": "in", "args": ["src:a"], "values": ["x", 2]}]}),
    ("a IS NOT NULL AND NOT b = 'y'", {"op": "and", "args": [{"op": "not_null", "args": ["src:a"]},
                                                              {"op": "not", "args": [{"op": "eq", "args": ["src:b", "y"]}]}]}),
    ("'src:not a column'", {"lit": "src:not a column"}),
    ("-- a comment\n -2.5", -2.5),
])
def test_sql_becomes_the_json_the_engine_runs(text, expected):
    assert from_sql("derive", text)["expr"] == expected


def test_filters_keep_or_drop():
    assert from_sql("filter", "WHERE src.\"Status\" = 'Issued'") == {"keep": {"op": "eq", "args": ["src:Status", "Issued"]}}
    assert from_sql("filter", "WHERE NOT (Status = 'Void')") == {"drop": {"op": "eq", "args": ["src:Status", "Void"]}}
    assert to_sql("filter", {"drop": {"op": "eq", "args": ["src:Status", "Void"]}}) == "WHERE NOT (src.\"Status\" = 'Void')"
    with pytest.raises(SqlError, match="before Silver values exist"):
        from_sql("filter", "WHERE silver.premium > 0")


def test_translate_is_a_case_over_one_value():
    spec = {"source": "src:Code", "match": "contains",
            "cases": [{"when": ["ren", "rwl"], "then": True}, {"when": None, "then": False}], "default": False}
    text = to_sql("translate", spec)
    assert "src.\"Code\" ILIKE '%ren%' OR src.\"Code\" ILIKE '%rwl%' THEN TRUE" in text
    assert "WHEN src.\"Code\" IS NULL THEN FALSE" in text
    assert canonical("translate", from_sql("translate", text)) == canonical("translate", spec)
    regex = from_sql("translate", "CASE WHEN Code ~ '^R' THEN 'R' END")
    assert regex == {"source": "src:Code", "match": "regex", "case_insensitive": False, "cases": [{"when": "^R", "then": "R"}]}
    with pytest.raises(SqlError, match="mixes them"):
        from_sql("translate", "CASE WHEN a = 'x' THEN 1 WHEN a ILIKE '%y%' THEN 2 END")
    with pytest.raises(SqlError, match="tests one value"):
        from_sql("translate", "CASE WHEN a = 'x' THEN 1 WHEN b = 'y' THEN 2 END")


def test_constants_and_the_mode_carried_over():
    assert from_sql("constant", "'Hartford'", {"value": "x", "mode": "fill_null"}) == {"value": "Hartford", "mode": "fill_null"}
    assert to_sql("constant", {"value": None}) == "NULL"
    with pytest.raises(SqlError, match="derive rule"):
        from_sql("constant", "UPPER('x')")


def test_specs_without_an_exact_sql_form_stay_json():
    with pytest.raises(NoSql, match="setting"):
        to_sql("lookup", {"source": "inline"})
    with pytest.raises(NoSql, match="case-sensitive"):
        to_sql("derive", {"expr": {"op": "eq", "args": ["src:a", "X"], "case_insensitive": False}})
    with pytest.raises(NoSql, match="Fix the JSON first"):
        to_sql("derive", {"expr": {"op": "nope", "args": []}})
    with pytest.raises(NoSql, match="cannot show 'extra'"):
        to_sql("derive", {"expr": 1, "extra": True})


@pytest.mark.parametrize("text, message, start", [
    ("a +", "ends where a value was expected", 3),
    ("UPPER(a", "Expected ) to close UPPER(", 7),
    ("FOO(a)", "FOO() is not a function", 0),
    ("a LIKE '%x%y%'", "'%text%' (contains)", 7),
    ("CASE WHEN a = 1 THEN 2", "Expected END", 22),
    ("'open", "never closed", 0),
    ("a = 1 b", "Unexpected 'b'", 6),
    ("REGEXP_REPLACE(a, 'x', 'y')", "end REGEXP_REPLACE with 'g'", 0),
    ("WHERE a = 1", "filter rule only", 0),
])
def test_errors_say_what_and_where(text, message, start):
    with pytest.raises(SqlError) as raised:
        from_sql("derive", text)
    assert message in raised.value.message
    assert raised.value.start == start
