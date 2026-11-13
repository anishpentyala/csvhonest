"""Single-pass CSV scanner.

Reads the raw file once, gathers per-column value counts (capped), row
shape problems, and file-level facts, then runs the mutation rules.
Nothing is mutated: every finding is computed from the raw text.
"""

from __future__ import annotations

import csv
import io
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .dialect import (
    DialectInfo,
    EncodingInfo,
    decode,
    detect_dialect,
    detect_encoding,
    sniff_has_header,
)
from .rules import ColumnStats, Finding, run_column_rules

DEFAULT_MAX_ROWS = 200_000
DEFAULT_DISTINCT_CAP = 100_000
DEFAULT_BYTE_CAP = 64 * 1024 * 1024  # distinct-value storage budget per file
_SAMPLE_BYTES = 65536


def open_text(path: str, enc: EncodingInfo):
    """Open a CSV for streaming text reads, honoring a detected BOM.

    Always decodes with errors='replace': detection runs on a sample, so a
    bad byte past the sample must not crash the scan (it is reported as a
    finding instead).
    """
    fh = open(path, "rb")
    if enc.bom and enc.encoding != "utf-8-sig":
        fh.seek(3)  # the BOM bytes are not cp1252 text
    return io.TextIOWrapper(fh, encoding=enc.encoding, errors="replace", newline="")


@dataclass
class ScanResult:
    path: str
    encoding: EncodingInfo
    dialect: DialectInfo
    headers: List[str]
    row_count: int  # data rows seen (after header)
    truncated: bool  # True when max_rows stopped the scan early
    # True/False when the header was auto-detected (and which way),
    # None when the caller forced it with --header/--no-header.
    header_auto: Optional[bool] = None
    decode_errors: int = 0  # replacement chars found while scanning

    @property
    def capped_columns(self) -> List[str]:
        return [s.name for s in self.stats if s.distinct_capped]
    findings: List[Finding] = field(default_factory=list)
    stats: List[ColumnStats] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.headers)

    def highest_severity(self) -> Optional[str]:
        order = {"high": 0, "medium": 1, "low": 2}
        if not self.findings:
            return None
        return min((f.severity for f in self.findings), key=lambda s: order[s])


def _file_level_findings(
    headers: List[str],
    ragged: Dict[int, List[int]],
    encoding: EncodingInfo,
    dialect: DialectInfo,
    decode_errors: int = 0,
) -> List[Finding]:
    findings: List[Finding] = []
    ncols = len(headers)
    dupes = sorted({h for h in headers if h and headers.count(h) > 1})
    if dupes:
        findings.append(
            Finding(
                code="DUPLICATE_HEADERS",
                severity="high",
                column=None,
                column_index=None,
                count=len(dupes),
                examples=dupes[:5],
                loaders=["pandas", "polars"],
                message=(
                    "Header names repeat; pandas silently renames them "
                    "(a, a.1) and polars silently renames them "
                    "(a, a_duplicated_0), so the loaded columns do not match "
                    "the file's header."
                ),
                suggestion="Rename duplicate columns before loading.",
            )
        )
    empty_headers = [i for i, h in enumerate(headers) if not h.strip()]
    if empty_headers:
        findings.append(
            Finding(
                code="EMPTY_HEADER",
                severity="medium",
                column=None,
                column_index=None,
                count=len(empty_headers),
                examples=[f"column position {i}" for i in empty_headers[:5]],
                loaders=["pandas", "polars"],
                message=(
                    "Some header cells are empty; loaders invent names "
                    "(Unnamed: 0) that shift between tools."
                ),
                suggestion="Fill header names before loading.",
            )
        )
    if ragged:
        short = sum(len(v) for w, v in ragged.items() if w < ncols)
        long_ = sum(len(v) for w, v in ragged.items() if w > ncols)
        examples = []
        for width, lines in sorted(ragged.items()):
            examples.append(f"{len(lines)} row(s) with {width} fields, first at line {lines[0]}")
        parts = []
        if short:
            parts.append(
                f"{short} short row(s) are silently padded with missing "
                f"values (pandas NaN, polars null)"
            )
        if long_:
            parts.append(f"{long_} long row(s) make both loaders raise")
        findings.append(
            Finding(
                code="RAGGED_ROWS",
                severity="high",
                column=None,
                column_index=None,
                count=sum(len(v) for v in ragged.values()),
                examples=examples[:5],
                loaders=["pandas", "polars"],
                message=(
                    "Row(s) with a different field count than the header: "
                    + "; ".join(parts)
                    + ". The file no longer matches its own shape."
                ),
                suggestion="Fix or drop the malformed lines before loading.",
            )
        )
    if decode_errors:
        findings.append(
            Finding(
                code="INVALID_ENCODING_BYTES",
                severity="medium",
                column=None,
                column_index=None,
                count=decode_errors,
                examples=[],
                loaders=["pandas", "polars"],
                message=(
                    f"{decode_errors} byte sequence(s) are not valid "
                    f"{encoding.encoding}; the scan replaced them with U+FFFD, "
                    f"and loaders will reject or mangle them too. Some may sit "
                    f"past the detection sample, so the reported encoding is "
                    f"not a guarantee for the whole file."
                ),
                suggestion="Re-export or convert the file to clean UTF-8.",
            )
        )
    if encoding.fell_back:
        findings.append(
            Finding(
                code="NON_UTF8_ENCODING",
                severity="medium",
                column=None,
                column_index=None,
                count=encoding.decode_errors,
                examples=[],
                loaders=["pandas", "polars"],
                message=(
                    f"File is not UTF-8 (detected {encoding.encoding}); "
                    f"loaders that assume UTF-8 raise or replace characters."
                ),
                suggestion="Pass encoding='cp1252' (pandas) or encoding='cp1252'/lossy utf8 (polars).",
            )
        )
    if not dialect.sniffer_agreed:
        findings.append(
            Finding(
                code="DIALECT_GUESSED",
                severity="low",
                column=None,
                column_index=None,
                count=1,
                examples=[f"delimiter {dialect.delimiter!r}"],
                loaders=["downstream-parse"],
                message=(
                    "The CSV sniffer could not settle the dialect; the "
                    "delimiter was inferred from column consistency. Verify "
                    "it matches the source."
                ),
                suggestion="Pass the delimiter explicitly when loading.",
            )
        )
    return findings


