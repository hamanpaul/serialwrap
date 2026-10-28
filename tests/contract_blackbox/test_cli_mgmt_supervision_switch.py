"""P4-T12 `supervision-mode set` 的交接護欄黑箱契約（SC-22，R1-F3）。

規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；
TTY 與 DUT I/O 經 P2 UART stdio bridge，測試資料只落在 pytest 暫存目錄。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from bb_harness import cli_prefix, isolated_env
from bb_l15 import (
    BridgeTarget,
    daemon_pid,
    finish_test_rig,
    json_stdout,
    make_push_file,
    prepare_profile,
    rpc,
    run_cli,
    start_file_push,
    start_python_daemon,
    stop_current_daemon,
    stop_owned_python_daemon,
    tx_bytes,
    wait_rx_contains,
    wait_session,
)
from bb_stubd import StubDaemon

pytestmark = [pytest.mark.posix_only]
ROOT = Path(__file__).resolve().parents[2]


def _short_case_root() -> Path:
    """Unix socket 名稱保留於工作樹短路徑，避免 pytest 暫存路徑超過 AF_UNIX 上限。"""
    parent = ROOT / "target" / "g5-tmp"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / f"s{os.getpid() % 10000:04d}{time.time_ns() % 1000000:06d}"
    root.mkdir()
    return root


def _rust_cli() -> Path:
    prefix = cli_prefix()
    assert len(prefix) == 1, f"Rust-only 測試需要單一 CLI executable：{prefix}"
    cli = Path(prefix[0])
    assert cli.is_file(), f"受測 CLI 不存在：{cli}"
    return cli


def _systemctl_stub(tmp_path: Path, env: dict[str, str]) -> Path:
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "systemctl.log"
    stub = stub_bin / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SW_G5_SYSTEMCTL_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    env["SW_G5_SYSTEMCTL_LOG"] = str(log)
    return log


def _stop_rig(bridge: BridgeTarget, daemon, cli: Path, env: dict[str, str]) -> None:
    finish_test_rig(bridge, daemon, cli, env)


def _wait_holder_named_serialwrapd(pid: int, endpoint: str, timeout_s: float = 10.0) -> None:
    """等待 holder 行程的 argv0 變為 serialwrapd 且 cmdline 帶有本案例的 `--socket`（exec 完成）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            parts = []
        if parts and os.path.basename(parts[0].decode("utf-8", errors="replace")) == "serialwrapd" and endpoint.encode() in parts:
            return
        time.sleep(0.02)
    raise AssertionError(f"holder pid={pid} 未在 {timeout_s:.0f} 秒內以 serialwrapd 身分出現於 /proc")


