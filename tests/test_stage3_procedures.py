"""P9-T17：Stage 3 擁有者程序文件的失敗測試（TDD red）。"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "docs" / "rust-refactor" / "procedures"
DOCS = [
    "stage3-runbook.md",
    "sequential-ab.md",
    "canary.md",
    "rollback-drill.md",
    "python-daemon-retirement.md",
    "evidence-index.md",
]  # od09-deferred-freeze.md 屬 P2 Task 16（命令為 realhw.acceptance.od09_freeze），不在本表


def _read(name: str) -> str:
    return (PROC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", DOCS)
def test_commands_parse(doc):
    from tools.stage3.__main__ import build_parser

    text = _read(doc)
    cmds = [
        ln.strip()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        for ln in block.splitlines()
        if ln.strip().startswith("python3 -m tools.stage3 ")
    ]
    assert cmds, doc
    for c in cmds:
        argv = shlex.split(c)[3:]
        build_parser().parse_args(argv)


def test_runbook_marks_every_blocked_owner_item():
    text = _read("stage3-runbook.md")
    for n in range(1, 19):
        assert f"【BLOCKED-OWNER BO-P9-{n:02d}】" in text


def test_runbook_order():
    text = _read("stage3-runbook.md")
    order = [
        "BO-P9-12", "BO-P9-06", "BO-P9-17", "BO-P9-01", "BO-P9-02", "BO-P9-18",
        "BO-P9-04", "BO-P9-05", "BO-P9-07", "BO-P9-10", "BO-P9-09", "BO-P9-13",
    ]
    pos = [text.index(f"【BLOCKED-OWNER {k}】") for k in order]
    assert pos == sorted(pos)


def test_od09_freeze_timed_at_stage0_exit():
    for doc in ("stage3-runbook.md", "od09-deferred-freeze.md"):
        assert "S1B-L3-MIXED-48H-WINDOWS" in _read(doc), doc
    od09 = _read("od09-deferred-freeze.md")  # P2 Task 16 交付（R4-F2）
    assert "python3 -m realhw.acceptance.od09_freeze freeze" in od09
    assert "python3 -m tools.stage3 od09" not in od09  # Stage 0 程序不得依賴本 Phase 工具
    runbook = _read("stage3-runbook.md")
    assert "realhw.acceptance.od09_freeze" in runbook
    assert "tools.stage3 od09 freeze" not in runbook


def test_od12_single_user_and_no_isolation_claim():
    assert "單人使用" in _read("canary.md")
    for doc in DOCS:
        text = _read(doc)
        assert "同使用者隔離" not in text or "不得宣稱「同使用者隔離」" in text, doc
        for ln in text.splitlines():
            if "Defender" in ln:
                assert "關閉" in ln, (doc, ln)  # OD-05：代表機台為第三方 EDR、Defender 關閉


def test_no_interactive_status_in_procedures():
    files = [PROC / d for d in DOCS + ["od09-deferred-freeze.md"]]
    files += sorted((ROOT / "tools" / "stage3").glob("*.py"))
    for path in files:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if "interactive-status" in ln or "interactive_status" in ln:
                assert "不得呼叫" in ln, (path, ln)


def test_retirement_procedure_verifies_shipped_bytes():
    text = _read("python-daemon-retirement.md")
    blocks = "\n".join(re.findall(r"```bash\n(.*?)```", text, re.S))
    marks = [
        "python3 -m tools.stage3 retirement verify --mode release",
        "python3 -m tools.stage3 retirement pack",
        "python3 -m tools.stage3 retirement verify-archive",
    ]
    pos = [blocks.index(m) for m in marks]
    assert pos == sorted(pos)
    assert "Compress-Archive" not in text
    for ln in blocks.splitlines():
        assert not ln.strip().startswith("tar "), ln

    runbook = _read("stage3-runbook.md")
    for ln in runbook.splitlines():
        if "python3 -m tools.stage3 evidence target" in ln and "--stage 3" in ln:
            assert "--bundle-dir" in ln, ln


# ---- P9-T17 審查後補強（R-A3、A5、A8）----

# 各文件中需擁有者步驟的行首標示（計畫 §0.2 BO 表）；`stage3-runbook.md` 另由 test_runbook_marks_every_blocked_owner_item 全覆蓋。
OWNER_MARKERS = {
    "sequential-ab.md": ["BO-P9-01", "BO-P9-02", "BO-P9-03", "BO-P9-04", "BO-P9-05", "BO-P9-15", "BO-P9-16"],
    "canary.md": ["BO-P9-06", "BO-P9-07", "BO-P9-08", "BO-P9-09"],
    "rollback-drill.md": ["BO-P9-10", "BO-P9-11"],
    "python-daemon-retirement.md": ["BO-P9-13", "BO-P9-17"],
    "evidence-index.md": ["BO-P9-14"],
}


def _bash_commands(doc: str) -> list[str]:
    text = _read(doc)
    return [
        ln.strip()
        for block in re.findall(r"```bash\n(.*?)```", text, re.S)
        for ln in block.splitlines()
        if ln.strip().startswith("python3 -m tools.stage3 ")
    ]


def test_retirement_commands_parse_with_retirement_parser():
    # `__main__.build_parser` 將 retirement 以 REMAINDER 掛載，故 retirement 行改以其真實解析器驗證（R-A7）。
    from tools.stage3 import retirement

    seen = 0
    for doc in DOCS:
        for command in _bash_commands(doc):
            argv = shlex.split(command)[3:]
            if argv[:1] == ["retirement"]:
                retirement.build_parser().parse_args(argv)
                seen += 1
    assert seen >= 5


def test_owner_markers_at_line_start():
    # 每個需擁有者步驟以行首 `【BLOCKED-OWNER BO-P9-xx】` 標示（R-A8b）；行中提及 BO 編號不算標示。
    for doc, ids in OWNER_MARKERS.items():
        lines = _read(doc).splitlines()
        for bo in ids:
            assert any(ln.startswith(f"【BLOCKED-OWNER {bo}】") for ln in lines), (doc, bo)


def test_no_interactive_status_hyphen_form():
    # 計畫 Task 17 Step 1：七份文件與 tools/stage3 皆不得出現 `interactive-status` 字串（R-A8a）。
    files = [PROC / d for d in DOCS + ["od09-deferred-freeze.md"]]
    files += sorted((ROOT / "tools" / "stage3").glob("*.py"))
    for path in files:
        assert "interactive-status" not in path.read_text(encoding="utf-8"), path


def test_windows_zipapp_commands_parse_with_collector_parser():
    # Windows 工作站的 sw-canary.pyz 參數與 tools.stage3 不同（canary.md §6～7）；以 collector 自己的解析器驗證。
    from tools.stage3 import collector_main

    seen = 0
    for doc in DOCS:
        for block in re.findall(r"```powershell\n(.*?)```", _read(doc), re.S):
            for ln in block.splitlines():
                if ln.strip().startswith("py -3 ") and "sw-canary.pyz" in ln:
                    tail = ln.split("sw-canary.pyz", 1)[1]
                    collector_main._parser().parse_args(shlex.split(tail))
                    seen += 1
    assert seen >= 4


# ---- P9-T17 審查二次補強：runbook 覆蓋率與文件對齊 ----


def _runbook_commands(prefix: str) -> list[list[str]]:
    """runbook 所有 bash 區塊中以 `prefix` 開頭的命令，回傳 shlex 切分後的 argv。"""
    text = _read("stage3-runbook.md")
    out: list[list[str]] = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for ln in block.splitlines():
            if ln.startswith(prefix):
                out.append(shlex.split(ln))
    return out


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_runbook_ingests_every_stage3_item():
    # 每個 Stage 3 出口項（both 則兩個平台）皆須有一行 evidence ingest，且 --kind 與目錄的 evidence_kind 相同。
    catalog = json.loads((ROOT / "tests" / "acceptance" / "stage_exits.json").read_text(encoding="utf-8"))
    ingested = {
        (_opt(argv, "--item"), _opt(argv, "--platform"), _opt(argv, "--kind"))
        for argv in _runbook_commands("python3 -m tools.stage3 evidence ingest")
    }
    missing = []
    for item in catalog["items"]:
        if item["stage"] != "3":
            continue
        platforms = ("linux", "windows") if item["platform"] == "both" else (item["platform"],)
        for platform in platforms:
            if (item["item_id"], platform, item["evidence_kind"]) not in ingested:
                missing.append((item["item_id"], platform))
    assert not missing, missing


def test_runbook_status_covers_all_stages():
    from tools.stage3.evidence_index import STAGES  # noqa: PLC0415

    seen = {_opt(argv, "--stage") for argv in _runbook_commands("python3 -m tools.stage3 evidence status")}
    assert set(STAGES) <= seen, set(STAGES) - seen


def test_runbook_targets_cover_all_stages():
    from tools.stage3.evidence_index import STAGES  # noqa: PLC0415

    seen = {_opt(argv, "--stage") for argv in _runbook_commands("python3 -m tools.stage3 evidence target")}
    assert set(STAGES) <= seen, set(STAGES) - seen


def test_runbook_runs_rollback_drill_on_both_platforms():
    platforms = {_opt(argv, "--platform") for argv in _runbook_commands("python3 -m tools.stage3 rollback-drill run")}
    assert {"linux", "windows"} <= platforms, platforms


def test_retirement_doc_matches_release_workflow():
    # 檔名與 `.github/workflows/release.yml` 一致（SHA256SUMS-<平台>），且記錄上傳前的重驗步驟（R-A8）。
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    text = _read("python-daemon-retirement.md")
    for name in ("SHA256SUMS-linux", "SHA256SUMS-windows"):
        assert name in workflow and name in text, name
    assert "SHA256SUMS.linux" not in text and "SHA256SUMS.windows" not in text
    assert "sha256sum -c" in text and "Get-FileHash" in text


def test_rollback_drill_start_fail_source_points_at_manifest_dir():
    # D8 來源的 release-manifest.json 位於 versions/<版本>；指向 --out 根目錄會在 D0 被拒（R-A3）。
    text = _read("rollback-drill.md")
    assert 'START_FAIL_SRC="$START_FAIL_OUT/versions/$NEWER_VERSION"' in text
    assert "設為 `--out` 加上 `-dev` 後綴" not in text  # 舊版錯誤說法（mkbundle.sh 不加後綴）
    assert "CURRENT_BUNDLE=" in text


def test_sequential_ab_infra_error_not_sticky_wording():
    text = _read("sequential-ab.md")
    assert "不會被後續 PASS 抵銷，但 INFRA_ERROR" not in text
    assert "只有目前 target 下有效的 FAIL" in text


# ---- P9-T17 審查補強（R-A3、R-A6、R-B2）：revision 變數、target 警告、L1 junit 命令與 daemon_live 收集 ----

_OLD_STAGES = ("0", "1A", "1B", "2.1", "2.2", "2.3", "2.4")


def _stage_var(stage: str) -> str:
    return "STAGE" + stage.replace(".", "")


def test_runbook_old_stage_targets_use_stage_scoped_revisions():
    # 階段 0～2.4 的 target 不得以目前 HEAD 派生的 `$CONTRACT_REV`／`$TOOL_REV`／`$CASE_LIST_SHA256` 設定（R-A3）。
    targets = _runbook_commands("python3 -m tools.stage3 evidence target")
    checked = 0
    for argv in targets:
        stage = _opt(argv, "--stage")
        if stage not in _OLD_STAGES:
            continue
        prefix = _stage_var(stage)
        assert _opt(argv, "--contract-revision") == f"${prefix}_CONTRACT_REV", argv
        assert _opt(argv, "--tool-revision") == f"${prefix}_TOOL_REV", argv
        assert _opt(argv, "--case-list-sha256") == f"${prefix}_CASE_LIST_SHA256", argv
        checked += 1
    assert checked == 2 * len(_OLD_STAGES)


def test_runbook_stage3_target_uses_head_derived_revisions():
    stage3 = [argv for argv in _runbook_commands("python3 -m tools.stage3 evidence target") if _opt(argv, "--stage") == "3"]
    assert len(stage3) == 2
    for argv in stage3:
        assert _opt(argv, "--contract-revision") == "$CONTRACT_REV", argv
        assert _opt(argv, "--tool-revision") == "$TOOL_REV", argv


def test_runbook_warns_target_once_and_freezes_head_during_stage3_ab():
    text = _read("stage3-runbook.md")
    assert "同一階段的 target 只設定一次" in text
    assert "自 `evidence target --stage 3` 起" in text
    assert "不得 commit、checkout 或 rebase" in text


def test_runbook_l1_junit_command_sets_resource_report_and_ingests_junit_xml():
    text = _read("stage3-runbook.md")
    assert 'SW_RESOURCE_REPORT="$JUNIT_LINUX_DIR/resource-report.json"' in text
    assert 'SERIALWRAP_DAEMON_EXE="$LINUX_BUNDLE/bin/serialwrapd"' in text
    assert "tests/contract_blackbox" in text
    junit = [argv for argv in _runbook_commands("python3 -m tools.stage3 evidence ingest") if _opt(argv, "--kind") == "junit"]
    assert junit and all(str(_opt(argv, "--report")).endswith("junit.xml") for argv in junit)


def test_evidence_index_l1_command_and_manual_daemon_live_procedure():
    text = _read("evidence-index.md")
    assert "SERIALWRAP_DAEMON_EXE=" in text and "SW_RESOURCE_REPORT=" in text
    assert "tests/contract_blackbox" in text
    assert "--daemon-pid" in text and "daemon_live" in text
    assert "live-daemon.json" in text


def test_evidence_index_report_sources_match_producers():
    text = _read("evidence-index.md")
    rows = {ln.split("|")[1].strip(): ln for ln in text.splitlines() if ln.startswith("| `")}
    functest = rows["`functest-report`"]
    assert "rglob" in functest and "不使用 `SW_RESOURCE_REPORT`" in functest
    bridge = rows["`bridge-selftest`"]
    assert "uart_stdio_bridge.selftest.runner" in bridge and "func-test/runner.py" not in bridge