def scan_file(
    path: str,
    max_rows: int = DEFAULT_MAX_ROWS,
    distinct_cap: int = DEFAULT_DISTINCT_CAP,
    byte_cap: int = DEFAULT_BYTE_CAP,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
    has_header: Optional[bool] = None,
) -> ScanResult:
    """Scan one CSV file and return every mutation finding."""
    # Detect encoding and dialect from a small sample, then stream the
    # file; memory stays bounded by the distinct-value cap, not file size.
    with open(path, "rb") as fh:
        sample = fh.read(_SAMPLE_BYTES)

    if encoding:
        enc = EncodingInfo(encoding, bom=False, fell_back=False, decode_errors=0)
    else:
        enc = detect_encoding(sample)
    dialect = detect_dialect(decode(sample, enc))
    if delimiter is not None:
        dialect = DialectInfo(delimiter=delimiter, quotechar='"', sniffer_agreed=True)

    # Always decode with replacement: detection saw only a sample, and a
    # bad byte later in the file must become a finding, not a crash.
    fh = open_text(path, enc)

    def make_reader():
        return csv.reader(fh, delimiter=dialect.delimiter, quotechar=dialect.quotechar)

    reader = make_reader()
    header_auto: Optional[bool] = None
    if has_header is None:
        import itertools

        preview: List[List[str]] = []
        for i, row in enumerate(reader):
            preview.append(row)
            if i >= 20:
                break
        has_header = sniff_has_header(preview)
        header_auto = has_header
        reader = itertools.chain(preview, reader)

    rows_iter = iter(reader)
    if has_header:
        headers = next(rows_iter, [])
    else:
        import itertools

        first = next(rows_iter, [])
        headers = [f"column_{i}" for i in range(len(first))]
        rows_iter = itertools.chain([first], rows_iter)

    ncols = len(headers)
    stats = [ColumnStats(name=h, index=i) for i, h in enumerate(headers)]
    ragged: Dict[int, List[int]] = {}
    row_count = 0
    truncated = False
    decode_errors = 0
    stored_bytes = 0
    line_no = 1 if has_header else 0

    for row in rows_iter:
        line_no += 1
        if row_count >= max_rows:
            truncated = True
            break
        if not row:
            # A zero-field row is a blank line, not data. A row of empty
            # cells is real missing data and must be counted.
            continue
        row_count += 1
        if len(row) != ncols:
            bucket = ragged.setdefault(len(row), [])
            if len(bucket) < 5:
                bucket.append(line_no)
        for i in range(min(len(row), ncols)):
            value = row[i]
            decode_errors += value.count("�")
            # Bound distinct-value storage by bytes as well as entries, so
            # huge cells cannot blow past the memory budget.
            if stored_bytes < byte_cap or value in stats[i].values:
                before = len(stats[i].values)
                stats[i].add(value, distinct_cap)
                if len(stats[i].values) > before:
                    stored_bytes += len(value)
            else:
                stats[i].row_count += 1
                if value == "":
                    stats[i].empty_count += 1
                stats[i].distinct_capped = True

    findings = _file_level_findings(headers, ragged, enc, dialect, decode_errors)
    for col in stats:
        findings.extend(run_column_rules(col))

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 3), f.code))

    return ScanResult(
        path=os.fspath(path),
        encoding=enc,
        dialect=dialect,
        headers=headers,
        row_count=row_count,
        truncated=truncated,
        header_auto=header_auto,
        decode_errors=decode_errors,
        findings=findings,
        stats=stats,
    )
