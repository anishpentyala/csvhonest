"""csvhonest: audit what your CSV loader will silently change before you load it.

csvhonest scans raw CSV text and reports the values that default loader
settings (pandas.read_csv, polars.read_csv) would silently mutate: leading
zeros dropped from IDs, strings like "NA" turned into NaN, large integers
losing exactness under float64 coercion, ambiguous dates, and lookalike
keys that collapse when stripped or Unicode-normalized.
"""

from .scan import scan_file, ScanResult
from .report import render_text, render_json

__version__ = "0.1.0"

__all__ = ["scan_file", "ScanResult", "render_text", "render_json", "__version__"]
