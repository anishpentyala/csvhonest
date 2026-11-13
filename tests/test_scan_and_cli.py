import json

from conftest import write_csv

from csvhonest.cli import main
from csvhonest.report import render_json, render_text, suggested_overrides
from csvhonest.scan import scan_file


def test_scan_simple_file(tmp_path):
    path = write_csv(tmp_path, "zip,amount\n02139,10\n02140,20\n")
    result = scan_file(path)
    assert result.row_count == 2
    assert result.column_count == 2
    codes = {f.code for f in result.findings}
    assert "LEADING_ZERO_LOSS" in codes
    assert result.highest_severity() == "high"


def test_scan_duplicate_headers(tmp_path):
    path = write_csv(tmp_path, "a,a,b\n1,2,3\n")
    result = scan_file(path)
    assert "DUPLICATE_HEADERS" in {f.code for f in result.findings}


def test_scan_ragged_rows(tmp_path):
    path = write_csv(tmp_path, "a,b\n1,2\n3,4,5\n6\n")
    result = scan_file(path)
    ragged = next(f for f in result.findings if f.code == "RAGGED_ROWS")
    assert ragged.count == 2
    assert result.highest_severity() == "high"


def test_scan_non_utf8(tmp_path):
    path = write_csv(
        tmp_path, "city\nZürich — café\n", encoding="cp1252"
    )
    result = scan_file(path)
    assert result.encoding.fell_back
    assert "NON_UTF8_ENCODING" in {f.code for f in result.findings}


def test_scan_max_rows_truncates(tmp_path):
    text = "a\n" + "\n".join(str(i) for i in range(10)) + "\n"
    path = write_csv(tmp_path, text)
    result = scan_file(path, max_rows=3)
    assert result.truncated
    assert result.row_count == 3


def test_clean_file_reports_nothing(tmp_path):
    path = write_csv(tmp_path, "name,city\nAnish,Kirkland\nGrace,Seattle\n")
    result = scan_file(path)
    assert result.findings == []
    assert result.highest_severity() is None


def test_json_report_shape(tmp_path):
    path = write_csv(tmp_path, "zip\n02139\n")
    result = scan_file(path)
    payload = json.loads(render_json(result))
    assert payload["file"] == path
    assert payload["highest_severity"] == "high"
    assert payload["findings"][0]["code"] == "LEADING_ZERO_LOSS"
    assert payload["suggested_overrides"]["pandas"]["dtype"] == {"zip": "str"}
    assert "zip" in payload["suggested_overrides"]["polars"]["schema_overrides"]


def test_overrides_for_na_collision(tmp_path):
    path = write_csv(tmp_path, "country\nNA\nUS\n")
    result = scan_file(path)
    overrides = suggested_overrides(result)
    assert overrides["pandas"]["keep_default_na"] is False
    # only empty cells stay missing; the colliding strings are data
    assert overrides["pandas"]["na_values"] == [""]


def test_cli_exit_code_high(tmp_path):
    path = write_csv(tmp_path, "zip\n02139\n")
    assert main([path]) == 1


def test_cli_exit_code_clean(tmp_path, capsys):
    path = write_csv(tmp_path, "a\nhello\n")
    assert main([path]) == 0
    out = capsys.readouterr().out
    assert "No silent-mutation findings" in out


def test_cli_fail_on_low(tmp_path):
    # integers with a hole (explicit empty cell) -> low severity finding only
    path = write_csv(tmp_path, 'n\n1\n2\n""\n')
    assert main([path, "--fail-on", "low"]) == 1
    assert main([path]) == 0


def test_cli_json_format(tmp_path, capsys):
    path = write_csv(tmp_path, "zip\n02139\n")
    main([path, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["highest_severity"] == "high"


def test_headerless_file_auto_detected(tmp_path):
    path = write_csv(tmp_path, "1,Goroka,5282\n2,Mag,20\n")
    result = scan_file(path)
    assert result.headers[0] == "column_0"
    assert result.row_count == 2


def test_header_forced_off(tmp_path):
    path = write_csv(tmp_path, "name,city\nAnish,Kirkland\n")
    result = scan_file(path, has_header=False)
    assert result.row_count == 2


def test_cli_missing_file(capsys):
    assert main(["/nonexistent/file.csv"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_text_report_mentions_fixes(tmp_path):
    path = write_csv(tmp_path, "zip\n02139\n")
    result = scan_file(path)
    text = render_text(result)
    assert "LEADING_ZERO_LOSS" in text
    assert "pd.read_csv" in text
    assert "pl.read_csv" in text
