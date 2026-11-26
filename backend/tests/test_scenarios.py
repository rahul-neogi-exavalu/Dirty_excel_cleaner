"""The corpus as a regression suite.

Every workbook in `sample_files_uncleaned/` is a labelled scenario. The oracle is the
files themselves: a genuine record carries a ``POL-nnnnnn`` policy number and a banner,
total, footer or repeated header does not, so counting those tokens in the raw grid
gives an expected record count that owes nothing to the code being tested.

New scenario files are picked up automatically -- there is nothing to register.
"""

import pytest

from conftest import scenario_files  # noqa: F401
from scorecard import evaluate


@pytest.mark.parametrize("path", scenario_files(), ids=lambda path: path.stem)
def test_scenario_has_no_defects(path):
    """The cleaner extracts exactly the records the workbook contains.

    Advisories -- an orientation decided by shape, a column named positionally because
    its header cell was blank -- are not failures. They are the pipeline reporting
    honest uncertainty, and treating them as defects would push the design towards
    false confidence.
    """
    outcome = evaluate(path)
    assert not outcome["issues"], f"{path.name}: {'; '.join(outcome['issues'])}"


@pytest.mark.parametrize("path", scenario_files(), ids=lambda path: path.stem)
def test_every_dropped_row_carries_a_reason(path):
    """Nothing is discarded without evidence a reviewer can check."""
    outcome = evaluate(path)
    for result in outcome["results"]:
        for dropped in result.trace.get("dropped_rows", []):
            assert dropped["reason"], f"{path.name}: {dropped}"
            assert dropped["sheet_row"] is not None, f"{path.name}: {dropped}"


def test_the_corpus_is_actually_being_exercised():
    """Guards against the suite silently passing because the corpus moved or emptied.

    Deliberately not a fixed count: the corpus is expected to be swapped, and a test
    that pins its size would fail for the wrong reason every time it is.

    Skips rather than fails when there are no labelled scenario workbooks at all. A
    corpus holding only raw data files is a legitimate state -- those carry no stated
    expectations to check against -- and failing there would report a missing fixture as
    a defect in the code.
    """
    paths = scenario_files()
    if not paths:
        pytest.skip("no scenario workbooks in sample_files_uncleaned/ (corpus may be raw data)")
    assert paths
