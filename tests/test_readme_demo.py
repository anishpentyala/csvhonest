from pathlib import Path

from csvhonest import scan_file


def test_readme_sample_matches_bundled_example():
    root = Path(__file__).resolve().parents[1]
    sample = root / "examples" / "import-surprises.csv"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "```csv\n" + sample.read_text(encoding="utf-8") + "```" in readme
    result = scan_file(str(sample))
    high = {(finding.code, finding.column) for finding in result.findings
            if finding.severity == "high"}
    assert high == {
        ("LEADING_ZERO_LOSS", "zip"),
        ("NA_COLLISION", "country"),
        ("FLOAT64_PRECISION_LOSS", "customer_id"),
    }
    assert result.row_count == 3
    assert result.column_count == 3
