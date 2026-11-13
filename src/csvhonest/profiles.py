"""Loader profiles: what each CSV loader does by default.

A profile records the defaults that decide whether a raw string survives a
load unchanged. Findings are attributed to the profiles whose defaults
trigger them, so the report can say "pandas changes this value; polars
keeps it" instead of guessing at a generic parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

# pandas STR_NA_VALUES: strings read_csv converts to NaN by default
# (keep_default_na=True). Source: pandas/io/parsers/readers.py.
PANDAS_DEFAULT_NA: FrozenSet[str] = frozenset(
    {
        "",
        "#N/A",
        "#N/A N/A",
        "#NA",
        "-1.#IND",
        "-1.#QNAN",
        "-NaN",
        "-nan",
        "1.#IND",
        "1.#QNAN",
        "<NA>",
        "N/A",
        "NA",
        "NULL",
        "NaN",
        "None",
        "n/a",
        "nan",
        "null",
    }
)

# Strings pandas converts to bool when they make up an entire column.
PANDAS_BOOL_STRINGS: FrozenSet[str] = frozenset({"True", "False"})

# Strings polars converts to bool when they make up an entire column
# (verified against polars 2.0: both cases are accepted).
POLARS_BOOL_STRINGS: FrozenSet[str] = frozenset({"true", "false", "True", "False"})


@dataclass(frozen=True)
class LoaderProfile:
    """Defaults of one CSV loader."""

    name: str
    # Exact strings converted to a missing-value marker by default.
    na_values: FrozenSet[str] = field(default_factory=frozenset)
    # Exact strings converted to booleans when a column is all bool-like.
    bool_strings: FrozenSet[str] = field(default_factory=frozenset)
    # Whether an all-integer column with missing values coerces to float64
    # (and can therefore lose exactness above 2**53).
    int_with_holes_becomes_float: bool = False
    # Whether a digit string with leading zeros is parsed as a number
    # (dropping the zeros) when the column is otherwise numeric.
    drops_leading_zeros: bool = True


PANDAS = LoaderProfile(
    name="pandas",
    na_values=PANDAS_DEFAULT_NA,
    bool_strings=PANDAS_BOOL_STRINGS,
    int_with_holes_becomes_float=True,
    drops_leading_zeros=True,
)

# polars read_csv defaults: no NA string list (null_values=None); an empty
# string in a column inferred as numeric becomes null; boolean inference
# accepts "true"/"false"; integers with nulls stay Int64 (exact).
POLARS = LoaderProfile(
    name="polars",
    na_values=frozenset({""}),
    bool_strings=POLARS_BOOL_STRINGS,
    int_with_holes_becomes_float=False,
    drops_leading_zeros=True,
)

PROFILES = {p.name: p for p in (PANDAS, POLARS)}

# Tag for findings that do not fire on a default load but make later
# parsing risky (e.g. dates whose day/month order cannot be known).
DOWNSTREAM = "downstream-parse"
