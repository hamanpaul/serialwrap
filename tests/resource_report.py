"""pytest 資源報告與平台 SKIP 閘門的純函式（P2-T13，TST-D-4）。

本模組只用 stdlib，供 `tests/resource_report_plugin.py`（報告 hook 的唯一宿主）與
`tests/conftest.py` 呼叫；不得 import `sw_core` 或 root conftest。

- 模組層平台 marker 以 AST 讀取（不 import 該模組；Windows 上 import 可能因頂層 `import pty`
  等失敗，見 `should_ignore_on_windows`）。
- 平台條件 SKIP 以「marker 結構」判定（`skipif` 的 condition 原始碼是否符合 `PLATFORM_SKIP_RE`），
  不以 skip reason 的文字判定。
- `_FORCE_OS` 為測試掛鉤：設為 `"nt"` 時模擬 Windows collection（`test_windows_collection_excludes_posix_modules`）。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PLATFORM_MARKERS = ("core_portable", "posix_only", "windows_native")
RESOURCE_MARKERS = (
    "memory_only",
    "process_rpc",
    "posix_pty",
    "windows_com",
    "physical_uart",
    "supervisor_runtime",
    "interactive_desktop",
)
# 與 P0 T14 規則第 3 條「平台條件 SKIP」同一 regex；tests/test_resource_markers.py 的 PLATFORM_COND 以測試比對。
PLATFORM_SKIP_RE = (
    r"sys\.platform|os\.name|platform\.system|AF_UNIX|openpty|geteuid|fcntl|msvcrt"
    r"|win32|_IS_WIN|IS_WINDOWS|posix"
)
PLATFORM_SKIP_PATTERN = re.compile(PLATFORM_SKIP_RE)

# 測試掛鉤：設為 "nt" 時以 Windows 規則排除 collection（見模組 docstring）。
_FORCE_OS: str | None = None

_BUCKET = {
    "passed": "passed",
    "xpassed": "passed",
    "failed": "failed",
    "error": "failed",
    "skipped": "skipped",
    "xfailed": "skipped",
}


def effective_os_name() -> str:
    """回傳目前採用的 OS 名稱（測試掛鉤 `_FORCE_OS` 優先於 `os.name`）。"""
    return _FORCE_OS or os.name


def effective_platform() -> str:
    """報告用平台名稱：`windows` 或 `linux`（與 l1_judge 的 platform 參數一致）。"""
    return "windows" if effective_os_name() == "nt" else "linux"


def _pytest_mark_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "mark"
            and isinstance(sub.value.value, ast.Name)
            and sub.value.value.id == "pytest"
        ):
            out.add(sub.attr)
    return out


def _is_pytestmark_target(target: ast.AST) -> bool:
    return isinstance(target, ast.Name) and target.id == "pytestmark"


def _module_pytestmark_values(tree: ast.Module) -> list[ast.AST]:
    values: list[ast.AST] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(_is_pytestmark_target(t) for t in stmt.targets):
            values.append(stmt.value)
        elif isinstance(stmt, ast.AnnAssign) and _is_pytestmark_target(stmt.target) and stmt.value is not None:
            values.append(stmt.value)
    return values


_PARSE_CACHE: dict[tuple[str, int], tuple[ast.Module, str]] = {}


def _parse(path: pathlib.Path) -> tuple[ast.Module, str]:
    """解析模組 AST（依路徑與 mtime 快取；collection 會對每個項目查詢，避免重複解析）。"""
    target = pathlib.Path(path)
    key = (str(target), target.stat().st_mtime_ns)
    cached = _PARSE_CACHE.get(key)
    if cached is None:
        src = target.read_text(encoding="utf-8")
        cached = (ast.parse(src, filename=str(target)), src)
        _PARSE_CACHE[key] = cached
    return cached


def module_platform_marks(path: pathlib.Path) -> set[str]:
    """AST 讀取模組層 `pytestmark` 內的平台 marker（不 import、不執行該模組）。"""
    tree, _ = _parse(pathlib.Path(path))
    marks: set[str] = set()
    for value in _module_pytestmark_values(tree):
        marks |= _pytest_mark_names(value)
    return marks & set(PLATFORM_MARKERS)


def should_ignore_on_windows(path: pathlib.Path) -> bool:
    """模組層平台 marker 恰為 `posix_only` 時，Windows collection 前排除（避免 import 階段 collection error）。"""
    return module_platform_marks(pathlib.Path(path)) == {"posix_only"}


def _mark_parts(marker: Any) -> tuple[str, Sequence[Any], Mapping[str, Any]]:
    mark = getattr(marker, "mark", marker)  # MarkDecorator -> Mark
    return getattr(mark, "name", ""), tuple(getattr(mark, "args", ())), dict(getattr(mark, "kwargs", {}))


def is_platform_skip(item_markers: Iterable[Any]) -> bool:
    """`skipif` marker 的 condition 為字串且符合平台條件 regex 時為 True（以結構判定，不看 reason）。

    純 bool condition（如 `skipif(True, ...)`）無法由 marker 物件還原原始碼，回 False；
    函式裝飾器的原始碼判定由 `static_platform_skip` 以 AST 完成。
    """
    for marker in item_markers:
        name, args, kwargs = _mark_parts(marker)
        if name != "skipif":
            continue
        condition = args[0] if args else kwargs.get("condition")
        if isinstance(condition, str) and PLATFORM_SKIP_PATTERN.search(condition):
            return True
    return False


def _skipif_condition_is_platform(call: ast.Call, src: str) -> bool:
    if not (isinstance(call.func, (ast.Name, ast.Attribute)) and _call_name(call) == "skipif"):
        return False
    cond: ast.AST | None = call.args[0] if call.args else None
    if cond is None:
        cond = next((kw.value for kw in call.keywords if kw.arg == "condition"), None)
    if cond is None:
        return False
    return bool(PLATFORM_SKIP_PATTERN.search(ast.get_source_segment(src, cond) or ""))


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _decorators_platform_skip(decorators: Iterable[ast.AST], src: str) -> bool:
    return any(isinstance(d, ast.Call) and _skipif_condition_is_platform(d, src) for d in decorators)


def _values_platform_skip(values: Iterable[ast.AST], src: str) -> bool:
    for value in values:
        for sub in ast.walk(value):
            if isinstance(sub, ast.Call) and _skipif_condition_is_platform(sub, src):
                return True
    return False


def static_platform_skip(path: pathlib.Path, cls_name: str | None, func_name: str) -> bool:
    """AST 判定 pytest 項目是否帶平台條件 `skipif` 裝飾器（模組、類別或函式層）。

    條件原始碼（字串或 AST 片段）符合 `PLATFORM_SKIP_RE` 才算；reason 文字不納入判定。
    """
    tree, src = _parse(pathlib.Path(path))
    if _values_platform_skip(_module_pytestmark_values(tree), src):
        return True
    for node in tree.body:
        if cls_name is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return _decorators_platform_skip(node.decorator_list, src)
        if cls_name is not None and isinstance(node, ast.ClassDef) and node.name == cls_name:
            if _decorators_platform_skip(node.decorator_list, src):
                return True
            if _values_platform_skip(_module_pytestmark_values(node), src):
                return True
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == func_name:
                    return _decorators_platform_skip(fn.decorator_list, src)
    return False


def _zero_counts() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "skipped": 0}


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _record_resources(rec: Mapping[str, Any]) -> list[str]:
    """回傳一列的全部資源 marker（去重、保序，只留 RESOURCE_MARKERS 內者）。"""
    values = rec.get("resources")
    if values is None:
        single = rec.get("resource")
        values = [single] if single else []
    return _dedupe(str(v) for v in values if v in RESOURCE_MARKERS)


def summarize(records: list[dict[str, Any]], *, ignored: list[str], platform: str) -> dict[str, Any]:
    """彙總計數、資源分類與平台 SKIP 閘門清單。

    `records` 每筆：`nodeid`、`platform`、`resources`（list，全部資源 marker；舊格式可用單值 `resource`）、
    `outcome`（None 表示已收集但未執行）、`is_platform_skip`（bool，由 marker 結構判定）。
    `outcome` 為 None 的列只參與閘門判定，不計數。同一列帶多個資源 marker 時，每個資源各計一次；
    平台計數只計一次（P2-T13 審查修正：原實作只取第一個資源 marker，多資源項目會漏計）。
    """
    counts: dict[str, dict[str, int]] = {p: _zero_counts() for p in PLATFORM_MARKERS}
    resources: dict[str, dict[str, int]] = {r: _zero_counts() for r in RESOURCE_MARKERS}
    skips: list[str] = []
    for rec in records:
        outcome = rec.get("outcome")
        bucket = _BUCKET.get(outcome) if outcome else None
        plat = rec.get("platform")
        if bucket is not None:
            if plat in counts:
                counts[plat][bucket] += 1
            for res in _record_resources(rec):
                resources[res][bucket] += 1
        if plat == "core_portable" and rec.get("is_platform_skip") is True:
            skips.append(rec["nodeid"])
    return {
        "platform": platform,
        "counts": counts,
        "resources": resources,
        "not_collected_platform": _dedupe(ignored),
        "core_portable_platform_skips": _dedupe(skips),
    }
