# tests/test_functest_harness.py
"""func-test DaemonHarness 隔離、artifact 選定與外部 bridge 支援測試。"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FUNC_TEST_DIR = REPO_ROOT / "func-test"
if str(FUNC_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FUNC_TEST_DIR))


def _capture_popen(captured: dict[str, Any]):
    """攔截 subprocess.Popen 參數的測試 helper。"""
    class FakeProc:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["args"] = args[0] if args else kwargs.get("args")
            captured["env"] = kwargs.get("env")
            captured["stdout"] = kwargs.get("stdout")
            captured["stderr"] = kwargs.get("stderr")
            self.pid = 12345

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            pass

    return FakeProc


@pytest.mark.memory_only
def test_harness_isolates_nine_dirs_and_strips_all_serialwrap(monkeypatch, tmp_path):
    """驗證 harness 隔離 9 個目錄變數並清除外層所有 SERIALWRAP_ 變數。"""
    from lib import daemon_harness

    captured: dict[str, Any] = {}
    monkeypatch.setenv("SERIALWRAP_ENDPOINT", "unix:///live.sock")
    monkeypatch.setenv("SERIALWRAP_PROFILE_DIR", "/live/profiles")
    monkeypatch.setenv("SERIALWRAP_TEST_DEVICE_ALLOWLIST", "COM9")
    monkeypatch.setenv("SERIALWRAP_TCP_PORT", "1")
    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="pty"))
    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen(captured))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    h.start()
    try:
        env = captured["env"]
        for key, sub in daemon_harness.ISOLATED_DIR_ENV.items():
            assert env[key] == str(h.root / sub), key
            assert pathlib.Path(env[key]).is_dir()
        leaked = {k for k in env if k.startswith("SERIALWRAP_")} - set(daemon_harness.ISOLATED_DIR_ENV)
        assert leaked == set(), leaked
        assert env["XDG_CONFIG_HOME"].startswith(str(h.root)) and "XDG_RUNTIME_DIR" not in env
    finally:
        h.stop()


@pytest.mark.memory_only
def test_cli_exe_env_override(monkeypatch):
    """驗證 SERIALWRAP_CLI_EXE 環境變數可覆寫 CLI 執行檔路徑。"""
    from lib import daemon_harness

    monkeypatch.setenv("SERIALWRAP_CLI_EXE", "/x/serialwrap")
    assert daemon_harness.resolve_cli_exe() == ["/x/serialwrap"]
    monkeypatch.delenv("SERIALWRAP_CLI_EXE", raising=False)
    assert daemon_harness.resolve_cli_exe() == [str(daemon_harness.ROOT_DIR / "serialwrap")]


@pytest.mark.memory_only
def test_daemon_exe_env_override(monkeypatch):
    """驗證 SERIALWRAP_DAEMON_EXE 環境變數可覆寫 daemon 執行檔路徑與 Popen argv。"""
    from lib import daemon_harness

    monkeypatch.setenv("SERIALWRAP_DAEMON_EXE", "/x/serialwrapd")
    assert daemon_harness.resolve_daemon_argv() == ["/x/serialwrapd"]
    captured: dict[str, Any] = {}
    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="pty"))
    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen(captured))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    h.start()
    try:
        argv = captured["args"]
        assert argv[0] == "/x/serialwrapd"
        assert "--profile-dir" in argv
        assert "--socket" in argv
        assert "--lock" in argv
    finally:
        h.stop()


@pytest.mark.memory_only
def test_daemon_log_not_devnull(monkeypatch, tmp_path):
    """驗證 daemon 程序 stdout/stderr 導向 evidence_dir/'daemon.log'，不再丟棄至 DEVNULL。"""
    from lib import daemon_harness

    captured: dict[str, Any] = {}
    evidence_dir = tmp_path / "evidence"
    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="pty", evidence_dir=evidence_dir))
    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen(captured))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    h.start()
    try:
        assert captured["stdout"] is not None
        assert captured["stdout"] != daemon_harness.subprocess.DEVNULL
        assert captured["stderr"] is not None
        assert captured["stderr"] != daemon_harness.subprocess.DEVNULL
        log_file = h.evidence_dir / "daemon.log"
        assert getattr(captured["stdout"], "name", "") == str(log_file)
        assert captured["stdout"] == captured["stderr"]
    finally:
        h.stop()


@pytest.mark.memory_only
def test_artifact_identity_python_baseline():
    """驗證 Python 基線的 artifact identity 收集結果。"""
    from lib import artifact_identity, daemon_harness

    repo_root = daemon_harness.ROOT_DIR
    cli_argv = daemon_harness.resolve_cli_exe()
    daemon_argv = daemon_harness.resolve_daemon_argv()
    artifacts = artifact_identity.collect_artifacts(cli_argv, daemon_argv, repo_root)
    assert artifacts["daemon"]["implementation"] == "python"
    pkg_sha = artifacts["daemon"]["package_tree_sha256"]
    repo_sha = artifacts["daemon"]["repo_tree_sha256"]
    assert len(pkg_sha) == 64 and all(c in "0123456789abcdef" for c in pkg_sha)
    assert len(repo_sha) == 64 and all(c in "0123456789abcdef" for c in repo_sha)
    assert pkg_sha != repo_sha
    assert artifacts["cli"]["version_stdout"].startswith("serialwrap ")


@pytest.mark.memory_only
def test_functest_manifest_records_deployed_bracket(monkeypatch, tmp_path):
    """驗證 runner 於 case 執行前後量測 deployed_content 並寫入 manifest.json。"""
    import realhw.deployed_content
    import runner
    from lib import daemon_harness
    from lib.yaml_loader import TestCase

    calls: list[tuple[str, str | None, Any]] = []
    fake_measures = [
        {"bundle_tree_sha256": "a" * 64, "runtime_tree_sha256": "1" * 64},
        {"bundle_tree_sha256": "b" * 64, "runtime_tree_sha256": "2" * 64},
    ]

    def fake_measure(cli_exe: Any, *, daemon_argv: Any = None, now: Any = None) -> dict[str, Any]:
        idx = len([c for c in calls if c[0] == "measure"])
        calls.append(("measure", str(cli_exe), daemon_argv))
        return fake_measures[min(idx, len(fake_measures) - 1)]

    monkeypatch.setattr(realhw.deployed_content, "measure", fake_measure)

    orig_start = daemon_harness.DaemonHarness.start
    orig_stop = daemon_harness.DaemonHarness.stop

    def tracked_start(self: Any) -> None:
        calls.append(("start", None, None))
        self._tmpdir = tempfile.TemporaryDirectory(prefix="sw-func-")
        self._root = pathlib.Path(self._tmpdir.name)
        self._socket_path = str(self._root / "run" / "serialwrapd.sock")
        (self._root / "run").mkdir(parents=True, exist_ok=True)
        pathlib.Path(self._socket_path).touch()

    def tracked_stop(self: Any) -> None:
        calls.append(("stop", None, None))
        if hasattr(self, "_tmpdir") and self._tmpdir is not None:
            self._tmpdir.cleanup()

    monkeypatch.setattr(daemon_harness.DaemonHarness, "start", tracked_start)
    monkeypatch.setattr(daemon_harness.DaemonHarness, "stop", tracked_stop)
    monkeypatch.setattr(runner.TestRunner, "_wait_for_socket", lambda self, timeout_s=5.0: None)

    report_dir = tmp_path / "reports"
    case = TestCase(
        name="dummy-case",
        category="state-machine",
        severity="critical",
        target={"platform": "shell"},
        profile={},
        env_files={},
        steps=[],
    )
    tr = runner.TestRunner(case, report_dir=report_dir)
    ok, msg = tr.run()
    assert ok is True, f"Runner failed: {msg}"

    manifest_file = report_dir / "dummy-case" / "pty" / "0" / "manifest.json"
    assert manifest_file.exists(), f"Manifest file missing: {manifest_file}"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["deployed_content"] == {
        "start": fake_measures[0],
        "end": fake_measures[1],
    }

    measure_calls = [i for i, c in enumerate(calls) if c[0] == "measure"]
    start_calls = [i for i, c in enumerate(calls) if c[0] == "start"]
    stop_calls = [i for i, c in enumerate(calls) if c[0] == "stop"]
    assert len(measure_calls) == 2
    assert len(start_calls) == 1
    assert len(stop_calls) == 1
    assert measure_calls[0] < start_calls[0]
    assert stop_calls[0] < measure_calls[1]

    cli_exe = daemon_harness.resolve_cli_exe()[0]
    assert calls[measure_calls[0]][1] == cli_exe
    assert calls[measure_calls[1]][1] == cli_exe


@pytest.mark.memory_only
def test_artifact_env_names_popped_by_repo_conftest():
    """驗證 tests/conftest.py 的 pop 清單包含 SERIALWRAP_CLI_EXE 與 SERIALWRAP_DAEMON_EXE。"""
    conftest_path = pathlib.Path(__file__).resolve().parent / "conftest.py"
    content = conftest_path.read_text(encoding="utf-8")
    assert '"SERIALWRAP_CLI_EXE"' in content or "'SERIALWRAP_CLI_EXE'" in content
    assert '"SERIALWRAP_DAEMON_EXE"' in content or "'SERIALWRAP_DAEMON_EXE'" in content


@pytest.mark.memory_only
def test_bridge_mode_maps_device_event_to_link_ops():
    """驗證 bridge 模式下 device_event、target_emit、pause_target 正確映射至 bridge 呼叫。"""
    from lib import daemon_harness

    calls: list[tuple[str, Any]] = []

    class FakeBridgeSession:
        def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append((action, params))
            return {"status": "ok"}

    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="bridge"))
    h.bridge = FakeBridgeSession()

    h.device_event("remove")
    assert calls[-1] == ("by_id_link_remove", None)

    h.target_emit(b"x")
    assert calls[-1] == ("dut_control", {"msg": {"action": "emit", "payload_b64": "eA=="}})

    h.pause_target(5)
    assert calls[-1] == ("dut_control", {"msg": {"action": "pause_responding", "duration_ms": 5000}})


@pytest.mark.memory_only
def test_settle_snapshot_retries_until_stable(monkeypatch):
    """驗證 BridgeSession.snapshot(settle=True) 在 offsets 穩定時回傳 settled:True，十輪不穩定則回傳 False。"""
    from lib import bridge_session

    call_count = 0

    def fake_call(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal call_count
        if action == "barrier":
            call_count += 1
            offsets = 30 if call_count >= 5 else (10 * ((call_count + 1) // 2) + (call_count % 2))
            return {"status": "ok", "offsets": {"serial_read": offsets}, "settled": True}
        return {"status": "ok"}

    bs = bridge_session.BridgeSession(
        run_id="r1",
        case_id="c1",
        port_id="p1",
        endpoint_spec={"kind": "posix-pty"},
        evidence_dir=pathlib.Path("/tmp"),
        target_json={},
        plan_json=None,
        dut_mode="local-process",
        env={},
        quiesce=False,
    )
    monkeypatch.setattr(bs, "call", fake_call)
    monkeypatch.setattr(bs, "_wal_current_seq", lambda: 42)

    res = bs.snapshot(settle=True)
    assert res["settled"] is True
    assert res["wal_seq"] == 42

    call_count = 0

    def fake_call_unstable(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal call_count
        if action == "barrier":
            call_count += 1
            return {"status": "ok", "offsets": {"serial_read": call_count}, "settled": True}
        return {"status": "ok"}

    monkeypatch.setattr(bs, "call", fake_call_unstable)
    res_unstable = bs.snapshot(settle=True)
    assert res_unstable["settled"] is False


@pytest.mark.process_rpc
def test_lazy_fake_target_import():
    """驗證在無 pty/termios 環境下 import runner 與 daemon_harness 成功，且 fake_target 保持 lazy import。"""
    cmd = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.modules['pty'] = None; "
            "sys.modules['termios'] = None; "
            "import pathlib; "
            f"sys.path.insert(0, r'{FUNC_TEST_DIR}'); "
            "import runner; "
            "from lib import daemon_harness; "
            "assert 'lib.fake_target' not in sys.modules, 'fake_target was eagerly imported'; "
            "print('OK')"
        ),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, f"子程序執行失敗:\nstdout: {res.stdout}\nstderr: {res.stderr}"
    assert "OK" in res.stdout


@pytest.mark.memory_only
def test_tools_docker_reads_artifact_env(monkeypatch):
    """驗證 tools/docker 的 uart_harness 與 remote_lab 讀取 SERIALWRAP_CLI_EXE 且 AST 不再引用 fake_target。"""
    import ast
    import importlib.util

    uart_harness_path = REPO_ROOT / "tools" / "docker" / "uart_harness.py"
    remote_lab_path = REPO_ROOT / "tools" / "docker" / "remote_lab.py"

    for path in (uart_harness_path, remote_lab_path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "fake_target" not in node.module, f"{path.name} 仍自 {node.module} 引用 TargetConfig"

    monkeypatch.setenv("SERIALWRAP_CLI_EXE", "/x/sw")

    spec = importlib.util.spec_from_file_location("uart_harness", uart_harness_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._serialwrap_bin() == "/x/sw"
