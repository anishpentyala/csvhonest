# Verification alignment correction (0.1.1)

Local Linux validation,2026-11-15. No Windows/macOS or all-loader-versions claim.

## Baseline defect

On unchanged main5ce242bf63bff769d7b0445058413108cda7eb8f with pandas2.3.3,
`name\nAlice\n   \nBob\nCarol\n` loads as Alice,Bob,Carol. The old verifier
instead compared the skipped raw space record with Bob, then Bob with Carol,
and called both proven value changes. A leading blank record also displaced
the header. Quoted spaces and whitespace-only lines inside quoted multiline
cells are real data, so stripping every parsed value or physical line is wrong.

Pandas documentation inspected2026-11-15 says `skip_blank_lines=True` and that
header indexing counts only nonblank/noncommented lines:
https://pandas.pydata.org/docs/reference/api/pandas.read_csv.html

The narrow fix filters ASCII space/tab/CRLF-only physical lines only at CSV
record boundaries. It preserves quoting, multiline cells, delimiter characters,
and non-ASCII whitespace. Remaining row/header-width/overwide-record or loaded
column-length mismatches refuse all cell claims. Requested failed verification
exits2, not a clean static-scan success. Static scanning rules are unchanged.

## Local checks

-168tests pass on CPython3.10.12 with pandas2.3.3/polars2.0.0:105old cases plus
  63new parameterized cases. New controls include leading/middle/trailing blanks,
  LF/CRLF/CR, quoted/multiline values, ASCII whitespace delimiters, NBSP/emspace/
  VT/formfeed, retained true leading-zero mutations, shape disagreement, missing
  raw source and refused-verification CLI exits.
- Without optional loaders:105pass/9skip on CPython3.9.25,3.12.15,3.13.17,3.14.9.
  The old optional-loader modules skip at collection and five new real-pandas
  cases skip; synthetic alignment/error controls still run.
-240seeded actual-pandas fixtures,2496loaded records, preserve every true
  leading-zero mutation and produce no name-column phantom mutation. Fixtures
  include quoted multiline whitespace, escaped quotes, leading/interrecord
  whitespace, three line endings and varied row caps.
- Against unchanged baseline, the63new cases give43failed/20passed. Four separate
  mutations are caught: filtering inside multiline cells(2failures), stripping
  all Unicode whitespace(5), comparing a misaligned prefix(5), clean CLI exit on
  refused verification(2). No setup/collection errors. Restored candidate passes.
- Offline wheel/sdist build. Neutral wheel-installed tests outside the checkout,
  with test source-path insertion removed:3.10with existing2.3.3/2.0.0loaders
  reused through a validation-only site path gives168pass;3.14withoutloaders
  gives105pass/9skip. Runtime module paths and bytes are checked against the wheel.
  No new loader dependency was downloaded.

## Boundaries

Matching shapes cannot detect every parser disagreement or a concurrent
same-shape file rewrite. This is not atomic snapshot verification, a content
hash, a universal CSV dialect implementation or a model of every future loader.
The Polars row-cap behavior can produce more rows than requested: that remaining
shape mismatch now refuses comparison instead of claiming an aligned prefix.
The report's existing `load_error` field also carries comparison/alignment
refusals; the displayed label is VERIFICATION FAILED. Loaded dimensions remain
facts about the loader, but no column mutations are emitted on refusal.
