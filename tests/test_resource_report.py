"""Task P2-T13: pytest 資源報告、平台 SKIP 閘門與 Windows collection 測試。

依據 Stage 0 測試基礎計畫 Task 13 規範（TST-D-4、R5-F3、R5-F4），驗證：
- 模組層 pytestmark AST 解析與 Windows 排除判定
- core_portable 平台 SKIP 閘門（升級 exit 1）
- 資源與平台計數彙總報告（含 not_collected_platform）
- deployed_content 測量欄位（R5-F4）
- l1-results.v1 格式欄位與執行結果收集（R5-F3）
- 既有 conftest hook 單一定義守衛
- GitHub workflow Windows 相關 jobs
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable]


@pytest.mark.memory_only
def test_platform_skip_regex_matches_p0() -> None:
    """resource_report.PLATFORM_SKIP_RE 與 P0 test_resource_markers.py 中的平台條件 regex 字串相等。"""
    from tests.test_resource_markers import PLATFORM_COND
    import tests.resource_report as resource_report

    assert resource_report.PLATFORM_SKIP_RE == PLATFORM_COND.pattern


@pytest.mark.memory_only
def test_module_platform_marks_ast(tmp_path: Path) -> None:
    """對暫存檔 pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty] 回 {"posix_only"}；

    只有 class 層 marker 的檔回空集合；不 import 該檔（檔內含 raise SystemExit 也不觸發）。
    """
    import tests.resource_report as resource_report

    f1 = tmp_path / "test_sample1.py"
    f1.write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    assert resource_report.module_platform_marks(f1) == {"posix_only"}

    f2 = tmp_path / "test_sample2.py"
    f2.write_text(
        "import pytest\n"
        "class TestSample:\n"
        "    pytestmark = [pytest.mark.posix_only]\n"
        "    def test_one(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert resource_report.module_platform_marks(f2) == set()


@pytest.mark.memory_only
def test_should_ignore_on_windows(tmp_path: Path) -> None:
    """模組層 posix_only → True；core_portable／混合平台（模組層無平台 marker）→ False。"""
    import tests.resource_report as resource_report

    f_posix = tmp_path / "test_posix.py"
    f_posix.write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.posix_only]\n",
        encoding="utf-8",
    )
    assert resource_report.should_ignore_on_windows(f_posix) is True

    f_core = tmp_path / "test_core.py"
    f_core.write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable]\n",
        encoding="utf-8",
    )
    assert resource_report.should_ignore_on_windows(f_core) is False

    f_mixed = tmp_path / "test_mixed.py"
    f_mixed.write_text(
        "import pytest\n"
        "# 模組層無平台 marker\n"
        "def test_foo(): pass\n",
        encoding="utf-8",
    )
    assert resource_report.should_ignore_on_windows(f_mixed) is False


@pytest.mark.process_rpc
def test_windows_collection_excludes_posix_modules(tmp_path: Path) -> None:
    """子程序以 monkeypatch os.name='nt' 的 conftest 外掛（-p 載入測試用 plugin 設定 resource_report._FORCE_OS='nt'）

    跑 pytest --collect-only -q tests/test_multiagent_e2e.py tests/test_wal.py：
    無 collection error；報告 not_collected_platform 含 tests/test_multiagent_e2e.py。
    """
    report_file = tmp_path / "resource_report.json"
    plugin_file = tmp_path / "force_nt_plugin.py"
    plugin_file.write_text(
        "try:\n"
        "    import tests.resource_report as rr\n"
        "    rr._FORCE_OS = 'nt'\n"
        "except ImportError:\n"
        "    pass\n",
        encoding="utf-8",
    )
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "force_nt_plugin",
        "tests/test_multiagent_e2e.py",
        "tests/test_wal.py",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), os.getcwd()])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert res.returncode == 0, f"pytest collect failed: {res.stdout}\n{res.stderr}"

    assert report_file.exists(), f"SW_RESOURCE_REPORT not written: {report_file}"
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert "tests/test_multiagent_e2e.py" in data.get("not_collected_platform", [])


@pytest.mark.process_rpc
def test_core_portable_platform_skip_fails_session(tmp_path: Path) -> None:
    """在 tmp_path 建含 conftest 副本與一個模組層 core_portable＋memory_only、

    函式帶 @pytest.mark.skipif(sys.platform=='win32', reason='x') 的檔：
    session exit 1、報告 core_portable_platform_skips 含該 nodeid。
    """
    test_dir = tmp_path / "sub"
    test_dir.mkdir()
    conftest_src = Path("tests/conftest.py").read_text(encoding="utf-8")
    (test_dir / "conftest.py").write_text(conftest_src, encoding="utf-8")
    (test_dir / "test_sample.py").write_text(
        "import pytest, sys\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
        "def test_bad():\n"
        "    pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "report.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_dir / "test_sample.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 1, f"Expected exit 1, got {res.returncode}: {res.stdout}\n{res.stderr}"
    assert report_file.exists(), "Report file should exist"
    data = json.loads(report_file.read_text(encoding="utf-8"))
    skips = data.get("core_portable_platform_skips", [])
    assert any("test_bad" in s for s in skips), f"Expected test_bad in skips: {skips}"


@pytest.mark.memory_only
def test_reason_text_not_used() -> None:
    """core_portable 函式以非平台條件 skip（skipif(True, reason='windows docs pending')）

    → 不列入 core_portable_platform_skips（以結構而非 reason 文字判定）。
    """
    import tests.resource_report as resource_report

    mark_non_platform = pytest.mark.skipif(True, reason="windows docs pending")
    assert resource_report.is_platform_skip([mark_non_platform]) is False

    mark_platform = pytest.mark.skipif("sys.platform == 'win32'", reason="windows docs pending")
    assert resource_report.is_platform_skip([mark_platform]) is True

    records = [
        {
            "nodeid": "tests/test_x.py::test_foo",
            "platform": "core_portable",
            "resource": "memory_only",
            "outcome": "skipped",
            "is_platform_skip": False,
        }
    ]
    summary = resource_report.summarize(records, ignored=[], platform="linux")
    assert summary.get("core_portable_platform_skips") == []


@pytest.mark.process_rpc
def test_resource_report_written(tmp_path: Path) -> None:
    """子程序以 SW_RESOURCE_REPORT=<tmp>/r.json 跑 tests/test_wal.py，報告 counts.core_portable.passed > 0。"""
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"pytest failed: {res.stdout}\n{res.stderr}"
    assert report_file.exists(), f"Report file not written: {report_file}"
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["counts"]["core_portable"]["passed"] > 0


def _install_layout(tmp_path: Path, tag: str) -> dict:
    """建立最小安裝樹並回傳路徑與摘要（R5-F4；Task 10 測試的最小安裝）。

    POSIX：`inst/bin/serialwrap`、`inst/bin/serialwrapd`、旁載 `inst/bin/version.dll`，入口由 `inst/bin` 直接執行。
    Windows（`os.name == "nt"`，realhw/deployed_content.py 的 current-release sidecar 規則）：
    穩定入口目錄 `stable/` 僅含 `serialwrap.exe`、`serialwrapd.exe`、`current-release.json`（指向 `inst`），
    安裝樹 `inst/bin` 含同位元組的 `.exe` 與旁載 `version.dll`（入口目錄不得有旁載，entry_dir_check）。
    """
    base = tmp_path / tag
    inst = base / "inst"
    bin_dir = inst / "bin"
    bin_dir.mkdir(parents=True)
    windows = os.name == "nt"
    cli_name = "serialwrap.exe" if windows else "serialwrap"
    daemon_name = "serialwrapd.exe" if windows else "serialwrapd"
    cli_payload = b"#!/bin/sh\nexit 0\n"
    daemon_payload = b"#!/bin/sh\nexit 0\n"
    (bin_dir / cli_name).write_bytes(cli_payload)
    (bin_dir / daemon_name).write_bytes(daemon_payload)
    for name in (cli_name, daemon_name):
        (bin_dir / name).chmod(0o755)
    (bin_dir / "version.dll").write_bytes(b"sideloaded dummy dll")
    cli_sha = hashlib.sha256(cli_payload).hexdigest()
    daemon_sha = hashlib.sha256(daemon_payload).hexdigest()
    manifest_file = inst / "release-manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "components": {
                    "cli": {"sha256": cli_sha},
                    "daemon": {"launcher": {"sha256": daemon_sha}, "runtime": {"sha256": daemon_sha}},
                },
            }
        ),
        encoding="utf-8",
    )
    if windows:
        stable = base / "stable"
        stable.mkdir()
        cli_file = stable / cli_name
        cli_file.write_bytes(cli_payload)
        (stable / daemon_name).write_bytes(daemon_payload)
        (stable / "current-release.json").write_text(json.dumps({"dir": str(inst)}), encoding="utf-8")
    else:
        cli_file = bin_dir / cli_name
    return {
        "cli": cli_file,
        "daemon": bin_dir / daemon_name,
        "inst": inst,
        "manifest_sha": hashlib.sha256(manifest_file.read_bytes()).hexdigest(),
        "daemon_name": daemon_name,
    }


@pytest.mark.process_rpc
def test_resource_report_deployed_content(tmp_path: Path) -> None:
    """以最小安裝（POSIX 與 Windows 兩種版面，見 _install_layout）：

    子程序帶 SERIALWRAP_CLI_EXE、SERIALWRAP_DAEMON_EXE、SW_RESOURCE_REPORT 跑 tests/test_wal.py →
    報告 deployed_content.start 與 .end 的 bundle_tree_sha256 皆等於 tree_digests(inst)[0]（含旁載檔）、
    release_manifest_sha256 == sha256_file(inst/release-manifest.json)；不帶 SERIALWRAP_CLI_EXE 再跑 → deployed_content is None。
    """
    import realhw.deployed_content as deployed_content

    layout = _install_layout(tmp_path, "case1")
    inst = layout["inst"]
    expected_tree_sha, _ = deployed_content.tree_digests(inst)

    report_file1 = tmp_path / "r1.json"
    env1 = dict(os.environ)
    env1["SERIALWRAP_CLI_EXE"] = str(layout["cli"])
    env1["SERIALWRAP_DAEMON_EXE"] = str(layout["daemon"])
    env1["SW_RESOURCE_REPORT"] = str(report_file1)
    res1 = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env1,
    )
    assert res1.returncode == 0, f"pytest failed: {res1.stdout}\n{res1.stderr}"
    assert report_file1.exists()
    data1 = json.loads(report_file1.read_text(encoding="utf-8"))
    assert "deployed_content" in data1
    dc1 = data1["deployed_content"]
    assert dc1 is not None
    assert dc1["start"]["error"] is None, dc1["start"]
    assert dc1["end"]["error"] is None, dc1["end"]
    assert dc1["start"]["bundle_tree_sha256"] == expected_tree_sha
    assert dc1["end"]["bundle_tree_sha256"] == expected_tree_sha
    assert dc1["start"]["release_manifest_sha256"] == layout["manifest_sha"]
    assert dc1["end"]["release_manifest_sha256"] == layout["manifest_sha"]

    report_file2 = tmp_path / "r2.json"
    env2 = dict(os.environ)
    env2.pop("SERIALWRAP_CLI_EXE", None)
    env2.pop("SERIALWRAP_DAEMON_EXE", None)
    env2["SW_RESOURCE_REPORT"] = str(report_file2)
    res2 = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env2,
    )
    assert res2.returncode == 0
    assert report_file2.exists()
    data2 = json.loads(report_file2.read_text(encoding="utf-8"))
    assert "deployed_content" in data2
    assert data2["deployed_content"] is None


@pytest.mark.process_rpc
def test_resource_report_contract_blackbox_measures_deployed_content(tmp_path: Path) -> None:
    """回歸（B 輪 L1 證據）：pytest 目標為 tests/contract_blackbox 且設 SW_RESOURCE_REPORT 時，
    報告 deployed_content.start／end 必須為 error is None 的量測結果（非 null）。

    修正前：contract_blackbox conftest 另載 plugin 副本，其 env 快照為 None 而根 conftest 已清除 env，
    deployed_content 恆為 null，P4 登記會把所有 pytest 型案例判為 FAIL。
    """
    import realhw.deployed_content as deployed_content

    layout = _install_layout(tmp_path, "case3")
    inst = layout["inst"]
    expected_tree_sha, _ = deployed_content.tree_digests(inst)

    report_file = tmp_path / "rb.json"
    env = dict(os.environ)
    env["SERIALWRAP_CLI_EXE"] = str(layout["cli"])
    env["SERIALWRAP_DAEMON_EXE"] = str(layout["daemon"])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    # 只做收集（-k 不命中任何案例，exit 5 為 no tests collected）；目的是 sessionstart／sessionfinish 的量測
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/contract_blackbox", "-k", "no_such_case_zz_regression"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode in (0, 5), f"pytest failed: {res.stdout}\n{res.stderr}"
    assert report_file.exists()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    dc = data.get("deployed_content")
    assert isinstance(dc, dict), f"deployed_content 不得為 null：{dc!r}"
    for side in ("start", "end"):
        measured = dc[side]
        assert measured["error"] is None, measured
        assert measured["bundle_tree_sha256"] == expected_tree_sha
        assert measured["release_manifest_sha256"] == layout["manifest_sha"]


@pytest.mark.process_rpc
def test_resource_report_cli_without_daemon_env_unmeasured(tmp_path: Path) -> None:
    """同上但不設 SERIALWRAP_DAEMON_EXE → start／end.error == "daemon_env_missing"、digest 皆 None；
    daemon 設為安裝樹外的檔案 → error == "daemon_outside_install"。

    repo 外的 daemon 一律建在 tmp_path 內（P2-T13 審查修正：原測試會把 stub 寫進 tests/contract_blackbox/bin，
    與 P3 追蹤路徑衝突並被複製進 Windows staging）。
    """
    layout = _install_layout(tmp_path, "case2")

    # Case 1: SERIALWRAP_DAEMON_EXE 未設
    report1 = tmp_path / "r1.json"
    env1 = dict(os.environ)
    env1["SERIALWRAP_CLI_EXE"] = str(layout["cli"])
    env1.pop("SERIALWRAP_DAEMON_EXE", None)
    env1["SW_RESOURCE_REPORT"] = str(report1)
    res1 = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env1,
    )
    assert res1.returncode == 0, res1.stdout + res1.stderr
    assert report1.exists()
    d1 = json.loads(report1.read_text(encoding="utf-8"))
    dc1 = d1.get("deployed_content")
    assert dc1 is not None
    assert dc1["start"]["error"] == "daemon_env_missing"
    assert dc1["end"]["error"] == "daemon_env_missing"
    assert dc1["start"]["bundle_tree_sha256"] is None
    assert dc1["end"]["bundle_tree_sha256"] is None

    # Case 2: SERIALWRAP_DAEMON_EXE 設為安裝樹外的檔案（建在 tmp_path，不寫入 repo）
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_daemon = outside_dir / layout["daemon_name"]
    outside_daemon.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_daemon.chmod(0o755)

    report2 = tmp_path / "r2.json"
    env2 = dict(os.environ)
    env2["SERIALWRAP_CLI_EXE"] = str(layout["cli"])
    env2["SERIALWRAP_DAEMON_EXE"] = str(outside_daemon)
    env2["SW_RESOURCE_REPORT"] = str(report2)
    res2 = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env2,
    )
    assert report2.exists()
    d2 = json.loads(report2.read_text(encoding="utf-8"))
    dc2 = d2.get("deployed_content")
    assert dc2 is not None
    assert dc2["start"]["error"] == "daemon_outside_install"
    assert dc2["end"]["error"] == "daemon_outside_install"


@pytest.mark.process_rpc
def test_windows_com_never_counted_without_device(tmp_path: Path) -> None:
    """無 SW_TEST_COM_PAIR 時，windows_com 項目不得計為 passed（P0 T14 第 6 條；§11.2 不以模擬代替 windows_com）。

    P2-T13 審查修正：原測試只餵空清單（空轉）。現況 tests/ 有四個檔使用 windows_com（已知清單鎖定，新增者須審閱），
    本測試實際以缺 device 的環境執行它們，斷言 resources.windows_com.passed == 0 且至少有一個 skipped。
    （計畫 Task 13 寫「本 Stage 無 windows_com 的 tests/ item」與現況不符，差異已於 PR 說明記錄。）
    """
    import tests.resource_report as resource_report  # noqa: F401  （確認模組可載入）

    # tests/test_p1oc_drain_stalled.py：P1-OC-T4 新增，已審閱——TestWindowsNativeDrainStalled 經
    # sw_core.serial_port.open_serial_port 於 SW_TEST_COM_PAIR 指定的真實 COM 上以 pyserial 後端開啟
    # （非 socket、非記憶體替身；唯一 monkeypatch 為寫入逾時常數），缺 device 時 skip，符合 windows_com 定義。
    known = {
        "tests/contract_blackbox/test_cli_mgmt_daemon.py",
        "tests/test_p1oc_drain_stalled.py",
        "tests/test_spike1_remaining.py",
        "tests/test_stage0_uart_exclusive.py",
    }
    users: set[str] = set()
    for path in sorted(Path("tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "windows_com"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            ):
                users.add(path.as_posix())
                break
    assert users == known, f"windows_com 使用檔與已知清單不符（新增者須審閱）：{sorted(users)}"

    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env.pop("SW_TEST_COM_PAIR", None)
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "windows_com", *sorted(known)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert report_file.exists(), res.stdout + res.stderr
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["resources"]["windows_com"]["passed"] == 0, data["resources"]["windows_com"]
    assert data["resources"]["windows_com"]["skipped"] >= 1, data["resources"]["windows_com"]


@pytest.mark.memory_only
def test_single_hook_definitions() -> None:
    """AST 掃 tests/conftest.py：pytest_collection_modifyitems、pytest_sessionfinish、

    pytest_configure 各至多一個頂層定義；檔中不出現 platform_resources.yaml。
    """
    conftest_path = Path("tests/conftest.py")
    tree = ast.parse(conftest_path.read_text(encoding="utf-8"))
    hook_counts: dict[str, int] = {
        "pytest_collection_modifyitems": 0,
        "pytest_sessionfinish": 0,
        "pytest_configure": 0,
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in hook_counts:
            hook_counts[node.name] += 1

    for hook_name, count in hook_counts.items():
        assert count <= 1, f"Hook {hook_name} defined {count} times (must be <= 1)"

    assert "platform_resources.yaml" not in conftest_path.read_text(encoding="utf-8")


@pytest.mark.memory_only
def test_workflow_has_windows_jobs() -> None:
    """解析 .github/workflows/tests.yml：

    有 pytest-windows（runs-on: windows-latest、指令含 -m "core_portable or windows_native" 與 SW_RESOURCE_REPORT）
    與 windows-com-selfhosted（runs-on 含 self-hosted 與 serialwrap-com、有 label 條件）。
    """
    wf_path = Path(".github/workflows/tests.yml")
    assert wf_path.exists(), f"{wf_path} does not exist"
    data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", {})

    assert "pytest-windows" in jobs, "Missing pytest-windows job in workflow"
    pw_job = jobs["pytest-windows"]
    assert pw_job.get("runs-on") == "windows-latest"
    pw_steps_text = yaml.dump(pw_job.get("steps", []))
    assert "core_portable or windows_native" in pw_steps_text
    assert "SW_RESOURCE_REPORT" in pw_steps_text

    assert "windows-com-selfhosted" in jobs, "Missing windows-com-selfhosted job in workflow"
    wc_job = jobs["windows-com-selfhosted"]
    runs_on = wc_job.get("runs-on", [])
    if isinstance(runs_on, str):
        runs_on = [runs_on]
    assert "self-hosted" in runs_on
    assert "serialwrap-com" in runs_on
    cond = str(wc_job.get("if", ""))
    assert "run-windows-com" in cond or "workflow_dispatch" in cond


@pytest.mark.process_rpc
def test_resource_report_l1_results_fields(tmp_path: Path) -> None:
    """在 tmp_path 建 conftest 副本＋一檔三函式 test_a（pass）、test_b（skipif(True, reason='x')）、test_c（pass），

    以 -k "not test_c" 與 SW_RESOURCE_REPORT=<tmp>/r.json 執行：
    報告 schema_version == 2、format == "l1-results.v1"、collected == [<a>, <b>]（-k 篩掉者不在 collected）、
    deselected == [<c>]、results 恰兩列且 <b> 的 outcome == "skipped"、skip_kind == "other"、
    exit_code == 0、finished is True、argv 含 "-k"。
    """
    test_dir = tmp_path / "sub"
    test_dir.mkdir()
    conftest_src = Path("tests/conftest.py").read_text(encoding="utf-8")
    (test_dir / "conftest.py").write_text(conftest_src, encoding="utf-8")
    (test_dir / "test_items.py").write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "def test_a(): pass\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def test_b(): pass\n"
        "def test_c(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-k", "not test_c", str(test_dir / "test_items.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"pytest failed: {res.stdout}\n{res.stderr}"
    assert report_file.exists()
    d = json.loads(report_file.read_text(encoding="utf-8"))
    assert d.get("schema_version") == 2
    assert d.get("format") == "l1-results.v1"
    assert d.get("finished") is True
    assert d.get("exit_code") == 0
    assert any("-k" in str(arg) for arg in d.get("argv", []))

    collected = d.get("collected", [])
    deselected = d.get("deselected", [])
    assert any("test_a" in nodeid for nodeid in collected)
    assert any("test_b" in nodeid for nodeid in collected)
    assert not any("test_c" in nodeid for nodeid in collected)
    assert any("test_c" in nodeid for nodeid in deselected)

    results = d.get("results", [])
    assert len(results) == 2
    res_b = next((r for r in results if "test_b" in r["nodeid"]), None)
    assert res_b is not None
    assert res_b["outcome"] == "skipped"
    assert res_b["skip_kind"] == "other"


@pytest.mark.process_rpc
def test_resource_report_phase_outcomes(tmp_path: Path) -> None:
    """fixture setup 拋例外的函式 → 該列 outcome == "error"、phases.setup == "failed"；

    teardown 拋例外而 call 通過 → error；xfail(strict=False) 而通過 → xpassed；報告 exit_code == 1。
    """
    test_dir = tmp_path / "sub"
    test_dir.mkdir()
    conftest_src = Path("tests/conftest.py").read_text(encoding="utf-8")
    (test_dir / "conftest.py").write_text(conftest_src, encoding="utf-8")
    (test_dir / "test_phases.py").write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "@pytest.fixture\n"
        "def bad_setup():\n"
        "    raise RuntimeError('setup failed')\n"
        "@pytest.fixture\n"
        "def bad_teardown():\n"
        "    yield\n"
        "    raise RuntimeError('teardown failed')\n"
        "def test_setup_err(bad_setup): pass\n"
        "def test_teardown_err(bad_teardown): pass\n"
        "@pytest.mark.xfail(strict=False)\n"
        "def test_xpass(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_dir / "test_phases.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 1, f"Expected exit code 1, got {res.returncode}"
    assert report_file.exists()
    d = json.loads(report_file.read_text(encoding="utf-8"))
    assert d.get("exit_code") == 1
    results = {r["nodeid"].split("::")[-1]: r for r in d.get("results", [])}

    r_setup = results.get("test_setup_err")
    assert r_setup is not None
    assert r_setup["outcome"] == "error"
    assert r_setup["phases"]["setup"] == "failed"

    r_teardown = results.get("test_teardown_err")
    assert r_teardown is not None
    assert r_teardown["outcome"] == "error"
    assert r_teardown["phases"]["teardown"] == "failed"

    r_xpass = results.get("test_xpass")
    assert r_xpass is not None
    assert r_xpass["outcome"] == "xpassed"


@pytest.mark.process_rpc
def test_resource_report_interrupted_not_finished(tmp_path: Path) -> None:
    """子程序第二個測試內 raise KeyboardInterrupt：

    報告存在、finished is True 但 exit_code == 2，且 results 只有已執行列；
    以 l1_judge.judge(report, [<a>, <b>, <c>], platform=…) → INSUFFICIENT，
    reasons 含 "runner exit 2" 與 not_run 非空。
    """
    import tests.acceptance.l1_judge as l1_judge

    test_dir = tmp_path / "sub"
    test_dir.mkdir()
    conftest_src = Path("tests/conftest.py").read_text(encoding="utf-8")
    (test_dir / "conftest.py").write_text(conftest_src, encoding="utf-8")
    (test_dir / "test_flow.py").write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "def test_1(): pass\n"
        "def test_2(): raise KeyboardInterrupt()\n"
        "def test_3(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_dir / "test_flow.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 2
    assert report_file.exists()
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report.get("finished") is True
    assert report.get("exit_code") == 2
    results = report.get("results", [])
    assert len(results) <= 2

    # P2-T13 修正：pytest 的 nodeid 相對於 rootdir（本測試的 sub 目錄，例如 test_flow.py::test_1），
    # 不是絕對路徑；原草稿以絕對路徑組必要集合，與 pytest 實際 nodeid 永不相符，會把三個案例都判成 missing。
    # 必要集合改取報告的收集清單（plan：collected 為 session.items 的 nodeid），仍為三個案例。
    nodeids = list(report["collected"])
    assert len(nodeids) == 3
    # 平台取報告本身記錄的執行平台（Windows 上為 windows）；hard-code linux 會使判定成為 INVALID（P2-T13 審查修正）。
    verdict = l1_judge.judge(report, nodeids, platform=report["platform"])
    assert verdict["verdict"] == "INSUFFICIENT"
    assert any("runner exit 2" in r for r in verdict.get("reasons", []))
    assert len(verdict.get("not_run", [])) > 0


@pytest.mark.process_rpc
def test_regen_lock_base_check_detects_removal(tmp_path: Path) -> None:
    """暫存 git repo：commit 1 鎖檔含 s0_bc.linux=[a,b]；commit 2 改為 [a]

    → regen_lock.py --check --base HEAD~1 exit 1、stdout 含 b；
    --allow-removal b → exit 0 但仍列出；只增為 [a,b,c] → exit 0；改 selector.argv → exit 1。
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    acc_dir = repo_dir / "tests" / "acceptance"
    acc_dir.mkdir(parents=True)
    shutil.copy("tests/acceptance/regen_lock.py", acc_dir / "regen_lock.py")
    # 預設 --check 同時驗案例鎖（P2-T13 審查修正：缺案例清單即失敗），故一併複製一致的案例檔。
    for name in ("cases.yaml", "cases.lock.json", "cases.counts.json"):
        shutil.copy(f"tests/acceptance/{name}", acc_dir / name)

    lock_file = acc_dir / "l1_required.lock.json"
    init_lock = {
        "schema_version": 1,
        "sets": {
            "s0_bc": {
                "linux": {
                    "selector": {"argv": ["-q", "tests/test_wal.py"], "paths": ["tests/test_wal.py"], "markexpr": ""},
                    "nodeids": ["tests/test_wal.py::test_a", "tests/test_wal.py::test_b"],
                }
            }
        },
    }
    lock_file.write_text(json.dumps(init_lock, indent=2), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init lock"], cwd=repo_dir, check=True, capture_output=True)

    # commit 2: 移除 b
    mod_lock = {
        "schema_version": 1,
        "sets": {
            "s0_bc": {
                "linux": {
                    "selector": {"argv": ["-q", "tests/test_wal.py"], "paths": ["tests/test_wal.py"], "markexpr": ""},
                    "nodeids": ["tests/test_wal.py::test_a"],
                }
            }
        },
    }
    lock_file.write_text(json.dumps(mod_lock, indent=2), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "removed b"], cwd=repo_dir, check=True, capture_output=True)

    # 執行 check 應 exit 1 且輸出含 b
    cmd1 = [sys.executable, str(acc_dir / "regen_lock.py"), "--check", "--base", "HEAD~1"]
    res1 = subprocess.run(cmd1, cwd=repo_dir, capture_output=True, text=True)
    assert res1.returncode == 1, f"Expected exit 1, got {res1.returncode}: {res1.stdout}\n{res1.stderr}"
    assert "test_b" in res1.stdout or "test_b" in res1.stderr

    # --allow-removal b 應 exit 0
    cmd2 = [sys.executable, str(acc_dir / "regen_lock.py"), "--check", "--base", "HEAD~1", "--allow-removal", "tests/test_wal.py::test_b"]
    res2 = subprocess.run(cmd2, cwd=repo_dir, capture_output=True, text=True)
    assert res2.returncode == 0, f"Expected exit 0 with allow-removal: {res2.stdout}\n{res2.stderr}"

    # 只增 [a, b, c] 應 exit 0
    add_lock = {
        "schema_version": 1,
        "sets": {
            "s0_bc": {
                "linux": {
                    "selector": {"argv": ["-q", "tests/test_wal.py"], "paths": ["tests/test_wal.py"], "markexpr": ""},
                    "nodeids": ["tests/test_wal.py::test_a", "tests/test_wal.py::test_b", "tests/test_wal.py::test_c"],
                }
            }
        },
    }
    lock_file.write_text(json.dumps(add_lock, indent=2), encoding="utf-8")
    res3 = subprocess.run(cmd1, cwd=repo_dir, capture_output=True, text=True)
    assert res3.returncode == 0, f"Expected exit 0 for add-only: {res3.stdout}\n{res3.stderr}"

    # 修改 selector.argv 應 exit 1
    chg_sel_lock = {
        "schema_version": 1,
        "sets": {
            "s0_bc": {
                "linux": {
                    "selector": {"argv": ["-q", "tests/test_other.py"], "paths": ["tests/test_wal.py"], "markexpr": ""},
                    "nodeids": ["tests/test_wal.py::test_a", "tests/test_wal.py::test_b", "tests/test_wal.py::test_c"],
                }
            }
        },
    }
    lock_file.write_text(json.dumps(chg_sel_lock, indent=2), encoding="utf-8")
    res4 = subprocess.run(cmd1, cwd=repo_dir, capture_output=True, text=True)
    assert res4.returncode == 1, f"Expected exit 1 for selector change: {res4.stdout}\n{res4.stderr}"


