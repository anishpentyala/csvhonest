# csvhonest

**Audit what your CSV loader will silently change - before you load it.**

`pd.read_csv` and `pl.read_csv` do not show you the values they mutate on
the way in. They drop leading zeros from ZIP codes and IDs, turn the
strings `"NA"`, `"None"`, `"null"` into missing values (even when they are
real data, like Namibia's country code), coerce integer columns with holes
to float64 (silently corrupting integers above 2^53), rename duplicate
headers, and let whitespace or Unicode lookalikes split your keys.
csvhonest reads the raw text and tells you exactly which values change,
under which loader's defaults, with the fix for each one.

```
$ csvhonest orders.csv
csvhonest: orders.csv
  4 data rows x 6 columns, encoding utf-8, delimiter ','
  4 high, 2 medium, 1 low finding(s)

  [HIGH] LEADING_ZERO_LOSS (column 'zip'; affects: pandas, polars)
        2 value(s) in column 'zip' have leading zeros and every value looks
        numeric, so a default load parses them as integers and the zeros are
        gone (e.g. ZIP codes, IDs).
        examples: 02139, 00501
        fix: Load the column as text: pandas dtype={'zip': str}, polars
        schema_overrides={'zip': pl.String}.
  ...

Suggested load arguments
------------------------
pandas:
  pd.read_csv(path,
    dtype={'zip': 'str'},
    keep_default_na=False,
    na_values=[''],
  )
```

Every behavioral claim is verified against real pandas and polars releases
in `tests/test_loader_claims.py`, so findings name the loaders they
actually affect instead of guessing about a generic parser.

## Install

```
pip install git+https://github.com/anishpentyala/csvhonest
```

Or from source: `pip install .` (pure standard library, Python 3.9+).
Building from source needs a current pip/setuptools (setuptools >= 68);
on an old system Python, run `python -m pip install --upgrade pip` first
or the wheel builds without its metadata and console script.

## Use

```
csvhonest data.csv                  # human report
csvhonest data.csv --format json    # machine report for CI
csvhonest data.csv --verify both    # also run pandas+polars and diff raw vs loaded
csvhonest a.csv b.csv --fail-on medium
csvhonest data.csv --no-header      # force first row to be data
```

`--verify pandas` (or `polars`, `both`) goes beyond static analysis: it
loads the file with the installed loader's defaults and reports the cells
that actually changed, per distinct raw value (`'02139' -> 2139`,
`'NA' -> <missing>`), plus the dtype each column landed on.

Exit code is 1 when a finding at or above `--fail-on` (default `high`)
exists, so the check slots into a pipeline or pre-commit step.

From Python:

```python
from csvhonest import scan_file

result = scan_file("orders.csv")
for finding in result.findings:
    print(finding.severity, finding.code, finding.message)
```

## What it checks

| Code | Severity | What silently happens |
| --- | --- | --- |
| `LEADING_ZERO_LOSS` | high | `02139` loads as `2139` when the column infers numeric (pandas, polars) |
| `PADDED_LEADING_ZERO_LOSS` | high | whitespace-padded numbers with leading zeros: pandas trims and parses them, losing zeros and padding; polars keeps the original strings |
| `NA_COLLISION` | high | `"NA"`, `"None"`, `"null"` become NaN (pandas defaults) |
| `INT64_OVERFLOW` | high | pandas switches to uint64/object, polars to Int128 |
| `FLOAT64_PRECISION_LOSS` | high | integers above 2^53 lose exactness under float64: mixed int/float columns (pandas, polars) or int columns with holes (pandas) |
| `WHITESPACE_KEY_COLLISION` | high | `"WA"` and `"WA "` merge on any strip, collapsing distinct records |
| `UNICODE_FORM_COLLISION` | high | visually identical strings in different normal forms load as separate keys |
| `DUPLICATE_HEADERS` | high | pandas renames `a, a.1`; polars renames `a, a_duplicated_0` |
| `RAGGED_ROWS` | high | short rows are silently padded with missing values (pandas NaN, polars null); long rows make both loaders raise |
| `INVALID_ENCODING_BYTES` | medium | byte sequences invalid for the detected encoding; loaders reject or mangle them |
| `SCI_NOTATION_FORM_LOSS` | medium | `"1E2"` becomes `100.0`; IDs that look like E-notation change value |
| `THOUSANDS_SEPARATOR` | medium | `"1,234"` blocks numeric inference; column stays text |
| `AMBIGUOUS_DATE` | medium | `01/02/03` parses day-first or month-first depending on the tool |
| `CURRENCY_OR_PERCENT` | medium | `"$5.00"`, `"5%"` load as text; naive casts misread them |
| `INVISIBLE_CHARACTERS` | medium | no-break spaces and zero-width characters break equality and joins |
| `FOOTER_ROWS` | medium | `Total`/`Source:` rows inside numeric columns |
| `NON_UTF8_ENCODING` | medium | cp1252 export that UTF-8-assuming tools reject or mangle |
| `INT_BECOMES_FLOAT64` | low | all-integer column with missing cells loads as float64 (pandas) |
| `BOOL_COERCION` | low | `"True"/"False"` strings become booleans |
| `WHITESPACE_EDGED_VALUES` | low | padded values survive the load but fail joins |
| `DIALECT_GUESSED` | low | the delimiter had to be inferred; verify it |

## Design notes

- **Reads raw text, never mutates.** Findings come from a single streamed
  pass with per-column value counters bounded in both entries
  (`--distinct-cap`, default 100k) and bytes (64 MB), so memory stays
  bounded on multi-GB files; `--max-rows` bounds time. Columns that hit a
  cap are named in the report and their findings reflect a partial sample.
- **Loader profiles, not vibes.** Findings are attributed to the loaders
  whose defaults trigger them (pandas and polars today), and
  `tests/test_loader_claims.py` re-verifies each claim against the
  installed versions.
- **Actionable output.** Every finding carries a fix, and the report ends
  with ready-to-paste `pd.read_csv` / `pl.read_csv` arguments that prevent
  the high-severity mutations.

## Limits, stated honestly

- **Header auto-detection is a heuristic.** A header made of numeric names
  (e.g. year columns `2020,2021,2022`) is misclassified as data. The
  report states which way the detection went; use `--header` /
  `--no-header` when you know.
- **Loader claims are version-pinned.** Each claim is verified against the
  installed pandas/polars in `tests/test_loader_claims.py` (developed
  against pandas 2.3.3, polars 2.0.0). Other versions can differ; the
  tests will say so. Findings about risks outside a default load carry
  `loaders: ["downstream-parse"]` instead of naming a loader.
- **Static findings are risks; `--verify` is proof.** The default scan
  infers what loader defaults would do. `--verify pandas|polars|both`
  actually loads the file and diffs every cell against the raw text.
- **The distinct-value caps bound memory, not correctness.** Past
  `--distinct-cap` (default 100k entries, 64 MB of stored values) a
  column's examples come from the capped sample and the column is named
  as capped in the report; rules still run.
- **Encoding is detected from a 64 KB sample.** Invalid bytes past the
  sample are replaced with U+FFFD and reported as
  `INVALID_ENCODING_BYTES`, not crashed on; the reported encoding is not
  a whole-file guarantee.
- **`--verify` replicates each loader's row handling** (first row is the
  header, pandas skips blank lines, polars keeps them as null rows, short
  rows are loader-filled) so the diff compares aligned rows; any residual
  row-count mismatch is stated as an alignment note.

## License

MIT
