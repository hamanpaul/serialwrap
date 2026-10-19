"""P4-T15：Agent 權限模型文件、Windows 安裝更新回滾文件與對外文件同步的紅燈測試。

計畫來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 15。

TDD 紅燈階段：`docs/rust-refactor/deploy/` 兩份文件與 README 雙語段落尚未交付，
標示 `xfail(strict=True, reason="P4-T15 紅燈")` 的測試會因缺文件或缺內容而失敗；
`--runxfail` 可看到真實失敗原因。

刻意不加 xfail 的兩項（計畫 §0.1，預期直接 PASS，須以突變檢查證明可轉紅）：
- `test_skill_windows_no_remote_not_supported_claim`：P1 T14 已移除「本期不支援 serialwrap remote」段落。
- `test_error_codes_have_skill_entries`：P0 OD-06 規則檢查器已存在，真實契約目錄應全碼通過。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "docs" / "rust-refactor" / "deploy"
PERM_MODEL = DEPLOY / "agent-permission-model.md"
WIN_INSTALL = DEPLOY / "windows-install-update-rollback.md"
README = ROOT / "README.md"
SKILL_WINDOWS = ROOT / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md"

REQUIRED_SECTIONS = (
    "## 1. 身分清單",
    "## 2. 受保護路徑",
    "## 3. Windows ACL 設定",
    "## 4. Linux 權限設定",
    "## 5. 同身分部署的限制",
    "## 6. 驗證程序",
    "## 7. 信任範圍",
)
README_TERMS = ("daemon restart", "supervision-mode set", "setup --rollback", "windows-task")


def _section(text: str, heading: str) -> str:
    """回傳指定二級標題到下一個二級標題（不含）之間的內文；標題不存在時失敗。"""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == heading]
    assert starts, f"缺少章節標題：{heading}"
    start = starts[0] + 1
    end = next((i for i in range(start, len(lines)) if re.match(r"^## ", lines[i])), len(lines))
    return "\n".join(lines[start:end])


def _load_validate():
    """延遲 import P0 的契約驗證器 `tools.contracts.validate`（OD-06 規則檢查器）。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.validate")


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason="P4-T15 紅燈")
def test_deploy_doc_has_required_sections():
    assert PERM_MODEL.is_file(), f"缺少文件：{PERM_MODEL.relative_to(ROOT).as_posix()}"
    lines = {line.strip() for line in PERM_MODEL.read_text(encoding="utf-8").splitlines()}
    missing = [h for h in REQUIRED_SECTIONS if h not in lines]
    assert not missing, f"agent-permission-model.md 缺少章節標題：{missing}"


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason="P4-T15 紅燈")
def test_deploy_doc_never_claims_same_user_isolation():
    # OD-12：兩份 deploy 文件與 README 不得宣稱「同使用者隔離」，除非同一行含「不得宣稱」。
    scanned = (PERM_MODEL, WIN_INSTALL, README)
    missing = [p.relative_to(ROOT).as_posix() for p in scanned if not p.is_file()]
    hits = []
    for path in scanned:
        if not path.is_file():
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "同使用者隔離" in line and "不得宣稱" not in line:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{no}")
    assert not missing and not hits, f"缺少文件：{missing}；宣稱同使用者隔離（OD-12 禁止）：{hits}"


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason="P4-T15 紅燈")
def test_deploy_doc_rejects_not_blocked_and_unknown_as_evidence():
    assert PERM_MODEL.is_file(), f"缺少文件：{PERM_MODEL.relative_to(ROOT).as_posix()}"
    text = PERM_MODEL.read_text(encoding="utf-8")
    s6 = _section(text, "## 6. 驗證程序")
    for token in ("not_blocked", "unknown", "不得作為"):
        assert token in s6, f"## 6. 驗證程序 缺少「{token}」（不得以 not_blocked／unknown 報告當阻斷證據）"
    s5 = _section(text, "## 5. 同身分部署的限制")
    assert "擁有者可自行恢復寫權" in s5 or "同 owner" in s5, "## 5 缺少同身分時擁有者可自行恢復寫權的說明"
    assert "not_blocked" in s5, "## 5 缺少 not_blocked 的同身分判定說明"


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.xfail(strict=True, reason="P4-T15 紅燈")
def test_readme_bilingual_mentions_new_commands():
    lines = README.read_text(encoding="utf-8").splitlines()
    en_idx = next((i for i, line in enumerate(lines) if line.strip() == "## English"), None)
    zh_idx = next((i for i, line in enumerate(lines) if line.strip() == "## 繁體中文"), None)
    assert en_idx is not None and zh_idx is not None and en_idx < zh_idx, "README 缺少 ## English 或 ## 繁體中文 段落"
    sections = {
        "English": "\n".join(lines[en_idx:zh_idx]),
        "繁體中文": "\n".join(lines[zh_idx:]),
    }
    for name, body in sections.items():
        missing = [term for term in README_TERMS if term not in body]
        assert not missing, f"README {name} 段缺少：{missing}"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_skill_windows_no_remote_not_supported_claim():
    # 不加 xfail：P1 T14 已移除該段（計畫 §0.1，預期直接 PASS）。
    assert SKILL_WINDOWS.is_file(), f"缺少資產：{SKILL_WINDOWS.relative_to(ROOT).as_posix()}"
    text = SKILL_WINDOWS.read_text(encoding="utf-8")
    assert "本期不支援 serialwrap remote" not in text, "SKILL_WINDOWS.md 仍含已移除的「本期不支援 serialwrap remote」段落"


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_error_codes_have_skill_entries():
    # 不加 xfail：P0 OD-06 規則檢查器對真實 contracts/error_codes.yaml 應全碼通過（計畫 §0.1，預期直接 PASS）。
    # run_all 會以子行程執行抽取命令，因此標 process_rpc。本 Phase 無新增碼由 test_rust_stage1b_registration.py 另驗。
    validate = _load_validate()
    findings, _summary = validate.run_all(ROOT)
    fails = [f for f in findings if f.severity == "FAIL"]
    assert fails == [], f"契約 OD-06 檢查未通過：{[(f.check, f.subject, f.message) for f in fails]}"
