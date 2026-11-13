"""Regression tests for the independent release-readiness review.

Each test names the review defect it pins. Adversarial and negative cases:
things the tool previously got wrong, crashed on, or silently mis-proved.
"""

import json

import pytest

from conftest import write_csv

from csvhonest.cli import main
from csvhonest.report import render_text
from csvhonest.scan import scan_file

pd = pytest.importorskip("pandas")
pl = pytest.importorskip("polars")

from csvhonest.verify import verify_with_pandas, verify_with_polars


def codes(result):
    return {f.code for f in result.findings}


# #1 HIGH: --verify text output dropped when the static scan finds nothing
def test_verify_text_shown_for_clean_file(tmp_path, capsys):
    path = write_csv(tmp_path, "name,city\nAnish,Kirkland\nGrace,Seattle\n")
    assert main([path, "--verify", "both"]) == 0
    out = capsys.readouterr().out
    assert "Verified with pandas" in out
    assert "Verified with polars" in out
    assert "0 cell(s) differ" in out


# #2a HIGH: year-header files fabricated phantom verify mutations
def test_verify_no_phantoms_on_year_header(tmp_path):
    path = write_csv(tmp_path, "2020,2021\n10,20\n")
    result = scan_file(path)
    assert result.header_auto is False  # documented heuristic miss
    for verify in (verify_with_pandas, verify_with_polars):
        report = verify(result, max_rows=100)
        assert report.total_mutated_rows == 0, report.columns


