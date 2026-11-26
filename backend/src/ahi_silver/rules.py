"""Per-profit-center business rules, applied while bronze rows become Silver rows.

The manual Databricks notebooks carried logic that belongs to one profit center's files:
a column derived from others, a source code translated to a business value, a value the
file never states but the business knows (the carrier, the market, the record grain), a
row filter, an address looked up from a companion file, a date written day-first. Here
that logic is data, not code: each rule is one record, scoped to a profit center and
optionally to one bronze table, file-name pattern or sheet, and to the transaction or
aggregate target.

Kinds, in the order they run for a load:

* ``filter``      -- keep or drop bronze rows, before anything is typed;
* ``date_format`` -- read one Silver date column with the file's own formats first;
* ``lookup``      -- fill Silver columns from a table keyed on a value (inline entries,
  or a bronze table such as a companion broker list);
* ``translate``   -- a source value to a business value, by a list of cases;
* ``derive``      -- an expression over bronze (``src:``) and Silver (``silver:``) values;
* ``constant``    -- a fixed value.

Within a kind, rules run by ``seq``. Every value a rule produces is cast to its Silver
column's type with the same cleansing as a mapped value, so a rule can never write a
value the column cannot hold; one it cannot read becomes NULL and is counted.

Expressions are JSON trees interpreted over polars Series -- never ``eval``. An operand
is ``"src:<bronze column>"``, ``"silver:<silver column>"``, a literal (string, number,
boolean, null, or ``{"lit": value}``) or a node ``{"op": name, "args": [...], ...}``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

import polars as pl

from . import cleanse, normalize, profit_center
from .catalog import SilverColumn

FILTER, DATE_FORMAT, LOOKUP, TRANSLATE, DERIVE, CONSTANT = (
    "filter", "date_format", "lookup", "translate", "derive", "constant")
KINDS = (FILTER, DATE_FORMAT, LOOKUP, TRANSLATE, DERIVE, CONSTANT)
# Kinds that write exactly one Silver column, named by the rule.
COLUMN_KINDS = (DATE_FORMAT, TRANSLATE, DERIVE, CONSTANT)
TRANSACTION, AGGREGATE, BOTH = "transaction", "aggregate", "both"
TARGETS = (TRANSACTION, AGGREGATE, BOTH)
MODES = ("set", "fill_null")
MATCHES = ("equals", "contains", "starts_with", "regex")
LOOKUP_MATCHES = ("equals", "contains")
SEED_USER = "seed"

# Aggregate columns a rule never writes: the key, hashes, lineage and load timestamps.
AGGREGATE_PROTECTED = frozenset({
    "ahi_aggregate_id", "business_key_hash", "row_hash", "source_system", "source_table", "source_file",
    "source_sheet", "ingestion_timestamp", "processed_timestamp", "silver_insert_timestamp",
    "silver_update_timestamp",
})

# The aggregate rows rebuilt from silver_transaction (silver_service.TRANSACTION).
ROLLUP_SOURCE = "silver_transaction"

_KEY = re.compile(r"[a-z0-9][a-z0-9_.\-]*")


class RuleError(ValueError):
    """A rule that cannot be applied as written."""


@dataclass
class Rule:
    key: str  # unique within its profit center: pc2041.retention.issued_only
    profit_center: str  # PC2041
    kind: str
    spec: dict
    column: str | None = None
    target: str = TRANSACTION  # transaction | aggregate | both
    scope_bronze_table: str | None = None  # a glob over the bronze table name
    scope_file_like: str | None = None  # a glob over the source file name
    scope_sheet: str | None = None  # a glob over the source sheet
    seq: int = 100
    enabled: bool = True
    note: str | None = None
    source_ref: str | None = None  # the notebook and line the rule came from
    approved_by: str | None = None
    approved_at: object = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["approved_at"] = str(self.approved_at) if self.approved_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict, profit_center: str | None = None) -> Rule:
        if not isinstance(data, dict):
            raise RuleError(f"a rule must be a mapping, not {type(data).__name__}")
        known = {name for name in cls.__dataclass_fields__}
        unknown = set(data) - known - {"id"}
        if unknown:
            raise RuleError(f"rule {data.get('key')!r}: unknown field(s) {sorted(unknown)}")
        values = {name: data[name] for name in known if name in data}
        values.setdefault("profit_center", profit_center)
        if values.get("spec") is None:
            values["spec"] = {}
        return cls(**values)

    @property
    def targets(self) -> tuple[str, ...]:
        return (TRANSACTION, AGGREGATE) if self.target == BOTH else (self.target,)

    def written(self) -> list[str]:
        """The Silver columns this rule writes."""
        if self.kind in COLUMN_KINDS:
            return [self.column] if self.column else []
        if self.kind == LOOKUP:
            return list((self.spec.get("columns") or {}).keys())
        return []


# --------------------------------------------------------------------------- validation

def normalize_pc(value) -> str | None:
    """PC2041 from 2041, pc2041 or 'PC 2041'; the profit center a rule belongs to."""
    number = profit_center.pc_number(str(value).strip()) if value not in (None, "") else None
    return f"PC{number}" if number else None


def allowed_columns(transaction: list[SilverColumn], aggregate: list[SilverColumn]) -> dict[str, dict[str, SilverColumn]]:
    """Per target, the Silver columns a rule may write: a transaction row's business
    columns; every aggregate column but its key, hashes and lineage."""
    from .catalog import MAPPED

    return {
        TRANSACTION: {column.name: column for column in transaction if column.role == MAPPED},
        AGGREGATE: {column.name: column for column in aggregate if column.name not in AGGREGATE_PROTECTED},
    }


def validate(rule: Rule, allowed: dict[str, dict[str, SilverColumn]] | None = None) -> list[str]:
    """Everything wrong with ``rule`` as written; an empty list when it can be applied."""
    errors: list[str] = []
    if not rule.key or not _KEY.fullmatch(str(rule.key)):
        errors.append(f"key {rule.key!r}: lowercase letters, digits, '_', '-' and '.' only")
    if not normalize_pc(rule.profit_center):
        errors.append(f"profit_center {rule.profit_center!r} is not a profit center (PC2041)")
    if rule.kind not in KINDS:
        errors.append(f"kind {rule.kind!r}: one of {', '.join(KINDS)}")
        return errors
    if rule.target not in TARGETS:
        errors.append(f"target {rule.target!r}: one of {', '.join(TARGETS)}")
    if not isinstance(rule.spec, dict):
        return errors + ["spec must be a mapping"]
    if rule.kind in COLUMN_KINDS and not rule.column:
        errors.append(f"a {rule.kind} rule names the Silver column it writes")
    if rule.kind not in COLUMN_KINDS and rule.column:
        errors.append(f"a {rule.kind} rule does not take 'column'")
    mode = rule.spec.get("mode", "set")
    if mode not in MODES:
        errors.append(f"mode {mode!r}: one of {', '.join(MODES)}")
    try:
        _SPEC_CHECKS[rule.kind](rule)
    except RuleError as error:
        errors.append(str(error))
    if allowed is not None and rule.target in TARGETS:
        for target in rule.targets:
            columns = allowed.get(target, {})
            for name in rule.written():
                if name not in columns:
                    errors.append(f"{name!r} is not {'an' if target == AGGREGATE else 'a'} {target} column a rule can write")
            if rule.kind == DATE_FORMAT and rule.column in columns and columns[rule.column].kind not in ("date", "timestamp"):
                errors.append(f"date_format writes a date column; {rule.column!r} is {columns[rule.column].data_type}")
    return errors


def parse(data: dict, allowed=None, profit_center: str | None = None) -> Rule:
    """A validated Rule from its dict form; RuleError listing every problem otherwise."""
    rule = Rule.from_dict(data, profit_center)
    rule.profit_center = normalize_pc(rule.profit_center) or rule.profit_center
    errors = validate(rule, allowed)
    if errors:
        raise RuleError(f"rule {rule.key!r}: " + "; ".join(errors))
    return rule


def load_seed(path: str | Path, allowed=None) -> list[Rule]:
    """The rules of one seed file: ``profit_center`` and a ``rules`` list (YAML or JSON)."""
    import yaml

    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pc = data.get("profit_center")
    if not normalize_pc(pc):
        raise RuleError(f"{path.name}: 'profit_center' is missing or not a profit center")
    rules, problems, seen = [], [], set()
    for item in data.get("rules") or []:
        try:
            rule = parse(item, allowed, pc)
        except (RuleError, TypeError) as error:
            problems.append(str(error))
            continue
        if normalize_pc(rule.profit_center) != normalize_pc(pc):
            problems.append(f"rule {rule.key!r}: profit_center differs from the file's {pc}")
        if rule.key in seen:
            problems.append(f"rule {rule.key!r}: key used twice")
        seen.add(rule.key)
        rules.append(rule)
    if problems:
        raise RuleError(f"{path.name}: " + " | ".join(problems))
    return rules


def _check_operand(value, where: str, bronze_only: bool = False) -> None:
    if isinstance(value, str):
        if value.startswith("silver:") and bronze_only:
            raise RuleError(f"{where}: a filter runs before Silver values exist; use src: columns")
        if value.startswith(("src:", "silver:")) and not value.split(":", 1)[1].strip():
            raise RuleError(f"{where}: {value!r} names no column")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, dict):
        if "lit" in value and len(value) == 1:
            return
        op = value.get("op")
        if op not in _OPS:
            raise RuleError(f"{where}: unknown op {op!r}")
        _, arity, params = _OPS[op]
        args = value.get("args", [])
        if not isinstance(args, list):
            raise RuleError(f"{where}: {op}.args must be a list")
        low, high = arity
        if len(args) < low or (high is not None and len(args) > high):
            raise RuleError(f"{where}: {op} takes {low}..{high if high is not None else 'n'} args, got {len(args)}")
        missing = [name for name, required in params.items() if required and name not in value]
        if missing:
            raise RuleError(f"{where}: {op} needs {', '.join(missing)}")
        extra = set(value) - {"op", "args"} - set(params)
        if extra:
            raise RuleError(f"{where}: {op} does not take {sorted(extra)}")
        for index, arg in enumerate(args):
            _check_operand(arg, f"{where}.{op}[{index}]", bronze_only)
        if op == "when":
            if not isinstance(value.get("cases"), list) or not value["cases"]:
                raise RuleError(f"{where}: when needs a non-empty 'cases' list")
            for index, case in enumerate(value["cases"]):
                if not isinstance(case, dict) or "if" not in case or "then" not in case:
                    raise RuleError(f"{where}: when.cases[{index}] needs 'if' and 'then'")
                _check_operand(case["if"], f"{where}.when.cases[{index}].if", bronze_only)
                _check_operand(case["then"], f"{where}.when.cases[{index}].then", bronze_only)
            _check_operand(value.get("else"), f"{where}.when.else", bronze_only)
        if op in ("regex_extract", "regex_replace"):
            try:
                re.compile(value["pattern"])
            except re.error as error:
                raise RuleError(f"{where}: {op} pattern does not compile: {error}") from None
        return
    raise RuleError(f"{where}: {value!r} is not an operand")


def _check_filter(rule: Rule) -> None:
    present = [name for name in ("keep", "drop") if name in rule.spec]
    if len(present) != 1:
        raise RuleError("a filter has exactly one of 'keep' or 'drop'")
    _check_operand(rule.spec[present[0]], present[0], bronze_only=True)


def _check_date_format(rule: Rule) -> None:
    formats = rule.spec.get("formats")
    if not isinstance(formats, list) or not formats or not all(isinstance(f, str) and f for f in formats):
        raise RuleError("date_format needs a non-empty 'formats' list")
    if "source" in rule.spec:
        _check_operand(rule.spec["source"], "source", bronze_only=True)


def _check_lookup(rule: Rule) -> None:
    spec = rule.spec
    if spec.get("source") not in ("inline", "bronze_table"):
        raise RuleError("lookup.source is 'inline' or 'bronze_table'")
    if "key" not in spec:
        raise RuleError("lookup needs 'key' (the value looked up)")
    _check_operand(spec["key"], "key")
    if spec.get("match", "equals") not in LOOKUP_MATCHES:
        raise RuleError(f"lookup.match: one of {', '.join(LOOKUP_MATCHES)}")
    columns = spec.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise RuleError("lookup needs 'columns': {silver column: field of the lookup}")
    if spec["source"] == "bronze_table":
        if not spec.get("table") or not spec.get("key_to"):
            raise RuleError("a bronze_table lookup names its 'table' and the 'key_to' column")
    else:
        entries = spec.get("entries")
        if not isinstance(entries, list) or not entries:
            raise RuleError("an inline lookup needs 'entries'")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("key") in (None, ""):
                raise RuleError(f"entries[{index}] needs a 'key'")
    default = spec.get("default")
    if default is not None and not isinstance(default, dict):
        raise RuleError("lookup.default is {silver column: value}")
    if isinstance(default, dict) and set(default) - set(columns):
        raise RuleError(f"lookup.default names columns not in 'columns': {sorted(set(default) - set(columns))}")


def _check_translate(rule: Rule) -> None:
    spec = rule.spec
    if "source" not in spec:
        raise RuleError("translate needs 'source'")
    _check_operand(spec["source"], "source")
    if spec.get("match", "equals") not in MATCHES:
        raise RuleError(f"translate.match: one of {', '.join(MATCHES)}")
    cases = spec.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RuleError("translate needs a non-empty 'cases' list")
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "when" not in case or "then" not in case:
            raise RuleError(f"cases[{index}] needs 'when' and 'then'")
        if spec.get("match") == "regex":
            for pattern in _listed(case["when"]):
                try:
                    re.compile(str(pattern))
                except re.error as error:
                    raise RuleError(f"cases[{index}] pattern does not compile: {error}") from None


def _check_derive(rule: Rule) -> None:
    if "expr" not in rule.spec:
        raise RuleError("derive needs 'expr'")
    _check_operand(rule.spec["expr"], "expr")


def _check_constant(rule: Rule) -> None:
    if "value" not in rule.spec:
        raise RuleError("constant needs 'value' (null is allowed)")
    if rule.column == "agg_data_source" and str(rule.spec["value"]).strip().casefold() == ROLLUP_SOURCE:
        raise RuleError(f"agg_data_source {ROLLUP_SOURCE!r} marks the roll-up the load rebuilds; use another value")


_SPEC_CHECKS = {FILTER: _check_filter, DATE_FORMAT: _check_date_format, LOOKUP: _check_lookup,
                TRANSLATE: _check_translate, DERIVE: _check_derive, CONSTANT: _check_constant}


# --------------------------------------------------------------------------- selection

@dataclass
class RowScope:
    """What a rule's scope is compared with, row by row."""

    pcs: list[str | None]  # padded number of the row's load (0608)
    files: list[str | None]
    sheets: list[str | None]

    def __len__(self) -> int:
        return len(self.pcs)

    @classmethod
    def of(cls, frame: pl.DataFrame, loads: list, pc: str | None) -> RowScope:
        """``loads``: each row's LoadInfo (``pc_id``, ``file_name``); ``pc`` the default."""
        height = frame.height
        files = frame["_source_file"].to_list() if "_source_file" in frame.columns else [None] * height
        sheets = frame["_source_sheet"].to_list() if "_source_sheet" in frame.columns else [None] * height
        return cls(
            [profit_center.pc_number(getattr(load, "pc_id", None) or pc) for load in loads],
            [getattr(load, "file_name", None) or name for load, name in zip(loads, files)],
            sheets,
        )


