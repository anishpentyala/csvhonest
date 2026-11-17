# csvhonest

**Audit what your CSV loader will silently change - before you load it.**

A CSV can look right in a text editor and change when pandas reads it. Catch lost ZIP-code zeros, a literal `NA` becoming missing, or an ID rounding before those values reach your analysis.

## Try it in 60 seconds

Install the versioned source release (Python 3.9+, Git required):

```sh
python -m pip install "git+https://github.com/anishpentyala/csvhonest.git@v0.1.3"
```

Create this **synthetic** sample as `surprises.csv`:

```csv
zip,country,customer_id
02139,NA,9007199254740993
00501,US,
10001,GB,42
```

Then run:

```sh
csvhonest surprises.csv
```

The report flags three high-severity risks: `LEADING_ZERO_LOSS`, `NA_COLLISION`, and `FLOAT64_PRECISION_LOSS`. It exits **1 because it found risks**, not because installation failed. The same input is bundled as [`examples/import-surprises.csv`](examples/import-surprises.csv).

Want to see what actually changes? Install pandas and verify this file with its default reader:

```sh
python -m pip install "pandas==2.3.3"
csvhonest surprises.csv --verify pandas
```

The verification reports four changed cells:

| Raw CSV text | pandas 2.3.3 default load | Why it matters |
| --- | --- | --- |
| `02139` | `2139` | ZIP-code zeros disappear |
| `00501` | `501` | Another ZIP code changes form |
| `NA` | missing | A literal country code is lost |
| `9007199254740993` | `9007199254740992` | An ID changes value after float conversion |

These are results from this tiny synthetic file, not a user benchmark. The missing ID makes pandas infer a floating-point column. Static scanning reports risks; `--verify pandas` loads and compares the actual file using the pandas version installed in your environment. Other inputs, loader versions and options can behave differently. Verification uses temporary local disk, as described below.

## When to use it

Use csvhonest before loading unfamiliar CSV exports, or in a pipeline that should stop on a risky import. It runs locally and the static scan needs only Python's standard library. Optional verification needs the loader being checked.

If you already know the schema, **explicit loader options are the first fix**, not another tool. For this sample, preserving every cell as text is as simple as:

```python
import pandas as pd

frame = pd.read_csv("surprises.csv", dtype=str, keep_default_na=False)
```

That is appropriate here because ZIP codes, country codes and customer IDs are text. It is not a universal loading policy: real numeric columns and missing values need a deliberate schema. csvhonest helps identify where to make that choice; it does not decide the intended meaning of your data.

## Install options

The pinned Git command above installs source without a manual clone. There is no PyPI release under the name `csvhonest`.

From a source checkout:

```sh
git clone https://github.com/anishpentyala/csvhonest.git
cd csvhonest
python -m pip install .
csvhonest examples/import-surprises.csv
```

Building needs setuptools >= 68. If an older pip cannot build the package, update pip in your virtual environment first. No pandas or Polars dependency is required for a static scan.

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

Numeric verification compares the exact source decimal spelling to the displayed
loaded number. A lost digit or nonzero value underflowing to zero is a
`value_changed`, even when the source uses a decimal point or exponent.
`1e2 -> 100` remains `form_changed`; ordinary `0.1` is not flagged for the
binary float's internal expansion. This is a comparison of displayed numeric
values, not a bit-exact floating-point audit or a guarantee for every loader.

Exit code is 1 when a finding at or above `--fail-on` (default `high`)
exists, so the check slots into a pipeline or pre-commit step.
A requested verification that cannot load the file, read its raw comparison,
or establish matching row/column shapes exits **2**, even if the static scan
found no high-severity risks. It returns a verification error with no cell
mutation claims, rather than comparing a potentially shifted prefix.

Pandas verification skips unquoted ASCII space/tab-only physical lines at CSV
record boundaries, including before the header. Quoted whitespace, embedded
blank lines inside a multiline quoted cell, and space/tab delimiters remain
data. Polars keeps its existing blank-record policy. This behavior was checked
with pandas 2.3.3 and polars 2.0.0; it does not certify every future loader
version or unusual parser configuration. Verification still uses each loader's
normal header interpretation, even when the static scanner was told
`--no-header`. With `--verify`, the CLI first copies the full input into a private temporary
directory. Static scanning and both loaders then use that capture; a rewrite or
deletion of the original after capture cannot fabricate mutation evidence.
The Python `verify_with_pandas` / `verify_with_polars` functions each capture
their input before loading and comparing. They do not retroactively make an
earlier independent `scan_file()` call part of that capture.

Capturing is a streaming disk copy, not a filesystem transaction: a writer can
still change bytes while the copy is in progress. All comparisons describe the
captured bytes, not a guaranteed point-in-time version of a concurrently written
file. Verification copies the complete input even with `--max-rows`; temporary
disk space can reach twice its size because a CLI capture and one verifier
capture coexist. Files are removed on success or failure. Without `--verify`,
static scanning keeps its existing streaming path. Matching shapes still cannot
detect every possible parser disagreement.


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
| `INT64_OVERFLOW` | high | pandas switches to uint64, or text (object in pandas 2, str in pandas 3) when signs mix; polars 2.x to Int128 (1.x raises) |
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
