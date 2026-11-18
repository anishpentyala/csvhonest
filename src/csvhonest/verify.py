"""Verify mode: run the real loader and diff raw text against loaded values.

Static findings say what a loader's defaults *would* change. Verification
loads the file with the installed pandas/polars and reports what it
*actually* changed on this exact file, per distinct raw value, so the
report separates proven mutations from risks.
"""

from __future__ import annotations

import csv
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from .dialect import DialectInfo, EncodingInfo
from .scan import ScanResult
from .snapshot import captured_path

_SAMPLE_CAP = 5
_ZERO_LITERAL_RE = re.compile(r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass
class ValueMutation:
    raw: str
    became: str  # rendered loaded value, or "<missing>"
    kind: str  # became_missing | value_changed | form_changed
    rows: int


@dataclass
class ColumnVerification:
    column: str
    index: int
    loaded_dtype: str
    mutations: List[ValueMutation] = field(default_factory=list)
    mutated_rows: int = 0


@dataclass
class VerificationReport:
    loader: str
    loaded_rows: int
    loaded_columns: int
    load_error: Optional[str] = None
    alignment_note: Optional[str] = None
    columns: List[ColumnVerification] = field(default_factory=list)

    @property
    def total_mutated_rows(self) -> int:
        return sum(c.mutated_rows for c in self.columns)


def _classify(raw: str, loaded_repr: Optional[str], loaded_value) -> Optional[str]:
    """Compare one raw cell to its loaded value; None means unchanged."""
    if loaded_repr is None:  # missing marker
        if raw == "":
            return None
        return "became_missing"
    if loaded_repr == raw:
        return None
    # Compare decimal spellings without first rounding the source through
    # float(). That would turn genuine precision loss/underflow into an
    # apparent formatting change. Use the loaded display, not its exact
    # binary expansion: ordinary 0.1 must not become a corruption claim.
    if isinstance(loaded_value, (int, float)):
        # Zero stays zero even when its exponent exceeds Decimal's supported
        # range. Validate the spelling without converting/expanding the exponent.
        if loaded_value == 0 and _ZERO_LITERAL_RE.fullmatch(raw.strip()):
            return "form_changed"
        try:
            if Decimal(raw) == Decimal(loaded_repr):
                return "form_changed"
        except (InvalidOperation, ValueError):
            # NaNs and unsupported exponent spellings cannot establish
            # numeric equality. Decimal construction/comparison does not
            # expand a huge exponent into a huge integer or decimal string.
            pass
    return "value_changed"


def _raw_records(fh, dialect, skips_blank_lines):
    """Skip pandas blank physical lines only at CSV record boundaries.

    Looking at parsed cell values loses quotes: an unquoted space-only line
    is blank for pandas, but a quoted space or a line inside a multiline cell
    is data. Space/tab delimiters themselves also make a line into fields.
    """
    at_start = True

    def lines():
        nonlocal at_start
        for line in fh:
            if (
                skips_blank_lines
                and at_start
                and not line.strip("\r\n" if skips_blank_lines == "empty" else " \t\r\n")
                and dialect.delimiter not in line
                and (not dialect.quotechar or dialect.quotechar not in line)
            ):
                continue
            at_start = False
            yield line

    reader = csv.reader(
        lines(), delimiter=dialect.delimiter, quotechar=dialect.quotechar,
        quoting=csv.QUOTE_MINIMAL if dialect.quotechar else csv.QUOTE_NONE,
    )
    while True:
        at_start = True
        row = next(reader, None)
        if row is None:
            return
        yield row


def _verify_generic(
    result: ScanResult,
    loader_name: str,
    loaded_columns: List[Tuple[str, str, list]],
    is_missing,
    max_rows: int,
    skips_blank_lines: bool,
) -> VerificationReport:
    """Diff raw cells against loaded columns.

    loaded_columns: per loaded column (name, dtype, values list). Missing
    values are represented by whatever is_missing detects.

    Alignment follows the loader's blank-record policy, otherwise the diff
    fabricates mutations: both pandas and polars treat the first row as the
    header by default, and pandas skips blank lines while polars keeps them
    as null rows (verified against pandas 2.3.3 / polars 2.0.0). A remaining
    shape mismatch refuses all cell claims instead of comparing a shifted prefix.
    """
    report = VerificationReport(
        loader=loader_name,
        loaded_rows=len(loaded_columns[0][2]) if loaded_columns else 0,
        loaded_columns=len(loaded_columns),
    )

    from .scan import open_text

    raw_rows: List[List[str]] = []
    try:
        with open_text(result.path, result.encoding) as fh:
            rows = iter(_raw_records(fh, result.dialect, skips_blank_lines))
            header = next(rows, [])  # first loader-visible record, not physical line
            while len(raw_rows) < max_rows:
                row = next(rows, None)
                if row is None:
                    break
                if not row:
                    row = [""]  # polars retains an empty physical line as a null row
                raw_rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        report.load_error = "raw comparison could not be completed: " + str(exc)
        return report

    n = len(raw_rows)
    if (
        n != report.loaded_rows
        or len(header) != report.loaded_columns
        or any(len(row) > report.loaded_columns for row in raw_rows)
        or any(len(values) != n for _, _, values in loaded_columns)
    ):
        report.alignment_note = (
            f"loader produced {report.loaded_rows} row(s) x {report.loaded_columns} column(s); "
            f"raw comparison has {n} data row(s) and {len(header)} header field(s). "
            "No cells compared because alignment is unverified."
        )
        report.load_error = "cannot verify row/column alignment"
        return report
    for idx, (name, dtype, values) in enumerate(loaded_columns):
        col = ColumnVerification(column=name, index=idx, loaded_dtype=dtype)
        # distinct raw -> Counter of outcomes
        outcomes: Dict[str, Counter] = {}
        became: Dict[str, str] = {}
        for r in range(n):
            row = raw_rows[r]
            value = values[r]
            if idx >= len(row):
                # Short row: the loader padded this cell itself; the raw
                # file has no value here at all.
                kind = "loader_filled"
                raw = "<no cell>"
                rendered = "<missing>" if is_missing(value) else _render(value)
            elif is_missing(value):
                raw = row[idx]
                kind = _classify(raw, None, None)
                rendered = "<missing>"
            else:
                raw = row[idx]
                rendered = _render(value)
                kind = _classify(raw, rendered, value)
            if kind is None:
                continue
            outcomes.setdefault(raw, Counter())[kind] += 1
            became.setdefault(raw, rendered)
        for raw, kinds in outcomes.items():
            for kind, count in kinds.items():
                col.mutations.append(
                    ValueMutation(raw=raw, became=became[raw], kind=kind, rows=count)
                )
                col.mutated_rows += count
        col.mutations.sort(key=lambda m: -m.rows)
        col.mutations = col.mutations[:_SAMPLE_CAP * 4]
        report.columns.append(col)
    return report


def _render(value) -> str:
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def _verify_with_pandas(result: ScanResult, max_rows: int) -> VerificationReport:
    try:
        import pandas as pd
    except ImportError:
        return VerificationReport(
            loader="pandas",
            loaded_rows=0,
            loaded_columns=0,
            load_error="pandas is not installed",
        )
    try:
        df = pd.read_csv(
            result.path,
            sep=result.dialect.delimiter,
            quotechar=result.dialect.quotechar,
            encoding=result.encoding.encoding,
            nrows=max_rows,
        )
    except Exception as exc:  # loader could not parse the file at all
        return VerificationReport(
            loader="pandas", loaded_rows=0, loaded_columns=0, load_error=str(exc)
        )

    loaded_columns = [(str(c), str(df[c].dtype), df[c].tolist()) for c in df.columns]
    return _verify_generic(
        result,
        "pandas",
        loaded_columns,
        lambda v: v is None or (isinstance(v, float) and math.isnan(v)),
        max_rows,
        skips_blank_lines=True,
    )


def _verify_with_polars(result: ScanResult, max_rows: int) -> VerificationReport:
    try:
        import polars as pl
    except ImportError:
        return VerificationReport(
            loader="polars",
            loaded_rows=0,
            loaded_columns=0,
            load_error="polars is not installed",
        )
    try:
        df = pl.read_csv(
            result.path,
            separator=result.dialect.delimiter,
            quote_char=result.dialect.quotechar,
            encoding=(
                "utf8-lossy" if result.encoding.fell_back else "utf8"
            ),
            n_rows=max_rows,
        )
    except Exception as exc:
        return VerificationReport(
            loader="polars", loaded_rows=0, loaded_columns=0, load_error=str(exc)
        )

    loaded_columns = [(c, str(df[c].dtype), df[c].to_list()) for c in df.columns]
    return _verify_generic(
        result,
        "polars",
        loaded_columns,
        lambda v: v is None,
        max_rows,
        skips_blank_lines=False,
    )


def _captured_verify(result, max_rows, loader, verify):
    try:
        with captured_path(result.path) as path:
            report = verify(replace(result, path=path), max_rows)
            if report.load_error:
                report.load_error = report.load_error.replace(path, str(result.path))
            return report
    except OSError as exc:
        return VerificationReport(
            loader=loader, loaded_rows=0, loaded_columns=0,
            load_error="input capture could not be completed: " + str(exc),
        )


def verify_with_pandas(result: ScanResult, max_rows: int) -> VerificationReport:
    """Load and compare the same private capture of the source bytes."""
    return _captured_verify(result, max_rows, "pandas", _verify_with_pandas)


def verify_with_polars(result: ScanResult, max_rows: int) -> VerificationReport:
    """Load and compare the same private capture of the source bytes."""
    return _captured_verify(result, max_rows, "polars", _verify_with_polars)


VERIFIERS = {"pandas": verify_with_pandas, "polars": verify_with_polars}