def _glob(pattern: str | None, value: str | None) -> bool:
    if not pattern:
        return True
    return value is not None and fnmatch.fnmatchcase(str(value).casefold(), pattern.casefold())


def select(rules, target: str, source_table: str | None) -> list[Rule]:
    """The enabled rules for ``target`` whose bronze-table scope fits ``source_table``,
    in the order they run."""
    order = {kind: index for index, kind in enumerate(KINDS)}
    chosen = [rule for rule in rules or []
              if rule.enabled and target in rule.targets and _glob(rule.scope_bronze_table, source_table)]
    return sorted(chosen, key=lambda rule: (order[rule.kind], rule.seq, rule.key))


def mask(rule: Rule, scope: RowScope) -> pl.Series:
    """Per row: whether ``rule`` applies (its profit center, file and sheet)."""
    number = profit_center.pc_number(rule.profit_center)
    cache: dict[tuple, bool] = {}
    values = []
    for pc, file, sheet in zip(scope.pcs, scope.files, scope.sheets):
        key = (pc, file, sheet)
        if key not in cache:
            cache[key] = pc == number and _glob(rule.scope_file_like, file) and _glob(rule.scope_sheet, sheet)
        values.append(cache[key])
    return pl.Series("mask", values, dtype=pl.Boolean)


