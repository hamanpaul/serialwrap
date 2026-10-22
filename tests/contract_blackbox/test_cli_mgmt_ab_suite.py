"""Stage 1B 管理面全群組黑箱 A/B 套件（P4-T16）。

AB_CASES 由 tests/acceptance/p4_required.lock.json 讀取（凍結於紅燈 commit），不得以 pytest collection 反推；
對每個案例以 P2 比較器在「Python CLI＋Python daemon」與「Rust CLI＋Python daemon」兩輪依序執行
（先停 A、確認資源釋放、再起 B，新模擬器實例），斷言比較器結果 pass；rust_only 案例只跑 B 並驗絕對契約。
凍結清單中任一 pytest 節點來源不存在、適用平台不可收集、或被 SKIP／xfail（未登記）→ FAIL。

`scripts/rust/ab_blackbox_1b.py` 直接讀取 P4 凍結清單，依序跑 Python CLI 基線與 Rust CLI，
中間執行資源釋放檢查，再以既有 `ab_compare.py --required` 判定；任何未核准 skip、xfail、
失敗或證據不足都不得通過。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bb_harness import cli_prefix, repo_root

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = repo_root()
LOCK_PATH = ROOT / "tests" / "acceptance" / "p4_required.lock.json"


def _load_lock() -> dict:
    if not LOCK_PATH.is_file():
        pytest.fail(f"缺少凍結案例鎖檔 {LOCK_PATH}，無法取得 AB_CASES")
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _load_ab_cases() -> list[str]:
    """從 p4_required.lock.json 讀取凍結之案例清單，不得以 pytest collection 反推。"""
    cases = _load_lock().get("cases", [])
    case_ids = [c["case_id"] for c in cases if isinstance(c, dict) and "case_id" in c]
    if not case_ids:
        pytest.fail("凍結集合 AB_CASES 為空")
    return case_ids


def _frozen_pytest_nodes() -> list[str]:
    """凍結集合中的 pytest 節點（tests/…::…）；Rust 節點（sw-cli::…）由 cargo 驗證，不在此範圍。"""
    nodes: set[str] = set()
    for case in _load_lock().get("cases", []):
        for node in case.get("tests", []):
            if isinstance(node, str) and node.startswith("tests/") and "::" in node:
                nodes.add(node)
    return sorted(nodes)


def _pytest_node_exists_in_source(node: str) -> bool:
    """確認凍結節點存在於原始碼，避免平台 collection 排除改變凍結分母。"""
    file_part, *segments = node.split("::")
    path = ROOT / file_part
    if not path.is_file() or not segments:
        return False
    try:
        scope = ast.parse(path.read_text(encoding="utf-8")).body
    except (OSError, SyntaxError, UnicodeError):
        return False
    for segment in segments:
        name = segment.split("[", 1)[0]
        found = next(
            (
                item
                for item in scope
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        if found is None:
            return False
        scope = getattr(found, "body", [])
    return True


def _pytest_node_markers(node: str) -> set[str]:
    """讀取節點所屬 module／class／function 的 pytestmark 與 decorators。"""
    file_part, *segments = node.split("::")
    path = ROOT / file_part
    if not path.is_file():
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    owners: list[ast.AST] = [tree]
    scope = tree.body
    for segment in segments:
        name = segment.split("[", 1)[0]
        found = next(
            (
                item
                for item in scope
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        if found is None:
            return set()
        owners.append(found)
        scope = getattr(found, "body", [])

    markers: set[str] = set()
    for owner in owners:
        expressions: list[ast.AST] = list(getattr(owner, "decorator_list", []))
        for statement in getattr(owner, "body", []):
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    expressions.append(statement.value)
        for expression in expressions:
            for item in ast.walk(expression):
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.value, ast.Attribute)
                    and item.value.attr == "mark"
                ):
                    markers.add(item.attr)
    return markers


@pytest.mark.rust_only
def test_cli_mgmt_ab_suite_execution(tmp_path: Path) -> None:
    """依 P4 凍結 pytest 節點跑 Python／Rust 兩輪，並以比較器核對必要案例。"""
    runner = ROOT / "scripts" / "rust" / "ab_blackbox_1b.py"
    assert runner.is_file(), f"Stage 1B A/B 執行器不存在：{runner}"
    cli = cli_prefix()
    assert cli and Path(cli[-1]).is_file(), "Stage 1B A/B 必須以 Rust dev bundle 設定 SERIALWRAP_CLI_EXE"
    resource_report = os.environ.get("SW_RESOURCE_REPORT")
    output = (Path(resource_report).resolve().parent / "ab-suite") if resource_report else tmp_path / "ab-1b"
    if resource_report:
        assert ROOT.resolve() in output.resolve().parents, f"A/B 證據路徑須位於本 worktree：{output}"
    result = subprocess.run(
        [sys.executable, str(runner), "--cli", cli[-1], "--out", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=7200,
        check=False,
    )
    summary_path = output / "summary.json"
    assert summary_path.is_file(), result.stdout + result.stderr
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary.get("result") == "pass", summary
    assert summary.get("exit_codes", {}).get("a") == 0, summary
    assert summary.get("exit_codes", {}).get("b") == 0, summary
    classes = [row.get("class") for row in summary.get("ab_report", {}).get("cases", [])]
    assert classes and set(classes) <= {"ok", "rust-only-ok", "not-applicable"}, summary
    assert summary.get("counts", {}).get("xfail-unapproved", 0) == 0, summary
    assert summary.get("counts", {}).get("evidence-insufficient", 0) == 0, summary
    assert result.returncode == 0, result.stdout + result.stderr


def _load_ab_executor():
    """惰性載入 A/B executor：Windows staging 缺 scripts/ 時只讓用到它的測試失敗，不中止整個收集。"""
    path = ROOT / "scripts" / "rust" / "ab_blackbox_1b.py"
    if not path.is_file():
        pytest.fail(f"A/B executor 缺失，無法執行此測試：{path}")
    spec = importlib.util.spec_from_file_location("ab_blackbox_1b", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_status_junit(path: Path, nodeid: str, *, outcome: str, message: str = "") -> None:
    path_text, *segments = nodeid.split("::")
    classname = ".".join([path_text.removesuffix(".py").replace("/", "."), *segments[:-1]])
    name = segments[-1]
    suite = ET.Element("testsuite", tests="1", failures="0", errors="0", skipped="0")
    testcase = ET.SubElement(suite, "testcase", classname=classname, name=name)
    if outcome == "skip":
        suite.set("skipped", "1")
        ET.SubElement(testcase, "skipped", message=message)
    elif outcome == "xfail":
        suite.set("skipped", "1")
        ET.SubElement(testcase, "skipped", type="pytest.xfail", message=message)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def test_ab_executor_rejects_unregistered_skip_and_xfail(tmp_path: Path) -> None:
    nodeid = "tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_cases_lock_present"
    junit = tmp_path / "junit.xml"

    _write_status_junit(junit, nodeid, outcome="skip", message="rust-only contract")
    ab_executor = _load_ab_executor()
    skip_errors = ab_executor._junit_required(junit, [nodeid])
    assert skip_errors, "未登記的 rust-only skip 不可放行"

    _write_status_junit(junit, nodeid, outcome="xfail", message="P4 regression")
    xfail_errors = ab_executor._junit_required(junit, [nodeid], approved_skips={nodeid: "rust-only contract"})
    assert xfail_errors, "xfail 不得因 skip allowlist 而放行"


def test_ab_executor_accepts_only_registered_python_skip(tmp_path: Path) -> None:
    nodeid = "tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_cases_lock_present"
    junit = tmp_path / "junit.xml"
    _write_status_junit(junit, nodeid, outcome="skip", message="rust-only contract")
    ab_executor = _load_ab_executor()

    assert ab_executor._junit_required(junit, [nodeid], approved_skips={nodeid: "rust-only contract"}) == []
    assert ab_executor._junit_required(junit, [nodeid], approved_skips={})
    assert ab_executor._junit_required(junit, [nodeid], approved_skips={nodeid: "other reason"})


def test_inherited_child_flag_alone_does_not_skip_the_outer_ab_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = tmp_path / "serialwrap"
    cli.write_bytes(b"rust cli")
    monkeypatch.setenv("SW_G5_AB_CHILD_TOKEN", "a" * 32)
    monkeypatch.setenv("SW_G5_AB_PARENT_PID", str(os.getppid()))
    monkeypatch.setenv("SW_G5_AB_CHILD", "1")
    monkeypatch.delenv("SW_RESOURCE_REPORT", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "cli_prefix", lambda: [str(cli)])
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        output = Path(argv[argv.index("--out") + 1])
        output.mkdir(parents=True)
        (output / "summary.json").write_text(
            json.dumps(
                {
                    "result": "pass",
                    "exit_codes": {"a": 0, "b": 0},
                    "ab_report": {"cases": [{"class": "ok"}]},
                    "counts": {"xfail-unapproved": 0, "evidence-insufficient": 0},
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    test_cli_mgmt_ab_suite_execution(tmp_path)

    assert len(calls) == 1, "只有 A/B executor 的子輪可短路，外層必須真正啟動 executor"


def test_frozen_cases_rejects_ab_suite_recursion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """凍結節點若含外層 A/B 測試即 ValueError（取代已移除的 env 遞迴防護）。"""
    lock = tmp_path / "p4_required.lock.json"
    lock.write_text(
        json.dumps({"cases": [{"case_id": "x", "tests": [
            "tests/contract_blackbox/test_cli_mgmt_ab_suite.py::test_cli_mgmt_ab_suite_execution"]}]}),
        encoding="utf-8",
    )
    executor = _load_ab_executor()
    monkeypatch.setattr(executor, "LOCK", lock)
    with pytest.raises(ValueError, match="遞迴"):
        executor._frozen_cases()


def test_cli_mgmt_ab_cases_lock_present() -> None:
    """凍結鎖與節點來源存在；host 的平台排除不得縮小凍結分母。"""
    assert LOCK_PATH.is_file(), f"找不到凍結案例鎖檔：{LOCK_PATH}"
    data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    assert "cases" in data and len(data["cases"]) > 0
    nodes = _frozen_pytest_nodes()
    assert nodes, "凍結集合中沒有 pytest 節點"
    missing = [node for node in nodes if not _pytest_node_exists_in_source(node)]
    assert not missing, f"凍結集合節點來源不存在（missing {len(missing)}）：{missing[:10]}"
    collectable = [
        node
        for node in nodes
        if not (os.name == "nt" and "posix_only" in _pytest_node_markers(node))
    ]
    if collectable:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *collectable],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        collected = {line.strip() for line in res.stdout.splitlines() if "::" in line}
        missing_collection = [node for node in collectable if node not in collected]
        assert res.returncode == 0 and not missing_collection, (
            f"適用平台凍結節點無法收集（missing {len(missing_collection)}）："
            f"{missing_collection[:10]}\n{res.stdout[-1000:]}{res.stderr[-1000:]}"
        )


def test_existing_out_dir_is_refused_without_overwrite(tmp_path: Path) -> None:
    """既有結果目錄（含先前成功 run 的 summary.json）一律拒絕，且不寫入任何檔案。"""
    executor = _load_ab_executor()
    out = tmp_path / "ab"
    out.mkdir()
    previous = out / "summary.json"
    previous.write_text('{"result":"pass"}', encoding="utf-8")
    rc = executor.main(["--cli", str(tmp_path / "serialwrap"), "--out", str(out)])
    assert rc == 2
    assert previous.read_text(encoding="utf-8") == '{"result":"pass"}'
    assert not (out / "summary.error.json").exists()


def test_failure_writes_error_summary_never_summary_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """執行失敗只寫 summary.error.json，不得產生或覆寫 summary.json。"""
    executor = _load_ab_executor()

    def boom(**_kwargs: object) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(executor, "execute", boom)
    out = tmp_path / "new"
    rc = executor.main(["--cli", str(tmp_path / "serialwrap"), "--out", str(out)])
    assert rc == 1
    assert not (out / "summary.json").exists()
    assert json.loads((out / "summary.error.json").read_text(encoding="utf-8"))["result"] == "fail"


def test_resolve_manifest_uses_pointer_and_never_falls_back(tmp_path: Path) -> None:
    """Windows dev bundle（bin\\current-release.json 指向 versions\\<V>）可定位 manifest；都缺則 ValueError。"""
    executor = _load_ab_executor()
    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    cli = bundle / "bin" / "serialwrap.exe"
    cli.write_bytes(b"x")
    version_dir = bundle / "versions" / "9.9.9-test"
    version_dir.mkdir(parents=True)
    manifest = version_dir / "release-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    (bundle / "bin" / "current-release.json").write_text(
        json.dumps({"dir": "versions\\9.9.9-test"}), encoding="utf-8"
    )
    assert executor.resolve_manifest(cli) == manifest
    manifest.unlink()
    with pytest.raises(ValueError, match="release-manifest"):
        executor.resolve_manifest(cli)


def test_registered_python_skip_is_approved_only_for_rust_only_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一登記案例內，只有 rust_only 節點可被核准為 Python 輪 skip；共用節點即使被 skip 也必須判失敗。"""
    executor = _load_ab_executor()
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / "t_mixed.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.rust_only\n"
        "def test_rust_side():\n"
        "    pass\n\n\n"
        "def test_shared():\n"
        "    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(executor, "ROOT", fake_root)
    rust_node = "t_mixed.py::test_rust_side"
    shared_node = "t_mixed.py::test_shared"
    selected = [rust_node, shared_node]
    lock_cases = [{"case_id": "mixed", "tests": selected}]
    case_map = [{"case_id": "mixed", "skip_registered": {"python": "rust-only 契約（behavior_changes 已登記）"}}]

    approved = executor._approved_python_skips(lock_cases, case_map, "linux", selected)
    assert approved == {rust_node: "rust-only contract"}

    junit = tmp_path / "junit.xml"
    _write_status_junit(junit, shared_node, outcome="skip", message="rust-only contract")
    assert executor._junit_required(junit, [shared_node], approved_skips=approved), "共用節點 skip 不得因案例登記而放行"

    _write_status_junit(junit, rust_node, outcome="skip", message="rust-only contract")
    assert executor._junit_required(junit, [rust_node], approved_skips=approved) == []
    assert executor._junit_required(junit, [rust_node], approved_skips={}), "未登記的 rust_only skip 仍須失敗"

    _write_status_junit(junit, rust_node, outcome="xfail", message="P4 regression")
    assert executor._junit_required(junit, [rust_node], approved_skips=approved), "xfail 不得因登記而放行"


