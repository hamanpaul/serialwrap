"""Stage 1B 管理面 L1.5 整合與不變量測試（P4-T16）。

以 P2 UART stdio bridge 建立隔離 PTY，並由 bridge `wire.ndjson` 與 `control.ndjson`
驗證實際 DUT byte 與 tty holder PID；測試不開啟主機上的實體序列埠。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bb_harness import cli_prefix
from bb_l15 import (
    BridgeTarget,
    control_marks,
    daemon_pid,
    installed_version,
    endpoint,
    finish_test_rig,
    json_stdout,
    prepare_profile,
    rx_bytes,
    rpc,
    run_cli,
    select_bundle,
    start_file_push,
    start_python_daemon,
    stop_current_daemon,
    stop_owned_python_daemon,
    make_install_root as setup_install_root,
    tx_bytes,
    update_bundles,
    wait_holder,
    wait_pause,
    wait_rx_contains,
    wait_session,
    wait_wire_contains,
    make_push_file,
    wal_rows,
)

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]


def _install_old(bundle: Path, env: dict[str, str], tmp_path: Path) -> Path:
    cli = select_bundle(env, bundle)
    setup_install_root(tmp_path, env)
    prepare_profile(env)
    result = run_cli(cli, ["setup", "--on-demand"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    return cli


def _start_test_rig(tmp_path: Path, env: dict[str, str], *, delay_ms: int = 1):
    profile_dir = prepare_profile(env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env, delay_ms=delay_ms)
    daemon = None
    try:
        daemon = start_python_daemon(env, profile_dir)
        return bridge, daemon
    except BaseException:
        bridge.stop()
        raise


def _finish_test_rig(bridge: BridgeTarget, daemon, cli: Path, env: dict[str, str]) -> None:
    finish_test_rig(bridge, daemon, cli, env)


def _serialwrapd_pids() -> set[int]:
    """掃描 /proc 中 cmdline 含 serialwrapd 的行程；非 Linux（無 /proc）回傳空集合。"""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return set()
    found: set[int] = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if b"serialwrapd" in cmdline:
            found.add(int(entry.name))
    return found


@pytest.mark.rust_only
@pytest.mark.process_rpc
def test_autostart_l15_tx_once(tmp_path: Path, env: dict[str, str]) -> None:
    """依平台的 on-demand 拉起契約（SC-18 僅限 Windows native，見 plan 05 `on_demand_autostart`）。

    - Windows：無 daemon 時 `cmd submit` 自動拉起；DUT wire 對唯一命令恰 TX 一次（原正向契約不變）。
    - POSIX：無 daemon 時**不得**自動拉起（exit 2／SOCKET_ERROR）；DUT wire TX 不增加；不 spawn 新的 serialwrapd。
    節點名保留以維持 l1_required.lock.json 的凍結 selector（改名屬需 owner 核准的移除流程）。
    """
    _, latest = update_bundles()
    cli = select_bundle(env, latest)
    prepare_profile(env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env)
    marker = f"L15_AUTOSTART_{os.getpid()}_{time.time_ns()}"
    if os.name == "nt":
        try:
            result = run_cli(cli, ["cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}"], env, timeout_s=120.0)
            assert result.returncode == 0, result.stdout + result.stderr
            assert marker.encode() in tx_bytes(bridge.evidence_dir)
            assert tx_bytes(bridge.evidence_dir).count(f"echo {marker}".encode()) == 1
            wait_session(env)
            current_pid = daemon_pid(env)
            stop_current_daemon(cli, env, current_pid)
        finally:
            stop_current = False
            try:
                stop_current = rpc(env, "health.status").get("ok") is True
            except AssertionError:
                pass
            if stop_current:
                stop_current_daemon(cli, env)
            bridge.stop()
        return
    tx_before = tx_bytes(bridge.evidence_dir)
    procs_before = _serialwrapd_pids()
    try:
        result = run_cli(cli, ["cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}"], env, timeout_s=60.0)
        output = result.stdout + result.stderr
        assert result.returncode == 2, output
        assert "SOCKET_ERROR" in output, output
        assert tx_bytes(bridge.evidence_dir) == tx_before, "POSIX 無 daemon 時 DUT wire 不得新增 TX"
        assert _serialwrapd_pids() - procs_before == set(), "POSIX 不得 spawn 新的 serialwrapd"
    finally:
        bridge.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_busy_l15_zero_tx(tmp_path: Path, env: dict[str, str]) -> None:
    """背景 hold 命令期間更新回 SESSION_BUSY，拒絕前後 DUT TX delta 為零。"""
    old, new = update_bundles()
    old_cli = _install_old(old, env, tmp_path)
    bridge, daemon = _start_test_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    cmd_id = None
    try:
        submitted = rpc(env, "command.submit", {"selector": "COM0", "cmd": "hold", "mode": "background", "timeout_s": 120})
        assert submitted.get("ok") is True and isinstance(submitted.get("cmd_id"), str), submitted
        cmd_id = submitted["cmd_id"]
        wait_wire_contains(bridge.evidence_dir, b"hold\r")
        tx_before = tx_bytes(bridge.evidence_dir)

        result = run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=120.0)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert tx_bytes(bridge.evidence_dir) == tx_before
        assert daemon_pid(env) == daemon.proc.pid
        assert installed_version(env) == "0.4.0"
    finally:
        if cmd_id:
            rpc(env, "command.cancel", {"cmd_id": cmd_id})
        _finish_test_rig(bridge, daemon, old_cli, env)


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_file_push_after_last_probe_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """最後預檢後開始 1 MiB push，更新必拒絕交接且不切換；push 完成並驗 MD5。"""
    old, new = update_bundles()
    old_cli = _install_old(old, env, tmp_path)
    bridge, daemon = _start_test_rig(tmp_path, env, delay_ms=10)
    new_cli = select_bundle(env, new)
    source = tmp_path / "last-probe.bin"
    expected_md5, remote = make_push_file(source)
    pause_dir = tmp_path / "pause-safety-check"
    env["SW_TEST_UPDATE_PAUSE_AFTER"] = "safety_check"
    env["SW_TEST_UPDATE_PAUSE_DIR"] = str(pause_dir)
    result_rows = []
    update_thread = threading.Thread(
        target=lambda: result_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
        daemon=True,
    )
    old_pid = daemon_pid(env)
    current_bytes = (Path(env["SERIALWRAP_INSTALL_ROOT"]) / "bin" / "current-release.json").read_bytes()
    try:
        update_thread.start()
        wait_pause(pause_dir, "safety_check")
        push_thread, push_rows = start_file_push(new_cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        bridge.mark("push-started", old_pid)
        assert push_thread.is_alive()
        pause_dir.joinpath("safety_check.resume").write_text("resume\n", encoding="utf-8")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        result = result_rows[0]
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert data.get("handoff") == "refused", data
        assert daemon_pid(env) == old_pid
        assert installed_version(env) == "0.4.0"
        assert (Path(env["SERIALWRAP_INSTALL_ROOT"]) / "bin" / "current-release.json").read_bytes() == current_bytes
        assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in wal_rows(env))
        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push_data = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push_data.get("bytes") == 1024 * 1024
        assert push_data.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()
    finally:
        if update_thread.is_alive():
            pause_dir.joinpath("safety_check.resume").write_text("resume\n", encoding="utf-8")
            update_thread.join(15.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        _finish_test_rig(bridge, daemon, old_cli, env)


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
def test_update_push_after_freeze_zero_tx_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """handoff_stop 已成功後 file push 回 FLUSHED_BY_SHUTDOWN，凍結期間 DUT TX 為零且無部分檔。"""
    old, new = update_bundles()
    old_cli = _install_old(old, env, tmp_path)
    bridge, daemon = _start_test_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    source = tmp_path / "frozen-push.bin"
    make_push_file(source)
    pause_dir = tmp_path / "pause-handoff"
    env["SW_TEST_UPDATE_PAUSE_AFTER"] = "handoff_stop:replied"
    env["SW_TEST_UPDATE_PAUSE_DIR"] = str(pause_dir)
    result_rows = []
    update_thread = threading.Thread(
        target=lambda: result_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
        daemon=True,
    )
    try:
        update_thread.start()
        wait_pause(pause_dir, "handoff_stop:replied")
        bridge.mark("handoff-frozen", os.getpid())
        frozen_tx = tx_bytes(bridge.evidence_dir)
        push = run_cli(new_cli, ["file", "push", "--selector", "COM0", "--local", str(source), "--remote", "/tmp/frozen-push.bin", "--ack-mode", "echo"], env, timeout_s=30.0)
        push_data = json_stdout(push)
        assert push.returncode == 2, push.stdout + push.stderr
        assert push_data.get("error_code") == "FLUSHED_BY_SHUTDOWN", push_data
        assert push_data.get("reason") == "handoff_frozen", push_data
        assert tx_bytes(bridge.evidence_dir) == frozen_tx
        assert not any(bridge.remote_root.iterdir()), list(bridge.remote_root.iterdir()) if bridge.remote_root.exists() else []
        pause_dir.joinpath("handoff_stop_replied.resume").write_text("resume\n", encoding="utf-8")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        result = result_rows[0]
        assert result.returncode == 0, result.stdout + result.stderr
        assert installed_version(env) == "0.4.1"
        assert wait_session(env)["state"] == "READY"
    finally:
        if update_thread.is_alive():
            pause_dir.joinpath("handoff_stop_replied.resume").write_text("resume\n", encoding="utf-8")
            update_thread.join(15.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        current_pid = None
        try:
            current_pid = daemon_pid(env)
        except AssertionError:
            pass
        if current_pid is not None:
            stop_current_daemon(new_cli, env, current_pid)
        stop_owned_python_daemon(daemon, env)
        bridge.stop()


@pytest.mark.case("bc05-process-lifecycle-stop-restart")
def test_reject_conflict_l15_zero_tx(tmp_path: Path, env: dict[str, str]) -> None:
    """外部 serialwrapd holder 佔住 bridge tty 時，第二個 daemon start 被拒且 DUT TX 為零。"""
    prepare_profile(env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env)
    other_socket = str(tmp_path / "other.sock")
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 3<>"$1"; exec -a serialwrapd sleep 120', "holder", bridge.slave_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pid = wait_holder(bridge.slave_path, present=True)
        assert pid == holder.pid
        bridge.mark("opened", pid)
        tx_before = tx_bytes(bridge.evidence_dir)
        cli = Path(cli_prefix()[-1])
        result = run_cli(cli, ["--socket", other_socket, "daemon", "start"], env)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "DAEMON_ALREADY_RUNNING_ELSEWHERE", data
        assert tx_bytes(bridge.evidence_dir) == tx_before
    finally:
        holder.terminate()
        holder.wait(timeout=5.0)
        wait_holder(bridge.slave_path, holder.pid, present=False)
        bridge.mark("closed", holder.pid)
        marks = control_marks(bridge.evidence_dir)
        opened = next((index for index, row in enumerate(marks)
                       if row.get("params", {}).get("label") == "opened"
                       and row.get("params", {}).get("op_id") == str(holder.pid)), None)
        closed = next((index for index, row in enumerate(marks)
                       if row.get("params", {}).get("label") == "closed"
                       and row.get("params", {}).get("op_id") == str(holder.pid)), None)
        assert opened is not None and closed is not None and opened < closed, marks
        bridge.stop()


@pytest.mark.case("sc20-daemon-restart")
def test_restart_no_stale_console_replay(tmp_path: Path, env: dict[str, str]) -> None:
    """console 正向控制確實抵達 DUT；restart 後不得回放未送出的 partial line。"""
    prepare_profile(env)
    bridge, daemon = _start_test_rig(tmp_path, env)
    cli = Path(cli_prefix()[-1])
    consoles = []
    console_fds: list[int] = []
    try:
        for label in ("primary", "buffered"):
            attached = rpc(env, "session.console_attach", {"selector": "COM0", "label": label})
            assert attached.get("ok") is True and attached.get("vtty"), attached
            consoles.append(attached["client_id"])
            console_fds.append(os.open(attached["vtty"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))
        control_marker = f"CONSOLE_CONTROL_{time.time_ns()}"
        # 第一個 console 持有 raw interactive；正向控制從該入口送出，避免第二個
        # line-buffer console 受既有 owner 的仲裁狀態影響。
        os.write(console_fds[0], f"echo {control_marker}\n".encode())
        wait_rx_contains(bridge.evidence_dir, control_marker.encode())
        assert f"echo {control_marker}".encode() in tx_bytes(bridge.evidence_dir)

        stale_marker = f"STALE_CONSOLE_{time.time_ns()}"
        tx_before_partial = tx_bytes(bridge.evidence_dir)
        os.write(console_fds[1], f"echo {stale_marker}".encode())
        time.sleep(0.15)
        assert tx_bytes(bridge.evidence_dir) == tx_before_partial
        assert stale_marker.encode() not in rx_bytes(bridge.evidence_dir)

        old_pid = daemon_pid(env)
        wait_holder(bridge.slave_path, old_pid, present=True)
        bridge.mark("opened", old_pid)
        restarted = run_cli(cli, ["daemon", "restart"], env, timeout_s=120.0)
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        new_pid = daemon_pid(env)
        assert new_pid != old_pid
        wait_holder(bridge.slave_path, old_pid, present=False)
        bridge.mark("closed", old_pid)
        wait_holder(bridge.slave_path, new_pid, present=True)
        bridge.mark("opened", new_pid)
        wait_session(env)
        tx_after_restart = tx_bytes(bridge.evidence_dir)
        time.sleep(0.25)
        assert tx_bytes(bridge.evidence_dir) == tx_after_restart
        assert stale_marker.encode() not in tx_bytes(bridge.evidence_dir)
        assert stale_marker.encode() not in rx_bytes(bridge.evidence_dir)

        marks = control_marks(bridge.evidence_dir)
        old_closed = next(i for i, row in enumerate(marks) if row.get("params", {}).get("label") == "closed" and row.get("params", {}).get("op_id") == str(old_pid))
        new_opened = next(i for i, row in enumerate(marks) if row.get("params", {}).get("label") == "opened" and row.get("params", {}).get("op_id") == str(new_pid))
        assert old_closed < new_opened, marks
    finally:
        for fd in console_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        for client_id in consoles:
            try:
                rpc(env, "session.console_detach", {"selector": "COM0", "client_id": client_id})
            except AssertionError:
                # restart 失敗時 endpoint 可能已停止；後續仍須回收隔離 daemon 與 bridge。
                pass
        _finish_test_rig(bridge, daemon, cli, env)


@pytest.mark.case("sc20-daemon-restart")
@pytest.mark.rust_only
def test_restart_no_reopen_before_handoff(tmp_path: Path, env: dict[str, str]) -> None:
    """P2 control marks 配對每個 daemon PID，並證明舊 tty 關閉後新 daemon 才開啟。"""
    prepare_profile(env)
    bridge, daemon = _start_test_rig(tmp_path, env)
    cli = Path(cli_prefix()[-1])
    try:
        old_pid = daemon_pid(env)
        wait_holder(bridge.slave_path, old_pid, present=True)
        bridge.mark("opened", old_pid)

        restarted = run_cli(cli, ["daemon", "restart"], env, timeout_s=120.0)
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        new_pid = daemon_pid(env)
        assert new_pid != old_pid
        wait_holder(bridge.slave_path, old_pid, present=False)
        bridge.mark("closed", old_pid)
        wait_holder(bridge.slave_path, new_pid, present=True)
        bridge.mark("opened", new_pid)

        marks = control_marks(bridge.evidence_dir)
        old_open = next((i for i, row in enumerate(marks)
                         if row.get("params", {}).get("label") == "opened"
                         and row.get("params", {}).get("op_id") == str(old_pid)), None)
        old_close = next((i for i, row in enumerate(marks)
                          if row.get("params", {}).get("label") == "closed"
                          and row.get("params", {}).get("op_id") == str(old_pid)), None)
        new_open = next((i for i, row in enumerate(marks)
                         if row.get("params", {}).get("label") == "opened"
                         and row.get("params", {}).get("op_id") == str(new_pid)), None)
        assert old_open is not None and old_close is not None and new_open is not None, marks
        assert old_open < old_close < new_open, marks
        assert wait_session(env)["state"] == "READY"
    finally:
        _finish_test_rig(bridge, daemon, cli, env)
