"""A source rewrite must not look like a loader mutation."""
import json
from pathlib import Path

import pytest

from csvhonest.cli import main
from csvhonest.scan import scan_file
from csvhonest.verify import verify_with_pandas, verify_with_polars


@pytest.mark.parametrize("loader", ["pandas", "polars"])
@pytest.mark.parametrize("delete", [False, True])
def test_source_changes_after_loader(tmp_path, monkeypatch, loader, delete):
    module = pytest.importorskip(loader)
    source = tmp_path / "input.csv"
    original = "zip\n02139\n00501\n" if delete else "name,city\nAlice,Seattle\n"
    source.write_text(original, encoding="utf-8")
    result = scan_file(str(source))
    read = module.read_csv
    seen = []

    def read_then_change(*args, **kwargs):
        seen.append(Path(args[0]))
        frame = read(*args, **kwargs)
        if delete:
            source.unlink()
        else:
            source.write_text("name,city\nGrace,Chicago\n", encoding="utf-8")
        return frame

    monkeypatch.setattr(module, "read_csv", read_then_change)
    verify = verify_with_pandas if loader == "pandas" else verify_with_polars
    report = verify(result, max_rows=100)
    assert report.load_error is None
    assert report.total_mutated_rows == (2 if delete else 0)
    assert seen[0] != source
    assert not seen[0].exists()


def test_cli_scan_and_both_loaders_share_capture(tmp_path, monkeypatch, capsys):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("polars")
    source = tmp_path / "input.csv"
    source.write_text("zip\n02139\n00501\n", encoding="utf-8")
    import csvhonest.cli as cli
    scan = cli.scan_file
    read = pd.read_csv

    def scan_then_rewrite(path, **kwargs):
        result = scan(path, **kwargs)
        source.write_text("zip\n10000\n20000\n", encoding="utf-8")
        return result

    def read_then_delete(*args, **kwargs):
        frame = read(*args, **kwargs)
        source.unlink()
        return frame

    monkeypatch.setattr(cli, "scan_file", scan_then_rewrite)
    monkeypatch.setattr(pd, "read_csv", read_then_delete)
    assert main([str(source), "--verify", "both", "--format", "json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["file"] == str(source)
    for report in output["verification"]:
        assert report["load_error"] is None
        assert sum(c["mutated_rows"] for c in report["columns"]) == 2


@pytest.mark.parametrize("verify", [verify_with_pandas, verify_with_polars])
def test_capture_failure_is_not_success(tmp_path, verify):
    source = tmp_path / "gone.csv"
    source.write_text("a,b\nx,y\n", encoding="utf-8")
    result = scan_file(str(source))
    source.unlink()
    report = verify(result, 100)
    assert "input capture could not be completed" in report.load_error
    assert report.columns == []


def test_capture_cleanup_after_copy_failure(tmp_path, monkeypatch):
    from csvhonest import snapshot
    source = tmp_path / "source.csv"
    source.write_text("a\nx\n", encoding="utf-8")
    destinations = []

    def fail_copy(src, dst):
        destinations.append(Path(dst))
        Path(dst).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(snapshot.shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        with snapshot.captured_path(source):
            pytest.fail("partial capture must not be yielded")
    assert not destinations[0].parent.exists()


def test_static_only_does_not_capture(tmp_path, monkeypatch, capsys):
    from csvhonest import snapshot
    source = tmp_path / "source.csv"
    source.write_text("name,city\nAlice,Seattle\n", encoding="utf-8")

    def no_copy(*args):
        pytest.fail("static mode must not copy the full input")

    monkeypatch.setattr(snapshot.shutil, "copyfile", no_copy)
    assert main([str(source), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["file"] == str(source)
