# Numeric spelling verification correction (0.1.2)

Local Linux validation, 2026-11-15. No Windows/macOS or all-loader-versions claim.

## Defect and scope

Unchanged main 2f9241c97d9bd271bfb1a34b197a3eddaa1e7e6a labels the pandas
conversion of `9007199254740993.0` or `9.007199254740993e15` to
`9007199254740992` as formatting only. The equivalent plain integer correctly
reports a value change. `1e-400 -> 0` also incorrectly becomes formatting only.
Converting the raw text to float before comparing destroys the evidence.

Compare the exact source decimal to the loaded number's displayed decimal
instead. Decimal construction and comparison do not expand large exponents.
An explicit zero coefficient stays zero even with an exponent beyond the
Decimal range, checked with bounded literal validation. Unsupported nonzero
exponents or invalid numeric spellings cannot establish numeric equality. Ordinary `0.1` does not become a corruption claim from
its internal binary float expansion. `1e2 -> 100`, negative zero and equivalent
infinity spellings remain formatting only. A finite source overflowing to an
infinity is a value change. Missing-value handling and static rules are unchanged.

This is a source/display numeric-value comparison, not a bit-exact float audit.
It does not detect information absent from the loader's display, add decimal
loader support, change CSV alignment or guarantee every loader/version.

## Evidence

- CPython 3.10.12 with actual pandas 2.3.3/polars 2.0.0: 222 tests pass,
  including 54 new parameterized cases. Cases include decimal/exponent precision
  loss, signed underflow, overflow, integer-to-fraction changes, finite/nonfinite,
  negative zero, ordinary decimal spellings, 2,000-digit exponents and a
  5,000-digit integer. Actual loader tests cover both loaders, not just mocks.
- CPython 3.14.9 without loaders: 143 pass/25 skip.
- The 54 new cases against unchanged baseline: 25 fail/29 pass, no collection
  or setup errors. The excessive integer exposes an existing uncaught int-string
  conversion limit as a failed assertion path; this is not all misclassification.
- Five mutants caught: the extreme-zero guard removed (5 failures), rounding raw through float (23 failures), comparing the
  exact loaded binary expansion (2), calling every numeric difference a value
  change (20), treating underflow as zero equality (8). Restored suite passes.
- Seeded actual-loader oracle: 1,000 decimal spellings loaded by each loader,
  independently checked with Fraction(source) against Fraction(display), not
  Decimal or float(source). All 2,000 comparisons agree. pandas counts:
  194 unchanged/109 format/697 value; polars: 251/135/614. This is not an
  exhaustive decimal-parser correctness proof.
- Offline wheel/sdist build and neutral wheel-installed CPython 3.10 `-I`
  tests outside checkout: 222 pass, test source-path insertion removed.
  Existing loader site path is reused only for validation; no loader downloaded.

## Pre-review correction

Additional private checks after the first packet found a zero edge case:
Polars 2.0.0 parses `0e` followed by a 2,000-digit exponent as zero, while
Decimal rejects that exponent. The first candidate incorrectly called that a
value change. The explicit zero-literal guard corrects this without expanding
or parsing the exponent as an integer. Actual signed-zero controls cover both
loaders; pandas keeps these extreme spellings as strings. The corrected head,
not the first packet, is the review/release candidate.
