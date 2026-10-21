#!/usr/bin/env python3
"""由 CPython 3.12.3 的 Unicode 15.0.0 資料產生 pyre 查表檔。"""

from __future__ import annotations

import argparse
import _sre
import json
import sys
import unicodedata
from pathlib import Path

from re import _casefix

MAX_CODEPOINT = 0x110000


def ranges_for(predicate):
    """把逐字元條件壓成含首尾碼位的區間。"""
    ranges = []
    start = None
    previous = None
    for codepoint in range(MAX_CODEPOINT):
        if predicate(chr(codepoint)):
            if start is None:
                start = codepoint
            previous = codepoint
        elif start is not None:
            ranges.append((start, previous))
            start = None
    if start is not None:
        ranges.append((start, previous))
    return ranges


def case_groups():
    """依 `_sre.unicode_tolower` 與 `_casefix` 建立單碼位等價集合。"""
    parent = {}

    def find(value):
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for codepoint in range(MAX_CODEPOINT):
        lowered = _sre.unicode_tolower(codepoint)
        if lowered != codepoint:
            union(codepoint, lowered)
    for lowered, extras in _casefix._EXTRA_CASES.items():
        for extra in extras:
            union(lowered, extra)

    groups = {}
    for codepoint in parent:
        groups.setdefault(find(codepoint), []).append(codepoint)
    return sorted(tuple(sorted(members)) for members in groups.values() if len(members) > 1)


def load_aliases(path):
    """讀取 `<名稱>\t<十六進位碼位>` 格式的 Unicode 15 名稱別名。"""
    aliases = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, codepoint = line.split("\t", 1)
        aliases[name.upper()] = int(codepoint, 16)
    return aliases


def rust_table(ranges, groups):
    """輸出 Rust 靜態查表資料。"""
    lines = [
        "//! CPython 3.12.3 / Unicode 15.0.0 衍生的 Python re 字元表。",
        "",
        "pub(super) const ALNUM_RANGES: &[(u32, u32)] = &[",
    ]
    for start, end in ranges["alnum"]:
        lines.append(f"    (0x{start:x}, 0x{end:x}),")
    lines.append("];\npub(super) const DECIMAL_RANGES: &[(u32, u32)] = &[")
    for start, end in ranges["decimal"]:
        lines.append(f"    (0x{start:x}, 0x{end:x}),")
    lines.append("];\npub(super) const SPACE_RANGES: &[(u32, u32)] = &[")
    for start, end in ranges["space"]:
        lines.append(f"    (0x{start:x}, 0x{end:x}),")
    lines.append("];\npub(super) const IDENT_START_RANGES: &[(u32, u32)] = &[")
    for start, end in ranges["ident_start"]:
        lines.append(f"    (0x{start:x}, 0x{end:x}),")
    lines.append("];\npub(super) const IDENT_CONTINUE_RANGES: &[(u32, u32)] = &[")
    for start, end in ranges["ident_continue"]:
        lines.append(f"    (0x{start:x}, 0x{end:x}),")
    lines.append("];\npub(super) const CASE_GROUPS: &[&[u32]] = &[")
    for members in groups:
        lines.append("    &[" + ", ".join(f"0x{value:x}" for value in members) + "],")
    lines.append("];\npub(super) const CASE_MEMBER_GROUPS: &[(u32, u16)] = &[")
    member_groups = sorted((member, group_index) for group_index, members in enumerate(groups) for member in members)
    for member, group_index in member_groups:
        lines.append(f"    (0x{member:x}, {group_index}),")
    lines.extend([
        "];",
        "",
        "pub(super) fn contains(ranges: &[(u32, u32)], codepoint: u32) -> bool {",
        "    ranges.binary_search_by(|(start, end)| {",
        "        if codepoint < *start { std::cmp::Ordering::Greater }",
        "        else if codepoint > *end { std::cmp::Ordering::Less }",
        "        else { std::cmp::Ordering::Equal }",
        "    }).is_ok()",
        "}",
        "",
        "pub(super) fn case_group(codepoint: u32) -> Option<&'static [u32]> {",
        "    let index = CASE_MEMBER_GROUPS.binary_search_by_key(&codepoint, |entry| entry.0).ok()?;",
        "    Some(CASE_GROUPS[CASE_MEMBER_GROUPS[index].1 as usize])",
        "}",
        "",
        "pub(super) fn unicode_name(name: &str) -> Option<char> {",
        "    let name = name.to_ascii_uppercase();",
        "    let names = include_bytes!(\"unicode_names.tsv\");",
        "    let mut low = 0usize;",
        "    let mut high = names.len();",
        "    while low < high {",
        "        let middle = (low + high) / 2;",
        "        let line_start = names[..middle].iter().rposition(|byte| *byte == b'\\n').map_or(0, |index| index + 1);",
        "        let line_end = names[middle..].iter().position(|byte| *byte == b'\\n').map_or(names.len(), |index| middle + index);",
        "        let line = &names[line_start..line_end];",
        "        let tab = line.iter().position(|byte| *byte == b'\\t')?;",
        "        match line[..tab].cmp(name.as_bytes()) {",
        "            std::cmp::Ordering::Less => low = line_end + 1,",
        "            std::cmp::Ordering::Greater => high = line_start,",
        "            std::cmp::Ordering::Equal => {",
        "                let text = std::str::from_utf8(&line[tab + 1..]).ok()?;",
        "                return char::from_u32(u32::from_str_radix(text, 16).ok()?);",
        "            }",
        "        }",
        "    }",
        "    None",
        "}",
    ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aliases", type=Path, required=True, help="Unicode 15 名稱別名 TSV")
    parser.add_argument("--rust-out", type=Path, required=True, help="Rust 查表輸出路徑")
    parser.add_argument("--names-out", type=Path, required=True, help="Unicode 名稱 TSV 輸出路徑")
    args = parser.parse_args()

    if unicodedata.unidata_version != "15.0.0":
        raise SystemExit(f"需要 Unicode 15.0.0，實際為 {unicodedata.unidata_version}")
    if sys.version_info[:3] != (3, 12, 3):
        raise SystemExit(f"需要 CPython 3.12.3，實際為 {sys.version.split()[0]}")

    ranges = {
        "alnum": ranges_for(str.isalnum),
        "decimal": ranges_for(str.isdecimal),
        "space": ranges_for(str.isspace),
        "ident_start": ranges_for(lambda ch: ch.isidentifier()),
        "ident_continue": ranges_for(lambda ch: ("A" + ch).isidentifier()),
    }
    groups = case_groups()

    names = {}
    for codepoint in range(MAX_CODEPOINT):
        name = unicodedata.name(chr(codepoint), None)
        if name is not None:
            names[name.upper()] = codepoint
    names.update(load_aliases(args.aliases))
    args.names_out.parent.mkdir(parents=True, exist_ok=True)
    args.names_out.write_text(
        "".join(f"{name}\t{codepoint:06X}\n" for name, codepoint in sorted(names.items())),
        encoding="ascii",
        newline="\n",
    )
    args.rust_out.parent.mkdir(parents=True, exist_ok=True)
    args.rust_out.write_text(rust_table(ranges, groups), encoding="utf-8", newline="\n")
    print(json.dumps({"unicode_version": unicodedata.unidata_version, "names": len(names), "case_groups": len(groups)}, sort_keys=True))


if __name__ == "__main__":
    main()
