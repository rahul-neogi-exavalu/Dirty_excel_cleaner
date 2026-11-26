"""Commission arithmetic: a rate times the premium is the commission amount.

Two pairs of required columns are tied by it -- CommissionPct and GrossCommissionAmount,
ProducerCommissionPct and ProducerCommissionAmount -- both against Premium:

    amount = rate x premium

With both of a pair in hand (a file column or a value the reviewer typed), each row is
checked against it, by size (files sign a return's rate differently). With one, the other
is derived row by row when the file is staged; a derived amount takes the premium's sign.

Rates are written either as fractions (0.25) or as percents (25, or 25%). A column's
scale is told by its values: against the amounts when both are there, else by size
(anything over 1 is a percent). Derived rates are fractions, as most files write them.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

PREMIUM = "premium"
# (rate, amount): each pair's two required columns (Silver names).
PAIRS = (("commission_pct", "gross_commission_amount"),
         ("producer_commission_pct", "producer_commission_amount"))
RATES = frozenset(rate for rate, _ in PAIRS)
AMOUNTS = frozenset(amount for _, amount in PAIRS)
# A row agrees when the amount is within a dollar (amounts rounded to whole dollars), within
# 1% of itself, or the rate it implies is within this of the stated one (rates rounded to
# three places: 0.243 for 0.24302...).
DOLLAR, SHARE, RATE_STEP = 1.0, 0.01, 0.0006
# How many disagreeing rows the reviewer is shown.
EXAMPLES = 3


def numbers(series: pl.Series) -> pl.Series:
    """The values as floats; blanks and text that is no number are null. Commas, currency
    signs and spaces are dropped, (12) is -12, and 12% is 0.12."""
    if series.dtype.is_numeric():
        return series.cast(pl.Float64)
    text = series.cast(pl.String).str.strip_chars().str.replace_all(r"[,$€£\s]", "")
    negative = text.str.starts_with("(") & text.str.ends_with(")")
    text = text.str.strip_chars("()")
    percent = text.str.ends_with("%")
    value = text.str.strip_chars_end("%").cast(pl.Float64, strict=False)
    value = pl.select(pl.when(percent).then(value / 100).otherwise(value)).to_series()
    return pl.select(pl.when(negative).then(-value).otherwise(value)).to_series()


def number(text: str) -> float | None:
    """One typed value, read the way ``numbers`` reads a column's."""
    return numbers(pl.Series([text], dtype=pl.String))[0]


def typed_rate(text: str) -> float | None:
    """A rate the reviewer typed, as a fraction: 12, 12% and 0.12 are all 12%."""
    value = number(text)
    if value is None or text.strip().endswith("%"):
        return value  # a percent sign was already divided out
    return value / 100 if abs(value) > 1 else value


def _agrees(rate: pl.Series, amount: pl.Series, premium: pl.Series) -> pl.Series:
    # Sizes only: on a return some files sign the rate too (-0.1 x -89.99 written as -9.00),
    # others only the premium and amount, so the signs say nothing about the arithmetic.
    rate, amount, premium = rate.abs(), amount.abs(), premium.abs()
    difference = (rate * premium - amount).abs()
    return pl.select(
        (difference <= DOLLAR) | (difference <= amount * SHARE)
        | ((amount / premium - rate).abs() <= RATE_STEP)
    ).to_series()


def _comparable(rate: pl.Series, amount: pl.Series, premium: pl.Series) -> pl.Series:
    return rate.is_not_null() & amount.is_not_null() & premium.is_not_null() & (premium != 0)


def scale(rate: pl.Series, amount: pl.Series | None = None, premium: pl.Series | None = None) -> float:
    """1 when the rates are fractions (0.25), 0.01 when they are percents (25)."""
    if amount is not None and premium is not None:
        rows = _comparable(rate, amount, premium)
        if int(rows.sum()):
            as_fraction = int((_agrees(rate, amount, premium) & rows).sum())
            as_percent = int((_agrees(rate / 100, amount, premium) & rows).sum())
            if as_fraction or as_percent:
                return 1.0 if as_fraction >= as_percent else 0.01
    biggest = rate.abs().max()
    return 0.01 if biggest is not None and biggest > 1 else 1.0


@dataclass
class Agreement:
    rows: int  # rows with a rate, an amount and a premium that is not zero
    disagree: int
    # (row number, rate, premium, amount the rate gives, amount stated)
    examples: list[tuple[int, float, float, float, float]]
    no_premium: int  # rows with a rate and an amount but no premium (or premium 0)


def check(rate: pl.Series, amount: pl.Series, premium: pl.Series) -> Agreement:
    """Row by row, does rate x premium give the amount? ``rate`` is a fraction."""
    rows = _comparable(rate, amount, premium)
    bad = rows & ~_agrees(rate, amount, premium).fill_null(False)
    frame = pl.DataFrame({"rate": rate, "premium": premium, "amount": amount, "bad": bad}).with_row_index("row", 1)
    examples = [(row, r, p, round(abs(r) * p, 2), a) for row, r, p, a in
                frame.filter(pl.col("bad")).head(EXAMPLES).select("row", "rate", "premium", "amount").iter_rows()]
    no_premium = int((rate.is_not_null() & amount.is_not_null() & ~(premium.is_not_null() & (premium != 0))).sum())
    return Agreement(int(rows.sum()), int(bad.sum()), examples, no_premium)


def derive_amount(rate: pl.Series, premium: pl.Series) -> pl.Series:
    """rate x premium, to the cent, as text; null where either is missing. The amount
    takes the premium's sign (a return's commission is negative), whatever the rate's."""
    return (rate.abs() * premium).round(2).cast(pl.String)


def derive_rate(amount: pl.Series, premium: pl.Series) -> pl.Series:
    """amount / premium as a fraction to six places, as text; null where there is no
    premium to divide by."""
    safe = pl.select(pl.when(premium != 0).then(premium)).to_series()
    return (amount / safe).round(6).cast(pl.String)
