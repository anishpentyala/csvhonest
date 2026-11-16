"""Exact source/display comparison without binary floating-point noise."""

import dataclasses

import pytest

from csvhonest.scan import scan_file
from csvhonest.verify import _classify, _render, verify_with_pandas, verify_with_polars


@pytest.mark.parametrize('raw,value', [
    ('9007199254740993', 9007199254740992.0),
    ('9007199254740993.0', 9007199254740992.0),
    ('9.007199254740993e15', 9007199254740992.0),
    ('-9.007199254740993e15', -9007199254740992.0),
    ('1e-400', 0.0), ('-1e-400', -0.0),
    ('1.0000000000000001', 1.0),
    ('1', 1.5), ('-1', -1.5),
    ('1e400', float('inf')), ('-1e400', -float('inf')),
    ('1e999999999999999999', float('inf')),
    ('1e-999999999999999999', 0.0),
    ('1e' + '9' * 2000, float('inf')),
    ('1e-' + '9' * 2000, 0.0),
    ('9' * 5000, 1.0),
    ('NaN', 1.0), ('sNaN', 1.0), ('not numeric', 1),
])
def test_numeric_value_changes(raw, value):
    assert _classify(raw, _render(value), value) == 'value_changed'


@pytest.mark.parametrize('raw,value', [
    ('02139', 2139), ('1E2', 100.0), ('100.00', 100.0),
    ('+001.000e0', 1.0), ('1.00e-1', 0.1),
    ('0.1000000000000000', 0.1),
    ('0e' + '9' * 2000, 0.0),
    ('-0.00e-' + '9' * 2000, -0.0),
    (' +.000e+' + '9' * 2000 + ' ', 0.0),
    ('-0.0', -0.0), ('-0', 0), ('+0.000', 0.0),
    ('Infinity', float('inf')), ('-Infinity', -float('inf')),
    ('9007199254740992.0', 9007199254740992.0),
    ('9.007199254740992e15', 9007199254740992),
])
def test_numeric_format_only(raw, value):
    assert _classify(raw, _render(value), value) == 'form_changed'


@pytest.mark.parametrize('raw,value', [('0.1', 0.1), ('123', 123), ('name', 'name')])
def test_unchanged_display(raw, value):
    assert _classify(raw, _render(value), value) is None


@pytest.mark.parametrize('raw', ['9007199254740993.0', '9.007199254740993e15',
                                  '1e-400', '-1e-400', '1.0000000000000001'])
@pytest.mark.parametrize('loader', ['pandas', 'polars'])
def test_actual_loader_precision_or_underflow(tmp_path, raw, loader):
    pytest.importorskip(loader)
    path = tmp_path / 'numbers.csv'
    path.write_text('number\n' + raw + '\n1.5\n', encoding='utf-8')
    verify = verify_with_pandas if loader == 'pandas' else verify_with_polars
    report = verify(scan_file(str(path)), 100)
    assert report.load_error is None
    mutation = next(m for c in report.columns for m in c.mutations if m.raw == raw)
    assert mutation.kind == 'value_changed', dataclasses.asdict(report)


@pytest.mark.parametrize('loader', ['pandas', 'polars'])
def test_actual_loader_ordinary_decimal_and_format(tmp_path, loader):
    pytest.importorskip(loader)
    path = tmp_path / 'numbers.csv'
    path.write_text('number\n0.1\n1e2\n-0.0\n', encoding='utf-8')
    verify = verify_with_pandas if loader == 'pandas' else verify_with_polars
    report = verify(scan_file(str(path)), 100)
    assert report.load_error is None
    mutations = {m.raw: m for c in report.columns for m in c.mutations}
    assert '0.1' not in mutations
    assert mutations['1e2'].kind == 'form_changed'
    assert mutations['-0.0'].kind == 'form_changed'


@pytest.mark.parametrize('raw', ['0e' + '9' * 2000, '-0.00e-' + '9' * 2000])
@pytest.mark.parametrize('loader', ['pandas', 'polars'])
def test_actual_loader_zero_extreme_exponent(tmp_path, raw, loader):
    pytest.importorskip(loader)
    path = tmp_path / 'numbers.csv'
    path.write_text('number\n' + raw + '\n1.5\n', encoding='utf-8')
    verify = verify_with_pandas if loader == 'pandas' else verify_with_polars
    report = verify(scan_file(str(path)), 100)
    assert report.load_error is None
    # pandas keeps these extreme spellings as text; polars parses them as zero.
    assert all(m.kind != 'value_changed' for c in report.columns for m in c.mutations)
