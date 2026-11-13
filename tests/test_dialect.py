from csvhonest.dialect import detect_dialect, detect_encoding, decode


def test_utf8_plain():
    info = detect_encoding("a,b\n1,2\n".encode("utf-8"))
    assert info.encoding == "utf-8"
    assert not info.bom
    assert not info.fell_back


def test_utf8_bom():
    info = detect_encoding(b"\xef\xbb\xbfname\nAnish\n")
    assert info.encoding == "utf-8-sig"
    assert info.bom


def test_cp1252_fallback():
    raw = "name,city\nAnish,Zürich — café\n".encode("cp1252")
    info = detect_encoding(raw)
    assert info.fell_back
    assert info.encoding == "cp1252"
    assert "Zürich" in decode(raw, info)


def test_comma_dialect():
    d = detect_dialect("a,b,c\n1,2,3\n4,5,6\n")
    assert d.delimiter == ","


def test_semicolon_dialect():
    text = "a;b;c\n1;2;3\n4;5;6\n"
    d = detect_dialect(text)
    assert d.delimiter == ";"


def test_tab_dialect():
    text = "a\tb\tc\n1\t2\t3\n4\t5\t6\n"
    d = detect_dialect(text)
    assert d.delimiter == "\t"


def test_consistent_multicolumn_beats_sniffer_miss():
    # A file the sniffer cannot classify still gets the delimiter that
    # yields a consistent multi-column shape.
    text = "x|y\n" * 300
    d = detect_dialect(text)
    assert d.delimiter == "|"
