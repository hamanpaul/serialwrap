from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
import yaml

# 模組層標註 core_portable，檔案文字檢查標 memory_only，subprocess 標 process_rpc
pytestmark = [pytest.mark.core_portable]

ROOT = Path(__file__).resolve().parents[1]


def _has_ast_node(tree: ast.AST, node_path: str) -> bool:
    """以 AST 檢查檔案中是否存在指定的節點路徑。"""
    if "::" in node_path:
        segments = node_path.split("::")
    elif "." in node_path:
        segments = node_path.split(".")
    else:
        segments = [node_path]

    curr_nodes = [tree]
    for seg in segments:
        next_nodes = []
        for parent in curr_nodes:
            for child in getattr(parent, "body", []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name == seg:
                    next_nodes.append(child)
                elif isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == seg:
                            next_nodes.append(child)
                elif isinstance(child, ast.AnnAssign):
                    if isinstance(child.target, ast.Name) and child.target.id == seg:
                        next_nodes.append(child)
        curr_nodes = next_nodes
        if not curr_nodes:
            return False
    return True


@pytest.mark.memory_only
def test_skill_chunk_size_512():
    """驗證 SKILL.md 的 chunk-size 預設已更正為 512，且 SKILL_WINDOWS.md 未誤植。"""
    skill_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    skill_win_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md").read_text(encoding="utf-8")
    assert "--chunk-size` 預設 2048" not in skill_text
    assert "預設 512" in skill_text
    assert "預設 2048" not in skill_win_text


@pytest.mark.memory_only
def test_skill_pull_writes_daemon_cwd():
    """驗證 SKILL.md 的 pull 說明更正為寫到 daemon 工作目錄並附 local_path。"""
    skill_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    skill_win_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL_WINDOWS.md").read_text(encoding="utf-8")
    assert "回傳檔案內容" not in skill_text
    assert "local_path" in skill_text
    assert "回傳檔案內容" not in skill_win_text


@pytest.mark.memory_only
def test_skill_events_not_tmpfs():
    """驗證 SKILL.md 的 event 說明確認 counter/event 非 tmpfs 且指向 STATE_DIR/events。"""
    skill_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "tmpfs 除外" not in skill_text
    assert "STATE_DIR/events" in skill_text


@pytest.mark.memory_only
def test_readme_rules_json_only():
    """驗證 README.md 不含「規則為 JSON/YAML 檔」（規則僅支援 JSON）。"""
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "規則為 JSON/YAML 檔" not in readme_text


@pytest.mark.memory_only
def test_readme_stdin_fields():
    """驗證 README.md 中 event stdin payload 欄位說明不含 trigger_ts。"""
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "trigger_ts" not in readme_text


@pytest.mark.memory_only
def test_claude_md_print_sort_keys():
    """驗證 CLAUDE.md「JSON 輸出必須維持緊湊且穩定」段落中 CLI 描述包含 sort_keys=True。"""
    claude_text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    match = re.search(r"###\s*JSON\s*輸出[^\n]*\n(.*?)(?=\n###|\Z)", claude_text, re.DOTALL)
    assert match is not None, "未找到 CLAUDE.md 的「JSON 輸出」段落"
    section = match.group(1)
    assert "CLI" in section
    cli_lines = [line for line in section.splitlines() if "CLI" in line]
    assert any("sort_keys=True" in line for line in cli_lines), (
        "CLAUDE.md「JSON 輸出」段中針對 CLI 的描述必須包含 sort_keys=True"
    )


@pytest.mark.memory_only
def test_conftest_docstring_13_files():
    """驗證 tests/conftest.py 的 docstring 已將 8 檔更正為 13 檔。"""
    conftest_text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "8 檔" not in conftest_text
    assert "13 檔" in conftest_text


@pytest.mark.memory_only
def test_file_transfer_comment_updated():
    """驗證 sw_core/file_transfer.py 註解中已移除舊的 ≈ 741 開銷估計。"""
    file_transfer_text = (ROOT / "sw_core" / "file_transfer.py").read_text(encoding="utf-8")
    assert "≈ 741" not in file_transfer_text
    assert "781～798" in file_transfer_text
    assert "97～114" in file_transfer_text


@pytest.mark.memory_only
def test_spec_errata_covers_assigned_rows():
    """驗證 docs/rust-refactor/spec-errata.md 涵蓋所有指派的規格勘誤列 ID。"""
    errata_path = ROOT / "docs" / "rust-refactor" / "spec-errata.md"
    assert errata_path.is_file(), f"{errata_path} 不存在"
    content = errata_path.read_text(encoding="utf-8")
    assigned_ids = [
        "CLI-11.01", "CLI-11.03", "CLI-11.11", "CLI-11.13",
        "RPC-D-03", "RPC-D-04", "RPC-D-09", "RPC-D-12",
        "ERR-7.3-3", "WAL-D1", "WAL-D4", "UIO-12.7",
        "TST-D-1", "TST-D-2", "TST-D-3",
        "EVT-D-S2", "EVT-D-S8", "EVT-D-S9",
        "GAP-X1", "GAP-X2", "GAP-X3", "GAP-X4",
    ]
    missing = [row_id for row_id in assigned_ids if row_id not in content]
    assert not missing, f"spec-errata.md 缺少以下調和表 ID：{missing}"


@pytest.mark.process_rpc
def test_spec_final_untouched():
    """確保勘誤作業未直接修改 serialwrap-rust-spec-final.md 定稿本文。"""
    res = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "docs/rust-refactor/serialwrap-rust-spec-final.md"],
        cwd=ROOT,
        capture_output=True,
    )
    assert res.returncode == 0, (
        "serialwrap-rust-spec-final.md 被修改（勘誤應記錄於 spec-errata.md，不得改動定稿本文）"
    )


