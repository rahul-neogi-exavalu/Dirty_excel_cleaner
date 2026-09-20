"""A batch must survive a bad file, and must refuse to look trustworthy when it is wrong.

These two concerns are tested together because they are the same promise from a user's
point of view: what reaches the tables is either correct, or it is loudly not.
"""

import shutil

import polars as pl

import pytest

from ahi_clean import cli, contracts, failures

from conftest import HEADER, one, records, scenario_files

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def a_real_workbook():
    paths = scenario_files()
    if not paths:
        pytest.skip("no scenario workbooks in the corpus")
    return paths[0]


@pytest.fixture
def batch(tmp_path):
    """A folder holding one good workbook and every kind of bad one."""
    source = tmp_path / "in"
    source.mkdir()
    (source / "a_corrupt.xlsx").write_bytes(b"this is not a zip file")
    (source / "b_encrypted.xlsx").write_bytes(OLE_MAGIC + b"\x00" * 512)
    (source / "c_legacy.xls").write_bytes(OLE_MAGIC + b"\x00" * 512)
    (source / "d_empty.xlsx").write_bytes(b"")
    shutil.copy(a_real_workbook(), source / "z_good.xlsx")
    return source, tmp_path / "out", tmp_path / "aud"


# --------------------------------------------------------------------------- #
# A bad file must not take the batch down
# --------------------------------------------------------------------------- #


def test_one_unreadable_file_does_not_stop_the_others(batch):
    """The confirmed defect: a corrupt workbook aborted the run and everything after it."""
    source, out, aud = batch
    code = cli.main([str(source / "*.xls"), str(source / "*.xlsx"), "--out", str(out), "--audit", str(aud)])

    assert code == cli.EXIT_PROBLEMS
    assert list(out.glob("z_good*.csv")), "the valid workbook produced no output"


def test_every_kind_of_bad_file_is_classified_distinctly(batch):
    source, out, aud = batch
    paths = cli.resolve_inputs([str(source / "*.xls"), str(source / "*.xlsx")])
    _outcomes, failed = cli.clean_batch(paths, out, aud)

    kinds = {failure.source.name: failure.kind for failure in failed}
    assert kinds["a_corrupt.xlsx"] == failures.CORRUPT
    assert kinds["b_encrypted.xlsx"] == failures.ENCRYPTED
    assert kinds["c_legacy.xls"] == failures.UNSUPPORTED_FORMAT
    assert "z_good.xlsx" not in kinds


def test_a_failure_carries_advice_a_person_can_act_on(batch):
    source, out, aud = batch
    paths = cli.resolve_inputs([str(source / "*.xlsx")])
    _outcomes, failed = cli.clean_batch(paths, out, aud)

    for failure in failed:
        assert failure.advice
        assert failure.as_dict()["kind"] == failure.kind


def test_an_unexpected_error_keeps_its_traceback_out_of_the_console(tmp_path, monkeypatch):
    source = tmp_path / "in"
    source.mkdir()
    shutil.copy(a_real_workbook(), source / "boom.xlsx")

    def explode(*_args, **_kwargs):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(cli, "read_workbook", explode)
    paths = cli.resolve_inputs([str(source / "*.xlsx")])
    _outcomes, failed = cli.clean_batch(paths, tmp_path / "out", tmp_path / "aud")

    assert [failure.kind for failure in failed] == [failures.UNEXPECTED]
    trace = tmp_path / "aud" / "boom.error.txt"
    assert trace.exists()
    assert "something nobody anticipated" in trace.read_text(encoding="utf-8")


