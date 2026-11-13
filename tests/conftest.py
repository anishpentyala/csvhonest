import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def write_csv(tmp_path, text, name="data.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)
