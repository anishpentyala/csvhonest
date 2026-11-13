"""Verify mode: real-loader diffing. Requires pandas and polars."""

import pytest

pytest.importorskip("pandas")
pl = pytest.importorskip("polars")

from conftest import write_csv

from csvhonest.cli import main
from csvhonest.scan import scan_file
from csvhonest.verify import verify_with_pandas, verify_with_polars


def mutations_by_column(report):
    return {c.column: c for c in report.columns}


def test_verify_pandas_leading_zeros(tmp_path):
    path = write_csv(tmp_path, "zip\n02139\n02140\n10001\n")
    result = scan_file(path)
    report = verify_with_pandas(result, max_rows=1000)
    assert report.load_error is None
    zip_col = mutations_by_column(report)["zip"]
    assert zip_col.loaded_dtype == "int64"
    assert zip_col.mutated_rows == 2
    m = next(m for m in zip_col.mutations if m.raw == "02139")
    assert m.became == "2139"
    assert m.kind == "form_changed"


def test_verify_pandas_na_collision(tmp_path):
    path = write_csv(tmp_path, "country\nUS\nNA\nFR\n")
    report = verify_with_pandas(scan_file(path), max_rows=1000)
    col = mutations_by_column(report)["country"]
    m = next(m for m in col.mutations if m.raw == "NA")
    assert m.kind == "became_missing"
    assert m.became == "<missing>"


def test_verify_pandas_precision_loss(tmp_path):
    path = write_csv(tmp_path, "id\n9007199254740993\nNA\n")
    report = verify_with_pandas(scan_file(path), max_rows=1000)
    col = mutations_by_column(report)["id"]
    m = next(m for m in col.mutations if m.raw == "9007199254740993")
    assert m.kind == "value_changed"


def test_verify_polars_keeps_na_strings(tmp_path):
    path = write_csv(tmp_path, "country\nUS\nNA\nFR\n")
    report = verify_with_polars(scan_file(path), max_rows=1000)
    col = mutations_by_column(report)["country"]
    assert col.mutated_rows == 0


def test_verify_polars_drops_leading_zeros(tmp_path):
    path = write_csv(tmp_path, "zip\n02139\n10001\n")
    report = verify_with_polars(scan_file(path), max_rows=1000)
    col = mutations_by_column(report)["zip"]
    assert col.mutated_rows == 1


def test_verify_clean_file_no_mutations(tmp_path):
    path = write_csv(tmp_path, "name\nAnish\nGrace\n")
    for verify in (verify_with_pandas, verify_with_polars):
        report = verify(scan_file(path), max_rows=1000)
        assert report.total_mutated_rows == 0


def test_verify_load_error_reported(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n3,4,5\n")
    report = verify_with_pandas(scan_file(path), max_rows=1000)
    assert report.load_error is not None


def test_cli_verify_flag(tmp_path, capsys):
    path = write_csv(tmp_path, "zip\n02139\n")
    main([path, "--verify", "both"])
    out = capsys.readouterr().out
    assert "Verified with pandas" in out
    assert "Verified with polars" in out
    assert "'02139' -> 2139" in out


def test_cli_verify_json(tmp_path, capsys):
    import json

    path = write_csv(tmp_path, "zip\n02139\n")
    main([path, "--format", "json", "--verify", "pandas"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"][0]["loader"] == "pandas"
    assert payload["verification"][0]["total_mutated_rows"] == 1
