import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLES = ROOT / "sample_files_uncleaned"

from ahi_clean.extract import extract_sheet  # noqa: E402
from ahi_clean.reader import read_workbook  # noqa: E402


def sheets(filename):
    """Every sheet of a sample workbook, extracted.

    One entry per sheet, each a list of that sheet's table regions -- normally one.
    """
    return [extract_sheet(grid) for grid in read_workbook(SAMPLES / filename)]


def tables(filename):
    """Every table region in a workbook, flattened across sheets."""
    return [region for grid in read_workbook(SAMPLES / filename) for region in extract_sheet(grid)]


@pytest.fixture(scope="session")
def file1():
    return sheets("File1_Scenarios1-4_Combined.xlsx")


@pytest.fixture(scope="session")
def file2():
    return sheets("File2_Scenarios5-8_Combined.xlsx")


@pytest.fixture(scope="session")
def file3():
    return sheets("File3_Scenario_subtotals_in_middle.xlsx")


@pytest.fixture(scope="session")
def file4():
    return sheets("File4_Scenario_transposed.xlsx")


@pytest.fixture(scope="session")
def file5():
    return sheets("File5_Scenario_MultiSheet.xlsx")


@pytest.fixture(scope="session")
def file6():
    return sheets("File6_Scenarios_MultipleSheetJoin.xlsx")
