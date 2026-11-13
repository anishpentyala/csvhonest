"""Mutation rules: each rule answers "what happens to these raw strings
under a loader's defaults?" and returns findings with concrete examples.

Rules work on the per-column value counts gathered by the scanner, so they
never need the file in memory and their cost is bounded by the distinct
value cap, not the file size.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .profiles import DOWNSTREAM, PANDAS, POLARS, LoaderProfile

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
FLOAT64_EXACT_MAX = 2**53  # beyond this not every integer is representable

_INT_RE = re.compile(r"^[+-]?\d+$")
# pandas and polars both parse 'inf'/'-inf'/'infinity' as float.
_FLOAT_RE = re.compile(
    r"^[+-]?((\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?|inf(inity)?)$", re.IGNORECASE
)
_SCI_RE = re.compile(r"^[+-]?\d+[eE][+-]?\d+$")
_LEADING_ZERO_RE = re.compile(r"^-?0\d+$")
_DATE_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")
_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
_CURRENCY_RE = re.compile(r"^[+-]?[$€£]\s?\d[\d.,]*$")
_PERCENT_RE = re.compile(r"^[+-]?\d[\d.,]*\s?%$")

# Invisible or confusable characters that make visually identical keys.
_SNEAKY_CHARS = {
    " ": "NO-BREAK SPACE",
    "​": "ZERO WIDTH SPACE",
    "‌": "ZERO WIDTH NON-JOINER",
    "‍": "ZERO WIDTH JOINER",
    "﻿": "BYTE ORDER MARK / ZERO WIDTH NO-BREAK SPACE",
    "­": "SOFT HYPHEN",
}

SEVERITIES = ("high", "medium", "low")


@dataclass
class Finding:
    code: str
    severity: str
    column: Optional[str]
    column_index: Optional[int]
    count: int
    examples: List[str]
    loaders: List[str]
    message: str
    suggestion: str


@dataclass
class ColumnStats:
    """Accumulated facts about one column, gathered in a single pass."""

    name: str
    index: int
    row_count: int = 0
    empty_count: int = 0
    values: Counter = field(default_factory=Counter)
    distinct_capped: bool = False

    def add(self, value: str, cap: int) -> None:
        self.row_count += 1
        if value == "":
            self.empty_count += 1
        if len(self.values) < cap or value in self.values:
            self.values[value] += 1
        else:
            self.distinct_capped = True

    # -- derived facts -------------------------------------------------

    def numeric_parseable(self, value: str) -> bool:
        return bool(_INT_RE.match(value) or _FLOAT_RE.match(value))

    def numeric_parseable_stripped(self, value: str) -> bool:
        """pandas trims surrounding whitespace when parsing numbers."""
        return self.numeric_parseable(value.strip())

    def non_empty_values(self) -> Iterable[Tuple[str, int]]:
        for v, n in self.values.items():
            if v != "":
                yield v, n

    def infers_numeric(self, strip: bool = False) -> bool:
        """True when every non-empty value parses as a number.

        strip=True models pandas, which trims whitespace around numbers;
        strip=False models polars, which keeps padded values as strings.
        """
        seen = False
        check = self.numeric_parseable_stripped if strip else self.numeric_parseable
        for v, _ in self.non_empty_values():
            if not check(v):
                return False
            seen = True
        return seen

    def infers_int(self) -> bool:
        seen = False
        for v, _ in self.non_empty_values():
            if not _INT_RE.match(v):
                return False
            seen = True
        return seen


def _examples(items: Iterable[str], limit: int = 3) -> List[str]:
    out: List[str] = []
    for v in items:
        if len(out) >= limit:
            break
        shown = v if len(v) <= 40 else v[:37] + "..."
        out.append(shown)
    return out


def rule_leading_zero_loss(stats: ColumnStats) -> List[Finding]:
    findings: List[Finding] = []
    suggestion = (
        "Load the column as text: pandas dtype={"
        f"'{stats.name}': str}}, polars schema_overrides={{"
        f"'{stats.name}': pl.String}}."
    )

    # Unpadded zeros are lost by both loaders when every raw value is
    # numeric; one text value keeps the whole column as strings.
    hits = [(v, n) for v, n in stats.non_empty_values() if _LEADING_ZERO_RE.match(v)]
    if hits and stats.infers_numeric():
        count = sum(n for _, n in hits)
        findings.append(
            Finding(
                code="LEADING_ZERO_LOSS",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in sorted(hits, key=lambda t: -t[1])),
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"{count} value(s) in column '{stats.name}' have leading zeros "
                    f"and every value looks numeric, so a default load parses them "
                    f"as integers and the zeros are gone (e.g. ZIP codes, IDs)."
                ),
                suggestion=suggestion,
            )
        )

    # Whitespace-padded zeros: pandas trims and parses them (zeros lost);
    # polars keeps the padded strings (verified against pandas 2.3.3 /
    # polars 2.0.0).
    padded = [
        (v, n)
        for v, n in stats.non_empty_values()
        if v != v.strip() and _LEADING_ZERO_RE.match(v.strip())
    ]
    if padded and stats.infers_numeric(strip=True):
        count = sum(n for _, n in padded)
        findings.append(
            Finding(
                code="PADDED_LEADING_ZERO_LOSS",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(repr(v) for v, _ in sorted(padded, key=lambda t: -t[1])),
                loaders=[PANDAS.name],
                message=(
                    f"{count} value(s) in column '{stats.name}' are "
                    f"whitespace-padded numbers with leading zeros; pandas trims "
                    f"and parses them, losing the zeros and the padding, while "
                    f"polars keeps the original strings."
                ),
                suggestion=suggestion,
            )
        )
    return findings


def rule_na_collision(stats: ColumnStats) -> List[Finding]:
    findings: List[Finding] = []
    for profile in (PANDAS, POLARS):
        hits = [
            (v, n)
            for v, n in stats.non_empty_values()
            if v in profile.na_values
        ]
        if not hits:
            continue
        count = sum(n for _, n in hits)
        findings.append(
            Finding(
                code="NA_COLLISION",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in sorted(hits, key=lambda t: -t[1])),
                loaders=[profile.name],
                message=(
                    f"{count} value(s) in column '{stats.name}' are strings "
                    f"that {profile.name} converts to missing by default. "
                    f"When they mark real missing data that is fine; when any "
                    f"is real data (country code 'NA', the name 'None'), it is "
                    f"silently lost."
                ),
                suggestion=(
                    "pandas: keep_default_na=False plus an explicit na_values "
                    "list. polars: pass an explicit null_values list."
                ),
            )
        )
    return findings


def rule_int_precision_loss(stats: ColumnStats) -> List[Finding]:
    overflow: List[Tuple[str, int]] = []  # beyond int64
    risky: List[Tuple[str, int]] = []  # beyond float64-exact, coerced by pandas
    for v, n in stats.non_empty_values():
        if not _INT_RE.match(v):
            continue
        try:
            i = int(v)
        except ValueError:
            continue
        if i > INT64_MAX or i < INT64_MIN:
            overflow.append((v, n))
        elif abs(i) > FLOAT64_EXACT_MAX:
            risky.append((v, n))

    findings: List[Finding] = []
    # Only an all-numeric column gets a numeric dtype; with text present the
    # column stays strings and no silent switch happens.
    column_numeric = stats.infers_numeric()
    if overflow and column_numeric:
        count = sum(n for _, n in overflow)
        findings.append(
            Finding(
                code="INT64_OVERFLOW",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in overflow),
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"{count} integer value(s) in column '{stats.name}' exceed "
                    f"int64 range; pandas silently switches dtype (uint64, or "
                    f"object strings when signs mix) and polars switches to "
                    f"Int128, so downstream code expecting a fixed-width int "
                    f"gets a different type or wrong comparisons."
                ),
                suggestion=(
                    f"Load as text (dtype=str / schema String) if these are IDs."
                ),
            )
        )
    has_float_like = any(
        _FLOAT_RE.match(v) and not _INT_RE.match(v)
        for v, _ in stats.non_empty_values()
    )
    if risky and column_numeric and has_float_like:
        # int + float mix -> the whole column is float64/Float64 in both
        # loaders (verified), so the big integers change value on load.
        count = sum(n for _, n in risky)
        findings.append(
            Finding(
                code="FLOAT64_PRECISION_LOSS",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in risky),
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"Column '{stats.name}' mixes integers above 2^53 with "
                    f"float values; both pandas and polars load the column as "
                    f"float64/Float64 and these integers no longer round-trip "
                    f"exactly (e.g. 18-digit IDs)."
                ),
                suggestion=(
                    f"Load '{stats.name}' as text (dtype=str / schema String) "
                    f"if these are IDs."
                ),
            )
        )
    elif risky and stats.empty_count > 0:
        # int-with-holes: only pandas coerces Int->Float64 when missing values exist
        count = sum(n for _, n in risky)
        findings.append(
            Finding(
                code="FLOAT64_PRECISION_LOSS",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in risky),
                loaders=[PANDAS.name],
                message=(
                    f"Column '{stats.name}' mixes integers above 2^53 with "
                    f"missing values; pandas coerces the column to float64 and "
                    f"these integers no longer round-trip exactly "
                    f"(e.g. 18-digit IDs)."
                ),
                suggestion=(
                    f"pandas: dtype={{'{stats.name}': 'Int64'}} (nullable integer) "
                    f"or str for IDs."
                ),
            )
        )
    elif risky and stats.infers_int():
        count = sum(n for _, n in risky)
        findings.append(
            Finding(
                code="LARGE_ID_PRECISION",
                severity="medium",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(v for v, _ in risky),
                loaders=[DOWNSTREAM],
                message=(
                    f"{count} integer(s) in column '{stats.name}' exceed 2^53; "
                    f"they survive the CSV load but lose exactness in JSON, "
                    f"JavaScript, and float-based tools."
                ),
                suggestion=f"Treat '{stats.name}' as a string ID, not a number.",
            )
        )
    return findings


def rule_int_to_float_with_holes(stats: ColumnStats) -> List[Finding]:
    if stats.empty_count == 0 or not stats.infers_int():
        return []
    non_empty = sum(n for _, n in stats.non_empty_values())
    if non_empty == 0:
        return []
    return [
        Finding(
            code="INT_BECOMES_FLOAT64",
            severity="low",
            column=stats.name,
            column_index=stats.index,
            count=stats.empty_count,
            examples=[],
            loaders=[PANDAS.name],
            message=(
                f"Column '{stats.name}' is all integers but has "
                f"{stats.empty_count} missing cell(s); pandas loads it as "
                f"float64 (values print as 123.0)."
            ),
            suggestion=(
                f"pandas: dtype={{'{stats.name}': 'Int64'}} to keep integers "
                f"with missing values."
            ),
        )
    ]


def rule_sci_notation(stats: ColumnStats) -> List[Finding]:
    hits = [(v, n) for v, n in stats.non_empty_values() if _SCI_RE.match(v)]
    if not hits or not stats.infers_numeric():
        return []
    count = sum(n for _, n in hits)
    return [
        Finding(
            code="SCI_NOTATION_FORM_LOSS",
            severity="medium",
            column=stats.name,
            column_index=stats.index,
            count=count,
            examples=_examples(v for v, _ in hits),
            loaders=[PANDAS.name, POLARS.name],
            message=(
                f"{count} value(s) in column '{stats.name}' use E-notation and "
                f"the column infers numeric, so the original string form is "
                f"lost ('1E2' becomes 100.0). If these are codes or SKUs, "
                f"the value itself changed."
            ),
            suggestion=(
                f"If '{stats.name}' holds identifiers, load it as text."
            ),
        )
    ]


def rule_bool_coercion(stats: ColumnStats) -> List[Finding]:
    findings: List[Finding] = []
    non_empty = [v for v, _ in stats.non_empty_values()]
    if not non_empty:
        return findings
    for profile in (PANDAS, POLARS):
        if all(v in profile.bool_strings for v in non_empty):
            findings.append(
                Finding(
                    code="BOOL_COERCION",
                    severity="low",
                    column=stats.name,
                    column_index=stats.index,
                    count=stats.row_count - stats.empty_count,
                    examples=_examples(non_empty),
                    loaders=[profile.name],
                    message=(
                        f"Column '{stats.name}' is entirely "
                        f"{'/'.join(sorted(profile.bool_strings))} strings; "
                        f"{profile.name} converts them to booleans, so the "
                        f"original strings are gone."
                    ),
                    suggestion=(
                        f"Load '{stats.name}' as text if the original strings "
                        f"matter (e.g. re-export fidelity)."
                    ),
                )
            )
    return findings


def rule_thousands_separator(stats: ColumnStats) -> List[Finding]:
    hits = [(v, n) for v, n in stats.non_empty_values() if _THOUSANDS_RE.match(v)]
    if not hits:
        return []
    # Only meaningful when the rest of the column is numeric: then the whole
    # column silently stays strings.
    others_numeric = all(
        stats.numeric_parseable(v)
        for v, _ in stats.non_empty_values()
        if not _THOUSANDS_RE.match(v)
    )
    if not others_numeric:
        return []
    count = sum(n for _, n in hits)
    return [
        Finding(
            code="THOUSANDS_SEPARATOR",
            severity="medium",
            column=stats.name,
            column_index=stats.index,
            count=count,
            examples=_examples(v for v, _ in hits),
            loaders=[PANDAS.name, POLARS.name],
            message=(
                f"{count} value(s) in column '{stats.name}' use thousands "
                f"separators; the column loads as text, so sums and sorts on "
                f"the numeric majority are wrong until cleaned."
            ),
            suggestion=(
                "pandas: thousands=',' parses them as numbers. "
                "polars: load as String and strip commas before casting."
            ),
        )
    ]


def rule_currency_or_percent(stats: ColumnStats) -> List[Finding]:
    hits = [
        (v, n)
        for v, n in stats.non_empty_values()
        if _CURRENCY_RE.match(v) or _PERCENT_RE.match(v)
    ]
    if not hits:
        return []
    count = sum(n for _, n in hits)
    return [
        Finding(
            code="CURRENCY_OR_PERCENT",
            severity="medium",
            column=stats.name,
            column_index=stats.index,
            count=count,
            examples=_examples(v for v, _ in hits),
            loaders=[PANDAS.name, POLARS.name],
            message=(
                f"{count} value(s) in column '{stats.name}' carry currency "
                f"symbols or percent signs; they load as text, and naive "
                f"float() conversion raises or misreads ('5%' -> 5, not 0.05)."
            ),
            suggestion=(
                f"Load '{stats.name}' as text, strip symbols, then cast; "
                f"divide percents by 100 explicitly."
            ),
        )
    ]


def rule_ambiguous_date(stats: ColumnStats) -> List[Finding]:
    ambiguous: List[Tuple[str, int]] = []
    formats = set()
    for v, n in stats.non_empty_values():
        m = _DATE_RE.match(v)
        if not m:
            continue
        a, b, _ = m.groups()
        formats.add(len(m.group(3)))
        if int(a) <= 12 and int(b) <= 12:
            ambiguous.append((v, n))
    if not ambiguous:
        return []
    count = sum(n for _, n in ambiguous)
    mixed = " Mixed 2- and 4-digit years are present too." if len(formats) > 1 else ""
    return [
        Finding(
            code="AMBIGUOUS_DATE",
            severity="medium",
            column=stats.name,
            column_index=stats.index,
            count=count,
            examples=_examples(v for v, _ in ambiguous),
            loaders=[DOWNSTREAM],
            message=(
                f"{count} date-like value(s) in column '{stats.name}' are "
                f"ambiguous between day-first and month-first (both parts "
                f"<= 12). Any later parse (to_datetime, Excel, a BI import) "
                f"picks an order silently and is wrong half the world over."
                f"{mixed}"
            ),
            suggestion=(
                "Fix the order at load time: pandas dayfirst=True/False, and "
                "validate a sample of parsed dates against the source."
            ),
        )
    ]


def rule_whitespace_keys(stats: ColumnStats) -> List[Finding]:
    stripped: Dict[str, set] = defaultdict(set)
    edged: List[Tuple[str, int]] = []
    for v, n in stats.non_empty_values():
        s = v.strip()
        if s != v:
            edged.append((v, n))
        stripped[s].add(v)
    collisions = {s: raws for s, raws in stripped.items() if len(raws) > 1}
    findings: List[Finding] = []
    if collisions:
        count = sum(
            sum(stats.values[r] for r in raws) for raws in collisions.values()
        )
        example = [repr(v) for v in sorted(next(iter(collisions.values())))[:2]]
        findings.append(
            Finding(
                code="WHITESPACE_KEY_COLLISION",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=example,
                loaders=[DOWNSTREAM],
                message=(
                    f"Column '{stats.name}' has values that differ only by "
                    f"surrounding whitespace; any strip/clean step collapses "
                    f"them into one key, merging distinct records."
                ),
                suggestion=(
                    "Decide per column: strip on load (pandas converters / "
                    "polars str.strip_chars) and check which keys merge first."
                ),
            )
        )
    elif edged:
        count = sum(n for _, n in edged)
        findings.append(
            Finding(
                code="WHITESPACE_EDGED_VALUES",
                severity="low",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(repr(v) for v, _ in edged),
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"{count} value(s) in column '{stats.name}' have leading or "
                    f"trailing whitespace; as strings they fail joins and "
                    f"group-bys against clean data, and pandas trims them "
                    f"silently when the column parses as numeric."
                ),
                suggestion="Strip on load or before joining.",
            )
        )
    return findings


def rule_unicode_lookalikes(stats: ColumnStats) -> List[Finding]:
    sneaky: List[Tuple[str, int, str]] = []
    nfc_map: Dict[str, set] = defaultdict(set)
    for v, n in stats.non_empty_values():
        if v.isascii():
            continue
        found = [label for ch, label in _SNEAKY_CHARS.items() if ch in v]
        if found:
            sneaky.append((v, n, found[0]))
        nfc_map[unicodedata.normalize("NFC", v)].add(v)

    findings: List[Finding] = []
    collisions = {k: raws for k, raws in nfc_map.items() if len(raws) > 1}
    if collisions:
        count = sum(
            sum(stats.values[r] for r in raws) for raws in collisions.values()
        )
        example = [repr(v) for v in sorted(next(iter(collisions.values())))[:2]]
        findings.append(
            Finding(
                code="UNICODE_FORM_COLLISION",
                severity="high",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=example,
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"Column '{stats.name}' contains visually identical strings "
                    f"with different Unicode normal forms; they load as "
                    f"distinct keys, so group-bys and joins split or miss them."
                ),
                suggestion=(
                    "Normalize on load: unicodedata.normalize('NFC', value) in "
                    "a converter, before any grouping or joining."
                ),
            )
        )
    if sneaky:
        count = sum(n for _, n, _ in sneaky)
        findings.append(
            Finding(
                code="INVISIBLE_CHARACTERS",
                severity="medium",
                column=stats.name,
                column_index=stats.index,
                count=count,
                examples=_examples(f"{v!r} ({label})" for v, _, label in sneaky),
                loaders=[PANDAS.name, POLARS.name],
                message=(
                    f"{count} value(s) in column '{stats.name}' contain "
                    f"invisible characters (no-break space, zero-width space, "
                    f"soft hyphen). They print like normal text but break "
                    f"equality, joins, and parsers."
                ),
                suggestion="Replace or strip these characters on load.",
            )
        )
    return findings


def rule_footer_rows(stats: ColumnStats) -> List[Finding]:
    """Values like 'Total' inside an otherwise numeric column."""
    if not stats.row_count:
        return []
    footerish = [
        (v, n)
        for v, n in stats.non_empty_values()
        if re.match(r"^(total|subtotal|source|note|notes|generated)\b", v, re.I)
    ]
    if not footerish:
        return []
    # Only report when the column is mostly numeric: a notes column is fine.
    numeric = sum(n for v, n in stats.non_empty_values() if stats.numeric_parseable(v))
    total = sum(n for _, n in stats.non_empty_values())
    if total == 0 or numeric / total < 0.9:
        return []
    count = sum(n for _, n in footerish)
    return [
        Finding(
            code="FOOTER_ROWS",
            severity="medium",
            column=stats.name,
            column_index=stats.index,
            count=count,
            examples=_examples(v for v, _ in footerish),
            loaders=[PANDAS.name, POLARS.name],
            message=(
                f"{count} footer-like value(s) (e.g. 'Total') sit in numeric "
                f"column '{stats.name}'; the column loads as text, or the "
                f"footer row lands in the data."
            ),
            suggestion="Drop footer rows with skipfooter / row filtering.",
        )
    ]


COLUMN_RULES = (
    rule_leading_zero_loss,
    rule_na_collision,
    rule_int_precision_loss,
    rule_int_to_float_with_holes,
    rule_sci_notation,
    rule_bool_coercion,
    rule_thousands_separator,
    rule_currency_or_percent,
    rule_ambiguous_date,
    rule_whitespace_keys,
    rule_unicode_lookalikes,
    rule_footer_rows,
)


def run_column_rules(stats: ColumnStats) -> List[Finding]:
    findings: List[Finding] = []
    for rule in COLUMN_RULES:
        findings.extend(rule(stats))
    return findings