def _prepare_flash_profile(env: dict[str, str]) -> Path:
    """以 passthrough 將 COM0 設為非 command-capable flash 候選。"""
    profile_dir = Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "l15.yaml").write_text(
        "profiles:\n"
        "  l15-flash:\n"
        "    platform: passthrough\n"
        "    ready_probe: ''\n"
        "targets:\n"
        "  - com: COM0\n"
        "    profile: l15-flash\n"
        "    device_by_id: usb-FAKE_TARGET_0-if00\n",
        encoding="utf-8",
    )
    config = Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
    if not config.exists():
        config.write_text("supervision_mode: on-demand\n", encoding="utf-8")
    return profile_dir


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.pidns_rig
def test_supervision_set_force_push_continues_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """1 MiB file push 進行時，force 切換必以 SESSION_BUSY 拒絕且不呼叫 systemctl。"""
    cli = _rust_cli()
    prepare_profile(env)
    systemctl_log = _systemctl_stub(tmp_path, env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env, delay_ms=1500)
    daemon = None
    push_thread = None
    try:
        daemon = start_python_daemon(env, Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles")
        wait_session(env)
        old_pid = daemon_pid(env)
        config = Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
        config_before = config.read_bytes()
        source = tmp_path / "supervision-push.bin"
        make_push_file(source)
        remote = f"/tmp/{source.name}"
        push_thread, push_rows = start_file_push(cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        assert push_thread.is_alive(), "file push 在切換命令開始前已結束"

        result = run_cli(cli, ["supervision-mode", "set", "systemd-user", "--force"], env)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert data.get("phase") == "supervision_set_safety_check", data
        assert data.get("force_ignored") is True, data
        assert daemon_pid(env) == old_pid
        assert config.read_bytes() == config_before
        assert not systemctl_log.exists() or systemctl_log.read_text(encoding="utf-8") == ""

        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push = push_rows[0]
        push_data = json_stdout(push)
        assert push.returncode == 0, push.stdout + push.stderr
        assert push_data.get("bytes") == len(source.read_bytes()), push_data
        actual = (bridge.remote_root / source.name).read_bytes()
        assert hashlib.sha256(actual).hexdigest() == hashlib.sha256(source.read_bytes()).hexdigest()
    finally:
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        if daemon is not None:
            _stop_rig(bridge, daemon, cli, env)
        else:
            bridge.stop()


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_supervision_set_flashing_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """P2 DUT 命中 TI sync 後維持 FLASHING；force 切換拒絕且 wire TX 不再增加。"""
    cli = _rust_cli()
    profile_dir = _prepare_flash_profile(env)
    tty_mcu = tmp_path / "dev" / "ttyMCU"
    env["SERIALWRAP_TTYMCU_PATH"] = str(tty_mcu)
    bridge = BridgeTarget.start(tmp_path / "bridge", env)
    daemon = None
    flash_fd: int | None = None
    try:
        daemon = start_python_daemon(env, profile_dir, state="ATTACHED")
        wait_session(env, state="ATTACHED")
        old_pid = daemon_pid(env)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not tty_mcu.exists():
            time.sleep(0.02)
        assert tty_mcu.is_symlink(), f"隔離 ttyMCU endpoint 未建立：{tty_mcu}"
        flash_fd = os.open(tty_mcu, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        os.write(flash_fd, b"\x55\x55")

        session = wait_session(env, state="FLASHING", timeout_s=20.0)
        assert session.get("com") == "COM0", session
        mcu = rpc(env, "mcu.status")
        assert mcu.get("flashing") is True, mcu
        tx_before = tx_bytes(bridge.evidence_dir)

        result = run_cli(cli, ["supervision-mode", "set", "on-demand", "--force"], env)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "FLASHING_BUSY", data
        assert data.get("phase") == "supervision_set_safety_check", data
        assert data.get("force_ignored") is True, data
        assert daemon_pid(env) == old_pid
        assert wait_session(env, state="FLASHING")["state"] == "FLASHING"
        assert rpc(env, "mcu.status").get("flashing") is True
        assert tx_bytes(bridge.evidence_dir) == tx_before
    finally:
        if flash_fd is not None:
            os.close(flash_fd)
        if daemon is not None:
            try:
                wait_session(env, state="ATTACHED", timeout_s=5.0)
            except AssertionError:
                pass
            _stop_rig(bridge, daemon, cli, env)
        else:
            bridge.stop()


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.process_rpc
@pytest.mark.pidns_rig
def test_supervision_set_rust_only_switch(tmp_path: Path, env: dict[str, str]) -> None:
    """Stubd 交接成功走 systemd-user reconcile；METHOD_NOT_FOUND fail-closed 且不送 daemon.stop。"""
    cli = _rust_cli()

    def run_case(root: Path, *, handoff_ok: bool) -> None:
        case_env = isolated_env(root)
        case_env.pop("USER", None)
        case_env.pop("LOGNAME", None)
        systemctl_log = _systemctl_stub(root, case_env)
        stub = StubDaemon(root / "stubd", {}, transport="unix")
        # POSIX supervision-mode set 走 local_default_endpoint（$SERIALWRAP_RUN_DIR/serialwrapd.sock），
        # 不讀 SERIALWRAP_ENDPOINT 或 config socket_path；stub 必須綁在同一路徑，否則 methods 為空。
        run_dir = Path(case_env["SERIALWRAP_RUN_DIR"])
        run_dir.mkdir(parents=True, exist_ok=True)
        stub.socket_path = run_dir / "serialwrapd.sock"
        stub.start()
        endpoint = stub.endpoint
        config = Path(case_env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"
        config.write_text(
            f"supervision_mode: on-demand\nsocket_path: {json.dumps(endpoint)}\n",
            encoding="utf-8",
        )
        case_env["SERIALWRAP_ENDPOINT"] = endpoint
        holder = subprocess.Popen(
            [
                "bash", "-c",
                'exec -a serialwrapd "$(command -v python3)" -c "import time; time.sleep(300)" --socket "$1"',
                "serialwrapd-holder", endpoint,
            ],
            env=case_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # 競態修正：bash 尚未 exec 成 `serialwrapd` 時，CLI 的安全檢查會把 holder 視為未知（DEVICE_STILL_HELD、
        # holder_unknown），不送 daemon.handoff_stop。必須等 /proc 中的 argv0 確實為 serialwrapd 才繼續。
        _wait_holder_named_serialwrapd(holder.pid, endpoint)
        handoff_reply = (
            {"ok": True, "stopping": True, "handoff_token": "a" * 32}
            if handoff_ok
            else {"ok": False, "error_code": "METHOD_NOT_FOUND"}
        )
        stub.script = {
            "methods": {
                "health.ping": [{"reply": {"ok": True}}],
                "mcu.status": [{"reply": {"ok": True, "flashing": False}}],
                "session.list": [{"reply": {"ok": True, "sessions": []}}],
                "health.status": [{"reply": {"ok": True, "pid": holder.pid, "version": "0.4.1"}}],
                "daemon.handoff_stop": [{"reply": handoff_reply}],
            }
        }
        watcher_stop = threading.Event()
        watcher_errors: list[str] = []

        def finish_after_grant() -> None:
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not watcher_stop.is_set():
                requests = [json.loads(line) for line in stub.requests()]
                if any(row.get("method") == "daemon.handoff_stop" for row in requests):
                    time.sleep(0.1)
                    try:
                        holder.terminate()
                        holder.wait(timeout=5.0)
                        stub.stop()
                    except BaseException as exc:
                        watcher_errors.append(f"{type(exc).__name__}: {exc}")
                    return
                time.sleep(0.01)

        watcher = None
        if handoff_ok:
            watcher = threading.Thread(target=finish_after_grant, daemon=True)
            watcher.start()
        config_before = config.read_bytes()
        try:
            result = run_cli(cli, ["supervision-mode", "set", "systemd-user"], case_env, timeout_s=90.0)
            requests = [json.loads(line) for line in stub.requests()]
            methods = [row.get("method", "") for row in requests]
            assert methods.count("daemon.handoff_stop") == 1, methods
            assert "daemon.stop" not in methods, methods
            if handoff_ok:
                if watcher is not None:
                    watcher.join(5.0)
                assert not watcher_errors, watcher_errors
                data = json_stdout(result)
                assert result.returncode == 0, result.stdout + result.stderr
                assert data.get("previous_mode") == "on-demand", data
                assert data.get("setup", {}).get("mode") == "systemd-user", data
                assert config.read_text(encoding="utf-8").find("supervision_mode: systemd-user") >= 0
                calls = systemctl_log.read_text(encoding="utf-8").splitlines()
                assert "--user daemon-reload" in calls, calls
                assert "--user enable serialwrap" in calls, calls
                assert "--user start serialwrap" in calls, calls
                ran = data.get("setup", {}).get("ran", [])
                assert ["serialwrap", "daemon", "handoff_stop"] in ran, ran
                assert ["serialwrap", "daemon", "stop"] not in ran, ran
            else:
                data = json_stdout(result)
                assert result.returncode == 2, result.stdout + result.stderr
                assert data.get("error_code") == "METHOD_NOT_FOUND", data
                assert data.get("probe") == "daemon.handoff_stop", data
                assert data.get("handoff_outcome") == "refused", data
                assert config.read_bytes() == config_before
                assert not systemctl_log.exists() or systemctl_log.read_text(encoding="utf-8") == ""
                assert holder.poll() is None, "unsupported handoff 不得停止 daemon"
        finally:
            watcher_stop.set()
            if watcher is not None:
                watcher.join(2.0)
            stub.stop()
            if holder.poll() is None:
                holder.send_signal(signal.SIGTERM)
                holder.wait(timeout=5.0)
            shutil.rmtree(root, ignore_errors=True)

    run_case(_short_case_root(), handoff_ok=True)
    run_case(_short_case_root(), handoff_ok=False)
