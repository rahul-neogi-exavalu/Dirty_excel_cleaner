"""Shared fixtures.

Two kinds of test live in this suite, deliberately separated:

* **Scenario tests** run against whatever workbooks are in `sample_files_uncleaned/`.
  They adapt automatically, because that corpus is expected to change.
* **Behaviour tests** build their sheets in memory. They pin what the code must do and
  must not break when the corpus is swapped, so they never name a file.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    """API tests run as a signed-in admin, and never write job history to the real
    app database that backend/.env may point at. Auth itself is tested in test_auth.py."""
    try:
        from api import auth, config
        from api.main import app
    except ImportError:  # the cleaning-only suites run without the API's dependencies
        yield None
        return
    monkeypatch.setattr(config, "APP_DB_CONFIGURED", False)
    user = auth.User(
        user_id="00000000-0000-4000-8000-000000000001", user_name="Test Reviewer",
        user_email_id="reviewer@example.com", password_hash="", is_admin=True, user_phone_number=None,
        is_active=True, must_change_password=False, last_login_at=None, expiry_date=None,
    )
    app.dependency_overrides[auth.current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(auth.current_user, None)

SAMPLES = ROOT / "sample_files_uncleaned"
SKIP_SHEETS = ("SCENARIO_INFO", "README")

from ahi_clean.extract import extract_sheet  # noqa: E402
from ahi_clean.reader import SheetGrid, read_workbook  # noqa: E402

HEADER = [
    "profitCenterName",
    "ProfitCenterNumber",
    "Producer/AgencyName",
    "InsuranceCompanyName",
    "Premium",
    "PolicyNumber",
    "AccountingEffectiveDate",
    "Commission%",
]

NAMES = [
    "profitcentername",
    "profitcenternumber",
    "producer_agencyname",
    "insurancecompanyname",
    "premium",
    "policynumber",
    "accountingeffectivedate",
    "commission",
]

ZONES = ["South Zone", "West Zone", "East Zone", "Central", "North Zone", "Mid Zone"]
AGENCIES = ["Pinnacle Agency Partners", "Apex Insurance Brokers", "Metro Agency Group"]
CARRIERS = ["National Indemnity Co", "Heritage Casualty Co", "Global Assurance Corp"]


def record(index):
    """One realistic data row: text, code, text, text, float, id, date, float."""
    return [
        f"{ZONES[index % len(ZONES)]} PC",
        1005 + index,
        AGENCIES[index % len(AGENCIES)],
        CARRIERS[index % len(CARRIERS)],
        10000.55 + index * 1111.11,
        f"POL-{200001 + index}",
        f"2026-01-{(index % 28) + 1:02d}",
        5.0 + index,
    ]


def records(count, start=0):
    return [record(index) for index in range(start, start + count)]


def blanks(count, width=8):
    return [[None] * width for _ in range(count)]


def sheet(rows, name="Report"):
    """Extract a hand-built sheet, returning every region it yields."""
    return extract_sheet(SheetGrid(name=name, rows=[list(row) for row in rows]))


def one(rows, name="Report"):
    """Extract a hand-built sheet that is expected to hold exactly one table."""
    found = sheet(rows, name)
    assert len(found) == 1, f"expected one region, got {len(found)}"
    return found[0]


def kinds(result):
    return {row["classification"] for row in result.trace["dropped_rows"]}


# --------------------------------------------------------------------------- #
# Corpus access, for the scenario suite only
# --------------------------------------------------------------------------- #


def workbook(stem: str):
    path = SAMPLES / f"{stem}.xlsx"
    if not path.exists():
        pytest.skip(f"{path.name} is not in the current corpus")
    return [grid for grid in read_workbook(path) if grid.name.upper() not in SKIP_SHEETS]


def tables(stem: str):
    return [result for grid in workbook(stem) for result in extract_sheet(grid)]


def scenario_files():
    """Every scenario workbook currently in the corpus, masters excluded."""
    return sorted(
        path for path in SAMPLES.glob("*.xlsx") if "master" not in path.name.lower()
    )
