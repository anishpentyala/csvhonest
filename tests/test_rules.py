from csvhonest.rules import ColumnStats, run_column_rules


def make_stats(values, name="col"):
    stats = ColumnStats(name=name, index=0)
    for v in values:
        stats.add(v, cap=1000)
    return stats


def codes(findings):
    return {f.code for f in findings}


def test_leading_zero_zip_codes():
    stats = make_stats(["02139", "02140", "10001", "00501"])
    found = run_column_rules(stats)
    assert "LEADING_ZERO_LOSS" in codes(found)
    f = next(f for f in found if f.code == "LEADING_ZERO_LOSS")
    assert f.severity == "high"
    assert f.count == 3
    assert set(f.loaders) == {"pandas", "polars"}


def test_leading_zero_safe_when_text_present():
    # A text value keeps the column as strings; zeros survive.
    stats = make_stats(["02139", "not-a-zip"])
    assert "LEADING_ZERO_LOSS" not in codes(run_column_rules(stats))


def test_na_collision_country_code():
    stats = make_stats(["US", "NA", "NA", "FR"], name="country")
    found = run_column_rules(stats)
    na = [f for f in found if f.code == "NA_COLLISION"]
    assert na, "expected NA_COLLISION"
    pandas_only = [f for f in na if "pandas" in f.loaders]
    assert pandas_only and pandas_only[0].count == 2
    # polars does not convert "NA" by default
    assert not any("polars" in f.loaders for f in na)


def test_na_collision_none_and_null():
    stats = make_stats(["None", "null", "ok"])
    assert "NA_COLLISION" in codes(run_column_rules(stats))


def test_float64_precision_loss_with_holes():
    stats = make_stats(["9007199254740993", "", "9007199254740995"])
    found = run_column_rules(stats)
    assert "FLOAT64_PRECISION_LOSS" in codes(found)
    f = next(f for f in found if f.code == "FLOAT64_PRECISION_LOSS")
    assert f.loaders == ["pandas"]


def test_large_int_without_holes_is_downstream_only():
    stats = make_stats(["9007199254740993", "9007199254740995"])
    found = run_column_rules(stats)
    assert "LARGE_ID_PRECISION" in codes(found)
    assert "FLOAT64_PRECISION_LOSS" not in codes(found)


def test_int64_overflow():
    stats = make_stats(["9223372036854775808", "1"])
    assert "INT64_OVERFLOW" in codes(run_column_rules(stats))


def test_int_becomes_float_with_empty():
    stats = make_stats(["1", "2", ""])
    found = run_column_rules(stats)
    assert "INT_BECOMES_FLOAT64" in codes(found)
    f = next(f for f in found if f.code == "INT_BECOMES_FLOAT64")
    assert f.severity == "low"


def test_sci_notation_form_loss():
    stats = make_stats(["1E2", "3E4", "250"])
    assert "SCI_NOTATION_FORM_LOSS" in codes(run_column_rules(stats))


def test_sci_notation_safe_in_text_column():
    stats = make_stats(["1E2", "ABC", "XYZ"])
    assert "SCI_NOTATION_FORM_LOSS" not in codes(run_column_rules(stats))


def test_bool_coercion_pandas_and_polars_cases():
    pandas_stats = make_stats(["True", "False", "True"])
    found = run_column_rules(pandas_stats)
    bool_findings = [f for f in found if f.code == "BOOL_COERCION"]
    assert any("pandas" in f.loaders for f in bool_findings)

    polars_stats = make_stats(["true", "false", "true"])
    found = run_column_rules(polars_stats)
    bool_findings = [f for f in found if f.code == "BOOL_COERCION"]
    assert any("polars" in f.loaders for f in bool_findings)


def test_thousands_separator():
    stats = make_stats(["1,234", "2,500", "42"])
    found = run_column_rules(stats)
    assert "THOUSANDS_SEPARATOR" in codes(found)


def test_thousands_separator_ignored_in_text_column():
    stats = make_stats(["1,234", "hello"])
    assert "THOUSANDS_SEPARATOR" not in codes(run_column_rules(stats))


def test_currency_and_percent():
    stats = make_stats(["$5.00", "$12.50", "7"])
    assert "CURRENCY_OR_PERCENT" in codes(run_column_rules(stats))
    stats = make_stats(["5%", "12%", "7"])
    assert "CURRENCY_OR_PERCENT" in codes(run_column_rules(stats))


def test_ambiguous_date():
    stats = make_stats(["01/02/2003", "03/04/2003", "12/11/2003"])
    found = run_column_rules(stats)
    assert "AMBIGUOUS_DATE" in codes(found)
    f = next(f for f in found if f.code == "AMBIGUOUS_DATE")
    assert f.loaders == ["downstream-parse"]


def test_unambiguous_date_not_flagged():
    stats = make_stats(["25/02/2003", "26/04/2003"])
    assert "AMBIGUOUS_DATE" not in codes(run_column_rules(stats))


def test_whitespace_key_collision():
    stats = make_stats(["WA", "WA ", " WA", "CA"])
    found = run_column_rules(stats)
    assert "WHITESPACE_KEY_COLLISION" in codes(found)
    f = next(f for f in found if f.code == "WHITESPACE_KEY_COLLISION")
    assert f.severity == "high"


def test_whitespace_edged_no_collision():
    stats = make_stats(["WA", "CA ", "OR"])
    found = run_column_rules(stats)
    assert "WHITESPACE_EDGED_VALUES" in codes(found)
    assert "WHITESPACE_KEY_COLLISION" not in codes(found)


def test_unicode_form_collision():
    stats = make_stats(["Café", "Café".replace("é", "é"), "plain"])
    found = run_column_rules(stats)
    assert "UNICODE_FORM_COLLISION" in codes(found)


def test_invisible_characters():
    stats = make_stats(["abc def", "normal"])
    found = run_column_rules(stats)
    assert "INVISIBLE_CHARACTERS" in codes(found)


def test_footer_rows():
    values = [str(i) for i in range(50)] + ["Total"]
    stats = make_stats(values)
    found = run_column_rules(stats)
    assert "FOOTER_ROWS" in codes(found)


def test_footer_ignored_in_text_column():
    stats = make_stats(["apples", "oranges", "Total"])
    assert "FOOTER_ROWS" not in codes(run_column_rules(stats))


def test_clean_column_has_no_findings():
    stats = make_stats(["alpha", "beta", "gamma"])
    assert run_column_rules(stats) == []


def test_distinct_cap_marks_capped():
    stats = ColumnStats(name="x", index=0)
    for i in range(10):
        stats.add(f"v{i}", cap=5)
    assert stats.distinct_capped