@pytest.mark.memory_only
def test_p0_rows_all_covered():
    """解析調和表所有表格列，驗證所有標 P0 之列皆登錄於 contracts/p0_row_coverage.yaml 且指向有效凍結位置。"""
    reconciliation_path = ROOT / "docs" / "rust-refactor" / "plans" / "spec-reconciliation.md"
    assert reconciliation_path.is_file(), f"{reconciliation_path} 不存在"

    content = reconciliation_path.read_text(encoding="utf-8")
    id_pattern = re.compile(r"^[A-Z]{2,4}-")
    p0_row_ids = set()

    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        row_id = cells[0]
        if id_pattern.match(row_id):
            stage = cells[-2]
            if "P0" in stage:
                p0_row_ids.add(row_id)

    assert p0_row_ids, "parse found no P0 rows（調和表表格格式可能已變動；空集合不得視為通過）"

    coverage_path = ROOT / "contracts" / "p0_row_coverage.yaml"
    assert coverage_path.is_file(), f"{coverage_path} 不存在（請建立該檔案）"

    coverage_data = yaml.safe_load(coverage_path.read_text(encoding="utf-8"))
    assert isinstance(coverage_data, dict), "p0_row_coverage.yaml 頂層必須為 mapping"
    rows = coverage_data.get("rows", [])
    assert isinstance(rows, list), "p0_row_coverage.yaml 的 rows 必須為 list"

    covered_rows = {}
    for item in rows:
        assert isinstance(item, dict) and "id" in item, f"row 格式不正確：{item}"
        covered_rows[item["id"]] = item

    uncovered = p0_row_ids - set(covered_rows.keys())
    assert not uncovered, f"調和表中有以下 P0 列未在 p0_row_coverage.yaml 中登錄：{sorted(uncovered)}"

    for row_id in sorted(p0_row_ids):
        row_data = covered_rows[row_id]
        frozen_in = row_data.get("frozen_in", [])
        assert isinstance(frozen_in, list) and frozen_in, (
            f"列 {row_id} 的 frozen_in 不得為空且必須為 list"
        )
        for target in frozen_in:
            assert isinstance(target, str) and target.strip(), (
                f"列 {row_id} 的 frozen_in 目標必須為非空字串：{target}"
            )
            target = target.strip()
            if "#" in target:
                path_str, anchor = target.split("#", 1)
                p = ROOT / path_str
                assert p.is_file(), f"列 {row_id} 的 frozen_in 目標檔案不存在：{p}"
                if path_str == "docs/rust-refactor/spec-errata.md":
                    assert anchor == row_id, f"列 {row_id} 的 spec-errata 錨點必須為本列 ID，實際 {anchor}"
                target_content = p.read_text(encoding="utf-8")
                assert re.search(
                    r"^#+\s+.*" + re.escape(anchor) + r"(?![0-9A-Za-z.\-])", target_content, re.MULTILINE
                ), (
                    f"列 {row_id} 的 frozen_in 目標 {target} 在 {path_str} 中未找到含 {anchor} 的標題"
                )
            elif "::" in target:
                path_str, node_path = target.split("::", 1)
                p = ROOT / path_str
                assert p.is_file(), f"列 {row_id} 的 frozen_in 目標檔案不存在：{p}"
                tree = ast.parse(p.read_text(encoding="utf-8"))
                assert _has_ast_node(tree, node_path), (
                    f"列 {row_id} 的 frozen_in 目標 {target} 在 {path_str} 中未找到 AST 節點 {node_path}"
                )
            else:
                p = ROOT / target
                assert p.exists(), f"列 {row_id} 的 frozen_in 目標檔案或路徑不存在：{p}"


