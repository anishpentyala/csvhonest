"""Handoff-hardening tests: the behaviors a reviewer asked to see pinned.

- bounded memory / distinct-value cardinality cap
- header auto-detection directions and its known misclassification case
- loader attribution consistency (no finding claims an unknown loader)
- generated override correctness (applying them really prevents mutations)
- --verify defaults (no verification unless requested)
"""

import io
import json

import pytest

from conftest import write_csv

from csvhonest.cli import main
from csvhonest.report import render_json, suggested_overrides
from csvhonest.rules import run_column_rules
from csvhonest.scan import scan_file

KNOWN_LOADERS = {"pandas", "polars", "downstream-parse"}


# -- bounded memory / cardinality -------------------------------------------


def test_distinct_cap_bounds_memory_and_scan_still_works(tmp_path):
    # 5k distinct values with a cap of 50: the counter must stay at 50,
    # the column must be flagged capped, and rules must still run.
    lines = ["id,code"] + [f"{i:05d},val{i}" for i in range(5000)]
    path = write_csv(tmp_path, "\n".join(lines) + "\n")
    result = scan_file(path, distinct_cap=50)
    code = next(s for s in result.stats if s.name == "code")
    idc = next(s for s in result.stats if s.name == "id")
    assert code.distinct_capped
    assert len(code.values) <= 50
    assert idc.distinct_capped
    assert len(idc.values) <= 50
    # capped stats still support rules: id has leading zeros and is numeric
    assert any(f.code == "LEADING_ZERO_LOSS" for f in result.findings)
    assert result.row_count == 5000


def test_uncapped_column_counts_all_distinct(tmp_path):
    lines = ["code"] + [f"val{i}" for i in range(200)]
    path = write_csv(tmp_path, "\n".join(lines) + "\n")
    result = scan_file(path, distinct_cap=1000)
    code = result.stats[0]
    assert not code.distinct_capped
    assert len(code.values) == 200


# -- header detection --------------------------------------------------------


def test_header_detection_text_header(tmp_path):
    path = write_csv(tmp_path, "name,city\nAnish,Kirkland\n")
    result = scan_file(path)
    assert result.header_auto is True
    assert result.headers == ["name", "city"]
    assert result.row_count == 1


def test_header_detection_numeric_first_row(tmp_path):
    path = write_csv(tmp_path, "1,Goroka,5282\n2,Madang,20\n")
    result = scan_file(path)
    assert result.header_auto is False
    assert result.headers[0] == "column_0"
    assert result.row_count == 2


def test_header_detection_numeric_header_is_a_known_miss(tmp_path):
    # A header of year-like column names is numeric, so the heuristic
    # misclassifies it as data. Pinned as a known limitation: the report
    # surfaces header_auto so the user can override with --header.
    path = write_csv(tmp_path, "2020,2021,2022\n10,20,30\n")
    result = scan_file(path)
    assert result.header_auto is False  # known misclassification
    fixed = scan_file(path, has_header=True)
    assert fixed.headers == ["2020", "2021", "2022"]
    assert fixed.row_count == 1


# -- loader attribution consistency ------------------------------------------


def test_findings_only_claim_known_loaders(tmp_path):
    text = (
        "zip,country,when,amt\n"
        "02139,NA,01/02/2026,\"1,234\"\n"
        "00501,None,03/04/2026,42\n"
    )
    path = write_csv(tmp_path, text)
    result = scan_file(path)
    assert result.findings, "expected findings in this file"
    for f in result.findings:
        assert set(f.loaders) <= KNOWN_LOADERS, f"{f.code}: {f.loaders}"


# -- generated override correctness ------------------------------------------

pd = pytest.importorskip("pandas")
pl = pytest.importorskip("polars")

MESSY = "zip,country\n02139,NA\n00501,US\n"


def _pandas_args(overrides):
    # JSON-shaped overrides use builtin-repr values that pandas accepts as-is.
    return dict(overrides["pandas"])


def _polars_args(overrides):
    args = dict(overrides["polars"])
    if "schema_overrides" in args:
        args["schema_overrides"] = {
            k: getattr(pl, v) for k, v in args["schema_overrides"].items()
        }
    return args


def test_suggested_pandas_overrides_prevent_the_mutations(tmp_path):
    path = write_csv(tmp_path, MESSY)
    result = scan_file(path)
    overrides = suggested_overrides(result)
    df = pd.read_csv(path, **_pandas_args(overrides))
    assert df["zip"].tolist() == ["02139", "00501"]
    assert df["country"].tolist() == ["NA", "US"]


def test_suggested_polars_overrides_prevent_the_mutations(tmp_path):
    path = write_csv(tmp_path, MESSY)
    result = scan_file(path)
    overrides = suggested_overrides(result)
    pf = pl.read_csv(path, **_polars_args(overrides))
    assert pf["zip"].to_list() == ["02139", "00501"]
    assert pf["country"].to_list() == ["NA", "US"]


MIXED = "x\n9007199254740993\n1.5\n"


def test_suggested_overrides_apply_to_mixed_float_bigint(tmp_path):
    # The review blocker: the generated fix for a mixed int/float column
    # must not raise (Int64 TypeError) and must preserve the big integer.
    path = write_csv(tmp_path, MIXED)
    result = scan_file(path)
    assert any(f.code == "FLOAT64_PRECISION_LOSS" for f in result.findings)
    overrides = suggested_overrides(result)
    df = pd.read_csv(path, **_pandas_args(overrides))
    assert df["x"].tolist() == ["9007199254740993", "1.5"]
    pf = pl.read_csv(path, **_polars_args(overrides))
    assert pf["x"].to_list() == ["9007199254740993", "1.5"]


def test_suggested_overrides_int64_still_used_for_pure_int_with_holes(tmp_path):
    # an actual empty cell, not the string "NA" (which keeps the column text)
    path = write_csv(tmp_path, 'id\n9007199254740993\n""\n')
    overrides = suggested_overrides(scan_file(path))
    assert overrides["pandas"]["dtype"] == {"id": "Int64"}
    df = pd.read_csv(path, **_pandas_args(overrides))
    assert df["id"].iloc[0] == 9007199254740993
    assert df["id"].isna().tolist()[1]


# -- --verify defaults --------------------------------------------------------


def test_no_verification_by_default(tmp_path, capsys):
    path = write_csv(tmp_path, "zip\n02139\n")
    main([path])
    out = capsys.readouterr().out
    assert "Verified with" not in out
    main([path, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert "verification" not in payload


def test_verify_both_reports_both_loaders(tmp_path, capsys):
    path = write_csv(tmp_path, "zip\n02139\n")
    main([path, "--format", "json", "--verify", "both"])
    payload = json.loads(capsys.readouterr().out)
    assert [v["loader"] for v in payload["verification"]] == ["pandas", "polars"]
