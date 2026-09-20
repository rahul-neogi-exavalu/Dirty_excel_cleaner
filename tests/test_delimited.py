"""CSV and other delimited files: one sheet, same grid, same reasoning."""

import pytest

from ahi_clean import rowclass
from ahi_clean.extract import extract_sheet
from ahi_clean.reader import read_workbook

HEADER = "profitCenterName,ProfitCenterNumber,Producer/AgencyName,Premium,PolicyNumber"


def rows(count=6, sep=","):
    return "\n".join(
        sep.join(
            [f"Zone {index} PC", f"{1005 + index}", "Metro Agency Group",
             f"{10000.55 + index:.2f}", f"POL-{200001 + index}"]
        )
        for index in range(count)
    )


def written(tmp_path, text, name="report.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return path


def only_table(path):
    grids = read_workbook(path)
    assert len(grids) == 1, "a delimited file is one sheet by definition"
    results = extract_sheet(grids[0])
    assert len(results) == 1
    return grids[0], results[0]


def test_a_csv_is_read_as_a_single_sheet(tmp_path):
    grid, result = only_table(written(tmp_path, f"{HEADER}\n{rows()}\n"))
    assert grid.name == "report"
    assert len(result.frame) == 6
    assert result.frame.columns[0] == "profitcentername"


def test_the_same_structural_rules_apply_to_a_csv(tmp_path):
    """Banner, subtotal and footer are found by the same reasoning as in a workbook."""
    body = rows(3)
    total = sum(10000.55 + index for index in range(3))
    text = (
        "ACME COMMISSION REPORT,,,,\nConfidential,,,,\n,,,,\n"
        f"{HEADER}\n{body}\n"
        f"Subtotal,,,{total:.2f},\n*** End of Report ***,,,,\n"
    )
    _grid, result = only_table(written(tmp_path, text))
    assert len(result.frame) == 3
    kinds = {row["classification"] for row in result.trace["dropped_rows"]}
    assert rowclass.FOOTER in kinds
    assert kinds & {rowclass.SUBTOTAL, rowclass.GRAND_TOTAL}


@pytest.mark.parametrize(
    "sep,name", [(",", "a.csv"), (";", "b.csv"), ("\t", "c.tsv"), ("|", "d.txt")]
)
def test_the_delimiter_is_detected_not_assumed(tmp_path, sep, name):
    """A semicolon export read as comma-delimited collapses into one column."""
    text = sep.join(HEADER.split(",")) + "\n" + rows(6, sep) + "\n"
    _grid, result = only_table(written(tmp_path, text, name))
    assert result.frame.shape == (6, 5)


def test_a_file_in_a_single_byte_encoding_still_reads(tmp_path):
    text = (
        "Centre,Producer,Premium,PolicyNumber\n"
        + "\n".join(
            f"Zone {i},Société Générale,{10000.5 + i:.2f},POL-{200001 + i}" for i in range(6)
        )
        + "\n"
    )
    grid, result = only_table(written(tmp_path, text, encoding="cp1252"))
    assert "cp1252" in grid.source_format
    assert "Société Générale" in result.frame["producer"].to_list()


def test_an_error_written_into_the_text_is_nulled(tmp_path):
    body = rows(5).splitlines()
    body[2] = body[2].replace("Metro Agency Group", "#VALUE!")
    text = f"{HEADER}\n" + "\n".join(body) + "\n"
    grid, result = only_table(written(tmp_path, text))
    assert grid.error_cells
    assert result.frame["producer_agencyname"][2] is None


def test_ragged_rows_are_padded_not_rejected(tmp_path):
    text = f"{HEADER}\n" + "\n".join(
        line.rsplit(",", 1)[0] if index == 2 else line
        for index, line in enumerate(rows(6).splitlines())
    ) + "\n"
    _grid, result = only_table(written(tmp_path, text))
    assert result.frame.shape == (6, 5)
    assert result.frame["policynumber"][2] is None


def test_quoted_fields_keep_their_commas(tmp_path):
    text = (
        "Centre,Address,Premium,PolicyNumber\n"
        + "\n".join(
            f'Zone {i},"9515 Delegates Row, Indianapolis",{10000.5 + i:.2f},POL-{200001 + i}'
            for i in range(6)
        )
        + "\n"
    )
    _grid, result = only_table(written(tmp_path, text))
    assert result.frame.shape == (6, 4)
    assert result.frame["address"][0] == "9515 Delegates Row, Indianapolis"


def test_a_leading_zero_survives_a_csv(tmp_path):
    """Better than .xlsx, where Excel destroys it before the pipeline sees the file."""
    text = "Centre,Code,Premium,PolicyNumber\n" + "\n".join(
        f"Zone {i},00{1005 + i},{10000.5 + i:.2f},POL-{200001 + i}" for i in range(6)
    ) + "\n"
    _grid, result = only_table(written(tmp_path, text))
    assert result.frame["code"][0].startswith("00")


def test_a_csv_no_longer_reports_as_an_unsupported_format(tmp_path):
    from ahi_clean import cli

    path = written(tmp_path, f"{HEADER}\n{rows()}\n")
    code = cli.main([str(path), "--out", str(tmp_path / "o"), "--audit", str(tmp_path / "a")])
    assert code == cli.EXIT_OK
    assert list((tmp_path / "o").glob("*.csv"))