@pytest.mark.process_rpc
def test_resource_report_plugin_works_under_confcutdir(tmp_path: Path) -> None:
    """在 tmp/'sub' 建 conftest.py（只以 spec_from_file_location 載入 <repo>/tests/resource_report_plugin.py 並 register，寫法同 P3 黑箱 conftest）

    與一個模組層 core_portable 的 passing 測試；
    子程序 python3 -m pytest -q <tmp>/sub --confcutdir=<tmp>/sub -p no:cacheprovider 帶 SW_RESOURCE_REPORT=<tmp>/r.json
    → 檔存在、format == "l1-results.v1"、finished is True、collected 為該 1 個 nodeid；
    子程序 sys.modules 不含 sw_core；完整 pytest tests/test_wal.py 時 sw_resource_report 只註冊一次（results 無重複列）。
    """
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    plugin_path = Path("tests/resource_report_plugin.py").resolve()
    (sub_dir / "conftest.py").write_text(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('sw_resource_report', r'{plugin_path}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "def pytest_configure(config):\n"
        "    if not config.pluginmanager.has_plugin('sw_resource_report'):\n"
        "        config.pluginmanager.register(mod, 'sw_resource_report')\n",
        encoding="utf-8",
    )
    (sub_dir / "test_one.py").write_text(
        "import sys\n"
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "def test_standalone():\n"
        "    assert not any(m == 'sw_core' or m.startswith('sw_core.') for m in sys.modules)\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(sub_dir),
            f"--confcutdir={sub_dir}",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"pytest under confcutdir failed: {res.stdout}\n{res.stderr}"
    assert report_file.exists()
    d = json.loads(report_file.read_text(encoding="utf-8"))
    assert d.get("format") == "l1-results.v1"
    assert d.get("finished") is True
    assert len(d.get("collected", [])) == 1

    report_file_full = tmp_path / "r_full.json"
    env_full = dict(os.environ)
    env_full["SW_RESOURCE_REPORT"] = str(report_file_full)
    res_full = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_wal.py"],
        capture_output=True,
        text=True,
        env=env_full,
    )
    assert res_full.returncode == 0
    assert report_file_full.exists()
    d_full = json.loads(report_file_full.read_text(encoding="utf-8"))
    nodeids = [r["nodeid"] for r in d_full.get("results", [])]
    assert len(nodeids) == len(set(nodeids)), "Duplicate nodeids found in results"


def _conftest_registering_plugin(path: Path) -> str:
    """回傳子目錄 conftest 內容：只以路徑載入報告外掛並註冊（寫法同 P3 黑箱 conftest）。"""
    plugin_path = Path("tests/resource_report_plugin.py").resolve()
    return (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('sw_resource_report', r'{plugin_path}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "def pytest_configure(config):\n"
        "    if not config.pluginmanager.has_plugin('sw_resource_report'):\n"
        "        config.pluginmanager.register(mod, 'sw_resource_report')\n"
    )


@pytest.mark.process_rpc
def test_resource_report_subtest_failure_marks_row(tmp_path: Path) -> None:
    """pytest 9 subtest 失敗 → 父列 outcome 為 failed（P2-T13 審查修正：原實作讓 SubtestReport 覆寫 call，記成 passed）。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "test_sub.py").write_text(
        "import unittest, pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "class T(unittest.TestCase):\n"
        "    def test_sub_fail(self):\n"
        "        for i in range(3):\n"
        "            with self.subTest(i=i):\n"
        "                self.assertTrue(i != 1)\n"
        "def test_plain():\n"
        "    pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "resource_report_plugin", str(sub / "test_sub.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    data = json.loads(report_file.read_text(encoding="utf-8"))
    rows = {r["nodeid"].split("::")[-1]: r for r in data["results"]}
    assert rows["test_sub_fail"]["outcome"] == "failed"
    assert rows["test_plain"]["outcome"] == "passed"


@pytest.mark.process_rpc
def test_resource_report_setup_only_records_no_passed_row(tmp_path: Path) -> None:
    """--setup-only 不進入 call：不得記為 passed（P2-T13 審查修正）；l1_judge 判為 not_run。"""
    import tests.acceptance.l1_judge as l1_judge

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "test_so.py").write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\n"
        "def test_one(): assert False\n"
        "def test_two(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--setup-only", "-p", "resource_report_plugin", str(sub / "test_so.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["results"] == []
    assert len(data["collected"]) == 2
    verdict = l1_judge.judge(data, data["collected"], platform=data["platform"])
    assert verdict["verdict"] == "INSUFFICIENT"
    assert len(verdict["not_run"]) == 2


@pytest.mark.process_rpc
def test_resource_report_multi_marker_counts(tmp_path: Path) -> None:
    """同一項目帶多個資源 marker：每個資源各計一次，平台只計一次（P2-T13 審查修正）。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "test_multi.py").write_text(
        "import pytest\n"
        "pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only, pytest.mark.process_rpc]\n"
        "def test_both(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "resource_report_plugin", str(sub / "test_multi.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["resources"]["memory_only"]["passed"] == 1
    assert data["resources"]["process_rpc"]["passed"] == 1
    assert data["counts"]["core_portable"]["passed"] == 1


@pytest.mark.process_rpc
def test_resource_report_stale_removed_when_run_killed(tmp_path: Path) -> None:
    """上一次執行留下的報告在本次 sessionstart 即移除；本次被中途終止時不得留下舊的 finished:true（P2-T13 審查修正）。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text(
        "import os\n"
        "def pytest_collection_finish(session):\n"
        "    os._exit(3)\n",
        encoding="utf-8",
    )
    (sub / "test_k.py").write_text("def test_x(): pass\n", encoding="utf-8")
    report_file = tmp_path / "r.json"
    report_file.write_text(json.dumps({"finished": True, "exit_code": 0, "stale": True}), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([os.getcwd(), os.path.join(os.getcwd(), "tests")])
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "resource_report_plugin", str(sub)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 3, res.stdout + res.stderr
    assert not report_file.exists(), "被終止的執行不得留下舊報告"


@pytest.mark.process_rpc
def test_resource_report_argv_under_pytest_main(tmp_path: Path) -> None:
    """以 pytest.main() 啟動（Windows win-pytest.sh 的方式）時 argv 記錄 pytest 實際參數（P2-T13 審查修正）。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "conftest.py").write_text(_conftest_registering_plugin(sub), encoding="utf-8")
    (sub / "test_argv.py").write_text(
        "import pytest\npytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]\ndef test_a(): pass\n",
        encoding="utf-8",
    )
    report_file = tmp_path / "r.json"
    env = dict(os.environ)
    env["SW_RESOURCE_REPORT"] = str(report_file)
    res = subprocess.run(
        [sys.executable, "-c", "import sys, pytest; raise SystemExit(pytest.main(sys.argv[1:]))", "-q", str(sub)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["argv"][1:] == ["-q", str(sub)], data["argv"]


@pytest.mark.memory_only
def test_summarize_counts_every_resource_marker() -> None:
    """summarize 對多資源列逐一計數，平台總數只計一次。"""
    import tests.resource_report as resource_report

    records = [
        {"nodeid": "a", "platform": "core_portable", "resources": ["process_rpc", "posix_pty"],
         "outcome": "passed", "is_platform_skip": False},
        {"nodeid": "b", "platform": "core_portable", "resource": "memory_only",
         "outcome": "failed", "is_platform_skip": False},
    ]
    summary = resource_report.summarize(records, ignored=[], platform="linux")
    assert summary["resources"]["process_rpc"]["passed"] == 1
    assert summary["resources"]["posix_pty"]["passed"] == 1
    assert summary["resources"]["memory_only"]["failed"] == 1
    assert summary["counts"]["core_portable"] == {"passed": 1, "failed": 1, "skipped": 0}


@pytest.mark.memory_only
def test_workflow_ci_l1_gates() -> None:
    """CI 閘門（P2-T13 審查修正）：PR 對 base 的鎖檔只增檢查、labeled 事件不重跑其餘 job、
    自架 COM job 不接受 fork PR、selftest 帶 --out、func-test 逐一執行三個 win-com 情境（--case 為完全相符 stem）。"""
    wf = yaml.safe_load(Path(".github/workflows/tests.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    pytest_job = jobs["pytest"]
    lock_steps = [st for st in pytest_job["steps"] if "regen_lock.py --check --base origin/${{ github.base_ref }}" in str(st.get("run", ""))]
    assert len(lock_steps) == 1, "pytest job 須含 regen_lock --check --base 步驟"
    assert "pull_request" in str(lock_steps[0].get("if", ""))
    lock_command = str(lock_steps[0].get("run", ""))
    allowed_removals = (
        "tests/test_cli_stderr_full_error.py::TestRunRpcStderrKeepsMessage::test_socket_error_stderr_includes_error_code_and_message",
        "tests/test_contracts_catalog.py::test_ec_passthrough_single_meta_entry",
        "tests/test_docker_l15.py::test_longrun_and_supervision_isolated",
        "tests/test_docker_l15.py::test_longrun_refuses_before_p1",
        "tests/test_docker_l15.py::test_run_l15_dry_run_labels_report",
        "tests/test_docker_l15.py::test_run_l15_dry_run_uses_sandbox_by_id",
        "tests/test_docker_l15.py::test_run_systemd_dry_run_asserts_dropin",
        "tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[handoff]",
        "tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[probe]",
        "tests/test_stage0_handoff_stop.py::test_handoff_flash_races_prepare[race]",
    )
    for nodeid in allowed_removals:
        assert nodeid in lock_command, f"L1 擁有者裁決清單缺少 {nodeid}"
    lock_tokens = shlex.split(lock_command)
    allow_index = lock_tokens.index("--allow-removal")
    assert tuple(lock_tokens[allow_index + 1 :]) == allowed_removals
    for name in ("pytest", "bridge-tools", "bridge-tools-windows", "pytest-windows"):
        assert "labeled" in str(jobs[name].get("if", "")), f"{name} 須排除 labeled 事件"
    com = jobs["windows-com-selfhosted"]
    assert "head.repo.full_name == github.repository" in str(com.get("if", ""))
    com_text = yaml.dump(com["steps"])
    assert "uart-stdio-bridge selftest" in com_text and "--out" in com_text
    assert "--transport-mode virtual_com0com" in com_text
    for case in ("win-com-txrx-bridge", "win-com-open-exclusive", "win-com-cancel-close"):
        assert case in com_text, f"windows-com-selfhosted 須執行 {case}"
    assert '--case "win-com-*"' not in com_text  # --case 為完全相符的 stem，不支援 glob
    pw_text = yaml.dump(jobs["pytest-windows"]["steps"])
    assert "--junitxml" in pw_text


@pytest.mark.memory_only
def test_win_pytest_does_not_add_ineffective_safe_directory() -> None:
    script = Path("scripts/stage0/win-pytest.sh").read_text(encoding="utf-8")
    assert "config --local --add safe.directory" not in script