@pytest.mark.memory_only
def test_skill_all_selector_caveat():
    """EVT-D-D11：SKILL 不得宣稱 ALL 規則重啟後涵蓋動態／熱插 COM，須載明 ALL 只在啟動時展開。"""
    skill_text = (ROOT / "sw_core" / "assets" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "的規則在 daemon 重啟後自動啟用。" not in skill_text
    assert "只在 daemon 啟動時展開到當下已知 COM" in skill_text


@pytest.mark.memory_only
def test_design_event_drop_and_counter_statements():
    """EVT-D-D6～D8、D5 與設計文件 L32／L43／L144／L218／L381／L403／L411 勘誤的回歸。"""
    text = (ROOT / "docs" / "design-event-trigger.md").read_text(encoding="utf-8")
    # dropped_count 只允許出現在「（無 `dropped_count`）」的否定敘述中一次
    assert "（無 `dropped_count`）" in text
    assert text.count("dropped_count") == 1
    assert "drop_oldest 觸發" not in text
    assert "`per_rule_busy`／`per_daemon_saturated` 為丟棄新 fire" in text
    assert "並行上限 + 飽和時丟棄新 fire" in text
    assert "不做跨 host reboot 的 counter 持久化" not in text
    assert "/tmp/serialwrap/events" not in text
    assert "新增 alias 後須 reload 才生效" not in text
    assert "新增 COM 後須 `event reload` 才會被涵蓋" not in text


@pytest.mark.memory_only
def test_spec_transport_windows_loopback_tcp():
    """RPC-D-14：serialwrap-spec §4.3 須區分 POSIX（僅 Unix socket）與 Windows（loopback TCP bind）。"""
    text = (ROOT / "docs" / "serialwrap-spec.md").read_text(encoding="utf-8")
    assert "POSIX 上，`tcp://...`" in text
    assert "Windows daemon 本體直接 listen loopback TCP（預設 48700，`SERIALWRAP_TCP_PORT` 可改）" in text
    assert "Windows 僅接受 loopback `tcp://`" in text
    assert "`daemon start` 只支援本機 socket，不支援 `--endpoint`" not in text


@pytest.mark.memory_only
def test_claude_md_errata_statements():
    """EVT-D-D9／WAL-D16、SES-16.6-01、PLT-D-02 的 CLAUDE.md 勘誤回歸。"""
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "動態偵測 session 只用 template `log_dir`，不套用 `defaults.log_dir`" in text
    assert "是否 probe 由 `ready_probe` 決定" in text
    assert "隔離實例必須**同時**設 `SERIALWRAP_BY_ID_DIR` 與 `SERIALWRAP_BY_PATH_DIR`" in text