# --------------------------------------------------------------------------- expressions

class Env:
    """Where operands are read: bronze text by column name, Silver values computed so far."""

    def __init__(self, frame: pl.DataFrame, columns: dict[str, pl.Series] | None, height: int,
                 headers: dict[str, str] | None = None):
        self.frame = frame
        self.columns = columns or {}
        self.height = height
        self.headers = headers or {}  # bronze column -> the header the file wrote
        self.missing: set[str] = set()
        self._index: dict[str, str] | None = None

    def bronze(self, name: str) -> pl.Series:
        actual = self._find(name)
        if actual is None:
            self.missing.add(name)
            return pl.Series(name, [None] * self.height, dtype=pl.String)
        return cleanse.text(self.frame[actual])

    def silver(self, name: str) -> pl.Series:
        if name in self.columns:
            return self.columns[name]
        self.missing.add(f"silver:{name}")
        return pl.Series(name, [None] * self.height, dtype=pl.String)

    def _find(self, name: str) -> str | None:
        """A bronze column by name as written, as stored, or by its words."""
        if name in self.frame.columns:
            return name
        if self._index is None:
            self._index = {}
            present = [column for column in self.frame.columns if not column.startswith("_")]
            # The file's own headers first, then the bronze names.
            for column in present:
                header = self.headers.get(column)
                if header:
                    self._index.setdefault(header.strip().casefold(), column)
            for column in present:
                self._index.setdefault(column.casefold(), column)
            for column in present:
                for label in (self.headers.get(column), column):
                    if label:
                        self._index.setdefault("w:" + normalize.compact(label), column)
        return self._index.get(name.strip().casefold()) or self._index.get("w:" + normalize.compact(name))


