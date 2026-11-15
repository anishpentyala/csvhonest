"""Do not turn a skipped physical record into a proven value mutation."""

import json
from unittest.mock import patch

import pytest

from csvhonest.cli import main
from csvhonest.report import render_verification_text
from csvhonest.scan import scan_file
from csvhonest.verify import VerificationReport, _verify_generic, verify_with_pandas


def generic(tmp_path, body, columns, skips=True, max_rows=100, delimiter=","):
    path = tmp_path / "data.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(body)
    result = scan_file(str(path), has_header=True, delimiter=delimiter)
    return _verify_generic(result, "synthetic", columns, lambda v: v is None,
                           max_rows, skips)


@pytest.mark.parametrize("line", ["", " ", "\t", " \t "])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("position", ["leading", "middle", "trailing"])
def test_pandas_visible_record_policy_preserves_alignment(tmp_path, line, ending, position):
    records = ["name", "Alice", "Bob", "Carol"]
    records.insert({"leading": 0, "middle": 2, "trailing": 4}[position], line)
    report = generic(tmp_path, ending.join(records) + ending,
                     [("name", "object", ["Alice", "Bob", "Carol"])])
    assert report.load_error is None
    assert report.alignment_note is None
    assert len(report.columns) == 1
    assert report.total_mutated_rows == 0


@pytest.mark.parametrize("value", [" ", "\t", "\n", " \n\t\n ", ' " '])
def test_quoted_whitespace_is_data_including_multiline(tmp_path, value):
    quoted = '"' + value.replace('"', '""') + '"'
    report = generic(tmp_path, "name\nAlice\n" + quoted + "\nBob\n",
                     [("name", "object", ["Alice", value, "Bob"])])
    assert report.load_error is None
    assert report.total_mutated_rows == 0


@pytest.mark.parametrize("value", ["\v", "\f", "\u00a0", "\u2003"])
def test_other_unicode_whitespace_is_not_a_pandas_blank_line(tmp_path, value):
    report = generic(tmp_path, "name\nAlice\n" + value + "\nBob\n",
                     [("name", "object", ["Alice", value, "Bob"])])
    assert report.load_error is None
    assert report.total_mutated_rows == 0


@pytest.mark.parametrize("delimiter", [" ", "\t"])
def test_blank_looking_delimiter_record_is_real_empty_fields(tmp_path, delimiter):
    body = "a" + delimiter + "b\nx" + delimiter + "y\n" + delimiter + "\np" + delimiter + "q\n"
    report = generic(tmp_path, body, [("a", "object", ["x", None, "p"]),
                                     ("b", "object", ["y", None, "q"])], delimiter=delimiter)
    assert report.load_error is None
    assert report.total_mutated_rows == 0


def test_known_leading_zero_mutation_after_skipped_record_is_not_hidden(tmp_path):
    report = generic(tmp_path, "zip\n001\n \t \n002\n100\n",
                     [("zip", "int64", [1, 2, 100])])
    assert report.load_error is None
    assert {(m.raw, m.became) for m in report.columns[0].mutations} == {("001", "1"), ("002", "2")}


def test_polars_policy_keeps_space_only_and_empty_records(tmp_path):
    report = generic(tmp_path, "name\nAlice\n \n\nBob\n",
                     [("name", "String", ["Alice", " ", None, "Bob"])], skips=False)
    assert report.load_error is None
    assert report.total_mutated_rows == 0


@pytest.mark.parametrize("columns", [[("a", "str", ["wrong"])],
                                     [("a", "str", ["wrong", "rows", "extra"])],
                                     [("a", "str", ["wrong", "rows"]), ("b", "str", ["extra", "col"])]])
def test_shape_mismatch_refuses_every_cell_claim(tmp_path, columns):
    report = generic(tmp_path, "a\nfirst\nsecond\n", columns)
    assert report.load_error is not None
    assert "No cells compared" in report.alignment_note
    assert report.columns == []
    assert "VERIFICATION FAILED" in render_verification_text(report)
    assert "cell(s) differ" not in render_verification_text(report)


def test_extra_raw_fields_that_can_become_implicit_index_refuse_claims(tmp_path):
    report = generic(tmp_path, "a\nx,first\ny,second\n",
                     [("a", "str", ["first", "second"])])
    assert report.load_error is not None
    assert report.columns == []


def test_unequal_loaded_column_lengths_refuse_instead_of_index_error(tmp_path):
    report = generic(tmp_path, "a,b\nx,y\np,q\n",
                     [("a", "str", ["x", "p"]), ("b", "str", ["y"])])
    assert report.load_error is not None
    assert report.columns == []


@pytest.mark.parametrize("error", [OSError("file replaced"), UnicodeError("decode failed")])
def test_raw_read_failure_is_incomplete_verification(tmp_path, error):
    path = tmp_path / "data.csv"
    path.write_text("a\nx\n")
    result = scan_file(str(path))
    with patch("csvhonest.scan.open_text", side_effect=error):
        report = _verify_generic(result, "synthetic", [("a", "str", ["x"])],
                                 lambda v: v is None, 100, True)
    assert report.load_error is not None
    assert report.columns == []


@pytest.mark.parametrize("loader", ["pandas", "polars"])
def test_requested_failed_verification_never_exits_success(tmp_path, capsys, loader):
    path = tmp_path / "data.csv"
    path.write_text("name,city\nAlice,Seattle\nBob,Paris\n")
    failed = VerificationReport(loader, 2, 2, load_error="cannot verify alignment")
    with patch.dict("csvhonest.cli.VERIFIERS", {loader: lambda *args: failed}):
        code = main([str(path), "--verify", loader, "--format", "json"])
    assert code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["verification"][0]["load_error"]


@pytest.mark.parametrize("body", ["name\nAlice\n \nBob\nCarol\n",
                                  " \t \nname\nAlice\nBob\n",
                                  'name\nAlice\n" \n\t\n "\nBob\n',
                                  "name\nAlice\n\u00a0\nBob\n"])
def test_real_installed_pandas_has_no_phantom_mutations(tmp_path, body):
    pytest.importorskip("pandas")
    path = tmp_path / "data.csv"
    path.write_text(body)
    report = verify_with_pandas(scan_file(str(path), has_header=True), 100)
    assert report.load_error is None
    assert report.total_mutated_rows == 0


def test_real_pandas_leading_blank_and_row_cap_keep_true_mutations(tmp_path):
    pytest.importorskip("pandas")
    path = tmp_path / "data.csv"
    path.write_text(" \nzip\n001\n\t\n002\n003\n")
    report = verify_with_pandas(scan_file(str(path), has_header=True, max_rows=2), 2)
    assert report.load_error is None
    assert report.loaded_rows == 2
    assert {(m.raw, m.became) for m in report.columns[0].mutations} == {("001", "1"), ("002", "2")}
