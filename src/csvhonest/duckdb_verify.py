"""Verify DuckDB defaults for a conservative CSV subset.

Not a static profile. No import, install or database connection until requested.
The existing 'both' selection deliberately remains pandas plus Polars.
"""
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile

from .dialect import DialectInfo
from .verify import VerificationReport, _verify_generic


def verify_with_duckdb(result, max_rows):
    try:
        import duckdb
    except ImportError:
        return VerificationReport('duckdb', 0, 0, load_error='duckdb is not installed')
    if result.encoding.encoding not in ('utf-8', 'utf-8-sig'):
        return VerificationReport(
            'duckdb', 0, 0,
            load_error='DuckDB verification supports UTF-8 CSV only; no transcoding performed',
        )
    path = str(result.path)
    try:
        # Do not pass the source name to DuckDB: its file arguments accept
        # glob patterns. A fixed name inside a private directory is literal.
        with tempfile.TemporaryDirectory(prefix='csvhonest-duckdb-') as directory:
            path = str(Path(directory) / 'input.csv')
            shutil.copyfile(result.path, path)
            with duckdb.connect(':memory:') as connection:
                cursor = connection.execute('SELECT * FROM sniff_csv(?)', [path])
                options = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))
                quote = options['Quote']
                escape = options['Escape']
                if (
                    not options['HasHeader']
                    or options['SkipRows'] != 0
                    or options['Comment'] not in ('', '(empty)')
                    or len(options['Delimiter']) != 1
                    or quote not in ('', '(empty)', '"', "'")
                    or escape not in ('', '(empty)', quote)
                ):
                    return VerificationReport(
                        'duckdb', 0, 0,
                        load_error='DuckDB detected a layout csvhonest cannot align safely',
                    )
                relation = connection.read_csv(path).limit(max_rows)
                names = relation.columns
                types = [str(t) for t in relation.types]
                rows = relation.fetchall()
                columns = [
                    (name, dtype, [row[i] for row in rows])
                    for i, (name, dtype) in enumerate(zip(names, types))
                ]
            dialect = DialectInfo(
                options['Delimiter'],
                None if quote in ('', '(empty)') else quote,
                True,
            )
            report = _verify_generic(
                replace(result, path=path, dialect=dialect),
                'duckdb', columns, lambda value: value is None, max_rows,
                skips_blank_lines='empty' if len(columns) > 1 else False,
            )
            if report.load_error:
                report.load_error = report.load_error.replace(path, str(result.path))
            return report
    except Exception as exc:
        return VerificationReport('duckdb', 0, 0, load_error=str(exc).replace(path, str(result.path)))
