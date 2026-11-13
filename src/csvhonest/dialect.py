"""Encoding and dialect detection for raw CSV bytes."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class EncodingInfo:
    encoding: str
    bom: bool
    fell_back: bool  # True when utf-8 failed and a legacy codec was used
    decode_errors: int  # bytes that did not decode cleanly under `encoding`


def detect_encoding(raw: bytes) -> EncodingInfo:
    """Pick an encoding for raw bytes, preferring exact UTF-8.

    Falls back to cp1252 (the common Windows/Excel export encoding) when
    UTF-8 fails, and records how many bytes still failed to decode.
    """
    bom = False
    data = raw
    if raw.startswith(b"\xef\xbb\xbf"):
        bom = True
        data = raw[3:]
        try:
            data.decode("utf-8", errors="strict")
            return EncodingInfo("utf-8-sig", bom=True, fell_back=False, decode_errors=0)
        except UnicodeDecodeError:
            pass
    else:
        try:
            data.decode("utf-8", errors="strict")
            return EncodingInfo("utf-8", bom=False, fell_back=False, decode_errors=0)
        except UnicodeDecodeError:
            pass

    errors = 0
    try:
        data.decode("cp1252", errors="strict")
    except UnicodeDecodeError:
        # cp1252 has a few undefined bytes; count them and replace.
        decoded = data.decode("cp1252", errors="replace")
        errors = decoded.count("�")
    return EncodingInfo("cp1252", bom=bom, fell_back=True, decode_errors=errors)


@dataclass(frozen=True)
class DialectInfo:
    delimiter: str
    quotechar: str
    sniffer_agreed: bool  # False when we fell back to the comma default


_CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]


def _column_count_spread(text: str, delimiter: str) -> Tuple[int, int]:
    """Return (min, max) field counts per line for a candidate delimiter."""
    counts: List[int] = []
    for line in text.splitlines()[:200]:
        if not line.strip():
            continue
        counts.append(line.count(delimiter) + 1)
    if not counts:
        return (1, 1)
    return (min(counts), max(counts))


def detect_dialect(text: str) -> DialectInfo:
    """Detect the delimiter and quote character of a text sample.

    Uses csv.Sniffer first; when the sniffer fails or picks a delimiter
    that produces a single column while another candidate splits the file
    into a consistent multi-column shape, the consistent candidate wins.
    """
    sample = text[:65536]
    sniffer = csv.Sniffer()
    sniffed: Optional[csv.Dialect] = None
    try:
        sniffed = sniffer.sniff(sample, delimiters="".join(_CANDIDATE_DELIMITERS))
    except csv.Error:
        sniffed = None

    best_delim = ","
    best_score = None
    for delim in _CANDIDATE_DELIMITERS:
        lo, hi = _column_count_spread(sample, delim)
        if hi <= 1:
            continue
        # Prefer many, consistent columns.
        score = (hi - lo == 0, hi)
        if best_score is None or score > best_score:
            best_score = score
            best_delim = delim

    if sniffed is not None:
        lo, hi = _column_count_spread(sample, sniffed.delimiter)
        if hi > 1:
            return DialectInfo(
                delimiter=sniffed.delimiter,
                quotechar=sniffed.quotechar or '"',
                sniffer_agreed=True,
            )

    # A single-column file has nothing to disagree about; only flag a
    # guessed dialect when a real multi-column decision was made.
    return DialectInfo(
        delimiter=best_delim, quotechar='"', sniffer_agreed=best_score is None
    )


def decode(raw: bytes, info: EncodingInfo) -> str:
    """Decode raw bytes according to a detected encoding."""
    errors = "strict" if info.decode_errors == 0 else "replace"
    return raw.decode(info.encoding, errors=errors)


def sniff_has_header(first_rows: List[List[str]]) -> bool:
    """Heuristic header check: first row all non-numeric, later rows not."""
    if len(first_rows) < 2:
        return True
    first = first_rows[0]
    rest = first_rows[1:]

    def numericish(v: str) -> bool:
        v = v.strip()
        if not v:
            return False
        try:
            float(v)
            return True
        except ValueError:
            return False

    first_numeric = sum(1 for v in first if numericish(v))
    rest_numeric = sum(1 for r in rest for v in r if numericish(v))
    if first and first_numeric == 0:
        return True
    if first and rest and first_numeric < rest_numeric / max(len(rest), 1):
        return True
    return first_numeric == 0


def text_stream(text: str) -> io.StringIO:
    return io.StringIO(text)
