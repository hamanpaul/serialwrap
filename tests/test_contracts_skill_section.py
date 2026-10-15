"""契約 T11：SKILL.md 受管區段生成測試（TDD red 階段）。

對應 `docs/rust-refactor/plans/01-stage0-contracts.md` Task T11。
測試 `tools.contracts.skill_section.render` 輸出格式、排序、標籤映射，
以及 `SKILL.md` 與 `SKILL_WINDOWS.md` 中受管區段的標記同步與插入位置。
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

from sw_core import cli

# 模組層只放平台 marker（core_portable）；資源 marker 放在函式層（見 T14 規則 2）。
pytestmark = [pytest.mark.core_portable]

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "sw_core" / "assets" / "skill" / "SKILL.md"
SKILL_WINDOWS_MD = REPO_ROOT / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md"

# T12 定義的 entry_pattern 常數，用於匹配受管表格資料列並擷取具名群組
ENTRY_PATTERN = (
    r"^\| `(?P<code>[^`]+)` \| [^|\n]* \| [^|\n]* \| "
    r"(?P<meaning>[^|\n]*\S[^|\n]*) \| (?P<action>[^|\n]*\S[^|\n]*) \|$"
)


def _load_skill_section() -> Any:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("tools.contracts.skill_section")


def _load_catalog() -> Any:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("tools.contracts.catalog")


# 合成 Catalogs 輔助資料結構（當 tools.contracts.catalog 尚未載入時之備援）
@dataclass(frozen=True)
class _FallbackScope:
    interfaces: tuple[str, ...]
    methods: tuple[str, ...]
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class _FallbackErrorCodeEntry:
    code: str
    status: str
    reachable: bool
    unreachable_reason: str | None
    constant: str | None
    scope: _FallbackScope
    meaning: str
    side_effect: str
    retryable: str
    uart_tx: str
    ok_response: bool
    agent_action: str
    extra_fields: tuple[str, ...] = ()
    exit_codes: Mapping[str, int] = frozenset() if False else None
    contract_tests: tuple[str, ...] = ()
    behavior_change: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.exit_codes is None:
            object.__setattr__(self, "exit_codes", {})


@dataclass(frozen=True)
class _FallbackPassthroughEntry:
    id: str
    namespace_ref: str
    scope: _FallbackScope
    rule: str
    owner_confirmation: str
    reconciliation_refs: tuple[str, ...]


@dataclass(frozen=True)
class _FallbackLastErrorEntry:
    value: str
    kind: str
    reason_forms: tuple[str, ...]
    reachable: bool
    unreachable_reason: str | None
    meaning: str
    agent_action: str
    terminal: bool
    auto_reprobe_attached: bool
    auto_reprobe_detached: bool
    manual_recovery: str
    logging: str
    detail: str
    states: tuple[str, ...]
    set_by: tuple[str, ...]
    cleared_by: tuple[str, ...]
    promoted_via: tuple[str, ...]
    error_code_namespace: str
    contract_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FallbackCatalogs:
    error_codes: tuple[Any, ...]
    passthrough: tuple[Any, ...]
    last_errors: tuple[Any, ...]
    behavior_changes: tuple[Any, ...] = ()
    owner_confirmations: tuple[Any, ...] = ()
    safety_invariants: tuple[Any, ...] = ()
    aux: Any = None
    revisions: Mapping[str, int] = frozenset() if False else None

    def __post_init__(self) -> None:
        if self.revisions is None:
            object.__setattr__(self, "revisions", {})


def _make_scope(
    interfaces: tuple[str, ...] = ("rpc", "rec"),
    methods: tuple[str, ...] = ("session.attach",),
    platforms: tuple[str, ...] = ("posix", "windows"),
) -> Any:
    try:
        cat_mod = _load_catalog()
        return cat_mod.Scope(interfaces=interfaces, methods=methods, platforms=platforms)
    except (ImportError, AttributeError):
        return _FallbackScope(interfaces=interfaces, methods=methods, platforms=platforms)


def _make_error_code_entry(
    code: str,
    status: str,
    reachable: bool,
    scope: Any,
    meaning: str,
    agent_action: str,
    retryable: str = "yes",
    side_effect: str = "無",
    unreachable_reason: str | None = None,
    behavior_change: tuple[str, ...] = (),
) -> Any:
    try:
        cat_mod = _load_catalog()
        return cat_mod.ErrorCodeEntry(
            code=code,
            status=status,
            reachable=reachable,
            unreachable_reason=unreachable_reason,
            constant=None,
            scope=scope,
            meaning=meaning,
            side_effect=side_effect,
            retryable=retryable,
            uart_tx="none",
            ok_response=False,
            agent_action=agent_action,
            extra_fields=(),
            exit_codes={},
            contract_tests=(),
            behavior_change=behavior_change,
            sources=(),
        )
    except (ImportError, AttributeError):
        return _FallbackErrorCodeEntry(
            code=code,
            status=status,
            reachable=reachable,
            unreachable_reason=unreachable_reason,
            constant=None,
            scope=scope,
            meaning=meaning,
            side_effect=side_effect,
            retryable=retryable,
            uart_tx="none",
            ok_response=False,
            agent_action=agent_action,
            extra_fields=(),
            exit_codes={},
            contract_tests=(),
            behavior_change=behavior_change,
            sources=(),
        )


def _make_passthrough_entry(
    entry_id: str,
    rule: str,
    scope: Any,
    namespace_ref: str = "session_last_errors",
    owner_confirmation: str = "not_required",
) -> Any:
    try:
        cat_mod = _load_catalog()
        return cat_mod.PassthroughEntry(
            id=entry_id,
            namespace_ref=namespace_ref,
            scope=scope,
            rule=rule,
            owner_confirmation=owner_confirmation,
            reconciliation_refs=(),
        )
    except (ImportError, AttributeError):
        return _FallbackPassthroughEntry(
            id=entry_id,
            namespace_ref=namespace_ref,
            scope=scope,
            rule=rule,
            owner_confirmation=owner_confirmation,
            reconciliation_refs=(),
        )


def _make_last_error_entry(
    value: str,
    reachable: bool,
    meaning: str,
    agent_action: str,
    terminal: bool = False,
    auto_reprobe_attached: bool = False,
    auto_reprobe_detached: bool = False,
    unreachable_reason: str | None = None,
) -> Any:
    try:
        cat_mod = _load_catalog()
        return cat_mod.LastErrorEntry(
            value=value,
            kind="named",
            reason_forms=(),
            reachable=reachable,
            unreachable_reason=unreachable_reason,
            meaning=meaning,
            agent_action=agent_action,
            terminal=terminal,
            auto_reprobe_attached=auto_reprobe_attached,
            auto_reprobe_detached=auto_reprobe_detached,
            manual_recovery="手動復原",
            logging="無",
            detail="無",
            states=("ATTACHED",),
            set_by=("session_manager.py",),
            cleared_by=("auto:清除",),
            promoted_via=(),
            error_code_namespace="forbidden",
            contract_tests=(),
        )
    except (ImportError, AttributeError):
        return _FallbackLastErrorEntry(
            value=value,
            kind="named",
            reason_forms=(),
            reachable=reachable,
            unreachable_reason=unreachable_reason,
            meaning=meaning,
            agent_action=agent_action,
            terminal=terminal,
            auto_reprobe_attached=auto_reprobe_attached,
            auto_reprobe_detached=auto_reprobe_detached,
            manual_recovery="手動復原",
            logging="無",
            detail="無",
            states=("ATTACHED",),
            set_by=("session_manager.py",),
            cleared_by=("auto:清除",),
            promoted_via=(),
            error_code_namespace="forbidden",
            contract_tests=(),
        )


def _make_catalogs(
    error_codes: tuple[Any, ...] = (),
    passthrough: tuple[Any, ...] = (),
    last_errors: tuple[Any, ...] = (),
) -> Any:
    try:
        cat_mod = _load_catalog()
        return cat_mod.Catalogs(
            error_codes=error_codes,
            passthrough=passthrough,
            last_errors=last_errors,
            behavior_changes=(),
            owner_confirmations=(),
            safety_invariants=(),
            aux=None,
            revisions={},
        )
    except (ImportError, AttributeError):
        return _FallbackCatalogs(
            error_codes=error_codes,
            passthrough=passthrough,
            last_errors=last_errors,
        )


@pytest.mark.memory_only
def test_render_row_format() -> None:
    """以合成 Catalogs（一筆 EXISTING、一筆 APPROVED）呼叫 render，

    輸出第三行逐字等於預期字串；APPROVED 列含 `（未生效）`。
    """
    skill_mod = _load_skill_section()

    scope_existing = _make_scope(
        interfaces=("rpc", "rec"),
        methods=("session.attach",),
        platforms=("posix", "windows"),
    )
    scope_approved = _make_scope(
        interfaces=("rpc",),
        methods=("cmd.abort",),
        platforms=("posix", "windows"),
    )

    entry_existing = _make_error_code_entry(
        code="AUTOBOOT_QUIET",
        status="EXISTING",
        reachable=True,
        scope=scope_existing,
        meaning="開機靜默窗等待中",
        agent_action="等待 quiet 視窗結束後重送",
        retryable="yes",
    )
    entry_approved = _make_error_code_entry(
        code="COMMAND_ABORTED_IN_FLIGHT",
        status="APPROVED",
        reachable=False,
        scope=scope_approved,
        meaning="指令於執行中被中止",
        agent_action="檢查 session 狀態並重試",
        retryable="no",
        unreachable_reason="尚未實作",
        behavior_change=("BC-03",),
    )
    pt_entry = _make_passthrough_entry(
        entry_id="session_last_error",
        rule="由 session.last_error 轉錄",
        scope=_make_scope(interfaces=("rpc",), methods=("_run_rpc",)),
    )

    cat = _make_catalogs(
        error_codes=(entry_existing, entry_approved),
        passthrough=(pt_entry,),
    )

    rendered = skill_mod.render(cat, "error_code")
    lines = rendered.splitlines()

    # 前兩行為固定表頭
    assert len(lines) >= 4
    # 第三行（首個資料列）逐字等於預期字串（按 code 排序 AUTOBOOT_QUIET 先）
    expected_third_line = (
        "| `AUTOBOOT_QUIET` | rpc、rec／posix、windows | 是 | "
        "開機靜默窗等待中 | 等待 quiet 視窗結束後重送 |"
    )
    assert lines[2] == expected_third_line

    # APPROVED 列包含「（未生效）」
    assert "（未生效）" in lines[3]
    assert "rpc（未生效）／posix、windows" in lines[3]

    # 表格後包含 passthrough 說明行
    assert any(
        line == "passthrough：session_last_error — 由 session.last_error 轉錄（值見下方 last_error 表）"
        for line in lines
    )


@pytest.mark.memory_only
def test_render_sorted_and_header() -> None:
    """前兩行為固定表頭；資料列 code 已排序。"""
    skill_mod = _load_skill_section()

    scope = _make_scope()
    e_zzz = _make_error_code_entry("ZZZ_ERR", "EXISTING", True, scope, "意義 Z", "處置 Z")
    e_aaa = _make_error_code_entry("AAA_ERR", "EXISTING", True, scope, "意義 A", "處置 A")
    e_mmm = _make_error_code_entry("MMM_ERR", "EXISTING", True, scope, "意義 M", "處置 M")

    cat_ec = _make_catalogs(error_codes=(e_zzz, e_aaa, e_mmm))
    rendered_ec = skill_mod.render(cat_ec, "error_code")
    lines_ec = rendered_ec.splitlines()

    # 表頭斷言
    assert lines_ec[0] == "| code | 介面／平台 | 可重試 | 意義 | Agent 處置 |"
    assert lines_ec[1] == "|---|---|---|---|---|"

    # 資料列排序斷言
    data_lines_ec = [l for l in lines_ec[2:] if l.startswith("| `")]
    codes = [re.search(r"^\|\s*`([^`]+)`", l).group(1) for l in data_lines_ec]
    assert codes == ["AAA_ERR", "MMM_ERR", "ZZZ_ERR"]
    assert codes == sorted(codes)

    # 驗證 session_last_error 之固定表頭與排序
    le_zzz = _make_last_error_entry("ZZZ_LE", True, "意義 Z", "處置 Z")
    le_aaa = _make_last_error_entry("AAA_LE", True, "意義 A", "處置 A")
    le_mmm = _make_last_error_entry("MMM_LE", True, "意義 M", "處置 M")

    cat_le = _make_catalogs(last_errors=(le_zzz, le_aaa, le_mmm))
    rendered_le = skill_mod.render(cat_le, "session_last_error")
    lines_le = rendered_le.splitlines()

    assert lines_le[0] == "| last_error | 終態 | 自動重探 | 意義 | Agent 處置 |"
    assert lines_le[1] == "|---|---|---|---|---|"

    data_lines_le = [l for l in lines_le[2:] if l.startswith("| `")]
    le_values = [re.search(r"^\|\s*`([^`]+)`", l).group(1) for l in data_lines_le]
    assert le_values == ["AAA_LE", "MMM_LE", "ZZZ_LE"]
    assert le_values == sorted(le_values)


@pytest.mark.memory_only
def test_render_last_error_reprobe_labels() -> None:
    """三種 auto_reprobe 組合得到三種標籤；reachable: false 列含 `（目前不可達）`。"""
    skill_mod = _load_skill_section()

    le_both = _make_last_error_entry(
        "REPROBE_BOTH",
        reachable=True,
        meaning="兩端皆重探",
        agent_action="處置",
        auto_reprobe_attached=True,
        auto_reprobe_detached=True,
    )
    le_attached = _make_last_error_entry(
        "REPROBE_ATTACHED_ONLY",
        reachable=True,
        meaning="僅 attached 重探",
        agent_action="處置",
        auto_reprobe_attached=True,
        auto_reprobe_detached=False,
    )
    le_none = _make_last_error_entry(
        "REPROBE_NONE",
        reachable=True,
        meaning="皆不重探",
        agent_action="處置",
        auto_reprobe_attached=False,
        auto_reprobe_detached=False,
    )
    le_unreachable = _make_last_error_entry(
        "UNREACHABLE_ENTRY",
        reachable=False,
        meaning="此錯誤未實作",
        agent_action="處置",
        auto_reprobe_attached=False,
        auto_reprobe_detached=False,
        unreachable_reason="保留",
    )

    cat = _make_catalogs(last_errors=(le_both, le_attached, le_none, le_unreachable))
    rendered = skill_mod.render(cat, "session_last_error")
    lines = rendered.splitlines()

    line_both = next(l for l in lines if "`REPROBE_BOTH`" in l)
    line_attached = next(l for l in lines if "`REPROBE_ATTACHED_ONLY`" in l)
    line_none = next(l for l in lines if "`REPROBE_NONE`" in l)
    line_unreachable = next(l for l in lines if "`UNREACHABLE_ENTRY`" in l)

    # 三種 auto_reprobe 標籤驗證
    assert "ATTACHED／DETACHED 自動重探" in line_both
    assert "僅 ATTACHED 自動重探" in line_attached
    assert "不自動重探" in line_none

    # reachable: false 在意義欄尾加「（目前不可達）」
    assert "此錯誤未實作（目前不可達）" in line_unreachable
    assert "（目前不可達）" not in line_none


@pytest.mark.memory_only
def test_skill_md_blocks_in_sync() -> None:
    """對 SKILL.md、SKILL_WINDOWS.md，兩個 marker 區塊 strip() 後等於 render(load_catalogs(ROOT), ns).strip()。"""
    skill_mod = _load_skill_section()
    cat_mod = _load_catalog()
    catalogs = cat_mod.load_catalogs(REPO_ROOT)

    for doc_path in (SKILL_MD, SKILL_WINDOWS_MD):
        assert doc_path.is_file(), f"{doc_path} 檔案不存在"
        content = doc_path.read_text(encoding="utf-8")

        for namespace, marker in (
            ("error_code", "error-codes"),
            ("session_last_error", "session-last-errors"),
        ):
            pattern = (
                rf"<!-- BEGIN: generated-fact marker=\"{marker}\" -->\n?"
                rf"(.*?)\n?<!-- END: generated-fact marker=\"{marker}\" -->"
            )
            match = re.search(pattern, content, re.DOTALL)
            assert match is not None, f"{doc_path.name} 缺少 marker='{marker}' 之 generated-fact 標記區塊"
            block_content = match.group(1).strip()
            expected = skill_mod.render(catalogs, namespace).strip()
            assert block_content == expected, f"{doc_path.name} 的 {marker} 標記區塊與 render 輸出不一致"


@pytest.mark.memory_only
def test_skill_section_placement() -> None:
    """SKILL.md 中該節標題位於 ## 參考檔案 之前；SKILL_WINDOWS.md 中

    ## 13. 錯誤碼與 last_error 處置目錄 位於 ## 12. 疑難排解 之後且為最後一個 ## 標題。
    """
    assert SKILL_MD.is_file(), "SKILL.md 不存在"
    assert SKILL_WINDOWS_MD.is_file(), "SKILL_WINDOWS.md 不存在"

    skill_text = SKILL_MD.read_text(encoding="utf-8")
    title_linux = "## 錯誤碼與 last_error 處置目錄（自動產生；勿手改）"
    ref_title = "## 參考檔案"

    assert title_linux in skill_text, f"SKILL.md 缺少標題：{title_linux}"
    assert ref_title in skill_text, f"SKILL.md 缺少標題：{ref_title}"
    assert skill_text.index(title_linux) < skill_text.index(ref_title), (
        "SKILL.md 中錯誤碼章節必須位於「## 參考檔案」之前"
    )

    win_text = SKILL_WINDOWS_MD.read_text(encoding="utf-8")
    title_win = "## 13. 錯誤碼與 last_error 處置目錄（自動產生；勿手改）"
    title_12 = "## 12. 疑難排解"

    assert title_win in win_text, f"SKILL_WINDOWS.md 缺少標題：{title_win}"
    assert title_12 in win_text, f"SKILL_WINDOWS.md 缺少標題：{title_12}"
    assert win_text.index(title_win) > win_text.index(title_12), (
        "SKILL_WINDOWS.md 中錯誤碼章節必須位於「## 12. 疑難排解」之後"
    )

    headings = [line.strip() for line in win_text.splitlines() if line.startswith("## ")]
    assert headings[-1] == title_win, f"SKILL_WINDOWS.md 最後一個章節必須為：{title_win}"


@pytest.mark.process_rpc
def test_render_deterministic_bytes() -> None:
    """以 subprocess 執行 python3 -m tools.contracts skill-section --namespace error_code

    兩次輸出逐位元組相同，且等於行程內 render()＋\\n。
    """
    cmd = [
        sys.executable,
        "-m",
        "tools.contracts",
        "skill-section",
        "--namespace",
        "error_code",
    ]
    proc1 = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True)
    proc2 = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True)

    assert proc1.returncode == 0, f"skill-section 執行失敗 (proc1): {proc1.stderr.decode('utf-8')}"
    assert proc2.returncode == 0, f"skill-section 執行失敗 (proc2): {proc2.stderr.decode('utf-8')}"
    assert proc1.stdout == proc2.stdout, "多次呼叫 skill-section 之二進位輸出不一致"

    skill_mod = _load_skill_section()
    cat_mod = _load_catalog()
    in_proc_expected = skill_mod.render(cat_mod.load_catalogs(REPO_ROOT), "error_code") + "\n"
    assert proc1.stdout.decode("utf-8") == in_proc_expected


@pytest.mark.memory_only
def test_skill_rows_match_entry_pattern() -> None:
    """T12 的 entry_pattern 能匹配每個資料列、不匹配表頭。"""
    skill_mod = _load_skill_section()
    pattern = re.compile(ENTRY_PATTERN)

    # 1. 測試 error_code 表
    scope = _make_scope()
    e1 = _make_error_code_entry("CODE_ONE", "EXISTING", True, scope, "意義 1", "處置 1")
    e2 = _make_error_code_entry(
        "CODE_TWO",
        "APPROVED",
        False,
        scope,
        "意義 2",
        "處置 2",
        unreachable_reason="未實作",
        behavior_change=("BC-01",),
    )
    pt = _make_passthrough_entry("pt_id", "passthrough 規則", scope)
    cat_ec = _make_catalogs(error_codes=(e1, e2), passthrough=(pt,))

    rendered_ec = skill_mod.render(cat_ec, "error_code")
    lines_ec = rendered_ec.splitlines()

    # 表頭前兩行不應匹配
    assert pattern.match(lines_ec[0]) is None, "表頭第 1 行不應匹配 entry_pattern"
    assert pattern.match(lines_ec[1]) is None, "表頭分隔線不應匹配 entry_pattern"

    # 資料列必須全數匹配並擷取出具名群組
    data_rows_ec = [l for l in lines_ec if l.startswith("| `")]
    assert len(data_rows_ec) == 2
    for row in data_rows_ec:
        m = pattern.match(row)
        assert m is not None, f"error_code 資料列未匹配 entry_pattern: {row}"
        assert m.group("code") in ("CODE_ONE", "CODE_TWO")
        assert m.group("meaning").strip()
        assert m.group("action").strip()

    # passthrough 補充行不應匹配 entry_pattern
    pt_lines = [l for l in lines_ec if l.startswith("passthrough：")]
    assert len(pt_lines) == 1
    assert pattern.match(pt_lines[0]) is None, "passthrough 行不應匹配 entry_pattern"

    # 2. 測試 session_last_error 表
    le1 = _make_last_error_entry("LE_ONE", True, "意義 L1", "處置 L1")
    le2 = _make_last_error_entry(
        "LE_TWO",
        False,
        "意義 L2",
        "處置 L2",
        unreachable_reason="未實作",
    )
    cat_le = _make_catalogs(last_errors=(le1, le2))

    rendered_le = skill_mod.render(cat_le, "session_last_error")
    lines_le = rendered_le.splitlines()

    # 表頭前兩行不應匹配
    assert pattern.match(lines_le[0]) is None, "last_error 表頭第 1 行不應匹配 entry_pattern"
    assert pattern.match(lines_le[1]) is None, "last_error 表頭分隔線不應匹配 entry_pattern"

    # 資料列必須全數匹配
    data_rows_le = [l for l in lines_le if l.startswith("| `")]
    assert len(data_rows_le) == 2
    for row in data_rows_le:
        m = pattern.match(row)
        assert m is not None, f"session_last_error 資料列未匹配 entry_pattern: {row}"
        assert m.group("code") in ("LE_ONE", "LE_TWO")
        assert m.group("meaning").strip()
        assert m.group("action").strip()


@pytest.mark.memory_only
def test_skill_cmd_still_prints_skill(capsys: pytest.CaptureFixture[str]) -> None:
    """serialwrap skill 輸出包含 錯誤碼與 last_error 處置目錄（沿用 tests/test_skill_cmd.py 的呼叫方式）。"""
    # 測試 linux 平台資產
    rc_linux = cli._run_skill(argparse.Namespace(platform="linux"))
    out_linux = capsys.readouterr().out
    assert rc_linux == 0
    assert "錯誤碼與 last_error 處置目錄" in out_linux

    # 測試 windows 平台資產
    rc_win = cli._run_skill(argparse.Namespace(platform="windows"))
    out_win = capsys.readouterr().out
    assert rc_win == 0
    assert "錯誤碼與 last_error 處置目錄" in out_win
