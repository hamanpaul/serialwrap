"""SKILL 受管區段生成（P0-T11，OD-06）。

`render(cat, namespace)` 依 `Catalogs` 產生 SKILL.md／SKILL_WINDOWS.md 內
`generated-fact` 標記區塊的表格內容：`error_code` 對應 `contracts/error_codes.yaml`
（表格後附 passthrough 補充行），`session_last_error` 對應
`contracts/session_last_errors.yaml`。輸出只由目錄資料決定（列依 code／value 的
Python 字串升冪，不含時間戳），供 `python3 -m tools.contracts skill-section`、
R-26 與 C14 逐位元組比對。本模組只使用標準函式庫與同套件的 `.catalog`，不得 import `sw_core`。
"""

from __future__ import annotations

from typing import Final, Mapping

from .catalog import Catalogs, ErrorCodeEntry, LastErrorEntry, PassthroughEntry

RETRYABLE_LABEL: Final[Mapping[str, str]] = {
    "yes": "是",
    "no": "否",
    "conditional": "視情況",
    "fix_args": "修正參數後",
}
"""retryable 列舉值對應「可重試」欄的中文標籤。"""

NAMESPACES: Final[tuple[str, ...]] = ("error_code", "session_last_error")

_ERROR_CODE_HEADER: Final[str] = "| code | 介面／平台 | 可重試 | 意義 | Agent 處置 |"
_LAST_ERROR_HEADER: Final[str] = "| last_error | 終態 | 自動重探 | 意義 | Agent 處置 |"
_TABLE_SEPARATOR: Final[str] = "|---|---|---|---|---|"
_APPROVED_MARK: Final[str] = "（未生效）"
_UNREACHABLE_MARK: Final[str] = "（目前不可達）"
_PASSTHROUGH_PREFIX: Final[str] = "passthrough："
_PASSTHROUGH_TAIL: Final[str] = "（值見下方 last_error 表）"
_CELL_FORBIDDEN: Final[tuple[str, ...]] = ("|", "\n", "\r")


def render(cat: Catalogs, namespace: str) -> str:
    """回傳指定命名空間的受管表格內容（不含結尾換行）。

    `namespace` 須為 `error_code` 或 `session_last_error`；其他值拋出 `ValueError`。
    """
    if namespace == "error_code":
        return _render_error_codes(cat)
    if namespace == "session_last_error":
        return _render_last_errors(cat)
    raise ValueError(f"未知的 namespace：{namespace!r}（允許值：{', '.join(NAMESPACES)}）")


def _render_error_codes(cat: Catalogs) -> str:
    lines = [_ERROR_CODE_HEADER, _TABLE_SEPARATOR]
    lines.extend(_error_code_row(e) for e in sorted(cat.error_codes, key=lambda e: e.code))
    if cat.passthrough:
        # 表格後空一行，避免 Markdown 把補充行併入表格。
        lines.append("")
        lines.extend(
            _passthrough_line(p) for p in sorted(cat.passthrough, key=lambda p: p.id)
        )
    return "\n".join(lines)


def _error_code_row(entry: ErrorCodeEntry) -> str:
    interfaces = "、".join(entry.scope.interfaces)
    if entry.status == "APPROVED":
        interfaces += _APPROVED_MARK
    platforms = "、".join(entry.scope.platforms)
    return (
        f"| `{entry.code}` | {interfaces}／{platforms} | "
        f"{RETRYABLE_LABEL[entry.retryable]} | "
        f"{_cell(entry.meaning)} | {_cell(entry.agent_action)} |"
    )


def _passthrough_line(entry: PassthroughEntry) -> str:
    return f"{_PASSTHROUGH_PREFIX}{entry.id} — {_cell(entry.rule)}{_PASSTHROUGH_TAIL}"


def _render_last_errors(cat: Catalogs) -> str:
    lines = [_LAST_ERROR_HEADER, _TABLE_SEPARATOR]
    lines.extend(_last_error_row(e) for e in sorted(cat.last_errors, key=lambda e: e.value))
    return "\n".join(lines)


def _last_error_row(entry: LastErrorEntry) -> str:
    terminal = "終態" if entry.terminal else "非終態"
    meaning = _cell(entry.meaning)
    if not entry.reachable:
        meaning += _UNREACHABLE_MARK
    reprobe = _reprobe_label(entry.auto_reprobe_attached, entry.auto_reprobe_detached)
    return (
        f"| `{entry.value}` | {terminal} | {reprobe} | "
        f"{meaning} | {_cell(entry.agent_action)} |"
    )


def _reprobe_label(attached: bool, detached: bool) -> str:
    if attached and detached:
        return "ATTACHED／DETACHED 自動重探"
    if attached:
        return "僅 ATTACHED 自動重探"
    if detached:
        # 目前目錄資料無此組合；保留獨立標籤，避免誤標為「不自動重探」。
        return "僅 DETACHED 自動重探"
    return "不自動重探"


def _cell(text: str) -> str:
    """受管區段欄位（表格儲存格與 passthrough 補充行）不得含管線符或換行，否則會破壞 entry_pattern 與 Markdown 表格結構。"""
    if any(ch in text for ch in _CELL_FORBIDDEN):
        raise ValueError(f"受管區段欄位不得含 '|' 或換行：{text!r}")
    return text
