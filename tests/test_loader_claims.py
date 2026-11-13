"""Empirical checks that csvhonest's loader-behavior claims hold against the
real installed pandas and polars. Skipped when they are not installed; these
tests are why the findings can name specific loaders without guessing.
"""

import io

import pytest

pd = pytest.importorskip("pandas")
pl = pytest.importorskip("polars")

# pandas 3 (PDEP-14 string inference) loads text columns as the new `str`
# dtype; pandas 2 loads them as `object`. Both mean the column stayed text:
# numeric inference was blocked and the values are the original strings.
_PANDAS_MAJOR = int(pd.__version__.split(".")[0])
PANDAS_TEXT_DTYPE = "str" if _PANDAS_MAJOR >= 3 else "object"


def test_pandas_drops_leading_zeros():
    df = pd.read_csv(io.StringIO("zip\n02139\n00501\n"))
    assert df["zip"].tolist() == [2139, 501]


def test_polars_drops_leading_zeros():
    pf = pl.read_csv(io.StringIO("zip\n02139\n00501\n"))
    assert pf["zip"].to_list() == [2139, 501]


def test_pandas_na_strings_become_nan():
    df = pd.read_csv(io.StringIO("c\nUS\nNA\nNone\nnull\n"))
    assert df["c"].isna().tolist() == [False, True, True, True]


def test_polars_keeps_na_strings():
    pf = pl.read_csv(io.StringIO("c\nUS\nNA\nNone\nnull\n"))
    assert pf["c"].to_list() == ["US", "NA", "None", "null"]


def test_pandas_int_with_hole_becomes_float64():
    df = pd.read_csv(io.StringIO("n\n1\nNA\n2\n"))
    assert str(df["n"].dtype) == "float64"


def test_pandas_big_int_with_hole_loses_exactness():
    df = pd.read_csv(io.StringIO("id\n9007199254740993\nNA\n"))
    assert int(df["id"].iloc[0]) != 9007199254740993


def test_polars_int_with_hole_stays_exact():
    pf = pl.read_csv(io.StringIO("n\n1\n\n2\n"))
    assert str(pf["n"].dtype) == "Int64"


def test_pandas_bool_strings():
    df = pd.read_csv(io.StringIO("f\nTrue\nFalse\n"))
    assert str(df["f"].dtype) == "bool"


def test_polars_bool_strings_both_cases():
    assert str(pl.read_csv(io.StringIO("f\ntrue\nfalse\n"))["f"].dtype) == "Boolean"
    assert str(pl.read_csv(io.StringIO("f\nTrue\nFalse\n"))["f"].dtype) == "Boolean"


def test_sci_notation_infers_numeric_both():
    assert str(pd.read_csv(io.StringIO("s\n1E2\n3E4\n250\n"))["s"].dtype).startswith("float")
    assert str(pl.read_csv(io.StringIO("s\n1E2\n3E4\n250\n"))["s"].dtype).startswith("Float")


def test_thousands_separator_blocks_numeric_and_fix_works():
    df = pd.read_csv(io.StringIO('amt\n"1,234"\n42\n'))
    assert str(df["amt"].dtype) == PANDAS_TEXT_DTYPE
    # consumer semantics: the values are the untouched strings, not numbers
    assert df["amt"].tolist() == ["1,234", "42"]
    fixed = pd.read_csv(io.StringIO('amt\n"1,234"\n42\n'), thousands=",")
    assert fixed["amt"].tolist() == [1234, 42]


def test_pandas_overflow_changes_dtype():
    assert str(pd.read_csv(io.StringIO("x\n9223372036854775808\n1\n"))["x"].dtype) == "uint64"
    mixed = pd.read_csv(io.StringIO("x\n9223372036854775808\n-1\n"))
    assert str(mixed["x"].dtype) == PANDAS_TEXT_DTYPE
    # consumer semantics: the overflowing value survives as its string form
    assert mixed["x"].tolist() == ["9223372036854775808", "-1"]


def test_polars_overflow_behavior():
    # polars 2.x infers Int128 on int64 overflow; polars 1.x raises a loud
    # ComputeError instead (no silent mutation). Both are version-pinned here.
    if int(pl.__version__.split(".")[0]) >= 2:
        pf = pl.read_csv(io.StringIO("x\n9223372036854775808\n1\n"))
        assert str(pf["x"].dtype) == "Int128"
        # consumer semantics: the overflowing integer survives exactly
        assert pf["x"].to_list() == [9223372036854775808, 1]
    else:
        with pytest.raises(pl.exceptions.ComputeError):
            pl.read_csv(io.StringIO("x\n9223372036854775808\n1\n"))


def test_both_rename_duplicate_headers_silently():
    assert list(pd.read_csv(io.StringIO("a,a,b\n1,2,3\n")).columns) == ["a", "a.1", "b"]
    assert pl.read_csv(io.StringIO("a,a,b\n1,2,3\n")).columns == ["a", "a_duplicated_0", "b"]


def test_pandas_strips_padded_numbers_polars_keeps_strings():
    df = pd.read_csv(io.StringIO("zip\n 02139\n 00501\n"))
    assert df["zip"].tolist() == [2139, 501]
    pf = pl.read_csv(io.StringIO("zip\n 02139\n 00501\n"))
    assert pf["zip"].to_list() == [" 02139", " 00501"]


def test_short_rows_padded_long_rows_raise():
    df = pd.read_csv(io.StringIO("a,b\n1,2\n6\n"))
    assert df.shape == (2, 2)
    pf = pl.read_csv(io.StringIO("a,b\n1,2\n6\n"))
    assert pf.shape == (2, 2)
    with pytest.raises(Exception):
        pd.read_csv(io.StringIO("a,b\n1,2\n3,4,5\n"))
    with pytest.raises(Exception):
        pl.read_csv(io.StringIO("a,b\n1,2\n3,4,5\n"))


def test_mixed_bigint_float_column_loses_exactness_both():
    df = pd.read_csv(io.StringIO("x\n9007199254740993\n1.5\n"))
    assert int(df["x"].iloc[0]) != 9007199254740993
    pf = pl.read_csv(io.StringIO("x\n9007199254740993\n1.5\n"))
    assert int(pf["x"][0]) != 9007199254740993


def test_inf_parses_as_float_both():
    assert pd.read_csv(io.StringIO("x\ninf\n1.5\n"))["x"].tolist()[0] == float("inf")
    assert pl.read_csv(io.StringIO("x\ninf\n1.5\n"))["x"].to_list()[0] == float("inf")


def test_pandas_skips_blank_lines_polars_keeps_null_row():
    df = pd.read_csv(io.StringIO("a,b\n1,2\n\n3,4\n"))
    assert df.shape == (2, 2)
    pf = pl.read_csv(io.StringIO("a,b\n1,2\n\n3,4\n"))
    assert pf.shape == (3, 2)


def test_suggested_fixes_work():
    df = pd.read_csv(io.StringIO("id\n9007199254740993\nNA\n"), dtype={"id": "Int64"})
    assert df["id"].iloc[0] == 9007199254740993
    df = pd.read_csv(io.StringIO("c\nUS\nNA\n"), keep_default_na=False, na_values=[])
    assert df["c"].tolist() == ["US", "NA"]