def _const(value, height: int) -> pl.Series:
    if isinstance(value, bool):
        return pl.Series("lit", [value] * height, dtype=pl.Boolean)
    if isinstance(value, int):
        return pl.Series("lit", [value] * height, dtype=pl.Int64)
    if isinstance(value, float):
        return pl.Series("lit", [value] * height, dtype=pl.Float64)
    if value is None:
        return pl.Series("lit", [None] * height, dtype=pl.String)
    return pl.Series("lit", [str(value)] * height, dtype=pl.String)


def evaluate(node, env: Env) -> pl.Series:
    """One operand as a Series of the frame's height."""
    if isinstance(node, str):
        if node.startswith("src:"):
            return env.bronze(node[4:].strip())
        if node.startswith("silver:"):
            return env.silver(node[7:].strip())
        return _const(node, env.height)
    if isinstance(node, dict):
        if "lit" in node and len(node) == 1:
            return _const(node["lit"], env.height)
        function, _, _ = _OPS[node["op"]]
        return function(node, env).alias("value")
    return _const(node, env.height)


def _args(node, env) -> list[pl.Series]:
    return [evaluate(arg, env) for arg in node.get("args", [])]


def as_text(series: pl.Series) -> pl.Series:
    """Values as trimmed text: 94.0 -> '94', True -> 'true', a date -> '2026-07-01'."""
    if series.dtype == pl.String:
        return cleanse.text(series)
    if series.dtype.is_float():
        frame = pl.DataFrame({"v": series})
        series = frame.select(
            pl.when(pl.col("v") == pl.col("v").round(0)).then(pl.col("v").cast(pl.Int64, strict=False).cast(pl.String))
            .otherwise(pl.col("v").cast(pl.String))).to_series()
    elif series.dtype == pl.Boolean:
        series = series.cast(pl.String).str.to_lowercase()
    else:
        series = series.cast(pl.String)
    return cleanse.text(series)


def as_number(series: pl.Series) -> pl.Series:
    """Values as Float64: amounts written $1,200.50 or (250.00) included."""
    if series.dtype.is_numeric() or isinstance(series.dtype, pl.Decimal):
        return series.cast(pl.Float64)
    if series.dtype == pl.Boolean:
        return series.cast(pl.Float64)
    raw = as_text(series)
    readings: dict[str, float | None] = {}
    for value in raw.drop_nulls().unique().to_list():
        number = cleanse.parse_number(value)
        readings[value] = float(number) if number is not None and number.is_finite() else None
    return pl.Series("value", [readings.get(value) for value in raw.to_list()], dtype=pl.Float64)


