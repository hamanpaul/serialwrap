#!/usr/bin/env python3
"""產生 contracts/golden/pycompat.tsv、pypath.posix.tsv 與 pypath.windows.tsv（P3-T4 的 Python oracle）。

oracle 為 CPython 標準函式庫，基準版本 3.12.3（CI 釘版）。
產生器只使用標準函式庫，不得 import sw_core。輸出為確定性：固定 seed、固定 corpus、依序寫出。

用法::

    python3 scripts/rust/goldens/gen_pycompat.py --out-dir contracts/golden
    python3 scripts/rust/goldens/gen_pycompat.py --out contracts/golden/pycompat.tsv

Windows 檔原生產生方式（亦可透過 WSL 執行）：:

    cd /mnt/c && /mnt/c/Windows/py.exe -3.12 <wslpath -w gen_pycompat.py> --windows-only --out-dir <wslpath -w contracts/golden>
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import random
import sys
from pathlib import Path

# 將 scripts/rust/goldens 目錄加入 sys.path 以載入 _tsv.py
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from _tsv import Row, write_tsv

BASELINE = (3, 12, 3)
FUZZ_SEED = 20261016

# ---------------------------------------------------------------------------
# 1. pycompat.tsv 產生器
# ---------------------------------------------------------------------------

def gen_int_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    cases: list[str] = [
        " 5", "+5", "-5", "1_000", "٤٢", "3.5", "0x10", "_1", "1__0", "",
        "9" * 50, "-" + ("9" * 50),
        "0", "-0", "+0", "1", "-1", "42", "-42", " 1_2_3_4 ", "1_000_000",
        "1_", "+_1", "-_1", "007", "+007", "-007", " 000 ", "+000", "-000",
        "١٢٣", "۱۲۳", "１２３", "१२३", " ٤٢ ", "+٤٢", "-٤٢",
        "++1", "--1", "+-1", "-+1", "1 2", "1a", "a1", "1.0", ".5", "1.",
        "0b101", "0o77", "0xdead", "0X10", "None", "True", "False", "nan", "inf",
        "   ", "\t\n\r", "1\x00", "1\n", "\n1", " 1 ",
        str(2**63 - 1), str(-(2**63)), str(2**63), str(-(2**63) - 1),
        str(2**64 - 1), str(2**64), str(2**127 - 1), str(-(2**127)),
        str(2**127), str(-(2**127) - 1), str(2**128 - 1), str(2**128),
        "1" + "0" * 40, "-1" + "0" * 40,
    ]

    for _ in range(150):
        val = rng.randint(-(2**130), 2**130)
        s = str(val)
        fmt = rng.choice(["raw", "spaces", "underscores", "plus"])
        if fmt == "spaces":
            s = f"{' ' * rng.randint(1, 3)}{s}{' ' * rng.randint(1, 3)}"
        elif fmt == "underscores" and len(s) > 3 and not s.startswith("-"):
            idx = rng.randint(1, len(s) - 1)
            s = f"{s[:idx]}_{s[idx:]}"
        elif fmt == "plus" and not s.startswith("-"):
            s = f"+{s}"
        cases.append(s)

    seen = set()
    i = 0
    for s in cases:
        if s in seen:
            continue
        seen.add(s)
        try:
            exp = str(int(s))
        except ValueError:
            exp = "ERR"
        rows.append((f"int_{i:04d}", "int", s, exp))
        i += 1
    return rows


def gen_float_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    cases: list[str] = [
        "1e3", " inf ", "-Infinity", "nan", "1_0.5", "0x1p3", "1e400", "", ".",
        "0.0", "-0.0", "+0.0", "1.0", "-1.0", "3.141592653589793", "100.5", "1e-5", "1e+10", "-1e-10",
        ".5", "1.", "+.5", "-.5", "+1.", "-1.", " 1.5 ", " \t1e3\n ",
        "1_000.5_000", "1_0e1_0", "1_0.5_0e1_0", "_1.0", "1_.0", "1._0", "1.0_", "1_e1", "1e_1", "1e1_",
        "inf", "+inf", "-inf", "Infinity", "+Infinity", "-infinity", "INFINITY", "InFiNiTy",
        "nan", "+nan", "-nan", "NaN", "NAN", "nAn", "   nan   ",
        "-1e400", "1e-400", "-1e-400", "1e", "e1", "abc", "None", "True", "++1.0", "--1.0",
        "1e+308", "-1e+308", "1e-308", "5e-324", "1.7976931348623157e308",
    ]

    for _ in range(150):
        r = rng.random()
        if r < 0.3:
            val = rng.uniform(-1e15, 1e15)
        elif r < 0.6:
            val = rng.uniform(-1e-15, 1e-15)
        else:
            val = rng.choice([0.0, -0.0, 1.0, -1.0, 1.5, 31.5])
        s = repr(val)
        if rng.random() < 0.3:
            s = f" {s} "
        cases.append(s)

    seen = set()
    i = 0
    for s in cases:
        if s in seen:
            continue
        seen.add(s)
        try:
            exp = repr(float(s))
        except ValueError:
            exp = "ERR"
        rows.append((f"float_{i:04d}", "float", s, exp))
        i += 1
    return rows


def gen_repr_str_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    cases: list[str] = [
        "it's", 'say "x"', 'both\'"', "\x7f", "\u200b", "é", "\U0001f600",
        "", "'", '"', "\\", "\t", "\n", "\r", "\x00", "\x01", "\x1f", "'\"'\"",
        "hello\x00world", "newline\ntest", "tab\ttest", "quote'test", 'dquote"test',
        "backslash\\test", "cr\rtest", "mixed\r\n\t'\"\\",
        "Hello World", "12345", "   ", "a'b\"c\\d\ne\rf\tg",
    ]

    # 2000 個隨機 code point 字串（依計畫規範）
    ranges = [
        (0x20, 0x7E),      # ASCII printable
        (0x00, 0x1F),      # ASCII control
        (0x7F, 0x9F),      # Latin-1 control
        (0xA0, 0xFF),      # Latin-1 supplement
        (0x100, 0xD7FF),   # BMP
        (0xE000, 0xFFFF),  # Private use / BMP
        (0x10000, 0x10FFFF), # Supplementary
    ]
    quote_pool = ["'", '"', "\\", "\n", "\r", "\t", " ", "a", "é", "中", "😀"]

    for _ in range(2000):
        length = rng.randint(0, 15)
        chars = []
        for _ in range(length):
            if rng.random() < 0.2:
                chars.append(rng.choice(quote_pool))
            else:
                lo, hi = rng.choice(ranges)
                chars.append(chr(rng.randint(lo, hi)))
        cases.append("".join(chars))

    for i, s in enumerate(cases):
        rows.append((f"repr_{i:04d}", "repr_str", s, repr(s)))
    return rows


def gen_splitlines_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    seps = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
    cases: list[str] = [
        "", "single line", "\n", "\r\n", "\r", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
        "line1\nline2\n", "line1\r\nline2\r\n", "line1\rline2\r",
        "a\nb\r\nc\rd\x1ce\x1df\x1eg\x85h\u2028i\u2029j",
        "a\n\nb", "a\r\n\r\nb", "\n\n\n",
        "trailing newline\n", "trailing crlf\r\n", "trailing cr\r",
    ]

    words = ["foo", "bar", "baz", "qux", "hello", "world", "123", "", "   "]
    for _ in range(180):
        n = rng.randint(1, 5)
        parts = [rng.choice(words) for _ in range(n)]
        chosen_seps = [rng.choice(seps) for _ in range(n - 1)]
        res = []
        for j in range(n - 1):
            res.append(parts[j])
            res.append(chosen_seps[j])
        res.append(parts[-1])
        if rng.random() < 0.5:
            res.append(rng.choice(seps))
        cases.append("".join(res))

    for i, s in enumerate(cases):
        exp = json.dumps(s.splitlines(), ensure_ascii=False)
        rows.append((f"split_{i:04d}", "splitlines", s, exp))
    return rows


def gen_strip_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    spaces = [
        " ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f",
        "\x85", "\xa0", "\u1680", "\u2000", "\u2001", "\u2002", "\u2003", "\u2004",
        "\u2005", "\u2006", "\u2007", "\u2008", "\u2009", "\u200a", "\u2028", "\u2029",
        "\u202f", "\u205f", "\u3000"
    ]
    cases: list[str] = [
        "", "hello", "123", "   ", "\t\n\r\x0b\x0c",
        "  hello  ", "\t\nfoo\r\n", "\u3000bar\u3000", "\xa0test\xa0",
        "  a b c  ", "\u3000x\u3000y\u3000", "\x1c\x1d\x1e\x1ftest\x1f\x1e\x1d\x1c",
        "\x85start\x85", "\u2028middle\u2029", "no-spaces",
    ]

    words = ["word", "test", "data", "abc", "中文字", "foo bar", ""]
    for _ in range(190):
        pre = "".join(rng.choice(spaces) for _ in range(rng.randint(0, 4)))
        post = "".join(rng.choice(spaces) for _ in range(rng.randint(0, 4)))
        mid = rng.choice(words)
        cases.append(f"{pre}{mid}{post}")

    for i, s in enumerate(cases):
        rows.append((f"strip_{i:04d}", "strip", s, s.strip()))
    return rows


def gen_decode_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    cases: list[bytes] = [
        b"", b"hello", b"123", b"a b c", b"\xc3\xa9", b"\xe2\x82\xac", b"\xf0\x9f\x98\x80",
        b"\xff", b"\xfe", b"\xc0", b"\xc1", b"\xf5", b"\xf8",
        b"\x80", b"\xbf", b"a\x80b",
        b"\xc2", b"abc\xc2", b"\xe2", b"\xe2\x82", b"abc\xe2\x82",
        b"\xf0", b"\xf0\x9f", b"\xf0\x9f\x98",
        b"\xc2\x20", b"\xe2\x20\x80", b"\xe2\x82\x20", b"\xf0\x20\x80\x80",
        b"\xf0\x9f\x20\x80", b"\xf0\x9f\x98\x20",
        b"\xc0\xaf", b"\xc1\xbf", b"\xe0\x80\xaf", b"\xe0\x9f\xbf", b"\xf0\x80\x80\xaf",
        b"\xed\xa0\x80", b"\xed\xad\xbf", b"\xed\xae\x80", b"\xed\xbf\xbf",
        b"\xf4\x90\x80\x80", b"\xf7\xbf\xbf\xbf",
    ]

    # 300 個序列
    while len(cases) < 300:
        length = rng.randint(0, 16)
        b = bytearray()
        for _ in range(length):
            if rng.random() < 0.6:
                # valid ASCII or UTF-8 char
                cp = rng.choice([0x41, 0x61, 0x20, 0xe9, 0x20ac, 0x1f600])
                b.extend(chr(cp).encode("utf-8"))
            else:
                b.append(rng.randint(0, 255))
        cases.append(bytes(b))

    for i, b in enumerate(cases):
        hex_input = b.hex()
        # decode_replace
        exp_rep = b.decode("utf-8", "replace")
        rows.append((f"dec_{i:04d}", "decode_replace", hex_input, exp_rep))

        # decode_strict
        try:
            exp_strict = b.decode("utf-8")
        except UnicodeDecodeError as e:
            exp_strict = str(e)
        rows.append((f"dec_{i:04d}", "decode_strict", hex_input, exp_strict))
    return rows


def gen_oserror_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    cases: list[tuple[int | None, int | None, str | None, str | None, str | None]] = [
        # plain only
        (None, None, None, None, "plain error message"),
        (None, None, None, None, "Connection timed out"),
        (None, None, None, None, "Device not found"),
        # errno only
        (2, None, "No such file or directory", None, None),
        (13, None, "Permission denied", None, None),
        (22, None, "Invalid argument", None, None),
        # errno with filename
        (2, None, "No such file or directory", "foo.txt", None),
        (13, None, "Permission denied", "/var/log/serialwrap.log", None),
        (22, None, "Invalid argument", "COM1", None),
        (17, None, "File exists", "already'there\".txt", None),
        # winerror only
        (22, 10048, "Address already in use", None, None),
        (None, 5, "Access is denied", None, None),
        (None, 2, "The system cannot find the file specified", None, None),
        # winerror with filename
        (22, 10048, "Address already in use", "127.0.0.1:48700", None),
        (None, 5, "Access is denied", r"C:\Windows\System32\drivers", None),
        (None, 2, "The system cannot find the file specified", r"C:\serialwrap\config.yaml", None),
    ]

    errnos = [1, 2, 9, 11, 13, 17, 22, 32, 98, 104, 110, 111]
    winerrors = [2, 3, 5, 32, 10048, 10054, 10060, 10061]
    strerrors = [
        "Operation not permitted", "No such file or directory", "Permission denied",
        "File exists", "Invalid argument", "Broken pipe", "Connection refused",
        "Connection reset by peer", "Timed out",
    ]
    filenames = [
        "file.txt", "path/to/target", r"C:\Users\golden\app.log",
        "file with 'single' quote", 'file with "double" quote',
        "both'and\"quotes.dat", "special\tname.txt",
    ]

    for _ in range(85):
        kind = rng.choice(["plain", "errno", "errno_fn", "win", "win_fn"])
        if kind == "plain":
            cases.append((None, None, None, None, f"custom plain error {rng.randint(1, 1000)}"))
        elif kind == "errno":
            cases.append((rng.choice(errnos), None, rng.choice(strerrors), None, None))
        elif kind == "errno_fn":
            cases.append((rng.choice(errnos), None, rng.choice(strerrors), rng.choice(filenames), None))
        elif kind == "win":
            cases.append((rng.choice(errnos), rng.choice(winerrors), rng.choice(strerrors), None, None))
        else:
            cases.append((rng.choice(errnos), rng.choice(winerrors), rng.choice(strerrors), rng.choice(filenames), None))

    for i, (errno, winerror, strerror, filename, plain) in enumerate(cases):
        case_input = "\x1f".join([
            str(errno) if errno is not None else "",
            str(winerror) if winerror is not None else "",
            strerror if strerror is not None else "",
            filename if filename is not None else "",
            plain if plain is not None else "",
        ])
        if plain is not None:
            exp = plain
        elif winerror is not None:
            exp = f"[WinError {winerror}] {strerror}"
            if filename is not None:
                exp += f": {repr(filename)}"
        elif errno is not None:
            exp = f"[Errno {errno}] {strerror}"
            if filename is not None:
                exp += f": {repr(filename)}"
        else:
            exp = ""
        rows.append((f"oserr_{i:04d}", "oserror", case_input, exp))
    return rows


def gen_py_val_rows(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    values: list = [
        None, True, False, 0, -0, 1, -1, 42, -42, 1000, -9999,
        2**63 - 1, -(2**63), 2**100, -(2**100),
        0.0, -0.0, 1.5, -1.5, 3.14159, 1e16, -1e-5,
        "", "hello", "it's", 'say "x"', "both'\"", "\t\n\r", "é", "😀",
        [], [1], [1, 2, 3], ["a", "b"], [True, False, None],
        [1, "text", True, None, 3.5], [[1, 2], [3, 4]],
        {}, {"a": 1}, {"b": 2, "a": 1}, {"key": "value", "flag": True, "count": 10},
        {"nested": {"x": 1, "y": 2}, "list": [1, 2, 3]},
    ]

    for _ in range(160):
        kind = rng.choice(["int", "float", "str", "list", "dict"])
        if kind == "int":
            val = rng.randint(-(2**70), 2**70)
        elif kind == "float":
            val = rng.uniform(-1000.0, 1000.0)
        elif kind == "str":
            chars = [rng.choice(["a", "b", "'", '"', "\\", " ", "é", "1"]) for _ in range(rng.randint(0, 8))]
            val = "".join(chars)
        elif kind == "list":
            val = [rng.randint(0, 10) for _ in range(rng.randint(0, 4))]
        else:
            val = {f"k{j}": rng.randint(0, 10) for j in range(rng.randint(0, 3))}
        values.append(val)

    for i, v in enumerate(values):
        s = json.dumps(v, ensure_ascii=False)
        rows.append((f"val_{i:04d}", "py_str", s, str(v)))
        rows.append((f"val_{i:04d}", "py_repr", s, repr(v)))
    return rows


def build_pycompat_tsv(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    rows.extend(gen_int_rows(rng))
    rows.extend(gen_float_rows(rng))
    rows.extend(gen_repr_str_rows(rng))
    rows.extend(gen_splitlines_rows(rng))
    rows.extend(gen_strip_rows(rng))
    rows.extend(gen_decode_rows(rng))
    rows.extend(gen_oserror_rows(rng))
    rows.extend(gen_py_val_rows(rng))
    return rows


# ---------------------------------------------------------------------------
# 2. pypath.posix.tsv 與 pypath.windows.tsv 產生器
# ---------------------------------------------------------------------------

def gen_pypath_corpus(flavor: str, rng: random.Random) -> dict[str, list]:
    is_win = (flavor == "windows")
    sep = "\\" if is_win else "/"
    altsep = "/" if is_win else None

    # 1. normpath
    normpath_cases = [
        "", ".", "..", "/", "//", "///",
        "a", "a/b", "a/b/", "a/./b", "a/../b", "a/b/.", "a/b/..",
        "a//b", "a///b", "../../a", "a/../../b", "/../../b",
        "foo/bar/baz", "./foo", "../foo", "foo/.", "foo/..",
    ]
    if is_win:
        win_norm = [
            r"C:\foo\bar", r"C:\foo\..\bar", r"C:\foo\.\bar", "C:\\", "C:",
            "C:foo", r"C:foo\..\bar", r"\\server\share\file", r"\\server\share",
            r"\\server\share\..\other", r"\\?\C:\foo\bar", "c:/foo/bar",
            r"C:\foo\\bar", r"\foo\bar", r"\..\foo", "foo\\bar\\", "foo\\bar\\.",
        ]
        normpath_cases.extend(win_norm)

    while len(normpath_cases) < 200:
        depth = rng.randint(1, 5)
        parts = []
        for _ in range(depth):
            parts.append(rng.choice([".", "..", "a", "b", "c", "foo", "bar", ""]))
        p = sep.join(parts)
        if rng.random() < 0.3:
            p = sep + p
        if is_win and rng.random() < 0.3:
            p = f"C:{sep}{p.lstrip(sep)}"
        normpath_cases.append(p)

    # 2. join
    join_cases: list[list[str]] = [
        [""], ["a"], ["a", "b"], ["a", "b", "c"],
        ["/a", "b"], ["a", "/b"], ["/a", "/b"],
        ["a/", "b"], ["a", ""], ["", "b"], ["", ""],
        ["a", "b/c", "d"], [".", "a"], ["..", "a"],
        ["//a", "b"], ["a//", "b"],
    ]
    if is_win:
        win_join = [
            [r"C:\foo", "bar"], [r"C:", "bar"], ["foo", r"D:\bar"],
            [r"C:\foo", r"\bar"], [r"\\server\share", "file"],
            [r"C:\foo", r"D:bar"], ["foo", r"C:\bar\baz"],
        ]
        join_cases.extend(win_join)

    while len(join_cases) < 200:
        n = rng.randint(1, 4)
        c = []
        for _ in range(n):
            part = rng.choice(["", "a", "b", "foo", "bar", "dir/sub", "dir\\sub"])
            if rng.random() < 0.2:
                part = f"{sep}{part}"
            if is_win and rng.random() < 0.2:
                part = f"{rng.choice(['C:', 'D:'])}{part}"
            c.append(part)
        join_cases.append(c)

    # 3. isabs
    isabs_cases = [
        "", "/", "//", "///", "/a", "/a/b", "a", "a/b", "./a", "../a", ".", "..",
        "~", "~/foo",
    ]
    if is_win:
        win_isabs = [
            "C:\\", r"C:\foo", "C:foo", r"\foo", r"\\server\share", r"\\server\share\file",
            r"foo\bar", "\\", "C:/foo", "C:", r"\\?\C:\foo",
        ]
        isabs_cases.extend(win_isabs)

    while len(isabs_cases) < 200:
        p = sep.join([rng.choice(["a", "b", "c", "dir"]) for _ in range(rng.randint(1, 3))])
        if rng.random() < 0.4:
            p = sep + p
        if is_win and rng.random() < 0.4:
            p = f"{rng.choice(['C:', 'D:'])}{sep}{p.lstrip(sep)}"
        isabs_cases.append(p)

    # 4. dirname
    dirname_cases = [
        "", "/", "//", "///", "/a", "/a/", "/a/b", "/a/b/",
        "a", "a/", "a/b", "a/b/", ".", "..", "./a", "../a",
        "a//b", "a///b", "foo/bar/baz.txt",
    ]
    if is_win:
        win_dir = [
            r"C:\foo\bar", r"C:\foo", "C:\\", "C:", "C:foo",
            r"\\server\share\file", r"\\server\share", r"\foo\bar", r"\foo",
        ]
        dirname_cases.extend(win_dir)

    while len(dirname_cases) < 200:
        depth = rng.randint(1, 4)
        p = sep.join([rng.choice(["dir", "sub", "file.txt", ""]) for _ in range(depth)])
        if rng.random() < 0.4:
            p = sep + p
        if is_win and rng.random() < 0.3:
            p = f"C:{sep}{p.lstrip(sep)}"
        dirname_cases.append(p)

    # 5. basename
    basename_cases = [
        "", "/", "//", "///", "/a", "/a/", "/a/b", "/a/b/",
        "a", "a/", "a/b", "a/b/", ".", "..", "./a", "../a",
        "a//b", "a///b", "foo/bar/baz.txt",
    ]
    if is_win:
        win_base = [
            r"C:\foo\bar", r"C:\foo", "C:\\", "C:", "C:foo",
            r"\\server\share\file", r"\\server\share", r"\foo\bar", r"\foo",
        ]
        basename_cases.extend(win_base)

    while len(basename_cases) < 200:
        depth = rng.randint(1, 4)
        p = sep.join([rng.choice(["dir", "sub", "file.txt", ""]) for _ in range(depth)])
        if rng.random() < 0.4:
            p = sep + p
        if is_win and rng.random() < 0.3:
            p = f"C:{sep}{p.lstrip(sep)}"
        basename_cases.append(p)

    # 6. splitext
    splitext_cases = [
        "", "a", "a.txt", "a.b.txt", ".profile", ".tar.gz",
        "/path/to/a.txt", "/path/to/.bashrc", "/path.with.dots/file",
        "/path.with.dots/file.ext", "dir/", "dir/.", "dir/..",
        "a.", "a..", ".a.b", "/.a",
    ]
    if is_win:
        win_split = [
            r"C:\path\file.ext", r"C:\path\.gitignore", r"C:\path\dir.with.dots\file",
            r"\\server\share\file.log", "file.", "file..ext",
        ]
        splitext_cases.extend(win_split)

    while len(splitext_cases) < 200:
        stem = rng.choice(["file", "test", ".hidden", "data", "archive.tar"])
        ext = rng.choice(["", ".txt", ".log", ".json", ".tar.gz", ".", ".."])
        parent = sep.join([rng.choice(["path", "sub", "dir.name"]) for _ in range(rng.randint(0, 2))])
        p = f"{parent}{sep}{stem}{ext}" if parent else f"{stem}{ext}"
        splitext_cases.append(p)

    # 7. expanduser
    expanduser_cases = [
        "~", "~/", "~/foo", "~/foo/bar", "~/foo/bar/",
        "~bob", "~bob/", "~bob/x", "~bob/x/y",
        "~alice", "~alice/", "~alice/foo",
        "~unknown", "~unknown/", "~unknown/x",
        "", "/", "/abs/path", "relative/path", ".", "..",
        "~/a/b/c.txt", "~bob/log.txt",
    ]
    if is_win:
        win_exp = [
            "~", "~\\", r"~\foo", r"~\foo\bar", "~/foo",
            "~bob", "~bob\\", r"~bob\x", "~bob/x",
            "~alice", "~alice\\", r"~alice\foo",
            r"C:\path\file", r"\\server\share\file",
        ]
        expanduser_cases.extend(win_exp)

    while len(expanduser_cases) < 200:
        base = rng.choice(["~", "~bob", "~alice", "~/sub", "~bob/sub", "other"])
        suffix = sep.join([rng.choice(["a", "b", "c"]) for _ in range(rng.randint(0, 2))])
        p = f"{base}{sep}{suffix}" if suffix else base
        expanduser_cases.append(p)

    return {
        "normpath": normpath_cases[:200],
        "join": join_cases[:200],
        "isabs": isabs_cases[:200],
        "dirname": dirname_cases[:200],
        "basename": basename_cases[:200],
        "splitext": splitext_cases[:200],
        "expanduser": expanduser_cases[:200],
    }


def build_pypath_posix_tsv(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    corpus = gen_pypath_corpus("posix", rng)

    # FakeHome 對照：current_user -> /golden/pw-home, bob -> /golden/bob-home
    def posix_expanduser_golden(p: str) -> str:
        if not p.startswith("~"):
            return p
        sep_idx = p.find("/")
        if sep_idx < 0:
            user = p[1:]
            rest = ""
        else:
            user = p[1:sep_idx]
            rest = p[sep_idx:]
        if not user:
            # "~" or "~/" -> 使用 HOME=/golden/home
            home = "/golden/home"
        elif user == "bob":
            home = "/golden/bob-home"
        else:
            # 查無此 user，原樣回傳
            return p
        home = home.rstrip("/")
        return (home + rest) or "/"

    for op, cases in corpus.items():
        for i, c in enumerate(cases):
            case_id = f"{op}_{i:04d}"
            if op == "join":
                s = "\x1f".join(c)
                exp = posixpath.join(*c)
            elif op == "normpath":
                s = c
                exp = posixpath.normpath(c)
            elif op == "isabs":
                s = c
                exp = "True" if posixpath.isabs(c) else "False"
            elif op == "dirname":
                s = c
                exp = posixpath.dirname(c)
            elif op == "basename":
                s = c
                exp = posixpath.basename(c)
            elif op == "splitext":
                s = c
                r, ext = posixpath.splitext(c)
                exp = f"{r}\x1f{ext}"
            elif op == "expanduser":
                s = c
                exp = posix_expanduser_golden(c)
            else:
                raise ValueError(f"unknown op {op}")
            rows.append((case_id, op, s, exp))
    return rows


def build_pypath_windows_tsv(rng: random.Random) -> list[Row]:
    rows: list[Row] = []
    corpus = gen_pypath_corpus("windows", rng)

    # Windows expanduser：USERPROFILE=C:\Users\golden, USERNAME=golden
    def win_expanduser_golden(p: str) -> str:
        if not p.startswith("~"):
            return p
        i = 1
        n = len(p)
        while i < n and p[i] not in ("\\", "/"):
            i += 1
        userhome = r"C:\Users\golden"
        if i != 1:  # ~user
            target_user = p[1:i]
            current_user = "golden"
            if target_user != current_user:
                if current_user != ntpath.basename(userhome):
                    return p
                userhome = ntpath.join(ntpath.dirname(userhome), target_user)
        return userhome + p[i:]

    for op, cases in corpus.items():
        for i, c in enumerate(cases):
            case_id = f"{op}_{i:04d}"
            if op == "join":
                s = "\x1f".join(c)
                exp = ntpath.join(*c)
            elif op == "normpath":
                s = c
                exp = ntpath.normpath(c)
            elif op == "isabs":
                s = c
                exp = "True" if ntpath.isabs(c) else "False"
            elif op == "dirname":
                s = c
                exp = ntpath.dirname(c)
            elif op == "basename":
                s = c
                exp = ntpath.basename(c)
            elif op == "splitext":
                s = c
                r, ext = ntpath.splitext(c)
                exp = f"{r}\x1f{ext}"
            elif op == "expanduser":
                s = c
                exp = win_expanduser_golden(c)
            else:
                raise ValueError(f"unknown op {op}")
            rows.append((case_id, op, s, exp))
    return rows


# ---------------------------------------------------------------------------
# 主程式入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, help="輸出目錄（同時產生三個 tsv）")
    parser.add_argument("--out", type=Path, help="單一輸出檔名（依檔名判定產生項目）")
    parser.add_argument("--windows-only", action="store_true", help="只產生 pypath.windows.tsv")
    args = parser.parse_args()

    if not args.out_dir and not args.out:
        parser.error("必須指定 --out-dir 或 --out")

    rng_compat = random.Random(FUZZ_SEED)
    rng_posix = random.Random(FUZZ_SEED + 1)
    rng_win = random.Random(FUZZ_SEED + 2)

    if args.out:
        target = args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        name = target.name
        if name == "pycompat.tsv":
            write_tsv(target, build_pycompat_tsv(rng_compat))
        elif name == "pypath.posix.tsv":
            write_tsv(target, build_pypath_posix_tsv(rng_posix))
        elif name == "pypath.windows.tsv":
            write_tsv(target, build_pypath_windows_tsv(rng_win))
        else:
            raise ValueError(f"無法從檔名判定產生目標：{name}")
        print(f"產生 {target} 完成")
        return

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.windows_only:
        p_win = out_dir / "pypath.windows.tsv"
        write_tsv(p_win, build_pypath_windows_tsv(rng_win))
        print(f"產生 {p_win} 完成")
        return

    p_compat = out_dir / "pycompat.tsv"
    write_tsv(p_compat, build_pycompat_tsv(rng_compat))
    print(f"產生 {p_compat} 完成")

    p_posix = out_dir / "pypath.posix.tsv"
    write_tsv(p_posix, build_pypath_posix_tsv(rng_posix))
    print(f"產生 {p_posix} 完成")

    p_win = out_dir / "pypath.windows.tsv"
    write_tsv(p_win, build_pypath_windows_tsv(rng_win))
    print(f"產生 {p_win} 完成")


if __name__ == "__main__":
    main()
