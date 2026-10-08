"""Write the 10 test workbooks for the profit-center-level rules, with a README.

    python backend/tools/make_pc_level_test_files.py

Each workbook drives one requirement: the date priority and the reviewer's date column,
sheets that are not data, a year-to-date file and the month appended after it, a month
sent twice, a revision, a companion list joined in Silver (with a repeated key), a
summary the profit center aggregated, and a file a data sheet rejects whole.

Column names are deliberately inconsistent: within a file the headers mix styles
(``Acct_Eff_Date``, ``comm pct``, ``MARKET PROVIDER``, ``Policy #``), and each profit
center writes the same field its own way, so the mapping (saved, exact, fuzzy, semantic,
AI, and the reviewer) is exercised on every file. Files of one profit center's series
(03-06) keep that profit center's own headers, as a real sender does, so its months can
share one bronze table.

Deterministic (fixed seed). Output: enhancements/test-files/pc-level/.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

OUT = Path(__file__).resolve().parents[2] / "enhancements" / "test-files" / "pc-level"
RNG = random.Random(20261008)

# The required fields (backend/config/bronze_required_columns.csv), as each sender writes
# them. Keys are the business fields; values the header each file uses.
FIELDS = ["aed", "commission_pct", "gross_commission", "insurer", "market_provider", "ped", "policy_number",
          "premium", "producer", "producer_commission_amount", "producer_commission_pct", "revenue",
          "transaction_detail", "ted"]

STYLES = {
    # PC0796: snake, Camel and abbreviations mixed.
    "PC0796": {"aed": "Acct_Eff_Date", "commission_pct": "comm pct", "gross_commission": "GrossCommAmt",
               "insurer": "Insurer Name", "market_provider": "MARKET PROVIDER", "ped": "POLICY EFFECTIVE DATE",
               "policy_number": "Policy #", "premium": "Written Premium", "producer": "Producer/Agency",
               "producer_commission_amount": "ProducerCommAmt", "producer_commission_pct": "producer_comm_%",
               "revenue": "Net Revenue", "transaction_detail": "Txn Type", "ted": "Trans Eff Dt"},
    # PC0843: long business words, one in capitals, one with a trailing unit.
    "PC0843": {"aed": "Accounting Effective", "commission_pct": "Commission Rate (%)",
               "gross_commission": "gross commission", "insurer": "CARRIER", "market_provider": "Mkt Provider",
               "ped": "PolicyEffDate", "policy_number": "policy_no", "premium": "Premium ($)",
               "producer": "Agency Name", "producer_commission_amount": "Agent Commission",
               "producer_commission_pct": "Agent Comm Pct", "revenue": "revenue_amt",
               "transaction_detail": "Transaction Description", "ted": "TRANSACTION_DATE"},
    # PC0515 (files 03-06): its own mix, the same in every file it sends.
    "PC0515": {"aed": "AccountingEffective Date", "commission_pct": "Comm%", "gross_commission": "Gross Comm",
               "insurer": "insurance_co", "market_provider": "Market Provider", "ped": "Pol_Ef_Dt",
               "policy_number": "POLICY NUMBER", "premium": "Prem Amt", "producer": "producer name",
               "producer_commission_amount": "Prod Comm Amt", "producer_commission_pct": "ProdCommPct",
               "revenue": "Revenue $", "transaction_detail": "trans_detail", "ted": "TransEffDate"},
    # PC0901: the transactions leave MarketProvider to the broker list (file 08).
    "PC0901": {"aed": "acctg_eff_dt", "commission_pct": "Commission %", "gross_commission": "GROSS_COMM_AMT",
               "insurer": "Insurance Company", "ped": "Policy Eff", "policy_number": "Pol Num",
               "premium": "GWP", "producer": "Writing Producer", "producer_commission_amount": "Producer Comm $",
               "producer_commission_pct": "producer commission pct", "revenue": "Revenue",
               "transaction_detail": "TxnDetail", "ted": "Txn Eff Date"},
    # PC0333: two data sheets with headers of their own.
    "PC0333": {"aed": "AED", "commission_pct": "CommPct", "gross_commission": "Gross Commission $",
               "insurer": "insurer", "market_provider": "market", "ped": "PED", "policy_number": "PolicyID",
               "premium": "premium_written", "producer": "Producer", "producer_commission_amount": "ProdComm",
               "producer_commission_pct": "Prod Comm %", "revenue": "REV", "transaction_detail": "Detail",
               "ted": "TED"},
}
CARRIERS = ["Harrier Insurance Co", "Accident Fund", "CompSource Mutual", "Tokio Marine Kiln", "WCF National"]
PRODUCERS = ["Brown & Brown", "Lockton", "Marsh", "Acme Brokers", "Hub International", "Alliant"]
DETAILS = ["New Business", "Renewal", "Endorsement", "Cancellation"]


def _day(year: int, month: int) -> date:
    return date(year, month, RNG.randint(1, 28))


def _row(style: dict, month: int, n: int, *, aed=None, ted=None, ped=None, premium=None) -> dict:
    """One transaction under ``style``'s headers. Dates default to the row's month."""
    premium = premium if premium is not None else round(RNG.uniform(400, 9000), 2)
    gross = round(premium * 0.12, 2)
    values = {
        "aed": aed or _day(2026, month), "ted": ted or _day(2026, month), "ped": ped or date(2026, month, 1),
        "commission_pct": 0.12, "gross_commission": gross, "insurer": RNG.choice(CARRIERS),
        "market_provider": RNG.choice(["Admitted", "E&S", "Specialty"]), "policy_number": f"POL-{month:02d}{n:05d}",
        "premium": premium, "producer": PRODUCERS[n % len(PRODUCERS)], "producer_commission_amount": round(gross / 2, 2),
        "producer_commission_pct": 0.06, "revenue": round(gross / 2, 2), "transaction_detail": RNG.choice(DETAILS),
    }
    return {style[field]: values[field] for field in FIELDS if field in style}


def _sheet(book, title: str, headers: list[str], rows: list[dict]) -> None:
    ws = book.create_sheet(title)
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(name) for name in headers])


def _book() -> openpyxl.Workbook:
    book = openpyxl.Workbook()
    book.remove(book.active)
    return book


def _headers(style: dict, extra: list[str] = ()) -> list[str]:
    return [style[field] for field in FIELDS if field in style] + list(extra)


def _months(style: dict, months, per_month: int = 6, start: int = 0, **dates) -> list[dict]:
    rows, n = [], start
    for month in months:
        for _ in range(per_month):
            n += 1
            rows.append(_row(style, month, n))
    return rows


# --- the ten files -----------------------------------------------------------------------


def f01_date_pick(path: Path) -> None:
    """AED is populated on every row but runs Feb-Jun (neither YTD nor monthly); TED runs
    Jan-Jun (YTD); PED is last year's. AED decides by priority and is flagged; the reviewer
    picks TED, and date_detail records TED."""
    style = STYLES["PC0796"]
    rows, n = [], 0
    for month in range(1, 7):
        for _ in range(6):
            n += 1
            rows.append(_row(style, month, n, aed=_day(2026, max(month, 2)), ted=_day(2026, month),
                             ped=_day(2025, RNG.randint(3, 11))))
    book = _book()
    _sheet(book, "Policy Txns", _headers(style), rows)
    book.save(path)


def f02_not_data_sheet(path: Path) -> None:
    """A data sheet with every required field, and a lookup sheet of producer codes (none of
    them): the lookup is left out on its own, the data sheet still goes to Bronze."""
    style = STYLES["PC0843"]
    book = _book()
    _sheet(book, "Transactions", _headers(style), _months(style, range(1, 7)))
    codes = [{"Producer Code": f"P-{index:03d}", "producer": name, "Since": 2010 + index}
             for index, name in enumerate(PRODUCERS, 1)]
    _sheet(book, "Producer Codes", ["Producer Code", "producer", "Since"], codes)
    book.save(path)


def f03_ytd(path: Path) -> list[dict]:
    """PC0515's year-to-date Jan-Jun file: the first of its series."""
    style = STYLES["PC0515"]
    rows = _months(style, range(1, 7))
    book = _book()
    _sheet(book, "ARR", _headers(style), rows)
    book.save(path)
    return rows


