#!/usr/bin/env python3
"""平台 cfg 邊界檢查（arch §2.1、計畫 Task 1）。純 stdlib（Cargo.toml 解析用 Python 3.11 以上的 tomllib）。

掃描 `crates/*`（排除 `crates/sw-pal/`）中以下違規寫法：

- ``cfg-predicate``：``#[cfg(``、``#![cfg(``、``#[cfg_attr(``、``cfg!`` 巨集（``()``、``[]``、``{}`` 任一定界符）
  的謂詞不是由 ``test``、``feature = "test-fakes"`` 與 ``all``／``any``／``not`` 組成；``cfg_attr`` 之後的屬性
  與其中的巢狀 ``cfg``／``cfg_attr`` 同樣逐一檢查。``#`` 與 ``[`` 之間的合法空白一律視為屬性開頭。
- ``os-module``：``std::os::unix``、``std::os::windows``（含 ``use std::{…, os::unix::…}`` 群組寫法）。
- ``os-name``：``std::env::consts::OS``、``FAMILY``（執行期平台名稱比較，arch §2.1 禁止）。
- ``ffi-crate``：``libc::``、``windows_sys::``。
- ``build-script``：``crates/*/build.rs`` 存在；``Cargo.toml`` 的 ``[package] build``、任何 ``build-dependencies``
  （頂層或 ``[target.*]`` 表內）。
- ``target-section``：``crates/*/Cargo.toml`` 含任何 ``[target.…]`` 區段。
- ``test-data-path``：``crates/*/tests/**/*.rs`` 中 ``CARGO_MANIFEST_DIR`` 只允許出現在 ``tests/common/mod.rs``。

輸出每行 ``<relpath>:<line>: <rule>: <text>``；乾淨 exit 0，違規 exit 1，工具錯誤（``crates/`` 不存在、
未掃描到任何檔案、Cargo.toml 無法解析）exit 2 並於 stderr 說明。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10：無 tomllib；check_cargo_toml 會以工具錯誤結束（fail-closed）
    tomllib = None

PAL_CRATE = "sw-pal"
ALLOWED_FEATURE = "test-fakes"
SCAN_DIRS = ("src", "tests", "benches", "examples")

RAW_START_RE = re.compile(r'[bc]?r(#*)"')
CHAR_LIT_RE = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}|.)|[^\\'\n])'")
CFG_ATTR_RE = re.compile(r"#\s*!?\s*\[\s*(cfg|cfg_attr)\s*\(")
CFG_CALL_RE = re.compile(r"\bcfg(_attr)?\s*\(")
CFG_MACRO_RE = re.compile(r"\bcfg\s*!\s*(?P<open>[(\[{])")
OS_MODULE_RE = re.compile(r"\bos\s*::\s*(?:\{[^;]*?\b)?(?:unix|windows)\b")
OS_NAME_RE = re.compile(r"\bconsts\s*::\s*(?:\{[^;]*?\b)?(?:OS|FAMILY)\b")
FFI_RE = re.compile(r"\b(libc|windows_sys)\s*::|\bextern\s+crate\s+(libc|windows_sys)\b")
CARGO_MANIFEST_DIR_RE = re.compile(r"\bCARGO_MANIFEST_DIR\b")
TOKEN_RE = re.compile(r"\s*(?:(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<str>\"\x01\d+\x02\")|(?P<p>[(),=]))")
CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def lex(src: str) -> tuple[str, str, list[str]]:
    """回傳 ``(nocomment, code, strings)``。

    - ``nocomment``：行／區塊註解以空白取代（換行保留），字串常值原樣保留（供 test-data-path 使用）。
    - ``code``：在 ``nocomment`` 基礎上，字串內容換成 ``\\x01<k>\\x02`` 佔位（k 為 ``strings`` 索引、
      換行數保留），字元常值換成 ``'_'``（供 cfg／os／ffi 比對，避免字串內容誤判）。
    - ``strings``：各字串常值的內容（不含引號）。字首 ``b``、``c``、``br``、``cr`` 皆視為字串常值。
    """
    nc: list[str] = []
    cd: list[str] = []
    strings: list[str] = []
    n = len(src)

    def emit_string(raw_seg: str, content: str) -> None:
        k = len(strings)
        strings.append(content)
        nc.append(raw_seg)
        cd.append('"\x01%d\x02"' % k + "\n" * raw_seg.count("\n"))

    i = 0
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            blank = " " * (j - i)
            nc.append(blank)
            cd.append(blank)
            i = j
            continue
        if src.startswith("/*", i):
            depth = 0
            j = i
            while j < n:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            blank = "".join("\n" if ch == "\n" else " " for ch in src[i:j])
            nc.append(blank)
            cd.append(blank)
            i = j
            continue
        prev_ident = i > 0 and _is_ident_char(src[i - 1])
        if not prev_ident:
            m = RAW_START_RE.match(src, i)
            if m:
                close = '"' + m.group(1)
                end = src.find(close, m.end())
                if end == -1:
                    end = n
                    content = src[m.end():]
                    seg_end = n
                else:
                    content = src[m.end():end]
                    seg_end = end + len(close)
                emit_string(src[i:seg_end], content)
                i = seg_end
                continue
            if c == '"' or (c in ("b", "c") and src.startswith(c + '"', i)):
                start = i
                i += 2 if c in ("b", "c") else 1
                cstart = i
                while i < n and src[i] != '"':
                    i += 2 if src[i] == "\\" else 1
                content = src[cstart:i]
                i += 1
                emit_string(src[start:i], content)
                continue
        if c == "'":
            m = CHAR_LIT_RE.match(src, i)
            if m:
                nc.append(m.group(0))
                cd.append("'_'")
                i = m.end()
                continue
        nc.append(c)
        cd.append(c)
        i += 1
    return "".join(nc), "".join(cd), strings


def _matching_close(code: str, open_idx: int) -> int:
    """回傳與 ``code[open_idx]`` 配對的閉合定界符位置（支援 ``()``、``[]``、``{}``）；找不到回 -1。"""
    stack: list[str] = []
    for j in range(open_idx, len(code)):
        ch = code[j]
        if ch in CLOSERS:
            stack.append(CLOSERS[ch])
        elif ch in ")]}":
            if not stack or stack.pop() != ch:
                return -1
            if not stack:
                return j
    return -1


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def _tokenize(pred: str) -> list[tuple[str, str]] | None:
    toks: list[tuple[str, str]] = []
    pos = 0
    pred = pred.strip()
    while pos < len(pred):
        m = TOKEN_RE.match(pred, pos)
        if not m or m.end() == pos:
            if pred[pos:].strip() == "":
                break
            return None
        if m.group("id") is not None:
            toks.append(("id", m.group("id")))
        elif m.group("str") is not None:
            toks.append(("str", m.group("str")[2:-2]))  # 去除引號與 \x01…\x02 標記，只留索引
        else:
            toks.append(("p", m.group("p")))
        pos = m.end()
    return toks


def _pred_ok(toks: list[tuple[str, str]], pos: int, strings: list[str]) -> tuple[bool, int]:
    def peek(p: int) -> tuple[str, str] | None:
        return toks[p] if p < len(toks) else None

    t = peek(pos)
    if t is None or t[0] != "id":
        return False, pos
    name = t[1]
    pos += 1
    if name in ("all", "any", "not"):
        if peek(pos) != ("p", "("):
            return False, pos
        pos += 1
        count = 0
        if peek(pos) == ("p", ")"):
            pos += 1
        else:
            while True:
                ok, pos = _pred_ok(toks, pos, strings)
                if not ok:
                    return False, pos
                count += 1
                nxt = peek(pos)
                if nxt == ("p", ","):
                    pos += 1
                    if peek(pos) == ("p", ")"):  # 允許尾端逗號，例如 all(test,)
                        pos += 1
                        break
                    continue
                if nxt == ("p", ")"):
                    pos += 1
                    break
                return False, pos
        if name == "not" and count != 1:
            return False, pos
        return True, pos
    if peek(pos) == ("p", "="):
        pos += 1
        s = peek(pos)
        if s is None or s[0] != "str":
            return False, pos
        pos += 1
        content = strings[int(s[1])]
        return (name == "feature" and content == ALLOWED_FEATURE), pos
    return name == "test", pos


def predicate_ok(pred: str, strings: list[str]) -> bool:
    toks = _tokenize(pred)
    if not toks:
        return False
    ok, end = _pred_ok(toks, 0, strings)
    return ok and end == len(toks)


def _cfg_call_ok(code: str, open_idx: int, is_attr: bool, strings: list[str]) -> bool:
    """``open_idx`` 為 ``cfg(`` 的開括號（或 ``cfg!`` 的定界符）；``is_attr`` 表示 ``cfg_attr``，謂詞取第一個參數。"""
    close_idx = _matching_close(code, open_idx)
    if close_idx == -1:
        return False
    inner = code[open_idx + 1:close_idx]
    pred = _split_top_level(inner)[0] if is_attr else inner
    return predicate_ok(pred, strings)


def cfg_violations(code: str, strings: list[str]) -> list[int]:
    """回傳違規的 offset 清單（cfg 相關巨集的起始位置）。"""
    hits: set[int] = set()
    for m in CFG_ATTR_RE.finditer(code):
        is_attr = m.group(1) == "cfg_attr"
        open_idx = m.end() - 1
        if not _cfg_call_ok(code, open_idx, is_attr, strings):
            hits.add(m.start())
            continue
        if is_attr:
            # cfg_attr 之後的屬性可能含 cfg(...)／cfg_attr(...)，例如 cfg_attr(not(test), cfg(unix))，逐一檢查
            close_idx = _matching_close(code, open_idx)
            for nm in CFG_CALL_RE.finditer(code, open_idx + 1, close_idx):
                if not _cfg_call_ok(code, nm.end() - 1, nm.group(1) is not None, strings):
                    hits.add(nm.start())
    for m in CFG_MACRO_RE.finditer(code):
        open_idx = m.start("open")
        if not _cfg_call_ok(code, open_idx, False, strings):
            hits.add(m.start())
    return sorted(hits)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def check_rust_file(rel: str, src: str, crate: str, is_test_file: bool, is_common: bool) -> list[str]:
    out: list[str] = []
    raw_lines = src.splitlines()
    nocomment, code, strings = lex(src)

    def report(line: int, rule: str) -> None:
        text = raw_lines[line - 1].strip() if 0 < line <= len(raw_lines) else ""
        out.append(f"{rel}:{line}: {rule}: {text}")

    if crate != PAL_CRATE:
        for off in cfg_violations(code, strings):
            report(_line_of(code, off), "cfg-predicate")
        for m in OS_MODULE_RE.finditer(code):
            report(_line_of(code, m.start()), "os-module")
        for m in OS_NAME_RE.finditer(code):
            report(_line_of(code, m.start()), "os-name")
        for m in FFI_RE.finditer(code):
            report(_line_of(code, m.start()), "ffi-crate")
    if is_test_file and not is_common:
        for m in CARGO_MANIFEST_DIR_RE.finditer(nocomment):
            report(_line_of(nocomment, m.start()), "test-data-path")
    return out


def _find_line(lines: list[str], pattern: str) -> int:
    rx = re.compile(pattern)
    for idx, line in enumerate(lines, start=1):
        if rx.search(line):
            return idx
    return 1


def check_cargo_toml(rel: str, text: str) -> list[str]:
    """以 tomllib 解析後檢查語意（引號 key、表形式 `[build-dependencies.x]`、任何 `[target.*]` 皆不漏）；行號由原文定位。"""
    if tomllib is None:
        raise RuntimeError("檢查 Cargo.toml 需要 Python 3.11 以上（tomllib）")
    data = tomllib.loads(text)
    lines = text.splitlines()
    out: list[str] = []

    def report(line_no: int, rule: str) -> None:
        src_line = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
        out.append(f"{rel}:{line_no}: {rule}: {src_line}")

    pkg = data.get("package")
    if isinstance(pkg, dict) and "build" in pkg:
        report(_find_line(lines, r"""^\s*["']?build["']?\s*="""), "build-script")
    if "build-dependencies" in data:
        report(_find_line(lines, r"build-dependencies"), "build-script")
    if "target" in data:
        targets = data["target"]
        keys = list(targets) if isinstance(targets, dict) and targets else [""]
        for key in keys:
            pat = (r"\[\s*target\s*\.\s*[\"']?" + re.escape(key)) if key else r"\[\s*target\s*\]"
            report(_find_line(lines, pat), "target-section")
        if isinstance(targets, dict):
            for table in targets.values():
                if isinstance(table, dict) and "build-dependencies" in table:
                    report(_find_line(lines, r"build-dependencies"), "build-script")
    return out


def run(root: Path) -> list[str]:
    crates_dir = root / "crates"
    if not crates_dir.is_dir():
        raise RuntimeError(f"找不到 crates/ 目錄：{crates_dir}（--root 是否指向 repo 根目錄？）")
    violations: list[str] = []
    scanned = 0
    for crate_dir in sorted(p for p in crates_dir.iterdir() if p.is_dir()):
        crate = crate_dir.name
        if crate != PAL_CRATE:
            build_rs = crate_dir / "build.rs"
            if build_rs.is_file():
                scanned += 1
                violations.append(f"{build_rs.relative_to(root).as_posix()}:1: build-script: build.rs")
            cargo_toml = crate_dir / "Cargo.toml"
            if cargo_toml.is_file():
                scanned += 1
                violations.extend(
                    check_cargo_toml(cargo_toml.relative_to(root).as_posix(), cargo_toml.read_text(encoding="utf-8"))
                )
        for sub in SCAN_DIRS:
            base = crate_dir / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.rs")):
                scanned += 1
                rel = path.relative_to(root).as_posix()
                is_test_file = sub == "tests"
                is_common = rel == f"crates/{crate}/tests/common/mod.rs"
                src = path.read_text(encoding="utf-8")
                violations.extend(check_rust_file(rel, src, crate, is_test_file, is_common))
    if scanned == 0:
        raise RuntimeError(f"crates/ 下未掃描到任何 .rs 或 Cargo.toml（{crates_dir}）；root 是否正確？")
    return violations


def _force_utf8_stdio() -> None:
    """固定 stdout／stderr 為 UTF-8、LF：Windows 預設 code page（如 cp1252）無法編碼繁中原始碼行。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser(description="平台 cfg 邊界檢查（arch §2.1）")
    default_root = Path(__file__).resolve().parents[2]
    ap.add_argument("--root", type=Path, default=default_root, help="repo 根目錄（預設為本腳本上兩層）")
    args = ap.parse_args(argv)
    try:
        found = run(args.root.resolve())
    except (RuntimeError, OSError, ValueError) as exc:  # tomllib.TOMLDecodeError 與 UnicodeDecodeError 皆為 ValueError
        print(f"check_cfg_boundary: {exc}", file=sys.stderr)
        return 2
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
