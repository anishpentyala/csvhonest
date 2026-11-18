"""Synthetic contract tests for the optional DuckDB verifier."""
import json
import pytest

pytest.importorskip('duckdb')
from conftest import write_csv
from csvhonest.cli import main
from csvhonest.scan import scan_file
from csvhonest.duckdb_verify import verify_with_duckdb


def verify(tmp_path, text, **kwargs):
    path = write_csv(tmp_path, text, **kwargs)
    return verify_with_duckdb(scan_file(path), 1000)


def mutations(report):
    assert report.load_error is None
    return [(m.raw, m.became, m.kind) for c in report.columns for m in c.mutations]


def test_numeric_spelling(tmp_path):
    assert ('1.00', '1', 'form_changed') in mutations(verify(tmp_path, 'n\n1.00\n2.50\n'))


def test_default_preserves_leading_zero_strings_and_na(tmp_path):
    assert not mutations(verify(tmp_path, 'id,country\n001,NA\n002,US\n'))


def test_boolean_normalization(tmp_path):
    assert ('true', 'True', 'value_changed') in mutations(verify(tmp_path, 'flag\ntrue\nfalse\n'))


def test_empty_physical_lines_skipped_but_spaces_retained(tmp_path):
    report = verify(tmp_path, 'id\n001\n\n  \n002\n')
    assert not mutations(report)
    assert report.loaded_rows == 4


def test_quoted_blank_is_retained(tmp_path):
    report = verify(tmp_path, 'id,n\n001,1\n"",\n002,2\n')
    assert not mutations(report)
    assert report.loaded_rows == 3


def test_multiline_record_stays_aligned(tmp_path):
    report = verify(tmp_path, 'id,n\n"a\n\nb",1.00\nz,2.50\n')
    assert ('1.00', '1', 'form_changed') in mutations(report)
    assert report.loaded_rows == 2


def test_headerless_refuses_claims(tmp_path):
    report = verify(tmp_path, '1,2\n3,4\n5,6\n')
    assert report.load_error
    assert not report.columns


def test_legacy_encoding_refuses_without_transcoding(tmp_path):
    report = verify(tmp_path, 'name\nJosé\nAna\n', encoding='cp1252')
    assert report.load_error
    assert not report.columns


def test_glob_characters_in_name_are_literal(tmp_path):
    assert ('1.00', '1', 'form_changed') in mutations(verify(tmp_path, 'n\n1.00\n2.50\n', name='a[1]*?.csv'))


def test_row_limit(tmp_path):
    path = write_csv(tmp_path, 'n\n1.00\n2.50\n3.25\n')
    report = verify_with_duckdb(scan_file(path), 1)
    assert len(mutations(report)) == 1
    assert report.loaded_rows == 1


def test_cli_explicit_opt_in(tmp_path, capsys):
    path = write_csv(tmp_path, 'n\n1.00\n2.50\n')
    assert main([path, '--verify', 'duckdb', '--format', 'json']) == 0
    report = json.loads(capsys.readouterr().out)['verification'][0]
    assert report['loader'] == 'duckdb'
    assert report['total_mutated_rows'] == 2


def test_multiple_columns_skip_only_empty_physical_lines(tmp_path):
    report = verify(tmp_path, 'a,b\n1.00,first\n\n2.50,last\n')
    assert len(mutations(report)) == 2
    assert report.loaded_rows == 2


def test_default_sniffer_can_treat_ragged_comma_file_as_one_column(tmp_path):
    report = verify(tmp_path, 'a,b\n1.00,first\n2.50\n')
    assert not mutations(report)
    assert report.loaded_columns == 1
    assert report.loaded_rows == 2


def test_bom_and_semicolon(tmp_path):
    report = verify(tmp_path, 'amount;note\n1.00;first\n2.50;last\n', encoding='utf-8-sig')
    assert len(mutations(report)) == 2


def test_quoted_delimiter_and_escaped_quote(tmp_path):
    report = verify(tmp_path, 'amount,note\n1.00,"a,b"\n2.50,"a""b"\n')
    assert len(mutations(report)) == 2
    assert not report.columns[1].mutations


def test_existing_both_does_not_select_duckdb(tmp_path, capsys):
    pytest.importorskip('pandas')
    pytest.importorskip('polars')
    path = write_csv(tmp_path, 'n\n1.00\n2.50\n')
    main([path, '--verify', 'both', '--format', 'json'])
    payload = json.loads(capsys.readouterr().out)
    assert [r['loader'] for r in payload['verification']] == ['pandas', 'polars']


def test_missing_optional_dependency_is_reported(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == 'duckdb':
            raise ImportError('synthetic missing optional dependency')
        return original(name, *args, **kwargs)

    path = write_csv(tmp_path, 'n\n1.00\n2.50\n')
    result = scan_file(path)
    monkeypatch.setattr(builtins, '__import__', missing)
    report = verify_with_duckdb(result, 100)
    assert report.load_error == 'duckdb is not installed'
    assert not report.columns


@pytest.mark.parametrize('initial,late,became,kind', [
    ('1', '1.5', '2', 'value_changed'),
    ('1', '007', '7', 'form_changed'),
    ('1', '0.0000001', '0', 'value_changed'),
    ('2020-01-01', '2020-06-15T13:45:59', '2020-06-15', 'value_changed'),
])
def test_mutation_after_default_sniff_sample(tmp_path, initial, late, became, kind):
    # Synthetic input, not a customer file. The default sniffer samples the
    # header and 20,479 preceding values, not the late value.
    path = write_csv(tmp_path, 'c0\n' + (initial + '\n') * 20_479 + late + '\n')
    report = verify_with_duckdb(scan_file(path), 30_000)
    assert mutations(report) == [(late, became, kind)]
    assert report.loaded_rows == 20_480


def test_within_sample_decimal_is_not_falsely_reported(tmp_path):
    assert not mutations(verify(tmp_path, 'c0\n' + '1\n' * 100 + '1.5\n'))


def test_explicit_comparison_limit_cannot_cover_late_mutation(tmp_path):
    path = write_csv(tmp_path, 'c0\n' + '1\n' * 20_479 + '1.5\n')
    report = verify_with_duckdb(scan_file(path), 10_000)
    assert not mutations(report)
    assert report.loaded_rows == 10_000
