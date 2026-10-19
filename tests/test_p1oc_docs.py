r"""Task P1-OC-T7：文件、SKILL 受管區段、#155 回歸 case 失敗測試（OD-14，TDD 紅燈階段）。

依據：`docs/rust-refactor/plans/02b-stage0-owner-confirmations.md` Task T7。
驗證：
1. `README.md` 的 English 章與 繁體中文 章各自包含 `DRAIN_STALLED` 與 `last_error_detail`；
2. 兩章各含 30 秒／seconds 與 `--completion-regex` 的 passthrough 說明（以章節切片後 regex `passthrough[\s\S]{0,400}30` 比對）；
3. 兩章含 `0660` 與 `SERIALWRAP_SOCKET_GROUP`，且 socket 語境不含 `0600`（OD-14 #3 還原）；
4. `docs/command-completion.md` 含 `passthrough` 小節與 `PROMPT_TIMEOUT`、`recovery_action`；
5. `python3 -m tools.contracts skill-section --namespace error_code` 輸出與兩份 SKILL 檔受管區段逐位元組相同（含 DRAIN_STALLED 且移除 passthrough 轉發；session_last_error 同）；
6. `regression/` 存在 `pt-quiet-ready` case，`docs/regression-plugin.md` 對照表含該 case。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_readme_sections() -> tuple[str, str]:
    """讀取 README.md 並切分為 (English 章節內容, 繁體中文 章節內容)。"""
    readme_path = REPO_ROOT / "README.md"
    assert readme_path.is_file(), f"找不到 README.md: {readme_path}"
    content = readme_path.read_text(encoding="utf-8")
    assert "## English" in content, "README.md 缺少 '## English' 標題"
    assert "## 繁體中文" in content, "README.md 缺少 '## 繁體中文' 標題"

    parts = content.split("## 繁體中文", 1)
    en_section = parts[0].split("## English", 1)[1]
    zh_section = parts[1]
    return en_section, zh_section


def test_readme_both_languages_mention_drain_stalled():
    """README.md 的 English 章與 繁體中文 章各自含 DRAIN_STALLED 與 last_error_detail。"""
    en_section, zh_section = _load_readme_sections()

    assert "DRAIN_STALLED" in en_section, "English 章節未包含 DRAIN_STALLED 錯誤碼說明"
    assert "last_error_detail" in en_section, "English 章節未包含 last_error_detail 欄位說明"

    assert "DRAIN_STALLED" in zh_section, "繁體中文 章節未包含 DRAIN_STALLED 錯誤碼說明"
    assert "last_error_detail" in zh_section, "繁體中文 章節未包含 last_error_detail 欄位說明"


def test_readme_passthrough_quiet_ready():
    r"""兩章各含 30 秒／seconds 與 --completion-regex 的 passthrough 說明（以章節切片後 regex passthrough[\s\S]{0,400}30 比對）。"""
    en_section, zh_section = _load_readme_sections()

    # English 章節檢查
    en_matches = list(re.finditer(r"passthrough[\s\S]{0,400}30", en_section, re.IGNORECASE))
    assert en_matches, "English 章節未找到符合 regex passthrough[\\s\\S]{0,400}30 之說明"
    has_en_quiet_ready = False
    for m in en_matches:
        window = en_section[max(0, m.start() - 200) : min(len(en_section), m.end() + 300)]
        has_seconds = bool(re.search(r"\b30\s*(?:seconds?|s\b)", window, re.IGNORECASE))
        has_regex = "--completion-regex" in window
        if has_seconds and has_regex:
            has_en_quiet_ready = True
            break
    assert has_en_quiet_ready, (
        "English 章節缺少 passthrough 30 秒靜默升 READY 與建議使用 --completion-regex 之說明"
    )

    # 繁體中文 章節檢查
    zh_matches = list(re.finditer(r"passthrough[\s\S]{0,400}30", zh_section, re.IGNORECASE))
    assert zh_matches, "繁體中文 章節未找到符合 regex passthrough[\\s\\S]{0,400}30 之說明"
    has_zh_quiet_ready = False
    for m in zh_matches:
        window = zh_section[max(0, m.start() - 200) : min(len(zh_section), m.end() + 300)]
        has_seconds = bool(re.search(r"30\s*(?:秒|s\b)", window))
        has_regex = "--completion-regex" in window
        if has_seconds and has_regex:
            has_zh_quiet_ready = True
            break
    assert has_zh_quiet_ready, (
        "繁體中文 章節缺少 passthrough 30 秒靜默升 READY 與建議使用 --completion-regex 之說明"
    )


def test_readme_socket_0660():
    """兩章含 0660 與 SERIALWRAP_SOCKET_GROUP，且不含 0600（socket 語境）。"""
    en_section, zh_section = _load_readme_sections()

    # English 章節檢查
    assert "0660" in en_section, "English 章節未包含 0660 socket 權限說明"
    assert "SERIALWRAP_SOCKET_GROUP" in en_section, "English 章節未包含 SERIALWRAP_SOCKET_GROUP 說明"
    en_socket_0600 = re.findall(
        r"(?:socket[^\n.]{0,80}0600|0600[^\n.]{0,80}socket)", en_section, re.IGNORECASE
    )
    assert not en_socket_0600, f"English 章節 socket 語境仍殘留 0600: {en_socket_0600}"

    # 繁體中文 章節檢查
    assert "0660" in zh_section, "繁體中文 章節未包含 0660 socket 權限說明"
    assert "SERIALWRAP_SOCKET_GROUP" in zh_section, "繁體中文 章節未包含 SERIALWRAP_SOCKET_GROUP 說明"
    zh_socket_0600 = re.findall(
        r"(?:socket[^\n.]{0,80}0600|0600[^\n.]{0,80}socket)", zh_section, re.IGNORECASE
    )
    assert not zh_socket_0600, f"繁體中文 章節 socket 語境仍殘留 0600: {zh_socket_0600}"


def test_command_completion_doc_passthrough_section():
    """docs/command-completion.md 含 passthrough 小節與 PROMPT_TIMEOUT、recovery_action。"""
    doc_path = REPO_ROOT / "docs" / "command-completion.md"
    assert doc_path.is_file(), f"找不到 docs/command-completion.md: {doc_path}"
    text = doc_path.read_text(encoding="utf-8")

    assert re.search(r"^#+\s*.*passthrough", text, re.MULTILINE | re.IGNORECASE), (
        "docs/command-completion.md 缺少 passthrough 小節標題"
    )
    assert "PROMPT_TIMEOUT" in text, "docs/command-completion.md 缺少 PROMPT_TIMEOUT 說明"
    assert "recovery_action" in text, "docs/command-completion.md 缺少 recovery_action 說明"


def test_skill_sections_up_to_date():
    """python3 -m tools.contracts skill-section --namespace error_code 輸出與 SKILL 檔受管區段逐位元組相同（session_last_error 同）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    skill_module = importlib.import_module("tools.contracts.skill_section")
    catalog_module = importlib.import_module("tools.contracts.catalog")

    cat = catalog_module.load_catalogs(REPO_ROOT)
    rendered_error_code = skill_module.render(cat, "error_code")
    rendered_last_error = skill_module.render(cat, "session_last_error")

    def extract_section(file_text: str, marker: str) -> str:
        pat = re.compile(
            rf'<!-- BEGIN: generated-fact marker="{re.escape(marker)}" -->\s*\n?(.*?)\n?\s*<!-- END: generated-fact marker="{re.escape(marker)}" -->',
            re.DOTALL,
        )
        match = pat.search(file_text)
        assert match is not None, f"未找到 marker=\"{marker}\" 的受管區塊標記"
        return match.group(1).strip()

    skill_files = [
        REPO_ROOT / "sw_core" / "assets" / "skill" / "SKILL.md",
        REPO_ROOT / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md",
    ]

    for sf in skill_files:
        assert sf.is_file(), f"SKILL 檔案不存在: {sf}"
        stext = sf.read_text(encoding="utf-8")

        actual_ec = extract_section(stext, "error-codes")
        assert actual_ec == rendered_error_code, (
            f"{sf.name} 的 error-codes 受管區段與 skill-section --namespace error_code 輸出不一致"
        )
        # OD-14 #4 斷言：error_code 表格必須包含 DRAIN_STALLED
        assert "DRAIN_STALLED" in actual_ec, (
            f"{sf.name} 的 error-codes 受管區段缺少 DRAIN_STALLED 錯誤碼"
        )
        # SC-34 斷言：passthrough 轉發行必須自 error_code 區塊移除
        assert "passthrough：" not in actual_ec, (
            f"{sf.name} 的 error-codes 受管區段仍殘留 passthrough 轉發說明"
        )

        actual_le = extract_section(stext, "session-last-errors")
        assert actual_le == rendered_last_error, (
            f"{sf.name} 的 session-last-errors 受管區段與 skill-section --namespace session_last_error 輸出不一致"
        )


def test_regression_case_registered():
    """regression/ 存在 pt-quiet-ready case，docs/regression-plugin.md 對照表含該 case。"""
    reg_dir = REPO_ROOT / "regression"
    assert reg_dir.is_dir(), f"找不到 regression 目錄: {reg_dir}"

    if str(reg_dir) not in sys.path:
        sys.path.insert(0, str(reg_dir))

    reg_core = importlib.import_module("serialwrap_regression.core")
    registered_cases = reg_core.load_registry()
    by_id = {c.id: c for c in registered_cases}

    assert "pt-quiet-ready" in by_id, "regression 註冊表中未找到 pt-quiet-ready case"

    case = by_id["pt-quiet-ready"]
    assert case.id == "pt-quiet-ready"

    # docs/regression-plugin.md 必須於對照表中登記該 case
    plugin_doc = (REPO_ROOT / "docs" / "regression-plugin.md").read_text(encoding="utf-8")
    assert "pt-quiet-ready" in plugin_doc, "docs/regression-plugin.md 對照表未登記 pt-quiet-ready case"
