"""golden TSV 共用讀寫（P3 各 golden 產生器共用）。

兩種格式，皆為四欄、無註解行、UTF-8、LF、依輸入順序寫出（不排序）：

- hex 模式（``write_tsv``；P3 起的預設）：每行 ``<case_id>\\t<op>\\t<hex(input UTF-8)>\\t<hex(expected UTF-8)>\\n``。
- raw 模式（``write_raw_tsv``；P6 telnet_filter／line_editor）：``input``／``expected`` 欄為產生器自行編碼的
  ASCII 原字串（例如以 ``,`` 分隔的 chunk hex），不再經 hex(UTF-8) 包裝；欄位不得含 ``\\t``／``\\n``／``\\r``。

``read_tsv(path)`` 讀 hex 模式，``read_tsv(path, raw=True)`` 讀 raw 模式（parity-amendments-2026-10-26 #182）。

欄位改用 hex 而非 JSON，讓 golden 的讀取不依賴受測的 ``pyjson``。
唯一性以 ``(case_id, op)`` 為鍵：同一輸入可對多個 profile 產生多列（例如 ``dumps:*``），
case_id 本身在這類情況下會重複。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

Row = tuple[str, str, str, str]


def _hex(text: str) -> str:
    # surrogatepass：允許寫出 lone surrogate 的原始位元組（產生器本身會略過這類案例，此處僅作防禦）。
    return text.encode("utf-8", "surrogatepass").hex()


def _check_unique(rows: list[Row]) -> None:
    seen: set[tuple[str, str]] = set()
    for case_id, op, _text, _expected in rows:
        key = (case_id, op)
        if key in seen:
            raise ValueError(f"golden 列重複：case_id={case_id!r} op={op!r}")
        seen.add(key)


def write_tsv(path: str | Path, rows: Iterable[Row]) -> None:
    rows = list(rows)
    _check_unique(rows)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for case_id, op, text, expected in rows:
            fh.write(f"{case_id}\t{op}\t{_hex(text)}\t{_hex(expected)}\n")


def write_raw_tsv(path: str | Path, rows: Iterable[Row]) -> None:
    """raw 模式寫出：欄位原樣寫入。先驗唯一性與分隔字元，全部通過才開檔（失敗不留半寫檔）。"""
    rows = list(rows)
    _check_unique(rows)
    for row in rows:
        for field in row:
            if any(ch in field for ch in "\t\n\r"):
                raise ValueError(f"raw golden 欄位不得含 TAB／換行：case_id={row[0]!r} 欄位={field[:40]!r}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write("\t".join(row) + "\n")


def read_tsv(path: str | Path, *, raw: bool = False) -> list[Row]:
    """供產生器自測與人工檢視；解碼規則與 Rust 端 ``tests/common`` 相同。

    ``raw=False``（預設）讀 hex 模式並把 input／expected 解回 UTF-8 字串；``raw=True`` 讀 raw 模式、欄位原樣回傳。
    """
    out: list[Row] = []
    text = Path(path).read_bytes().decode("utf-8")
    for line in text.split("\n"):
        if not line:
            continue
        case_id, op, text_hex, expected_hex = line.split("\t")
        if raw:
            out.append((case_id, op, text_hex, expected_hex))
            continue
        out.append(
            (
                case_id,
                op,
                bytes.fromhex(text_hex).decode("utf-8", "surrogatepass"),
                bytes.fromhex(expected_hex).decode("utf-8", "surrogatepass"),
            )
        )
    return out
