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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dialect import DialectInfo, EncodingInfo
from .scan import ScanResult

_SAMPLE_CAP = 5
_INT_RE = re.compile(r"^[+-]?\d+$")


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
    # Numeric comparison under different string form: 02139 -> 2139,
    # 1E2 -> 100.0. Integers are compared exactly (not as floats) so
    # float64 precision loss shows as a value change, not a form change.
    if _INT_RE.match(raw) and isinstance(loaded_value, (int, float)):
        try:
            exact = int(loaded_value)
        except (TypeError, ValueError, OverflowError):
            return "value_changed"
        if int(raw) == exact:  # same integer, only the string form differs
            return "form_changed"
        return "value_changed"
    try:
        raw_num = float(raw)
    except ValueError:
        raw_num = None
    if raw_num is not None and isinstance(loaded_value, (int, float)):
        try:
            loaded_num = float(loaded_value)
        except (TypeError, ValueError):
            return "value_changed"
        if raw_num == loaded_num:
            return "form_changed"
        return "value_changed"
    return "value_changed"


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

    Alignment replicates the loader's own view exactly, otherwise the diff
    fabricates mutations: both pandas and polars treat the first row as the
    header by default, and pandas skips blank lines while polars keeps them
    as null rows (verified against pandas 2.3.3 / polars 2.0.0).
    """
    report = VerificationReport(
        loader=loader_name,
        loaded_rows=len(loaded_columns[0][2]) if loaded_columns else 0,
        loaded_columns=len(loaded_columns),
    )

    from .scan import open_text

    raw_rows: List[List[str]] = []
    with open_text(result.path, result.encoding) as fh:
        reader = csv.reader(
            fh, delimiter=result.dialect.delimiter, quotechar=result.dialect.quotechar
        )
        rows = iter(reader)
        next(rows, None)  # row 1 is the loader's header, not data
        while len(raw_rows) < max_rows:
            row = next(rows, None)
            if row is None:
                break
            if not row:
                if skips_blank_lines:
                    continue  # pandas skip_blank_lines=True
                row = [""]  # polars keeps a blank line as a null row
            raw_rows.append(row)

    n = min(len(raw_rows), report.loaded_rows)
    if len(raw_rows) != report.loaded_rows:
        report.alignment_note = (
            f"loader produced {report.loaded_rows} row(s) vs "
            f"{len(raw_rows)} raw data row(s); compared the first {n}."
        )
    for idx, (name, dtype, values) in enumerate(loaded_columns):
        if idx >= len(result.stats):
            break
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


def verify_with_pandas(result: ScanResult, max_rows: int) -> VerificationReport:
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


def verify_with_polars(result: ScanResult, max_rows: int) -> VerificationReport:
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


VERIFIERS = {"pandas": verify_with_pandas, "polars": verify_with_polars}