def test_a_clean_batch_still_reports_success(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    shutil.copy(a_real_workbook(), source / "good.xlsx")
    code = cli.main([str(source / "*.xlsx"), "--out", str(tmp_path / "o"), "--audit", str(tmp_path / "a")])
    assert code == cli.EXIT_OK


# --------------------------------------------------------------------------- #
# Contracts: the output must refuse to look trustworthy when it is not
# --------------------------------------------------------------------------- #


def test_a_clean_extraction_violates_nothing():
    assert contracts.check_result(one([HEADER] + records(6))) == []


def test_a_record_vanishing_without_a_logged_reason_is_a_violation():
    """A row may only leave the output by being classified and logged."""
    result = one([HEADER] + records(6))
    result.frame = result.frame.head(-1)
    assert [item.check for item in contracts.check_result(result)] == [contracts.ROW_CONSERVATION]


def test_an_emptied_key_column_is_a_violation():
    """The signature of the misalignment class: labels present, values one place left."""
    result = one([HEADER] + records(6))
    result.frame = result.frame.with_columns(pl.lit(None, dtype=pl.String).alias("policynumber"))
    checks = {item.check for item in contracts.check_result(result)}
    assert contracts.EMPTY_KEY_COLUMN in checks


def test_the_key_check_reads_types_recorded_at_coercion_not_surviving_values():
    """Otherwise it could never fire -- an all-null column has nothing left to identify it."""
    result = one([HEADER] + records(6))
    assert result.trace["inferred_types"]["policynumber"] == "id_string"
    result.frame = result.frame.with_columns(pl.lit(None, dtype=pl.String).alias("policynumber"))
    assert any(
        item.check == contracts.EMPTY_KEY_COLUMN for item in contracts.check_result(result)
    )


def test_an_empty_column_with_no_stated_cause_is_a_violation():
    result = one([HEADER] + records(6))
    result.frame = result.frame.with_columns(pl.lit(None, dtype=pl.String).alias("insurancecompanyname"))
    checks = {item.check for item in contracts.check_result(result)}
    assert contracts.UNEXPLAINED_EMPTY_COLUMN in checks


def test_an_empty_column_the_pipeline_explained_is_accepted():
    result = one([HEADER] + records(6))
    result.frame = result.frame.with_columns(pl.lit(None, dtype=pl.String).alias("insurancecompanyname"))
    result.trace["empty_columns"] = ["insurancecompanyname"]
    checks = {item.check for item in contracts.check_result(result)}
    assert contracts.UNEXPLAINED_EMPTY_COLUMN not in checks


def test_duplicate_column_names_are_impossible_to_construct():
    """The guarantee moved into the type system when the dataframe library changed.

    polars refuses a frame with repeated column names outright, so the contract that
    used to catch this can no longer fire through the normal path. Asserting the
    stronger property is more honest than asserting a check that cannot run.
    """
    result = one([HEADER] + records(6))
    with pytest.raises(Exception, match="more than one occurrence"):
        result.frame.columns = list(result.frame.columns[:-1]) + ["premium"]


def test_a_removed_total_must_reconcile_with_the_rows_that_were_kept():
    data = records(5)
    total = sum(row[4] for row in data)
    rows = [HEADER] + data + [["Grand Total", None, None, None, total, None, None, None]]

    assert contracts.check_result(one(rows)) == []

    tampered = one(rows)
    tampered.frame = tampered.frame.head(-2)
    checks = {item.check for item in contracts.check_result(tampered)}
    assert contracts.TOTAL_RECONCILIATION in checks


def test_a_violation_fails_the_run_with_its_own_exit_code(tmp_path, monkeypatch):
    source = tmp_path / "in"
    source.mkdir()
    shutil.copy(a_real_workbook(), source / "good.xlsx")

    monkeypatch.setattr(
        contracts,
        "check_all",
        lambda results: [contracts.Violation("t", contracts.ROW_CONSERVATION, "forced")],
    )
    code = cli.main([str(source / "*.xlsx"), "--out", str(tmp_path / "o"), "--audit", str(tmp_path / "a")])
    assert code == cli.EXIT_CONTRACT_VIOLATION


def test_the_corpus_passes_every_contract():
    """A contract that only ever fires on synthetic damage is not proof of much."""
    from ahi_clean.extract import extract_sheet
    from ahi_clean.reader import read_workbook

    for path in scenario_files():
        results = [result for grid in read_workbook(path) for result in extract_sheet(grid)]
        violations = contracts.check_all(results)
        assert not violations, f"{path.name}: {[item.as_dict() for item in violations]}"


# --------------------------------------------------------------------------- #
# The pandas cutover
# --------------------------------------------------------------------------- #


def test_the_pipeline_does_not_import_pandas():
    """polars everywhere, asserted rather than assumed.

    pandas is still installed here, so a plain import would succeed and prove nothing.
    What matters is that nothing the pipeline loads pulls it in.

    Checked in a fresh interpreter rather than by clearing `sys.modules` in this one.
    Re-importing the package in place leaves two copies of every module registered, and
    anything that later looks a function up by name -- pickling for a process pool, for
    instance -- then fails to recognise its own object.
    """
    import subprocess
    import sys as _sys

    from conftest import ROOT

    probe = (
        "import sys;"
        "sys.path.insert(0, r'" + str(ROOT / "src") + "');"
        "import ahi_clean.cli, ahi_clean.extract, ahi_clean.orchestrate,"
        " ahi_clean.join, ahi_clean.pivot, ahi_clean.coerce;"
        "sys.exit(1 if 'pandas' in sys.modules else 0)"
    )
    result = subprocess.run([_sys.executable, "-c", probe], capture_output=True)
    assert result.returncode == 0, "something in the pipeline still imports pandas"


def test_requirements_pin_polars_and_not_pandas():
    from conftest import ROOT

    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "polars" in text
    assert "pandas" not in text


# --------------------------------------------------------------------------- #
# Parallel batches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("executor", ["thread", "process"])
def test_parallel_output_is_identical_to_sequential(tmp_path, executor):
    """Workers must change how fast a batch runs, never what it produces."""
    source = tmp_path / "in"
    source.mkdir()
    for index in range(4):
        shutil.copy(a_real_workbook(), source / f"book_{index}.xlsx")
    paths = cli.resolve_inputs([str(source / "*.xlsx")])

    serial_dir = tmp_path / "serial"
    cli.clean_batch(paths, serial_dir, tmp_path / "sa", workers=1)
    parallel_dir = tmp_path / "parallel"
    cli.clean_batch(paths, parallel_dir, tmp_path / "pa", workers=4, executor=executor)

    serial = {path.name: path.read_bytes() for path in serial_dir.glob("*.csv")}
    parallel = {path.name: path.read_bytes() for path in parallel_dir.glob("*.csv")}
    assert serial and serial == parallel


def test_parallel_results_keep_the_input_order(tmp_path):
    """Completion order must not leak into the output, so runs stay diff-comparable."""
    source = tmp_path / "in"
    source.mkdir()
    for index in range(4):
        shutil.copy(a_real_workbook(), source / f"book_{index}.xlsx")
    paths = cli.resolve_inputs([str(source / "*.xlsx")])

    outcomes, _failed = cli.clean_batch(
        paths, tmp_path / "o", tmp_path / "a", workers=4, executor="thread"
    )
    assert [outcome["source"] for outcome in outcomes] == paths


def test_a_bad_file_does_not_stop_a_parallel_batch(batch):
    """The isolation must hold across a process boundary, where tracebacks do not travel."""
    source, out, aud = batch
    paths = cli.resolve_inputs([str(source / "*.xls"), str(source / "*.xlsx")])
    outcomes, failed = cli.clean_batch(paths, out, aud, workers=4, executor="process")

    assert len(outcomes) == 1
    kinds = {failure.source.name: failure.kind for failure in failed}
    assert kinds["a_corrupt.xlsx"] == failures.CORRUPT
    assert kinds["c_legacy.xls"] == failures.UNSUPPORTED_FORMAT
