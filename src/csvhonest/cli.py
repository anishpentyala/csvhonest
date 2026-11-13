"""Command line interface: csvhonest FILE... [--format text|json]."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .report import exit_code, render_json, render_text
from .scan import DEFAULT_DISTINCT_CAP, DEFAULT_MAX_ROWS, scan_file
from .verify import VERIFIERS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csvhonest",
        description=(
            "Audit what your CSV loader will silently change before you "
            "load it: dropped leading zeros, strings turned into NaN, "
            "float64 precision loss, ambiguous dates, lookalike keys."
        ),
    )
    parser.add_argument("files", nargs="+", help="CSV file(s) to audit")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"stop after this many data rows (default: {DEFAULT_MAX_ROWS})",
    )
    parser.add_argument(
        "--distinct-cap",
        type=int,
        default=DEFAULT_DISTINCT_CAP,
        help=(
            "store at most this many distinct values per column "
            f"(default: {DEFAULT_DISTINCT_CAP}); capped columns are named"
        ),
    )
    parser.add_argument("--encoding", help="override encoding detection")
    parser.add_argument("--delimiter", help="override delimiter detection")
    header = parser.add_mutually_exclusive_group()
    header.add_argument(
        "--header",
        dest="has_header",
        action="store_true",
        default=None,
        help="force first row to be the header (default: auto-detect)",
    )
    header.add_argument(
        "--no-header",
        dest="has_header",
        action="store_false",
        help="force first row to be data",
    )
    parser.add_argument(
        "--verify",
        choices=("pandas", "polars", "both"),
        help=(
            "also run the installed loader with defaults and diff raw vs "
            "loaded values, confirming mutations on this exact file"
        ),
    )
    parser.add_argument(
        "--fail-on",
        choices=("high", "medium", "low"),
        default="high",
        help="exit 1 when a finding at or above this severity exists (default: high)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    worst = 0
    for path in args.files:
        try:
            result = scan_file(
                path,
                max_rows=args.max_rows,
                distinct_cap=args.distinct_cap,
                encoding=args.encoding,
                delimiter=args.delimiter,
                has_header=args.has_header,
            )
        except OSError as exc:
            print(f"csvhonest: cannot read {path}: {exc}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        verifications = []
        if args.verify:
            names = ("pandas", "polars") if args.verify == "both" else (args.verify,)
            for name in names:
                verifications.append(VERIFIERS[name](result, args.max_rows))
        if args.format == "json":
            print(render_json(result, verifications))
        else:
            print(render_text(result, verifications))
            print()
        worst = max(worst, exit_code(result, fail_on=args.fail_on))
    return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