_JAVA_TOKENS = [("yyyy", "%Y"), ("yy", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("M", "%m"),
                ("dd", "%d"), ("d", "%d"), ("HH", "%H"), ("H", "%H"), ("mm", "%M"), ("ss", "%S")]


def chrono_format(fmt: str) -> str:
    """'dd/MM/yyyy' (the notebooks' style) -> '%d/%m/%Y'; a format with '%' is kept."""
    if "%" in fmt:
        return fmt
    out, index = [], 0
    while index < len(fmt):
        for token, code in _JAVA_TOKENS:
            if fmt.startswith(token, index):
                out.append(code)
                index += len(token)
                break
        else:
            out.append(fmt[index])
            index += 1
    return "".join(out)


def parse_dates(series: pl.Series, formats: list[str] | None = None) -> pl.Series:
    """Dates from ``series``: with ``formats`` only those (first that reads wins), else
    the document's formats via ``cleanse.dates``."""
    if series.dtype == pl.Date:
        return series
    if isinstance(series.dtype, pl.Datetime):
        return series.dt.date()
    raw = as_text(series)
    if not formats:
        return cleanse.dates(raw)[0]
    parsed = pl.Series("value", [None] * len(raw), dtype=pl.Date)
    for fmt in formats:
        code = chrono_format(fmt)
        attempt = (raw.str.to_datetime(code, strict=False).dt.date() if "%H" in code
                   else raw.str.to_date(code, strict=False))
        parsed = parsed.fill_null(attempt)
    return parsed


def _numeric(function):
    def run(node, env):
        values = [as_number(series) for series in _args(node, env)]
        result = values[0]
        for other in values[1:]:
            result = function(result, other)
        return result
    return run


def _div_safe(node, env):
    left, right = (as_number(series) for series in _args(node, env))
    frame = pl.DataFrame({"a": left, "b": right})
    return frame.select(pl.when((pl.col("b") == 0) | pl.col("b").is_null()).then(None)
                        .otherwise(pl.col("a") / pl.col("b"))).to_series()


def _round(node, env):
    return as_number(_args(node, env)[0]).round(int(node.get("digits", 2)))


def _coalesce(node, env):
    values = _args(node, env)
    dtypes = {series.dtype for series in values if series.null_count() < len(series)}
    if len(dtypes) > 1:
        values = [as_text(series) for series in values]
    result = values[0]
    for other in values[1:]:
        if other.dtype != result.dtype:
            other = other.cast(result.dtype, strict=False)
        result = result.fill_null(other)
    return result


def _lpad(node, env):
    text = as_text(_args(node, env)[0])
    return text.str.pad_start(int(node["width"]), str(node.get("fill", "0"))[:1] or "0")


def _strip_zeros(node, env):
    text = as_text(_args(node, env)[0])
    stripped = text.str.strip_chars_start("0")
    return pl.DataFrame({"t": text, "s": stripped}).select(
        pl.when(pl.col("t").is_not_null() & (pl.col("s") == "")).then(pl.lit("0")).otherwise(pl.col("s"))).to_series()


def _text_op(method):
    def run(node, env):
        return getattr(as_text(_args(node, env)[0]).str, method)()
    return run


def _concat(node, env):
    values = [as_text(series) for series in _args(node, env)]
    frame = pl.DataFrame({f"a{index}": series for index, series in enumerate(values)})
    joined = frame.select(pl.concat_str([pl.col(name) for name in frame.columns],
                                         separator=str(node.get("sep", "")), ignore_nulls=True)).to_series()
    return cleanse.text(joined)


def _regex_extract(node, env):
    return as_text(_args(node, env)[0]).str.extract(node["pattern"], int(node.get("group", 1)))


def _regex_replace(node, env):
    return cleanse.text(as_text(_args(node, env)[0]).str.replace_all(node["pattern"], str(node.get("replacement", ""))))


def _to_date(node, env):
    return parse_dates(_args(node, env)[0], node.get("formats"))


def _year(node, env):
    return parse_dates(_args(node, env)[0]).dt.year().cast(pl.Int32)


def _month(node, env):
    value = _args(node, env)[0]
    if value.dtype == pl.String:  # a month name or number, else a date
        month, _ = cleanse.integers(value, pl.Int32, months=True)
        month = pl.DataFrame({"m": month}).select(
            pl.when((pl.col("m") >= 1) & (pl.col("m") <= 12)).then(pl.col("m")).otherwise(None)).to_series()
        return month.fill_null(parse_dates(value).dt.month().cast(pl.Int32))
    return parse_dates(value).dt.month().cast(pl.Int32)


def _add_months(node, env):
    return parse_dates(_args(node, env)[0]).dt.offset_by(f"{int(node['months'])}mo")


def _make_date(node, env):
    year, month, day = (as_number(series).cast(pl.Int32, strict=False) for series in _args(node, env))
    frame = pl.DataFrame({"y": year, "m": month, "d": day})
    text = frame.select(pl.format("{}-{}-{}", pl.col("y"), pl.col("m"), pl.col("d"))).to_series()
    return text.str.to_date("%Y-%m-%d", strict=False)


def _null_if(node, env):
    value = _args(node, env)[0]
    text = as_text(value)
    folded = text.str.to_lowercase() if node.get("case_insensitive", True) else text
    values = [str(v).strip() for v in node["values"]]
    if node.get("case_insensitive", True):
        values = [v.casefold() for v in values]
    hit = folded.is_in(values).fill_null(False)
    return pl.DataFrame({"v": value, "h": hit}).select(
        pl.when(pl.col("h")).then(None).otherwise(pl.col("v"))).to_series()


def _when(node, env):
    result = evaluate(node.get("else"), env)
    # Evaluated last case first, so the first case that holds wins.
    for case in reversed(node["cases"]):
        condition = _truth(evaluate(case["if"], env))
        then = evaluate(case["then"], env)
        if then.dtype != result.dtype:
            if result.null_count() == len(result):
                result = result.cast(then.dtype, strict=False)
            elif then.null_count() == len(then):
                then = then.cast(result.dtype, strict=False)
            else:
                then, result = as_text(then), as_text(result)
        result = pl.DataFrame({"c": condition, "t": then, "r": result}).select(
            pl.when(pl.col("c")).then(pl.col("t")).otherwise(pl.col("r"))).to_series()
    return result


def _truth(series: pl.Series) -> pl.Series:
    if series.dtype != pl.Boolean:
        series = cleanse.booleans(as_text(series))[0]
    return series.fill_null(False)


def _folded(series: pl.Series, insensitive: bool) -> pl.Series:
    text = as_text(series)
    return text.str.to_lowercase() if insensitive else text


def _compare(kind: str):
    def run(node, env):
        left, right = _args(node, env)
        numeric = all(s.dtype.is_numeric() or isinstance(s.dtype, pl.Decimal) or s.dtype == pl.String
                      for s in (left, right)) and kind in ("gt", "ge", "lt", "le")
        if numeric:
            left, right = as_number(left), as_number(right)
        elif left.dtype == pl.Date or right.dtype == pl.Date:
            left, right = parse_dates(left), parse_dates(right)
        elif left.dtype.is_numeric() and right.dtype.is_numeric():
            left, right = left.cast(pl.Float64), right.cast(pl.Float64)
        else:
            insensitive = node.get("case_insensitive", True)
            left, right = _folded(left, insensitive), _folded(right, insensitive)
        frame = pl.DataFrame({"a": left, "b": right})
        expr = {"eq": pl.col("a") == pl.col("b"), "ne": pl.col("a") != pl.col("b"), "gt": pl.col("a") > pl.col("b"),
                "ge": pl.col("a") >= pl.col("b"), "lt": pl.col("a") < pl.col("b"),
                "le": pl.col("a") <= pl.col("b")}[kind]
        return frame.select(expr).to_series().fill_null(False)
    return run


def _in(node, env):
    insensitive = node.get("case_insensitive", True)
    text = _folded(_args(node, env)[0], insensitive)
    values = [str(v).strip() for v in node["values"]]
    if insensitive:
        values = [v.lower() for v in values]
    return text.is_in(values).fill_null(False)


def _contains(node, env):
    insensitive = node.get("case_insensitive", True)
    text = _folded(_args(node, env)[0], insensitive)
    needles = [str(v) for v in _listed(node["value"])]
    if insensitive:
        needles = [v.lower() for v in needles]
    result = pl.Series("value", [False] * len(text), dtype=pl.Boolean)
    for needle in needles:
        result = result | text.str.contains(needle, literal=True).fill_null(False)
    return result


def _starts_with(node, env):
    insensitive = node.get("case_insensitive", True)
    text = _folded(_args(node, env)[0], insensitive)
    prefix = str(node["value"]).lower() if insensitive else str(node["value"])
    return text.str.starts_with(prefix).fill_null(False)


def _is_null(node, env):
    value = _args(node, env)[0]
    return (as_text(value) if value.dtype == pl.String else value).is_null()


def _not_null(node, env):
    return ~_is_null(node, env)


def _and(node, env):
    result = None
    for series in _args(node, env):
        series = _truth(series)
        result = series if result is None else result & series
    return result


def _or(node, env):
    result = None
    for series in _args(node, env):
        series = _truth(series)
        result = series if result is None else result | series
    return result


def _not(node, env):
    return ~_truth(_args(node, env)[0])


def _upper(node, env):
    return as_text(_args(node, env)[0]).str.to_uppercase()


def _lower(node, env):
    return as_text(_args(node, env)[0]).str.to_lowercase()


def _to_number(node, env):
    return as_number(_args(node, env)[0])


def _to_text(node, env):
    return as_text(_args(node, env)[0])


# op -> (function, (min args, max args or None), {parameter: required})
_OPS = {
    "add": (_numeric(lambda a, b: a + b), (2, None), {}),
    "sub": (_numeric(lambda a, b: a - b), (2, 2), {}),
    "mul": (_numeric(lambda a, b: a * b), (2, None), {}),
    "div_safe": (_div_safe, (2, 2), {}),
    "round": (_round, (1, 1), {"digits": False}),
    "to_number": (_to_number, (1, 1), {}),
    "to_text": (_to_text, (1, 1), {}),
    "coalesce": (_coalesce, (1, None), {}),
    "lpad": (_lpad, (1, 1), {"width": True, "fill": False}),
    "strip_leading_zeros": (_strip_zeros, (1, 1), {}),
    "upper": (_upper, (1, 1), {}),
    "lower": (_lower, (1, 1), {}),
    "trim": (_to_text, (1, 1), {}),
    "concat": (_concat, (1, None), {"sep": False}),
    "regex_extract": (_regex_extract, (1, 1), {"pattern": True, "group": False}),
    "regex_replace": (_regex_replace, (1, 1), {"pattern": True, "replacement": False}),
    "to_date": (_to_date, (1, 1), {"formats": False}),
    "make_date": (_make_date, (3, 3), {}),
    "year": (_year, (1, 1), {}),
    "month": (_month, (1, 1), {}),
    "add_months": (_add_months, (1, 1), {"months": True}),
    "null_if": (_null_if, (1, 1), {"values": True, "case_insensitive": False}),
    "when": (_when, (0, 0), {"cases": True, "else": False}),
    "eq": (_compare("eq"), (2, 2), {"case_insensitive": False}),
    "ne": (_compare("ne"), (2, 2), {"case_insensitive": False}),
    "gt": (_compare("gt"), (2, 2), {}),
    "ge": (_compare("ge"), (2, 2), {}),
    "lt": (_compare("lt"), (2, 2), {}),
    "le": (_compare("le"), (2, 2), {}),
    "in": (_in, (1, 1), {"values": True, "case_insensitive": False}),
    "contains": (_contains, (1, 1), {"value": True, "case_insensitive": False}),
    "starts_with": (_starts_with, (1, 1), {"value": True, "case_insensitive": False}),
    "is_null": (_is_null, (1, 1), {}),
    "not_null": (_not_null, (1, 1), {}),
    "and": (_and, (2, None), {}),
    "or": (_or, (2, None), {}),
    "not": (_not, (1, 1), {}),
}
OPS = tuple(_OPS)


def _listed(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


# --------------------------------------------------------------------------- application

def _value_text(value) -> str | None:
    """A rule's literal as the text the column's cleansing reads."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def coerce(values: pl.Series, column: SilverColumn | None) -> tuple[pl.Series, int]:
    """``values`` as ``column``'s type, cleansed like a mapped value; and how many could
    not be read."""
    if column is None:
        return as_text(values), 0
    from .transform import _series  # the column's polars type

    target = _series(column, []).dtype
    if values.dtype == target:
        return values.alias(column.name), 0
    if column.kind == "string":
        return as_text(values).alias(column.name), 0
    if column.kind in ("date", "timestamp") and values.dtype == pl.Date:
        typed = values if column.kind == "date" else values.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
        return typed.alias(column.name), 0
    if column.kind == "decimal" and values.dtype.is_float():
        text = pl.Series(column.name, [None if v is None else format(Decimal(repr(v)), "f") for v in values.to_list()],
                         dtype=pl.String)
        return cleanse.by_type(text, column)
    return cleanse.by_type(as_text(values).alias(column.name), column)


@dataclass
class Stat:
    key: str
    kind: str
    columns: list[str]
    rows_in_scope: int = 0
    rows_changed: int = 0
    rows_dropped: int = 0
    unreadable: int = 0
    missing_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Run:
    """The rules of one transform call, and what each did."""

    def __init__(self, rules, target: str, source_table: str | None,
                 by_name: dict[str, SilverColumn], lookups: dict[str, list[dict]] | None = None,
                 headers: dict[str, str] | None = None):
        self.rules = select(rules, target, source_table)
        self.by_name = by_name
        self.lookups = lookups or {}
        self.headers = headers or {}
        self.stats: dict[str, Stat] = {}

    def __bool__(self) -> bool:
        return bool(self.rules)

    def _stat(self, rule: Rule) -> Stat:
        if rule.key not in self.stats:
            self.stats[rule.key] = Stat(rule.key, rule.kind, rule.written())
        return self.stats[rule.key]

    def report(self) -> list[dict]:
        return [stat.as_dict() for stat in self.stats.values()]

    @staticmethod
    def _skipped(stat: Stat, env: Env) -> bool:
        """A rule that reads a column the file does not have is not applied to the load:
        it changes nothing, and the missing column is reported."""
        if not env.missing:
            return False
        stat.missing_sources = sorted(set(stat.missing_sources) | env.missing)
        return True

    def applied(self) -> list[dict]:
        """The rules that applied to at least one row, as stored: for the run's audit."""
        return [rule.as_dict() for rule in self.rules if rule.key in self.stats]

    # -- filters ------------------------------------------------------------------------

    def filter(self, frame: pl.DataFrame, scope: RowScope) -> pl.Series | None:
        """Per row, whether it is kept; None when no filter applies."""
        keep = None
        for rule in (r for r in self.rules if r.kind == FILTER):
            applies = mask(rule, scope)
            if not applies.any():
                continue
            env = Env(frame, None, frame.height, self.headers)
            if "keep" in rule.spec:
                condition = _truth(evaluate(rule.spec["keep"], env))
                drop = applies & ~condition
            else:
                drop = applies & _truth(evaluate(rule.spec["drop"], env))
            stat = self._stat(rule)
            stat.rows_in_scope += int(applies.sum())
            if self._skipped(stat, env):
                continue  # a filter on a column the file lacks would drop every row
            stat.rows_dropped += int(drop.sum())
            keep = ~drop if keep is None else keep & ~drop
        return keep

    # -- date formats -------------------------------------------------------------------

    def reparse(self, name: str, raw: pl.Series, typed: pl.Series, frame: pl.DataFrame,
                scope: RowScope) -> tuple[pl.Series, int | None]:
        """``typed`` with ``name``'s date_format rules applied; the new unreadable count,
        or None when no rule touched the column."""
        touched = False
        for rule in (r for r in self.rules if r.kind == DATE_FORMAT and r.column == name):
            applies = mask(rule, scope)
            if not applies.any():
                continue
            env = Env(frame, None, frame.height, self.headers)
            source = evaluate(rule.spec["source"], env) if "source" in rule.spec else raw
            stat = self._stat(rule)
            stat.rows_in_scope += int(applies.sum())
            if self._skipped(stat, env):
                continue
            override = parse_dates(source, rule.spec["formats"])
            column = self.by_name.get(name)
            override, _ = coerce(override, column)
            before = typed
            typed = pl.DataFrame({"m": applies, "o": override, "t": typed}).select(
                pl.when(pl.col("m") & pl.col("o").is_not_null()).then(pl.col("o")).otherwise(pl.col("t"))
            ).to_series().alias(name)
            stat.rows_changed += int(_changed(before, typed).sum())
            touched = True
            if "source" in rule.spec:
                raw = source
        if not touched:
            return typed, None
        unreadable = int((cleanse.text(as_text(raw)).is_not_null() & typed.is_null()).sum())
        return typed, unreadable

    # -- column rules -------------------------------------------------------------------

    def apply(self, columns: dict[str, pl.Series], frame: pl.DataFrame, scope: RowScope) -> list[str]:
        """Lookups, translations, derivations and constants, in order, into ``columns``.
        Returns the Silver columns any rule changed."""
        changed: set[str] = set()
        height = len(scope)
        for rule in self.rules:
            if rule.kind not in (LOOKUP, TRANSLATE, DERIVE, CONSTANT):
                continue
            applies = mask(rule, scope)
            if not applies.any():
                continue
            env = Env(frame, columns, height, self.headers)
            stat = self._stat(rule)
            stat.rows_in_scope += int(applies.sum())
            if rule.kind == LOOKUP:
                results = self._lookup(rule, env)
            elif rule.kind == TRANSLATE:
                results = {rule.column: self._translate(rule, env)}
            elif rule.kind == DERIVE:
                results = {rule.column: (evaluate(rule.spec["expr"], env), pl.Series([True] * height))}
            else:
                text = _value_text(rule.spec["value"])
                results = {rule.column: (pl.Series("value", [text] * height, dtype=pl.String),
                                         pl.Series([True] * height))}
            if self._skipped(stat, env):
                continue  # never overwrite values with what a missing column would give
            fill_only = rule.spec.get("mode", "set") == "fill_null"
            row_changed = pl.Series([False] * height)
            for name, (values, hit) in results.items():
                column = self.by_name.get(name)
                typed, failures = coerce(values, column)
                stat.unreadable += failures
                old = columns.get(name)
                if old is None:
                    old = pl.Series(name, [None] * height, dtype=typed.dtype)
                elif old.dtype != typed.dtype:
                    typed = typed.cast(old.dtype, strict=False)
                write = applies & hit.fill_null(False)
                if fill_only:
                    write = write & old.is_null()
                new = pl.DataFrame({"w": write, "n": typed, "o": old}).select(
                    pl.when(pl.col("w")).then(pl.col("n")).otherwise(pl.col("o"))).to_series().alias(name)
                difference = _changed(old, new)
                if difference.any():
                    changed.add(name)
                row_changed = row_changed | difference
                columns[name] = new
            stat.rows_changed += int(row_changed.sum())
        return sorted(changed)

    def _translate(self, rule: Rule, env: Env) -> tuple[pl.Series, pl.Series]:
        spec = rule.spec
        source = as_text(evaluate(spec["source"], env))
        insensitive = spec.get("case_insensitive", True)
        how = spec.get("match", "equals")
        cases = spec["cases"]
        has_default = "default" in spec
        outcome: dict[str | None, tuple[bool, str | None]] = {}
        null_case = next((case for case in cases if any(when is None for when in _listed(case["when"]))), None)
        for value in source.unique().to_list():
            if value is None:  # a blank stays blank, unless a case names it
                outcome[value] = (True, _value_text(null_case["then"])) if null_case else (False, None)
                continue
            found = self._case(value, cases, how, insensitive)
            if found is not None:
                outcome[value] = (True, _value_text(found["then"]))
            elif has_default:
                outcome[value] = (True, _value_text(spec["default"]))
            else:
                outcome[value] = (False, None)
        rows = [outcome[value] for value in source.to_list()]
        return (pl.Series("value", [r[1] for r in rows], dtype=pl.String),
                pl.Series("hit", [r[0] for r in rows], dtype=pl.Boolean))

    @staticmethod
    def _case(value: str, cases: list[dict], how: str, insensitive: bool) -> dict | None:
        """The first case whose ``when`` (one value or a list) matches ``value``."""
        probe = value.casefold() if insensitive else value
        for case in cases:
            for when in _listed(case["when"]):
                if when is None:
                    continue
                text = str(when).strip()
                if how == "regex":
                    hit = re.search(text, value, re.IGNORECASE if insensitive else 0) is not None
                else:
                    needle = text.casefold() if insensitive else text
                    hit = (probe == needle if how == "equals" else needle in probe if how == "contains"
                           else probe.startswith(needle))
                if hit:
                    return case
        return None

    def _lookup(self, rule: Rule, env: Env) -> dict[str, tuple[pl.Series, pl.Series]]:
        spec = rule.spec
        columns: dict[str, str] = spec["columns"]
        if spec["source"] == "inline":
            entries = [(str(entry["key"]), {name: entry.get(field) for name, field in columns.items()})
                       for entry in spec["entries"]]
        else:
            rows = self.lookups.get(rule.key, [])
            # The lookup's columns by name as written in the rule, or as Bronze stored them.
            index: dict[str, str] = {}
            for name in (rows[0].keys() if rows else []):
                index.setdefault(name.casefold(), name)
                index.setdefault("w:" + normalize.compact(name), name)

            def actual(field: str) -> str:
                return index.get(field.casefold()) or index.get("w:" + normalize.compact(field)) or field

            key_to = actual(spec["key_to"])
            fields = {name: actual(field) for name, field in columns.items()}
            entries = [(str(row.get(key_to)).strip(), {name: row.get(field) for name, field in fields.items()})
                       for row in rows if row.get(key_to) not in (None, "")]
        how = spec.get("match", "equals")
        folded = [(" ".join(key.split()).casefold(), values) for key, values in entries]
        exact: dict[str, dict] = {}
        for key, values in folded:
            exact.setdefault(key, values)  # the first entry for a key wins
        default = spec.get("default")
        source = as_text(evaluate(spec["key"], env))
        found: dict[str | None, dict | None] = {}
        for value in source.unique().to_list():
            match = None
            if value is not None:
                probe = " ".join(value.split()).casefold()
                if how == "equals":
                    match = exact.get(probe)
                else:
                    match = next((values for key, values in folded if key and key in probe), None)
            found[value] = match if match is not None else default
        rows = [found[value] for value in source.to_list()]
        hit = pl.Series("hit", [row is not None for row in rows], dtype=pl.Boolean)
        return {name: (pl.Series("value", [_value_text(row.get(name)) if row else None for row in rows],
                                 dtype=pl.String), hit)
                for name in columns}


def _changed(old: pl.Series, new: pl.Series) -> pl.Series:
    """Per row, whether the value differs (NULL against a value counts)."""
    if old.dtype != new.dtype:
        old, new = as_text(old), as_text(new)
    return pl.DataFrame({"o": old, "n": new}).select(pl.col("o").ne_missing(pl.col("n"))).to_series()
