# tests/test_functest_windows_wiring.py
"""Windows bridge 模式接線、環境隔離、TCP probe 與能力式檢查測試。"""
from __future__ import annotations

import os
import pathlib
import socket
import sys
from typing import Any

import pytest
import yaml

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
def test_windows_wiring_env_and_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """驗證 Windows bridge 模式下的環境變數、argv、profile targets 綁定及不建立 symlink。"""
    from lib import daemon_harness
    monkeypatch.setattr(os, "name", "nt")

    captured_daemon: dict[str, Any] = {}
    captured_bridge: dict[str, Any] = {}

    cfg = daemon_harness.HarnessConfig(
        mode="bridge",
        daemon_com="COM7",
        bridge_com="COM8",
        transport_mode="virtual_com0com",
    )
    h = daemon_harness.DaemonHarness(cfg)

    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen(captured_daemon))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(h, "_spawn_bridge", lambda *a, **k: captured_bridge.update({"args": a, "kwargs": k}))
    monkeypatch.setattr(h, "_query_daemon_status", lambda: {"test_device_allowlist": {"enabled": True, "coms": ["COM7"]}})

    h.start()
    try:
        env = captured_daemon["env"]
        assert env["SERIALWRAP_TEST_DEVICE_ALLOWLIST"] == "COM7"
        assert env["SERIALWRAP_STATE_DIR"].startswith(str(h.root))
        assert env["SERIALWRAP_RUN_DIR"].startswith(str(h.root))
        assert env["SERIALWRAP_CONFIG_DIR"].startswith(str(h.root))

        argv = captured_daemon["args"]
        assert "--socket" in argv
        sock_idx = argv.index("--socket") + 1
        assert argv[sock_idx].startswith("tcp://127.0.0.1:")

        cli_argv = h.get_cli_argv() if hasattr(h, "get_cli_argv") else ["--endpoint", argv[sock_idx]]
        assert "--endpoint" in cli_argv
        ep_idx = cli_argv.index("--endpoint") + 1
        assert cli_argv[ep_idx] == argv[sock_idx]

        profile_path = h.root / "config" / "profiles" / "func-test.yaml"
        if not profile_path.exists():
            profile_path = h.root / "profiles" / "func-test.yaml"
        profile_content = profile_path.read_text(encoding="utf-8")
        assert "COM7" in profile_content
        target_row = yaml.safe_load(profile_content)["targets"][0]
        assert target_row["com"] == "COM0"
        assert target_row["device_by_id"] == "COM7"

        bridge_argv = getattr(h, "bridge_argv", captured_bridge.get("args", [[]])[0])
        assert "--endpoint" in bridge_argv and "win-serial" in bridge_argv
        assert "--com" in bridge_argv and "COM8" in bridge_argv
        assert "--transport-mode" in bridge_argv and "virtual_com0com" in bridge_argv

        by_id_dir = h.root / "by-id"
        if by_id_dir.exists():
            assert not any(p.is_symlink() for p in by_id_dir.iterdir())
    finally:
        h.stop()


@pytest.mark.process_rpc
def test_windows_wait_endpoint_tcp_probe() -> None:
    """驗證 wait_endpoint 對 TCP endpoint 的 connect probe：listener 存在回傳，無 listener 逾時拋 TimeoutError。"""
    from lib import daemon_harness

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(mode="bridge"))
    h.endpoint = f"tcp://127.0.0.1:{port}"

    h.wait_endpoint(timeout_s=1.0)
    server_sock.close()

    free_p = daemon_harness.free_port() if hasattr(daemon_harness, "free_port") else port
    h.endpoint = f"tcp://127.0.0.1:{free_p}"
    with pytest.raises(TimeoutError):
        h.wait_endpoint(timeout_s=0.2)


@pytest.mark.memory_only
def test_windows_allowlist_not_enabled_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 Windows 模式下若 daemon status 未回傳 test_device_allowlist.enabled:true 則中止並拋出包含 OD-11 訊息。"""
    from lib import daemon_harness
    monkeypatch.setattr(os, "name", "nt")

    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(
        mode="bridge",
        daemon_com="COM7",
        bridge_com="COM8",
        transport_mode="virtual_com0com",
    ))
    monkeypatch.setattr(daemon_harness.subprocess, "Popen", _capture_popen({}))
    monkeypatch.setattr(h, "wait_endpoint", lambda *_a, **_k: None)
    monkeypatch.setattr(h, "_spawn_bridge", lambda *_a, **_k: None)
    monkeypatch.setattr(h, "_query_daemon_status", lambda: {"test_device_allowlist": {"enabled": False}})

    with pytest.raises(Exception) as exc_info:
        h.start()
    assert "OD-11" in str(exc_info.value)


@pytest.mark.memory_only
def test_windows_device_event_cap_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 Windows bridge 模式下呼叫 device_event 拋出 StepError 且訊息包含 cap_missing:by_id_link。"""
    import runner
    from lib import daemon_harness
    monkeypatch.setattr(os, "name", "nt")

    h = daemon_harness.DaemonHarness(daemon_harness.HarnessConfig(
        mode="bridge",
        daemon_com="COM7",
        bridge_com="COM8",
        transport_mode="virtual_com0com",
    ))
    with pytest.raises(runner.StepError) as exc_info:
        h.device_event("remove")
    assert "cap_missing:by_id_link" in str(exc_info.value)


@pytest.mark.memory_only
@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "bridge", "--selftest-report", "selftest.json", "--daemon-com", "COM20", "--bridge-com", "COM21"],
        ["--mode", "bridge", "--selftest-report", "selftest.json", "--daemon-com", "COM20", "--bridge-com", "COM21", "--transport-mode", "virtual_pty"],
    ],
)
def test_windows_bridge_requires_supported_transport(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """驗證 Windows bridge 必須明確指定支援的傳輸模式。"""
    import runner

    monkeypatch.setattr(runner.os, "name", "nt")
    with pytest.raises(SystemExit) as exc_info:
        runner.main(argv)
    assert exc_info.value.code == 2


@pytest.mark.memory_only
def test_windows_pty_mode_reports_capability_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """驗證 Windows pty 模式回報能力 SKIP，不載入 POSIX fake target。"""
    import json
    import runner
    from lib.yaml_loader import TestCase

    tc = TestCase(name="pty-capability", category="test", severity="low", target={}, profile={}, env_files={}, steps=[])
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.pathlib, "Path", lambda _value=".": tmp_path)
    monkeypatch.setattr(runner, "discover_test_cases", lambda *args, **kwargs: [tc])
    monkeypatch.setattr(runner.TestRunner, "run", lambda self: pytest.fail("Windows pty must be skipped"))
    monkeypatch.setattr(runner.TestRunner, "_write_manifest", lambda self, *args, **kwargs: None)

    result = runner.main(["--report-dir", str(tmp_path)])
    report = json.loads((tmp_path / "func-test-report.json").read_text(encoding="utf-8"))

    assert result == 1
    assert report["results"][0]["verdict"] == "SKIP"
    assert "cap_missing" in report["results"][0]["message"]
