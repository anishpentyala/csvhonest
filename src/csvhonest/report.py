"""Human and machine reports for a scan, plus suggested loader fixes."""

from __future__ import annotations

import json
from typing import Dict, List

from .profiles import PANDAS_DEFAULT_NA
from .rules import Finding
from .scan import ScanResult
from .verify import VerificationReport

_SEVERITY_BADGE = {"high": "HIGH", "medium": "MED ", "low": "LOW "}


def suggested_overrides(result: ScanResult) -> Dict[str, object]:
    """Concrete loader arguments that prevent the high-severity mutations."""
    pandas_dtype: Dict[str, str] = {}
    polars_schema: List[str] = []
    na_collision = False
    for f in result.findings:
        if f.code in ("LEADING_ZERO_LOSS", "INT64_OVERFLOW", "SCI_NOTATION_FORM_LOSS") and f.column:
            pandas_dtype[f.column] = "str"
            polars_schema.append(f.column)
        elif f.code == "FLOAT64_PRECISION_LOSS" and f.column:
            if "polars" in f.loaders:
                # Mixed int/float column: Int64 cannot hold the floats
                # (TypeError on load), so only text preserves the big
                # integers. The pandas-only (holes) case can use Int64.
                pandas_dtype[f.column] = "str"
                polars_schema.append(f.column)
            else:
                pandas_dtype[f.column] = "Int64"
        elif f.code == "NA_COLLISION" and "pandas" in f.loaders:
            na_collision = True
        elif f.code == "WHITESPACE_KEY_COLLISION" and f.column:
            pandas_dtype.setdefault(f.column, "str")
            if f.column not in polars_schema:
                polars_schema.append(f.column)

    pandas_args: Dict[str, object] = {}
    if pandas_dtype:
        pandas_args["dtype"] = pandas_dtype
    if na_collision:
        # The colliding strings are data, not missing markers: turn the
        # default list off entirely and treat only empty cells as missing.
        pandas_args["keep_default_na"] = False
        pandas_args["na_values"] = [""]
    polars_args: Dict[str, object] = {}
    if polars_schema:
        polars_args["schema_overrides"] = {c: "String" for c in sorted(set(polars_schema))}
    if na_collision:
        polars_args["null_values"] = [""]
    return {"pandas": pandas_args, "polars": polars_args}


def _format_args(args: Dict[str, object], prefix: str) -> List[str]:
    lines: List[str] = []
    for key, value in args.items():
        lines.append(f"    {key}={value!r},")
    return lines


def render_verification_text(report: VerificationReport) -> str:
    lines: List[str] = []
    lines.append(f"Verified with {report.loader} (actual default load of this file)")
    if report.load_error:
        lines.append(f"  LOAD FAILED: {report.load_error}")
        return "\n".join(lines)
    lines.append(
        f"  {report.loaded_rows} rows x {report.loaded_columns} columns loaded; "
        f"{report.total_mutated_rows} cell(s) differ from the raw text"
    )
    if report.alignment_note:
        lines.append(f"  alignment note: {report.alignment_note}")
    for col in report.columns:
        if not col.mutations:
            continue
        lines.append(f"  column '{col.column}' (loaded as {col.loaded_dtype}):")
        for m in col.mutations[:5]:
            lines.append(
                f"    {m.raw!r} -> {m.became} ({m.kind.replace('_', ' ')}, "
                f"{m.rows} row(s))"
            )
    return "\n".join(lines)


