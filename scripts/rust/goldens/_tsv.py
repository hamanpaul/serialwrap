"""golden TSV 共用讀寫（P3 各 golden 產生器共用）。

格式：每行 ``<case_id>\\t<op>\\t<hex(input UTF-8)>\\t<hex(expected UTF-8)>\\n``，
檔案無註解行、UTF-8、LF、依輸入順序寫出（不排序）。

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


def write_tsv(path: str | Path, rows: Iterable[Row]) -> None:
    rows = list(rows)
    seen: set[tuple[str, str]] = set()
    for case_id, op, _text, _expected in rows:
        key = (case_id, op)
        if key in seen:
            raise ValueError(f"golden 列重複：case_id={case_id!r} op={op!r}")
        seen.add(key)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for case_id, op, text, expected in rows:
            fh.write(f"{case_id}\t{op}\t{_hex(text)}\t{_hex(expected)}\n")


def read_tsv(path: str | Path) -> list[Row]:
    """供產生器自測與人工檢視；解碼規則與 Rust 端 ``tests/common`` 相同。"""
    out: list[Row] = []
    text = Path(path).read_bytes().decode("utf-8")
    for line in text.split("\n"):
        if not line:
            continue
        case_id, op, text_hex, expected_hex = line.split("\t")
        out.append(
            (
                case_id,
                op,
                bytes.fromhex(text_hex).decode("utf-8", "surrogatepass"),
                bytes.fromhex(expected_hex).decode("utf-8", "surrogatepass"),
            )
        )
    return out