def test_frozen_registry_keeps_shared_doctor_node_out_of_python_skip_allowlist() -> None:
    """真實凍結資料：sc25 的共用節點不得進入 Python skip allowlist，執行器也不得因此拋出 ValueError。"""
    executor = _load_ab_executor()
    lock_cases, nodes = executor._frozen_cases()
    case_map = executor._frozen_case_map(lock_cases)
    selected = executor._applicable_nodes(nodes, "linux")
    approved = executor._approved_python_skips(lock_cases, case_map, "linux", selected)
    shared = "tests/contract_blackbox/test_cli_mgmt_doctor.py::test_doctor_ab_shared_checks"
    assert shared in selected
    assert shared not in approved
    assert approved and all("rust_only" in executor._markers(node) for node in approved)


def test_outer_ab_run_fails_when_executor_leaves_no_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外層 A/B 測試不得因子行程回傳 0 而通過；執行器未產生 summary.json 即失敗（不得零執行器產出即 PASS）。"""
    cli = tmp_path / "serialwrap"
    cli.write_bytes(b"rust cli")
    monkeypatch.setattr(sys.modules[__name__], "cli_prefix", lambda: [str(cli)])
    monkeypatch.delenv("SW_RESOURCE_REPORT", raising=False)
    calls: list[list[str]] = []

    def silent_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", silent_run)
    with pytest.raises(AssertionError):
        test_cli_mgmt_ab_suite_execution(tmp_path)
    assert len(calls) == 1