def render_text(result: ScanResult, verifications: List[VerificationReport] = ()) -> str:
    lines: List[str] = []
    lines.append(f"csvhonest: {result.path}")
    lines.append(
        f"  {result.row_count} data rows x {result.column_count} columns, "
        f"encoding {result.encoding.encoding}, delimiter {result.dialect.delimiter!r}"
        + (" (scan truncated at --max-rows)" if result.truncated else "")
    )
    if result.encoding.bom:
        lines.append("  note: UTF-8 BOM present")
    if result.header_auto is not None:
        lines.append(
            "  header: auto-detected as "
            + ("present" if result.header_auto else "absent")
            + " (heuristic; override with --header/--no-header)"
        )

    if result.capped_columns:
        lines.append(
            "  note: distinct-value cap hit in column(s): "
            + ", ".join(result.capped_columns)
            + " (findings there reflect a partial sample)"
        )
    if not result.findings and not verifications:
        lines.append("  No silent-mutation findings. The file loads as written.")
        return "\n".join(lines)
    if not result.findings:
        lines.append("  No silent-mutation findings from the static scan.")

    counts = {"high": 0, "medium": 0, "low": 0}
    for f in result.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    lines.append(
        f"  {counts['high']} high, {counts['medium']} medium, {counts['low']} low finding(s)"
    )
    lines.append("")

    for f in result.findings:
        where = f"column '{f.column}'" if f.column else "file"
        loaders = ", ".join(f.loaders)
        lines.append(f"  [{_SEVERITY_BADGE.get(f.severity, '?')}] {f.code} ({where}; affects: {loaders})")
        lines.append(f"        {f.message}")
        if f.examples:
            lines.append(f"        examples: {', '.join(f.examples)}")
        lines.append(f"        fix: {f.suggestion}")
        lines.append("")

    overrides = suggested_overrides(result)
    if overrides["pandas"] or overrides["polars"]:
        lines.append("Suggested load arguments")
        lines.append("------------------------")
        if overrides["pandas"]:
            lines.append("pandas:")
            lines.append("  pd.read_csv(path,")
            lines.extend(_format_args(overrides["pandas"], "pandas"))
            lines.append("  )")
        if overrides["polars"]:
            lines.append("polars:")
            lines.append("  pl.read_csv(path,")
            lines.extend(_format_args(overrides["polars"], "polars"))
            lines.append("  )")
    for verification in verifications:
        lines.append("")
        lines.append(render_verification_text(verification))
    return "\n".join(lines)


def _verification_json(v: VerificationReport) -> dict:
    return {
        "loader": v.loader,
        "loaded_rows": v.loaded_rows,
        "loaded_columns": v.loaded_columns,
        "load_error": v.load_error,
        "alignment_note": v.alignment_note,
        "total_mutated_rows": v.total_mutated_rows,
        "columns": [
            {
                "column": c.column,
                "loaded_dtype": c.loaded_dtype,
                "mutated_rows": c.mutated_rows,
                "mutations": [
                    {"raw": m.raw, "became": m.became, "kind": m.kind, "rows": m.rows}
                    for m in c.mutations
                ],
            }
            for c in v.columns
            if c.mutations
        ],
    }


def render_json(result: ScanResult, verifications: List[VerificationReport] = ()) -> str:
    payload = {
        "file": result.path,
        "rows": result.row_count,
        "columns": result.column_count,
        "encoding": result.encoding.encoding,
        "delimiter": result.dialect.delimiter,
        "truncated": result.truncated,
        "header_auto": result.header_auto,
        "capped_columns": result.capped_columns,
        "decode_errors": result.decode_errors,
        "highest_severity": result.highest_severity(),
        "findings": [
            {
                "code": f.code,
                "severity": f.severity,
                "column": f.column,
                "column_index": f.column_index,
                "count": f.count,
                "examples": f.examples,
                "loaders": f.loaders,
                "message": f.message,
                "suggestion": f.suggestion,
            }
            for f in result.findings
        ],
        "suggested_overrides": suggested_overrides(result),
    }
    if verifications:
        payload["verification"] = [_verification_json(v) for v in verifications]
    return json.dumps(payload, indent=2, ensure_ascii=False)


def exit_code(result: ScanResult, fail_on: str = "high") -> int:
    order = {"high": 0, "medium": 1, "low": 2}
    threshold = order[fail_on]
    for f in result.findings:
        if order.get(f.severity, 3) <= threshold:
            return 1
    return 0
