"""A rule's logic as PostgreSQL-style SQL, and back: another view of the same JSON spec.

The JSON spec stays what is stored and what runs (``rules.py``); SQL is a way to read and
write it. ``to_sql`` renders a spec, ``from_sql`` parses SQL into the spec it means. Only
expressions are SQL here -- never a statement -- and nothing is ever run as SQL: the
parser accepts a fixed set of operators and functions and builds the same JSON tree a
person would write by hand.

The SQL view is offered only when it is exact: ``to_sql`` parses its own output back and
refuses (``NoSql``) unless that gives the same rule. So switching views never changes
what a rule does.

What each kind looks like:

* ``derive``    -- an expression:   ``CASE WHEN src."Line" = 'Fee' THEN src."Amount" ELSE 0 END``
* ``filter``    -- a condition:     ``WHERE src."Status" IN ('Issued', 'Bound')``
                   (a ``drop`` rule is ``WHERE NOT (...)``)
* ``translate`` -- a CASE over one value, each WHEN matched the same way:
                   ``CASE src."Code" WHEN 'A' THEN 'Active' ELSE 'Other' END``
* ``constant``  -- one value:       ``'Hartford'``

``lookup`` and ``date_format`` are settings, not logic: they stay JSON.

Columns are ``src."Header as written"`` (a bronze column; unqualified names are bronze
too) and ``silver.column``. As in the rules engine, text comparisons ignore case:
``=``, ``<>``, ``IN``, ``NULLIF`` and ``ILIKE`` do; ``LIKE`` and ``~`` respect it.
Division by zero gives NULL.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass

from . import rules

SQL_KINDS = (rules.DERIVE, rules.FILTER, rules.TRANSLATE, rules.CONSTANT)
# Spec keys each kind's SQL stands for; any other key keeps a rule JSON-only. ``mode``
# is not SQL: it is carried over from the JSON as it is.
_KEYS = {
    rules.DERIVE: {"expr"},
    rules.FILTER: {"keep", "drop"},
    rules.TRANSLATE: {"source", "match", "cases", "default", "case_insensitive"},
    rules.CONSTANT: {"value"},
}
KEPT = ("mode",)


class NoSql(ValueError):
    """The spec has no exact SQL form; the message says why, for the reviewer."""


@dataclass
class SqlError(ValueError):
    """SQL that cannot become a rule: where (character offsets), and why."""

    message: str
    start: int | None = None
    end: int | None = None

    def __str__(self) -> str:
        return self.message


# ============================================================================ tokens


@dataclass
class Token:
    kind: str  # num | str | qid | id | op | eof
    text: str
    start: int
    end: int

    @property
    def word(self) -> str:
        return self.text.upper() if self.kind == "id" else ""


_OPERATORS = ("!~*", "<>", "!=", "<=", ">=", "||", "::", "~*", "!~", "~", "=", "<", ">", "+", "-", "*", "/",
              "%", "(", ")", ",", ".")
_NUMBER = re.compile(r"(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def tokenize(text: str) -> list[Token]:
    tokens, index = [], 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise SqlError("This comment is never closed with */.", index, len(text))
            index = end + 2
        elif char in "'\"":
            quote, start, index, parts = char, index, index + 1, []
            while True:
                end = text.find(quote, index)
                if end < 0:
                    what = "text" if quote == "'" else "column name"
                    raise SqlError(f"This {what} is never closed with {quote}.", start, len(text))
                parts.append(text[index:end])
                if text.startswith(quote * 2, end):  # '' inside '...' is one '
                    parts.append(quote)
                    index = end + 2
                    continue
                index = end + 1
                break
            tokens.append(Token("str" if quote == "'" else "qid", "".join(parts), start, index))
        elif char.isdigit() or (char == "." and text[index + 1:index + 2].isdigit()):
            match = _NUMBER.match(text, index)
            tokens.append(Token("num", match.group(0), index, match.end()))
            index = match.end()
        elif char.isalpha() or char == "_":
            match = _WORD.match(text, index)
            tokens.append(Token("id", match.group(0), index, match.end()))
            index = match.end()
        else:
            for op in _OPERATORS:
                if text.startswith(op, index):
                    tokens.append(Token("op", op, index, index + len(op)))
                    index += len(op)
                    break
            else:
                raise SqlError(f"'{char}' is not part of the SQL rules understand.", index, index + 1)
    tokens.append(Token("eof", "", len(text), len(text)))
    return tokens


# ============================================================================ parse tree

# Internal nodes while parsing. A literal is wrapped (Lit) so it is never mistaken for a
# column, a JSON op is a dict, a column is "src:..." / "silver:...".


@dataclass
class Lit:
    value: object


@dataclass
class Interval:
    months: int


@dataclass
class RegexMatch:  # x ~* 'pattern': only a translate rule matches by regex
    arg: object
    pattern: str
    insensitive: bool
    start: int
    end: int


@dataclass
class Case:
    subject: object | None  # CASE x WHEN ... (simple) or None (searched)
    whens: list  # [(condition or value, then)]
    has_else: bool
    otherwise: object
    start: int
    end: int


_RESERVED = {"AND", "OR", "NOT", "CASE", "WHEN", "THEN", "ELSE", "END", "IS", "NULL", "IN", "LIKE", "ILIKE",
             "BETWEEN", "TRUE", "FALSE", "CAST", "AS", "INTERVAL", "EXTRACT", "FROM", "WHERE", "SELECT"}
OR, AND, NOT, IS, CMP, LIKE, OTHER, ADD, MUL, UNARY, CAST = range(1, 12)
_COMPARE = {"=": "eq", "<>": "ne", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
_NUMERIC_TYPES = {"NUMERIC", "DECIMAL", "INT", "INTEGER", "BIGINT", "SMALLINT", "REAL", "FLOAT", "FLOAT8",
                  "FLOAT4", "DOUBLE", "MONEY"}
_TEXT_TYPES = {"TEXT", "VARCHAR", "CHAR", "CHARACTER", "STRING"}
_FUNCTIONS = ("COALESCE", "NULLIF", "ROUND", "UPPER", "LOWER", "TRIM", "CONCAT", "CONCAT_WS", "LPAD",
              "STRIP_LEADING_ZEROS", "REGEXP_SUBSTR", "REGEXP_REPLACE", "TO_DATE", "MAKE_DATE", "DATE_PART",
              "CAST", "EXTRACT")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.index = 0

    # --- tokens
    @property
    def token(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.token
        self.index += 1
        return token

    def at(self, *words_or_ops: str) -> bool:
        token = self.token
        return (token.kind == "op" and token.text in words_or_ops) or (token.kind == "id" and token.word in words_or_ops)

    def expect(self, word_or_op: str, why: str = "") -> Token:
        if not self.at(word_or_op):
            found = self.token.text or "the end"
            raise SqlError(f"Expected {word_or_op}{why}, found {found!r}." if self.token.kind != "eof"
                           else f"Expected {word_or_op}{why}, but the SQL ends here.", self.token.start, self.token.end)
        return self.advance()

    def fail(self, message: str, token: Token | None = None):
        token = token or self.token
        raise SqlError(message, token.start, max(token.end, token.start + 1))

    # --- expressions (Pratt)
    def expression(self, floor: int = 0):
        left = self.prefix()
        while True:
            power = self.infix_power()
            if power is None or power <= floor:
                return left
            left = self.infix(left, power)

    def infix_power(self) -> int | None:
        token = self.token
        if token.kind == "op":
            return {"||": OTHER, "~": OTHER, "~*": OTHER, "!~": OTHER, "!~*": OTHER, "+": ADD, "-": ADD,
                    "*": MUL, "/": MUL, "%": MUL, "::": CAST}.get(token.text, CMP if token.text in _COMPARE else None)
        word = token.word
        if word in ("OR",):
            return OR
        if word == "AND":
            return AND
        if word == "IS":
            return IS
        if word in ("IN", "LIKE", "ILIKE", "BETWEEN"):
            return LIKE
        if word == "NOT" and self.peek().word in ("IN", "LIKE", "ILIKE", "BETWEEN"):
            return LIKE
        return None

    def infix(self, left, power: int):
        token = self.advance()
        text, word = token.text, token.word
        if word == "OR":
            return _flat("or", left, self.expression(OR))
        if word == "AND":
            return _flat("and", left, self.expression(AND))
        if word == "IS":
            negate = bool(self.at("NOT")) and self.advance()
            if not self.at("NULL"):
                self.fail("Rules test IS NULL or IS NOT NULL only.")
            self.advance()
            return {"op": "not_null" if negate else "is_null", "args": [left]}
        if word == "NOT":
            return {"op": "not", "args": [self.infix(left, power)]}
        if word == "IN":
            return {"op": "in", "args": [left], "values": self.value_list("IN")}
        if word in ("LIKE", "ILIKE"):
            pattern = self.advance()
            if pattern.kind != "str":
                self.fail(f"{word} takes a quoted pattern, such as '%text%'.", pattern)
            return _like(left, pattern, insensitive=word == "ILIKE")
        if word == "BETWEEN":
            low = self.expression(LIKE)
            self.expect("AND", " in BETWEEN ... AND ...")
            high = self.expression(LIKE)
            return {"op": "and", "args": [{"op": "ge", "args": [left, low]}, {"op": "le", "args": [left, high]}]}
        if text in _COMPARE:
            return {"op": _COMPARE[text], "args": [left, self.expression(CMP)]}
        if text in ("~", "~*", "!~", "!~*"):
            pattern = self.advance()
            if pattern.kind != "str":
                self.fail(f"{text} takes a quoted regular expression.", pattern)
            found = RegexMatch(left, pattern.text, text.endswith("*"), token.start, pattern.end)
            return {"op": "not", "args": [found]} if text.startswith("!") else found
        if text == "||":
            return _flat("concat", left, self.expression(OTHER))
        if text in ("+", "-"):
            right = self.expression(ADD)
            if isinstance(right, Interval):
                return {"op": "add_months", "args": [left], "months": right.months * (1 if text == "+" else -1)}
            if isinstance(left, Interval):
                if text == "-":
                    self.fail("Subtract an interval from a date: date - INTERVAL '1 month'.", token)
                return {"op": "add_months", "args": [right], "months": left.months}
            return _flat("add", left, right) if text == "+" else {"op": "sub", "args": [left, right]}
        if text == "*":
            return _flat("mul", left, self.expression(MUL))
        if text == "/":
            return {"op": "div_safe", "args": [left, self.expression(MUL)]}
        if text == "%":
            self.fail("The remainder operator % is not available in rules.", token)
        if text == "::":
            return self.cast(left, self.type_name())
        self.fail(f"Unexpected {text!r}.", token)

    def prefix(self):
        token = self.token
        if token.kind == "num":
            self.advance()
            return Lit(_number(token.text))
        if token.kind == "str":
            self.advance()
            return Lit(token.text)
        if token.kind == "op":
            if token.text == "(":
                self.advance()
                inner = self.expression()
                self.expect(")", " to close (")
                return inner
            if token.text == "-":
                self.advance()
                inner = self.expression(UNARY)
                if isinstance(inner, Lit) and isinstance(inner.value, (int, float)) and not isinstance(inner.value, bool):
                    return Lit(-inner.value)
                return {"op": "sub", "args": [Lit(0), inner]}
            if token.text == "+":
                self.advance()
                return self.expression(UNARY)
            self.fail(f"Unexpected {token.text!r}.")
        if token.kind == "eof":
            self.fail("The SQL ends where a value was expected.")
        word = token.word
        if word in ("TRUE", "FALSE"):
            self.advance()
            return Lit(word == "TRUE")
        if word == "NULL":
            self.advance()
            return Lit(None)
        if word == "NOT":
            self.advance()
            return {"op": "not", "args": [self.expression(NOT)]}
        if word == "CASE":
            return self.case()
        if word == "INTERVAL":
            self.advance()
            return self.interval()
        if word == "CAST" and self.peek().text == "(":
            self.advance()
            self.advance()
            value = self.expression()
            self.expect("AS", " in CAST(value AS type)")
            kind = self.type_name()
            self.expect(")", " to close CAST(")
            return self.cast(value, kind)
        if word == "EXTRACT" and self.peek().text == "(":
            self.advance()
            self.advance()
            field = self.advance()
            self.expect("FROM", " in EXTRACT(YEAR FROM date)")
            value = self.expression()
            self.expect(")", " to close EXTRACT(")
            return self.date_part(field.text, value, field)
        if token.kind == "id" and self.peek().text == "(":
            return self.call()
        if token.kind in ("id", "qid"):
            return self.column()
        self.fail(f"Unexpected {token.text!r}.")

    def column(self) -> str:
        first = self.advance()
        if first.kind == "id" and first.word in _RESERVED:
            self.fail(f"Unexpected {first.text!r}.", first)
        if self.at(".") and self.peek().kind in ("id", "qid"):
            if first.text.casefold() not in ("src", "silver"):
                self.fail(f"Columns are src.\"Header\" (bronze) or silver.column, not {first.text}.", first)
            self.advance()
            name = self.advance()
            return f"{first.text.casefold()}:{name.text}"
        return f"src:{first.text}"

    def type_name(self) -> str:
        token = self.advance()
        if token.kind != "id":
            self.fail("Expected a type such as NUMERIC, TEXT or DATE.", token)
        name = token.word
        if name == "DOUBLE" and self.token.word == "PRECISION":
            self.advance()
        if name == "CHARACTER" and self.token.word == "VARYING":
            self.advance()
        if self.at("("):  # NUMERIC(12, 2), VARCHAR(40): the size does not matter here
            while not self.at(")"):
                if self.token.kind == "eof":
                    self.fail("Expected ) to close the type's size.")
                self.advance()
            self.advance()
        self._type_token = token
        return name

    def cast(self, value, name: str):
        if name in _NUMERIC_TYPES:
            return {"op": "to_number", "args": [value]}
        if name in _TEXT_TYPES:
            return {"op": "to_text", "args": [value]}
        if name == "DATE":
            return {"op": "to_date", "args": [value]}
        self.fail(f"Rules cast to NUMERIC, TEXT or DATE, not {name}.", self._type_token)

    def date_part(self, field: str, value, token: Token):
        part = field.strip().upper()
        if part not in ("YEAR", "MONTH"):
            self.fail(f"Rules take the YEAR or MONTH of a date, not {field}.", token)
        return {"op": part.lower(), "args": [value]}

    def interval(self) -> Interval:
        token = self.advance()
        if token.kind != "str":
            self.fail("INTERVAL takes a quoted length, such as '3 months'.", token)
        match = re.fullmatch(r"\s*([+-]?\d+)\s*(months?|mons?|years?)\s*", token.text, re.IGNORECASE)
        if not match:
            self.fail("Rules move dates by whole months or years: INTERVAL '3 months'.", token)
        count = int(match.group(1))
        return Interval(count * 12 if match.group(2).lower().startswith("y") else count)

    def value_list(self, what: str) -> list:
        self.expect("(", f" after {what}")
        values = []
        while True:
            token = self.token
            value = self.expression()
            if not isinstance(value, Lit):
                self.fail(f"{what} lists values, such as ('Issued', 'Bound'), not columns or expressions.", token)
            values.append(value.value)
            if self.at(","):
                self.advance()
                continue
            self.expect(")", f" to close {what} (")
            return values

    def case(self) -> Case:
        start = self.advance()
        subject = None if self.at("WHEN") else self.expression()
        whens = []
        while self.at("WHEN"):
            self.advance()
            condition = self.expression()
            self.expect("THEN", " after WHEN ...")
            whens.append((condition, self.expression()))
        if not whens:
            self.fail("CASE needs at least one WHEN ... THEN ....")
        has_else, otherwise = False, None
        if self.at("ELSE"):
            self.advance()
            has_else, otherwise = True, self.expression()
        end = self.expect("END", " to close CASE")
        return Case(subject, whens, has_else, otherwise, start.start, end.end)

    def call(self):
        name_token = self.advance()
        name = name_token.word
        self.expect("(")
        args, tokens = [], []
        if not self.at(")"):
            while True:
                tokens.append(self.token)
                args.append(self.expression())
                if self.at(","):
                    self.advance()
                    continue
                break
        self.expect(")", f" to close {name}(")
        return _function(self, name, name_token, args, tokens)


def _number(text: str):
    return float(text) if any(char in text for char in ".eE") else int(text)


def _flat(op: str, left, right) -> dict:
    """a AND b AND c as one and[...] (likewise or, add, mul, concat without a separator)."""
    args = []
    for side in (left, right):
        if isinstance(side, dict) and side.get("op") == op and set(side) == {"op", "args"}:
            args.extend(side["args"])
        else:
            args.append(side)
    return {"op": op, "args": args}


_LIKE_CONTAINS = re.compile(r"%((?:[^%_\\]|\\.)+)%")
_LIKE_PREFIX = re.compile(r"((?:[^%_\\]|\\.)+)%")


def _like(arg, pattern: Token, insensitive: bool) -> dict:
    """'%text%' is contains, 'text%' starts_with; LIKE respects case, ILIKE ignores it."""
    for regex, op in ((_LIKE_CONTAINS, "contains"), (_LIKE_PREFIX, "starts_with")):
        match = regex.fullmatch(pattern.text)
        if match:
            node = {"op": op, "args": [arg], "value": re.sub(r"\\(.)", r"\1", match.group(1))}
            if not insensitive:
                node["case_insensitive"] = False
            return node
    raise SqlError("Rules match a pattern as '%text%' (contains) or 'text%' (starts with); "
                   "use \\% or \\_ for a literal % or _.", pattern.start, pattern.end)


def _literal(parser: _Parser, value, token: Token, what: str, kinds=(str,)):
    if not isinstance(value, Lit) or not isinstance(value.value, kinds) or isinstance(value.value, bool):
        parser.fail(what, token)
    return value.value


def _function(parser: _Parser, name: str, token: Token, args: list, tokens: list[Token]):
    def count(low: int, high: int | None = None):
        high = low if high is None else high
        if not low <= len(args) <= high:
            want = str(low) if low == high else f"{low} to {high}"
            parser.fail(f"{name} takes {want} value{'s' if high != 1 else ''}, not {len(args)}.", token)

    def integer(position: int, what: str) -> int:
        return _literal(parser, args[position], tokens[position], f"{name}'s {what} is a whole number.", (int,))

    def text(position: int, what: str) -> str:
        return _literal(parser, args[position], tokens[position], f"{name}'s {what} is quoted text.")

    if name == "COALESCE":
        count(1, 99)
        return {"op": "coalesce", "args": args}
    if name == "NULLIF":
        count(2)
        value = _literal(parser, args[1], tokens[1], "NULLIF's second value is a value, such as 'N/A'.",
                         (str, int, float))
        inner = args[0]
        if isinstance(inner, dict) and inner.get("op") == "null_if" and "case_insensitive" not in inner:
            return {**inner, "values": inner["values"] + [value]}
        return {"op": "null_if", "args": [inner], "values": [value]}
    if name == "ROUND":
        count(1, 2)
        return {"op": "round", "args": [args[0]], "digits": integer(1, "number of places") if len(args) > 1 else 0}
    if name in ("UPPER", "LOWER", "TRIM"):
        count(1)
        return {"op": name.lower(), "args": args}
    if name == "CONCAT":
        count(1, 99)
        return _flat_all("concat", args)
    if name == "CONCAT_WS":
        count(2, 99)
        return {"op": "concat", "args": args[1:], "sep": text(0, "separator")}
    if name == "LPAD":
        count(2, 3)
        return {"op": "lpad", "args": [args[0]], "width": integer(1, "width"),
                "fill": text(2, "fill") if len(args) > 2 else " "}
    if name == "STRIP_LEADING_ZEROS":
        count(1)
        return {"op": "strip_leading_zeros", "args": args}
    if name == "REGEXP_SUBSTR":
        count(2, 6)
        if len(args) > 2 and integer(2, "start") != 1:
            parser.fail("REGEXP_SUBSTR reads from the start: its third value is 1.", tokens[2])
        if len(args) > 3 and integer(3, "occurrence") != 1:
            parser.fail("REGEXP_SUBSTR takes the first match: its fourth value is 1.", tokens[3])
        if len(args) > 4 and text(4, "flags") != "":
            parser.fail("REGEXP_SUBSTR's flags are '' here; use (?i) in the pattern to ignore case.", tokens[4])
        return {"op": "regex_extract", "args": [args[0]], "pattern": text(1, "pattern"),
                "group": integer(5, "group") if len(args) > 5 else 0}
    if name == "REGEXP_REPLACE":
        count(3, 4)
        if len(args) < 4 or text(3, "flags") != "g":
            parser.fail("Rules replace every match: end REGEXP_REPLACE with 'g', as in "
                        "REGEXP_REPLACE(x, 'pattern', 'replacement', 'g').", token)
        return {"op": "regex_replace", "args": [args[0]], "pattern": text(1, "pattern"),
                "replacement": re.sub(r"\\(\d)", r"$\1", text(2, "replacement"))}
    if name == "TO_DATE":
        count(1, 99)
        formats = [text(position, "format") for position in range(1, len(args))]
        return {"op": "to_date", "args": [args[0]], **({"formats": formats} if formats else {})}
    if name == "MAKE_DATE":
        count(3)
        return {"op": "make_date", "args": args}
    if name == "DATE_PART":
        count(2)
        return parser.date_part(text(0, "part"), args[1], tokens[0])
    parser.fail(f"{name}() is not a function rules know. They know: {', '.join(sorted(_FUNCTIONS))}, and "
                "CASE, IN, LIKE / ILIKE, IS NULL, BETWEEN, || and + - * /.", token)


def _flat_all(op: str, args: list) -> dict:
    node = {"op": op, "args": [args[0]]}
    for arg in args[1:]:
        node = _flat(op, node, arg)
    return node


# ============================================================================ to JSON


def _json(node, where: str = "expression"):
    """The internal tree as the JSON the engine runs."""
    if isinstance(node, Lit):
        value = node.value
        if isinstance(value, str) and value.startswith(("src:", "silver:")):
            return {"lit": value}  # text that only looks like a column
        return value
    if isinstance(node, str):  # a column
        return node
    if isinstance(node, Interval):
        raise SqlError("An INTERVAL is added to a date: date + INTERVAL '1 month'.")
    if isinstance(node, RegexMatch):
        raise SqlError("~ matches a regular expression only in a translate rule's WHEN; here use "
                       "REGEXP_SUBSTR(value, 'pattern') IS NOT NULL.", node.start, node.end)
    if isinstance(node, Case):
        cases = []
        for condition, then in node.whens:
            if node.subject is not None:
                condition = {"op": "eq", "args": [node.subject, condition]}
            cases.append({"if": _json(condition), "then": _json(then)})
        out = {"op": "when", "cases": cases}
        if node.has_else:
            out["else"] = _json(node.otherwise)
        return _tidy(out)
    if isinstance(node, dict):
        out = {key: ([_json(arg) for arg in value] if key == "args" else value) for key, value in node.items()}
        return _tidy(out)
    raise SqlError(f"{node!r} is not an expression.")


def _tidy(node: dict) -> dict:
    """Flattened and merged as the engine's own JSON would be written: or of contains
    on one value is one contains with a list, nested and / or / add / mul are one."""
    op = node.get("op")
    if op in ("and", "or", "add", "mul") or (op == "concat" and not node.get("sep")):
        args = []
        for arg in node["args"]:
            if isinstance(arg, dict) and arg.get("op") == op and set(arg) == set(node):
                args.extend(arg["args"])
            else:
                args.append(arg)
        node = {**node, "args": args}
    if op == "or":
        merged = []
        for arg in node["args"]:
            last = merged[-1] if merged else None
            if (isinstance(arg, dict) and isinstance(last, dict) and arg.get("op") == last.get("op") == "contains"
                    and arg["args"] == last["args"]
                    and arg.get("case_insensitive", True) == last.get("case_insensitive", True)):
                merged[-1] = {**last, "value": _listed(last["value"]) + _listed(arg["value"])}
            else:
                merged.append(arg)
        if len(merged) == 1:
            return merged[0]
        node = {**node, "args": merged}
    return node


def _listed(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _parse(text: str):
    parser = _Parser(text)
    if parser.token.word == "WHERE":
        parser.fail("WHERE belongs to a filter rule only.")
    node = parser.expression()
    if parser.token.kind != "eof":
        parser.fail(f"Unexpected {parser.token.text!r}: the expression ended before it.")
    return node


def from_sql(kind: str, text: str, base: dict | None = None) -> dict:
    """The spec ``text`` means for a ``kind`` rule; ``mode`` is kept from ``base``."""
    if kind not in SQL_KINDS:
        raise SqlError(f"A {kind} rule is edited as JSON.")
    if not text.strip():
        raise SqlError("Write the rule's SQL here.", 0, 0)
    if kind == rules.FILTER:
        parser = _Parser(text)
        if parser.token.word == "WHERE":
            parser.advance()
        node = parser.expression()
        if parser.token.kind != "eof":
            parser.fail(f"Unexpected {parser.token.text!r}: the condition ended before it.")
        condition = _json(node)
        if isinstance(condition, dict) and condition.get("op") == "not" and set(condition) == {"op", "args"}:
            spec = {"drop": condition["args"][0]}
        else:
            spec = {"keep": condition}
    elif kind == rules.DERIVE:
        spec = {"expr": _json(_parse(text))}
    elif kind == rules.CONSTANT:
        node = _parse(text)
        if not isinstance(node, Lit):
            raise SqlError("A constant is one value, such as 'Hartford', 12.5 or NULL; "
                           "an expression is a derive rule.", 0, len(text))
        spec = {"value": node.value}
    else:
        spec = _translate_from(_parse(text), text)
    for key in KEPT:
        if base and key in base:
            spec[key] = base[key]
    _check(kind, spec)
    return spec


def _check(kind: str, spec: dict) -> None:
    rule = rules.Rule(key="sql.view", profit_center="PC0001", kind=kind, spec=spec,
                      column="value" if kind in rules.COLUMN_KINDS else None)
    try:
        rules._SPEC_CHECKS[kind](rule)
    except rules.RuleError as error:
        raise SqlError(f"As a rule: {error}.") from None


def _translate_from(node, text: str) -> dict:
    """CASE over one value, each WHEN matched the same way, every THEN a value."""
    if not isinstance(node, Case):
        raise SqlError("A translate rule is a CASE: CASE src.\"Code\" WHEN 'A' THEN 'Active' ... END.", 0, len(text))

    def value(item, what):
        if not isinstance(item, Lit):
            raise SqlError(f"A translate rule's {what} is a value, such as 'Active'; "
                           "for an expression, make it a derive rule.", node.start, node.end)
        return item.value

    source = node.subject
    match, insensitive, cases = None, None, []

    def agree(found_match, found_insensitive):
        nonlocal match, insensitive
        if match is None:
            match, insensitive = found_match, found_insensitive
        elif (match, insensitive) != (found_match, found_insensitive):
            raise SqlError("A translate rule matches every WHEN the same way (all =, all ILIKE '%…%', all "
                           "ILIKE '…%' or all ~*); this CASE mixes them. Make it a derive rule instead.",
                           node.start, node.end)

    def same_source(arg):
        nonlocal source
        if source is None:
            source = arg
        elif _json(arg) != _json(source):
            raise SqlError("A translate rule tests one value in every WHEN; this CASE tests more than one. "
                           "Make it a derive rule instead.", node.start, node.end)

    def whens_of(condition) -> list:
        if isinstance(condition, dict) and condition.get("op") == "or":
            return [found for arg in condition["args"] for found in whens_of(arg)]
        if isinstance(condition, RegexMatch):
            same_source(condition.arg)
            agree("regex", condition.insensitive)
            return [condition.pattern]
        if isinstance(condition, dict):
            op, args = condition.get("op"), condition.get("args", [])
            if op == "is_null":
                same_source(args[0])
                return [None]
            if op == "eq" and isinstance(args[1], Lit):
                same_source(args[0])
                agree("equals", True)
                return [args[1].value]
            if op == "in":
                same_source(args[0])
                agree("equals", True)
                return list(condition["values"])
            if op in ("contains", "starts_with"):
                same_source(args[0])
                agree(op, condition.get("case_insensitive", True))
                return _listed(condition["value"])
        raise SqlError("A translate rule's WHEN tests its value with =, IN, ILIKE / LIKE, ~* / ~ or IS NULL; "
                       "for any other test, make it a derive rule.", node.start, node.end)

    for condition, then in node.whens:
        if node.subject is not None:
            if isinstance(condition, Lit) and condition.value is None:
                raise SqlError("WHEN NULL never matches in SQL; test blanks with CASE WHEN x IS NULL THEN ....",
                               node.start, node.end)
            whens = [value(condition, "WHEN")]
            agree("equals", True)
        else:
            whens = whens_of(condition)
        cases.append({"when": whens[0] if len(whens) == 1 else whens, "then": value(then, "THEN")})
    spec = {"source": _json(source)}
    if match and match != "equals":
        spec["match"] = match
    if insensitive is False:
        spec["case_insensitive"] = False
    spec["cases"] = cases
    if node.has_else:
        spec["default"] = value(node.otherwise, "ELSE")
    return spec


# ============================================================================ to SQL

_POWER = {"or": OR, "and": AND, "not": NOT, "is_null": IS, "not_null": IS, "eq": CMP, "ne": CMP, "lt": CMP,
          "le": CMP, "gt": CMP, "ge": CMP, "in": LIKE, "contains": LIKE, "starts_with": LIKE, "add": ADD,
          "sub": ADD, "add_months": ADD, "mul": MUL, "div_safe": MUL}
_SYMBOL = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_]*")
INDENT = "  "


def _quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _ident(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _literal_sql(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    raise NoSql(f"{json.dumps(value)} is not a single value SQL can show.")


def _escape_like(text: str) -> str:
    return re.sub(r"([%_\\])", r"\\\1", str(text))


def _sql(node, floor: int = 0, depth: int = 0) -> str:
    """One operand as SQL, in parentheses when it binds looser than where it sits."""
    if isinstance(node, str):
        if node.startswith("src:"):
            return "src." + _ident(node[4:].strip())
        if node.startswith("silver:"):
            name = node[7:].strip()
            return "silver." + (name if _PLAIN_NAME.fullmatch(name) else _ident(name))
        return _quote(node)
    if isinstance(node, dict) and "op" not in node:
        if set(node) == {"lit"}:
            return _literal_sql(node["lit"])
        raise NoSql(f"{json.dumps(node)} is not an operand.")
    if not isinstance(node, dict):
        return _literal_sql(node)
    op, args = node["op"], node.get("args", [])
    power = _POWER.get(op)
    text = _op_sql(node, op, args, depth)
    return f"({text})" if power is not None and power <= floor else text


def _op_sql(node: dict, op: str, args: list, depth: int) -> str:
    def arg(index: int, floor: int = 0) -> str:
        return _sql(args[index], floor, depth)

    def call(name: str, *parts: str) -> str:
        return f"{name}({', '.join(parts)})"

    insensitive = node.get("case_insensitive", True)
    if op in ("eq", "ne", "in", "null_if") and insensitive is False:
        raise NoSql("A case-sensitive comparison has no SQL form here (= ignores case, as the rules do); "
                    "edit it as JSON.")
    power = _POWER.get(op, 99)
    if op in ("and", "or"):
        return f" {op.upper()} ".join(_sql(item, power, depth) for item in args)
    if op == "not":
        return f"NOT {_sql(args[0], NOT, depth)}"
    if op in ("add", "mul"):
        return f" {'+' if op == 'add' else '*'} ".join(_sql(item, power, depth) for item in args)
    if op in ("sub", "div_safe"):
        return f"{arg(0, power - 1)} {'-' if op == 'sub' else '/'} {arg(1, power)}"
    if op in _SYMBOL:
        return f"{arg(0, CMP)} {_SYMBOL[op]} {arg(1, CMP)}"
    if op in ("is_null", "not_null"):
        return f"{arg(0, IS)} IS {'NOT ' if op == 'not_null' else ''}NULL"
    if op == "in":
        return f"{arg(0, LIKE)} IN ({', '.join(_literal_sql(value) for value in node['values'])})"
    if op in ("contains", "starts_with"):
        keyword = "ILIKE" if insensitive else "LIKE"
        tests = [f"{arg(0, LIKE)} {keyword} "
                 + _quote(("%" if op == "contains" else "") + _escape_like(value) + "%")
                 for value in _listed(node["value"])]
        if op == "starts_with" and len(tests) > 1:
            raise NoSql("starts_with takes one value.")
        return " OR ".join(tests) if len(tests) == 1 else "(" + " OR ".join(tests) + ")"
    if op == "add_months":
        months = int(node["months"])
        return f"{arg(0, ADD - 1)} {'+' if months >= 0 else '-'} INTERVAL '{abs(months)} months'"
    if op == "round":
        return call("ROUND", arg(0), repr(int(node.get("digits", 2))))
    if op == "to_number":
        return f"CAST({arg(0)} AS NUMERIC)"
    if op == "to_text":
        return f"CAST({arg(0)} AS TEXT)"
    if op in ("trim", "upper", "lower"):
        return call(op.upper(), arg(0))
    if op == "coalesce":
        return call("COALESCE", *(_sql(item, 0, depth) for item in args))
    if op == "concat":
        parts = [_sql(item, 0, depth) for item in args]
        return call("CONCAT_WS", _quote(node["sep"]), *parts) if node.get("sep") else call("CONCAT", *parts)
    if op == "lpad":
        return call("LPAD", arg(0), repr(int(node["width"])), _quote(str(node.get("fill", "0"))[:1] or "0"))
    if op == "strip_leading_zeros":
        return call("STRIP_LEADING_ZEROS", arg(0))
    if op == "regex_extract":
        group = int(node.get("group", 1))
        if group == 0:
            return call("REGEXP_SUBSTR", arg(0), _quote(node["pattern"]))
        return call("REGEXP_SUBSTR", arg(0), _quote(node["pattern"]), "1", "1", "''", repr(group))
    if op == "regex_replace":
        replacement = re.sub(r"\$\{?(\d)\}?", r"\\\1", str(node.get("replacement", "")))
        return call("REGEXP_REPLACE", arg(0), _quote(node["pattern"]), _quote(replacement), "'g'")
    if op == "to_date":
        return call("TO_DATE", arg(0), *(_quote(fmt) for fmt in node.get("formats") or []))
    if op == "make_date":
        return call("MAKE_DATE", *(_sql(item, 0, depth) for item in args))
    if op in ("year", "month"):
        return f"EXTRACT({op.upper()} FROM {arg(0)})"
    if op == "null_if":
        text = arg(0)
        for value in node["values"]:
            text = call("NULLIF", text, _literal_sql(value))
        return text
    if op == "when":
        pad = INDENT * (depth + 1)
        lines = ["CASE"]
        for case in node["cases"]:
            lines.append(f"{pad}WHEN {_sql(case['if'], 0, depth + 1)} THEN {_sql(case['then'], 0, depth + 1)}")
        if "else" in node:
            lines.append(f"{pad}ELSE {_sql(node['else'], 0, depth + 1)}")
        lines.append(INDENT * depth + "END")
        return "\n".join(lines)
    raise NoSql(f"The {op} operation has no SQL form here.")


def _translate_sql(spec: dict) -> str:
    source = _sql(spec["source"], LIKE)
    how = spec.get("match", "equals")
    insensitive = spec.get("case_insensitive", True)
    if how == "equals" and insensitive is False:
        raise NoSql("A case-sensitive translate has no SQL form here (= ignores case); edit it as JSON.")
    simple = how == "equals" and all(not isinstance(case["when"], (list, tuple)) and case["when"] is not None
                                     for case in spec["cases"])
    lines = [f"CASE {source}" if simple else "CASE"]
    for case in spec["cases"]:
        values = _listed(case["when"])
        if simple:
            test = _literal_sql(values[0])
        else:
            tests = []
            plain = [value for value in values if value is not None]
            if any(value is None for value in values):
                tests.append(f"{source} IS NULL")
            if how == "equals" and plain:
                tests.append(f"{source} = {_literal_sql(plain[0])}" if len(plain) == 1 else
                             f"{source} IN ({', '.join(_literal_sql(value) for value in plain)})")
            elif how in ("contains", "starts_with"):
                keyword = "ILIKE" if insensitive else "LIKE"
                tests += [f"{source} {keyword} " + _quote(("%" if how == "contains" else "") + _escape_like(value) + "%")
                          for value in plain]
            elif how == "regex":
                tests += [f"{source} {'~*' if insensitive else '~'} {_quote(value)}" for value in plain]
            test = " OR ".join(tests)
        lines.append(f"{INDENT}WHEN {test} THEN {_literal_sql(case['then'])}")
    if "default" in spec:
        lines.append(f"{INDENT}ELSE {_literal_sql(spec['default'])}")
    lines.append("END")
    return "\n".join(lines)


def _render(kind: str, spec: dict) -> str:
    if kind == rules.DERIVE:
        return _sql(spec["expr"])
    if kind == rules.CONSTANT:
        return _literal_sql(spec["value"])
    if kind == rules.FILTER:
        if "keep" in spec:
            return "WHERE " + _sql(spec["keep"])
        return "WHERE NOT (" + _sql(spec["drop"]) + ")"
    return _translate_sql(spec)


def to_sql(kind: str, spec: dict) -> str:
    """The spec as SQL; NoSql (saying why) when it has no exact SQL form."""
    if kind not in SQL_KINDS:
        raise NoSql(f"A {kind.replace('_', ' ')} rule is a setting, not logic: it is edited as JSON.")
    if not isinstance(spec, dict):
        raise NoSql("The spec is not a JSON object.")
    extra = sorted(set(spec) - _KEYS[kind] - set(KEPT))
    if extra:
        raise NoSql(f"SQL cannot show {', '.join(map(repr, extra))}: edit this spec as JSON.")
    try:
        _check(kind, spec)
    except SqlError as error:
        raise NoSql(f"Fix the JSON first. {error}") from None
    try:
        text = _render(kind, spec)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, NoSql):
            raise
        raise NoSql("This spec has no SQL form here: edit it as JSON.") from None
    # Exact or not offered: the SQL, read back, must be this very rule.
    try:
        again = from_sql(kind, text, spec)
    except SqlError as error:
        raise NoSql(f"This spec has no exact SQL form ({error}).") from None
    if canonical(kind, again) != canonical(kind, spec):
        raise NoSql("This spec cannot be shown as SQL without changing what it does.")
    return text


# ============================================================================ sameness


def canonical(kind: str, spec: dict) -> dict:
    """A spec with every way of writing the same thing written one way: literals bare,
    defaults stated, chains flat, one-item lists as their item, keep NOT x as drop x."""
    spec = copy.deepcopy(spec)
    if kind == rules.FILTER and "keep" in spec:
        keep = _canon(spec.pop("keep"))
        if isinstance(keep, dict) and keep.get("op") == "not":
            spec["drop"] = keep["args"][0]
        else:
            spec["keep"] = keep
    for key in ("expr", "drop", "source"):
        if key in spec:
            spec[key] = _canon(spec[key])
    if kind == rules.TRANSLATE:
        spec.setdefault("match", "equals")
        spec.setdefault("case_insensitive", True)
        for case in spec.get("cases", []):
            if isinstance(case.get("when"), list):
                # Any one value matching is a match: the blank (tested apart) first.
                whens = [value for value in case["when"] if value is None] + \
                        [value for value in case["when"] if value is not None]
                case["when"] = whens[0] if len(whens) == 1 else whens
    if kind == rules.CONSTANT and isinstance(spec.get("value"), dict) and set(spec["value"]) == {"lit"}:
        spec["value"] = spec["value"]["lit"]
    return spec


_DEFAULTS = {"round": {"digits": 2}, "lpad": {"fill": "0"}, "regex_extract": {"group": 1},
             "regex_replace": {"replacement": ""}, "eq": {"case_insensitive": True},
             "ne": {"case_insensitive": True}, "in": {"case_insensitive": True},
             "null_if": {"case_insensitive": True}, "contains": {"case_insensitive": True},
             "starts_with": {"case_insensitive": True}, "concat": {"sep": ""}}


def _canon(node):
    if isinstance(node, dict) and set(node) == {"lit"}:
        value = node["lit"]
        return {"lit": value} if isinstance(value, str) and value.startswith(("src:", "silver:")) else value
    if not isinstance(node, dict) or "op" not in node:
        return node
    out = {key: value for key, value in node.items()}
    out["args"] = [_canon(arg) for arg in node.get("args", [])]
    for name, value in _DEFAULTS.get(node["op"], {}).items():
        out.setdefault(name, value)
    if node["op"] == "lpad":
        out["fill"] = str(out["fill"])[:1] or "0"
    if node["op"] == "regex_replace":
        out["replacement"] = re.sub(r"\$\{(\d)\}", r"$\1", str(out["replacement"]))
    if node["op"] == "contains" and isinstance(out.get("value"), list) and len(out["value"]) == 1:
        out["value"] = out["value"][0]
    if node["op"] == "when":
        out["cases"] = [{"if": _canon(case["if"]), "then": _canon(case["then"])} for case in node["cases"]]
        if "else" in node:
            out["else"] = _canon(node["else"])
    if node["op"] == "null_if" and out["args"] and isinstance(out["args"][0], dict) \
            and out["args"][0].get("op") == "null_if" and out["args"][0]["case_insensitive"] == out["case_insensitive"]:
        inner = out["args"][0]
        out = {**out, "args": inner["args"], "values": list(inner["values"]) + list(out["values"])}
    if node["op"] in ("and", "or", "add", "mul") or (node["op"] == "concat" and not out.get("sep")):
        flat = []
        for arg in out["args"]:
            if isinstance(arg, dict) and arg.get("op") == node["op"] and \
                    {key: value for key, value in arg.items() if key != "args"} == \
                    {key: value for key, value in out.items() if key != "args"}:
                flat.extend(arg["args"])
            else:
                flat.append(arg)
        out["args"] = flat
    if node["op"] == "or":
        merged = []
        for arg in out["args"]:
            last = merged[-1] if merged else None
            if (isinstance(arg, dict) and isinstance(last, dict) and arg.get("op") == last.get("op") == "contains"
                    and arg["args"] == last["args"] and arg["case_insensitive"] == last["case_insensitive"]):
                merged[-1] = {**last, "value": _listed(last["value"]) + _listed(arg["value"])}
            else:
                merged.append(arg)
        out = merged[0] if len(merged) == 1 else {**out, "args": merged}
    if out.get("op") == "contains" and isinstance(out.get("value"), list) and len(out["value"]) == 1:
        out["value"] = out["value"][0]
    return out