def f04_july(path: Path) -> None:
    """July, under another file name and sheet name, with one column more: appended into
    PC0515's Jan-Jun table (not a table of its own), whose schema evolves."""
    style = STYLES["PC0515"]
    rows = _months(style, [7], start=500)
    for row in rows:
        row["Endorsement Notes"] = RNG.choice(["", "late", "re-rated", "audit"]) or None
    book = _book()
    _sheet(book, "July 2026", _headers(style, ["Endorsement Notes"]), rows)
    book.save(path)


def f05_june_again(path: Path) -> None:
    """June sent again: a month already loaded with the Jan-Jun file. Flagged, and the
    control table rejects it."""
    style = STYLES["PC0515"]
    book = _book()
    _sheet(book, "ARR", _headers(style), _months(style, [6], start=700))
    book.save(path)


def f06_revision(path: Path, original: list[dict]) -> None:
    """The Jan-Jun file again, corrected: the same policies, premiums restated. Exactly the
    dates of file 03: the reviewer marks it a revision of 03 (by its exact name), and
    Ingest replaces 03's rows; July stays."""
    style = STYLES["PC0515"]
    premium = style["premium"]
    rows = []
    for row in original:
        fixed = dict(row)
        fixed[premium] = round(row[premium] * 1.05, 2)
        rows.append(fixed)
    book = _book()
    _sheet(book, "ARR", _headers(style), rows)
    book.save(path)


def f07_transactions(path: Path) -> None:
    """PC0901's transactions, without MarketProvider: the broker list (08) received the same
    day carries it. Writing Producer and Region are the join's keys."""
    style = STYLES["PC0901"]
    rows = _months(style, range(1, 7))
    regions = {"Brown & Brown": "East", "Lockton": "West", "Marsh": "South", "Acme Brokers": "North",
               "Hub International": "East", "Alliant": "West"}
    for index, row in enumerate(rows):
        name = row[style["producer"]]
        row[style["producer"]] = name.upper() if index % 3 == 0 else name  # case varies: matched case aside
        row["Region"] = regions[name]
    book = _book()
    _sheet(book, "Txns", _headers(style, ["Region"]), rows)
    book.save(path)


