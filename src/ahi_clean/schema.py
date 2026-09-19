"""Canonical schema: the field names, their aliases, and their output dtypes.

The alias dictionary is the backbone of both the orientation vote and the header
hunt -- knowing the target schema ahead of time is the strongest signal we have
for telling a header band apart from a row of data.
"""

from __future__ import annotations

import re

# Output dtypes are deliberate, not inherited from the source files:
#   * profit_center_number is an identifier, not a quantity. It arrives as the
#     number 1005 in some files and the text 'PC0001' in others; storing it as a
#     string is the only representation that survives both.
#   * commission_pct is a whole-number percent (11.06 means 11.06%). No /100.
#   * zip_code is a string so that New Jersey's 08085 keeps its leading zero.
CANONICAL_FIELDS: dict[str, dict] = {
    "profit_center_name": {
        "aliases": ["profitCenterName", "profit center name", "profit_center_name"],
        "dtype": "string",
    },
    "profit_center_number": {
        "aliases": ["ProfitCenterNumber", "profit center number", "profit_center_number", "pc number"],
        "dtype": "string",
    },
    "producer_agency_name": {
        "aliases": ["Producer/AgencyName", "producer agency name", "producer", "agency name", "agency"],
        "dtype": "string",
    },
    "insurance_company_name": {
        "aliases": ["InsuranceCompanyName", "insurance company name", "insurance company", "carrier"],
        "dtype": "string",
    },
    "premium": {"aliases": ["Premium", "premium amount", "written premium"], "dtype": "float"},
    "policy_number": {"aliases": ["PolicyNumber", "policy number", "policy no", "policy"], "dtype": "string"},
    "accounting_effective_date": {
        "aliases": ["AccountingEffectiveDate", "accounting effective date", "effective date"],
        "dtype": "date",
    },
    "commission_pct": {
        "aliases": ["Commission%", "commission pct", "commission percent", "commission"],
        "dtype": "float",
    },
    # Dimension-side fields (File6's Producer lookup sheet).
    "address": {"aliases": ["Address", "street address"], "dtype": "string"},
    "state": {"aliases": ["State"], "dtype": "string"},
    # "zip" rather than "string": the source cell holds the number 8085, so the
    # leading zero of New Jersey's 08085 is already gone by the time we read it and
    # has to be restored by width, not preserved.
    "zip_code": {"aliases": ["ZIP Code", "zip", "zipcode", "postal code"], "dtype": "zip"},
}

# Fields that make a sheet look like a transactional fact table rather than a lookup.
FACT_SIGNAL_FIELDS = {"premium", "policy_number", "accounting_effective_date", "commission_pct"}

# Fields a data row must populate to count as a record. Subtotal and total rows
# leave these empty, which is what lets us strip them by content.
RECORD_IDENTIFYING_FIELDS = {"policy_number", "accounting_effective_date"}


def normalize_key(value) -> str:
    """Collapse a header label to a comparable key.

    ``Producer/AgencyName``, ``producer_agency_name`` and ``Producer / Agency Name``
    all normalize to ``produceragencyname``.
    """
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


_ALIAS_LOOKUP: dict[str, str] = {}
for _canonical, _spec in CANONICAL_FIELDS.items():
    _ALIAS_LOOKUP[normalize_key(_canonical)] = _canonical
    for _alias in _spec["aliases"]:
        _ALIAS_LOOKUP[normalize_key(_alias)] = _canonical


def match_alias(value) -> str | None:
    """Return the canonical field name for a single header label, or None."""
    return _ALIAS_LOOKUP.get(normalize_key(value))


def match_aliases(values) -> tuple[float, dict[int, str]]:
    """Score a candidate header sequence against the canonical schema.

    Returns ``(match_rate, {position: canonical_name})`` where ``match_rate`` is the
    fraction of *non-empty* cells that resolve to a canonical field. Empty cells are
    excluded from the denominator so that a header band with a gutter hole in it --
    File1's header spans B2:J2 with F2 empty -- is not penalised for the gap.
    """
    mapping: dict[int, str] = {}
    populated = 0
    for index, value in enumerate(values):
        if value is None or str(value).strip() == "":
            continue
        populated += 1
        canonical = match_alias(value)
        if canonical is not None:
            mapping[index] = canonical
    if populated == 0:
        return 0.0, {}
    return len(mapping) / populated, mapping


def dtype_of(canonical: str) -> str:
    """Target dtype for a canonical field; unknown fields pass through as strings."""
    spec = CANONICAL_FIELDS.get(canonical)
    return spec["dtype"] if spec else "string"