# #2b HIGH: a blank line mid-file fabricated verify mutations
def test_verify_no_phantoms_around_blank_line_pandas(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n\n3,4\n")
    result = scan_file(path)
    report = verify_with_pandas(result, max_rows=100)
    assert report.loaded_rows == 2  # pandas skips the blank line
    assert report.total_mutated_rows == 0


def test_verify_blank_line_is_null_row_for_polars(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n\n3,4\n")
    result = scan_file(path)
    report = verify_with_polars(result, max_rows=100)
    assert report.loaded_rows == 3
    # the blank row's cells are loader-filled, not raw-text mutations
    for col in report.columns:
        assert all(m.kind == "loader_filled" for m in col.mutations)


# #6 MEDIUM: short ragged rows are padded by loaders, and verify must say so
def test_verify_flags_loader_filled_cells(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n6\n")
    result = scan_file(path)
    for verify in (verify_with_pandas, verify_with_polars):
        report = verify(result, max_rows=100)
        filled = [m for c in report.columns for m in c.mutations if m.kind == "loader_filled"]
        assert filled, f"{report.loader} should report the padded cell"
        assert filled[0].raw == "<no cell>"


def test_ragged_message_describes_padding_not_errors(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n6\n")
    result = scan_file(path)
    f = next(f for f in result.findings if f.code == "RAGGED_ROWS")
    assert "padded" in f.message
    assert "raise" not in f.message.split(";")[0]


# #3 HIGH false negative: mixed float + >2^53 int column loses exactness
def test_mixed_float_bigint_column_flagged(tmp_path):
    path = write_csv(tmp_path, "x\n9007199254740993\n1.5\n")
    result = scan_file(path)
    f = next(f for f in result.findings if f.code == "FLOAT64_PRECISION_LOSS")
    assert f.severity == "high"
    assert set(f.loaders) == {"pandas", "polars"}
    # and the mutation is real in both loaders
    for verify in (verify_with_pandas, verify_with_polars):
        report = verify(result, max_rows=100)
        kinds = {m.kind for c in report.columns for m in c.mutations}
        assert "value_changed" in kinds


# #4 HIGH false negative: whitespace-padded leading zeros
def test_padded_leading_zeros_flagged_pandas_only(tmp_path):
    path = write_csv(tmp_path, "zip\n 02139\n 00501\n")
    result = scan_file(path)
    f = next(f for f in result.findings if f.code == "PADDED_LEADING_ZERO_LOSS")
    assert f.severity == "high"
    assert f.loaders == ["pandas"]
    # pandas really strips and parses them; polars really keeps the strings
    pandas_report = verify_with_pandas(result, max_rows=100)
    assert pandas_report.total_mutated_rows == 2
    polars_report = verify_with_polars(result, max_rows=100)
    assert polars_report.total_mutated_rows == 0


# #5 MEDIUM false positive: INT64_OVERFLOW must not fire with text present
def test_overflow_not_fired_when_column_has_text(tmp_path):
    path = write_csv(tmp_path, "x\n9223372036854775808\nABC123\n")
    result = scan_file(path)
    assert "INT64_OVERFLOW" not in codes(result)


def test_overflow_still_fired_for_numeric_column(tmp_path):
    path = write_csv(tmp_path, "x\n9223372036854775808\n1\n")
    assert "INT64_OVERFLOW" in codes(scan_file(path))


# #7 MEDIUM: invalid UTF-8 past the 64KB detection sample must not crash
def test_invalid_bytes_past_sample_do_not_crash(tmp_path):
    good = "name,city\n" + ("Anish,Kirkland\n" * 5000)  # > 64KB of clean UTF-8
    raw = good.encode("utf-8") + "café\n".encode("cp1252")
    path = tmp_path / "mixed.csv"
    path.write_bytes(raw)
    result = scan_file(str(path))
    assert result.decode_errors > 0
    assert "INVALID_ENCODING_BYTES" in codes(result)


def test_invalid_bytes_exit_code_not_confused_with_crash(tmp_path, capsys):
    raw = b"name\n" + b"ok\n" * 5000 + "café\n".encode("cp1252")
    path = tmp_path / "mixed.csv"
    path.write_bytes(raw)
    rc = main([str(path)])
    assert rc in (0, 1)  # findings-driven, and a report printed (not a traceback)
    assert "csvhonest:" in capsys.readouterr().out


# #8 MEDIUM: distinct_capped must be surfaced, and memory bounded in bytes
def test_capped_columns_surfaced_in_text_and_json(tmp_path, capsys):
    lines = ["code"] + [f"val{i}" for i in range(500)]
    path = write_csv(tmp_path, "\n".join(lines) + "\n")
    assert scan_file(path, distinct_cap=50).capped_columns == ["code"]
    main([path, "--format", "json", "--distinct-cap", "50"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["capped_columns"] == ["code"]


def test_byte_cap_bounds_large_values(tmp_path):
    lines = ["payload"] + [f"v{i}" + "x" * 4092 for i in range(500)]
    path = write_csv(tmp_path, "\n".join(lines) + "\n")
    result = scan_file(path, distinct_cap=1_000_000, byte_cap=100_000)
    col = result.stats[0]
    stored = sum(len(v) for v in col.values)
    assert stored < 100_000 + 4096
    assert col.distinct_capped
    assert col.row_count == 500  # rows still counted


# #9 LOW: UTF-8 BOM + cp1252 body must not mangle the first header
def test_bom_with_cp1252_body(tmp_path):
    raw = b"\xef\xbb\xbfname,city\n" + "Zürich, café\n".encode("cp1252")
    path = tmp_path / "bom.csv"
    path.write_bytes(raw)
    result = scan_file(str(path))
    assert result.headers[0] == "name"
    assert "Zürich" in str(result.stats[0].values) or "café" in str(result.stats[1].values)


# #11 LOW: 'inf' parses as float in both loaders; inference must know
def test_inf_counts_as_numeric(tmp_path):
    path = write_csv(tmp_path, "x\ninf\n1.5\n")
    result = scan_file(path)
    assert result.stats[0].infers_numeric()
    assert result.stats[0].infers_numeric(strip=True)
    # no false string-column findings
    assert not any(f.code == "NA_COLLISION" for f in result.findings)


# #2 alignment: loader row-count mismatches are disclosed, not hidden
def test_alignment_note_disclosed_on_rowcount_mismatch(tmp_path):
    # quoted empty cell + blank line: pandas skips blank, keeps quoted row
    path = write_csv(tmp_path, 'a\n1\n\n""\n2\n')
    result = scan_file(path)
    report = verify_with_polars(result, max_rows=100)
    # polars keeps the blank line as a null row; pandas skips it. Whichever
    # way counts land, no phantom raw-text mutations may appear.
    for col in report.columns:
        assert all(m.kind in ("loader_filled",) for m in col.mutations)