def f08_broker_list(path: Path) -> None:
    """The broker list that came with 07. 'Lockton' is listed twice (West and South):
    joining on the producer alone repeats a key and is blocked; adding Region = Territory
    as a second key makes each row match one."""
    rows = [("Brown & Brown", "Admitted", "East", 41), ("Lockton", "E&S", "West", 18),
            ("Lockton", "Specialty", "South", 7), ("Marsh", "Admitted", "South", 33),
            ("Acme Brokers", "Specialty", "North", 5), ("Hub International", "E&S", "East", 22),
            ("Alliant", "Admitted", "West", 12), ("Gallagher", "E&S", "North", 9)]
    book = _book()
    _sheet(book, "Brokers", ["Broker Account", "Mkt Provider", "Territory", "No. of Agents"],
           [dict(zip(["Broker Account", "Mkt Provider", "Territory", "No. of Agents"], row)) for row in rows])
    book.save(path)


def f09_aggregated(path: Path) -> None:
    """PC2030's premium summary, as the profit center lays it out: a sheet a month, a title,
    the header two blank rows above the partners, a blank row after each, blank spacer
    columns, a TOTALS column and row; February wrote nothing. Detected as aggregated, staged
    as _agg_stg, loaded as _agg, then into silver_aggregate as reported (product lines
    unpivoted, totals left out)."""
    partners = ["Accident Fund Insurance Company of America", "CompSource Mutual Insurance Company",
                "Convex Insurance UK Limited", "Hastings Insurance Company", "Tokio Marine Kiln - Syndicate 510",
                "United Fire & Casualty Company", "WCF National Insurance Company"]
    book = _book()
    for month in ["January", "February", "March", "April", "May", "June"]:
        ws = book.create_sheet(f"{month} 2026")
        if month == "February":
            ws.append([])
            ws.append([None, "NO PREMIUM WRITTEN IN FEBRUARY"])
            continue
        for row in (["Waypoint Premium Summary by Delegated Authority Partner"], [f"{month} 2026"], [], [],
                    ["Delegated Authority Partner", "Casualty Treaty", None, "Property Treaty", None, "Workers Comp",
                     None, "TOTALS"], [], []):
            ws.append(row)
        sums = [0.0, 0.0, 0.0]
        for name in partners:
            values = [round(RNG.choice([0, RNG.uniform(1e5, 9e7)]), 2) for _ in range(3)]
            sums = [a + b for a, b in zip(sums, values)]
            ws.append([name, values[0], None, values[1], None, values[2], None, round(sum(values), 2)])
            ws.append([])
        ws.append(["TOTALS", round(sums[0], 2), None, round(sums[1], 2), None, round(sums[2], 2), None,
                   round(sum(sums), 2)])
    book.save(path)


def f10_whole_file(path: Path) -> None:
    """Two data sheets with every required field: 'Producers' runs Jan-Jun (fit), 'Endorsements'
    runs Mar-Jun (neither YTD nor monthly). A data sheet unfit for another reason than missing
    columns rejects the whole file: both sheets are recorded REJECTED (unless the reviewer
    corrects Endorsements' dates)."""
    style = STYLES["PC0333"]
    book = _book()
    _sheet(book, "Producers", _headers(style), _months(style, range(1, 7)))
    other = dict(style, premium="Endorsement Premium", transaction_detail="Endorsement Type")
    _sheet(book, "Endorsements", _headers(other), _months(other, range(3, 7), per_month=4, start=900))
    book.save(path)


FILES = [
    ("01_PC0796_DatePick_Policy Txns_07152026.xlsx", f01_date_pick),
    ("02_PC0843_Transactions_with_Producer_Codes_07022026.xlsx", f02_not_data_sheet),
    ("03_PC0515_ARR Jan-Jun 2026_07132026.xlsx", f03_ytd),
    ("04_PC0515 July 2026 production_08142026.xlsx", f04_july),
    ("05_PC0515_ARR June resend_08202026.xlsx", f05_june_again),
    ("06_PC0515_ARR Jan-Jun 2026 revised_08252026.xlsx", f06_revision),
    ("07_PC0901_Transactional_09082026.xlsx", f07_transactions),
    ("08_PC0901_BrokerList_09082026.xlsx", f08_broker_list),
    ("09_PC2030_Waypoint Premium Summary January 1 2026 to June 30 2026_07312026.xlsx", f09_aggregated),
    ("10_PC0333_Producers and Endorsements_07202026.xlsx", f10_whole_file),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    original = None
    for name, make in FILES:
        path = OUT / name
        if make is f06_revision:
            make(path, original)
        else:
            result = make(path)
            if make is f03_ytd:
                original = result
        print("wrote", path.relative_to(OUT.parents[2]))


if __name__ == "__main__":
    main()
