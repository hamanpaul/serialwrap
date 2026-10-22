"""P4-T12 `setup` 更新與 `setup --rollback` 黑箱契約（SC-23）。

規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；所有 tty 互動經隔離 P2 bridge。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bb_harness import isolated_env, repo_root
from bb_l15 import (
    BridgeTarget,
    RaisingThread,
    current_release_path,
    daemon_pid,
    finish_test_rig,
    installed_version,
    json_stdout,
    make_push_file,
    prepare_profile,
    resume_pause,
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
    wait_pause,
    wait_rx_contains,
    wait_session,
    wait_wire_contains,
    wal_rows,
)

pytestmark = [pytest.mark.posix_only]

# busy 案例的 push 大小（依 ACK-gated 路徑實測吞吐決定，見該案例 docstring）。
BUSY_PUSH_BYTES = 16 * 1024
# 首個 chunk 的 ACK 延遲（毫秒）：須大於 setup 被拒所需時間。量測（WSL2 bridge）：in-flight push 期間
# `setup --on-demand` 被拒約 7.2 s；取 20 s（餘裕約 13 s）。
# 單一 chunk 等待逾時須大於此延遲，否則 CLI 在 ACK 到來前已逾時並送出清理指令（`SW_XFER_CLEANUP_`），
# 該 TX 會落在拒絕視窗內、破壞零 TX 斷言；故另以 `chunk_timeout_s` 放寬（見 BUSY_CHUNK_TIMEOUT_S）。
BUSY_FIRST_ACK_DELAY_MS = 20000
BUSY_CHUNK_TIMEOUT_S = BUSY_FIRST_ACK_DELAY_MS / 1000 + 40


def _install_old(tmp_path: Path, env: dict[str, str], bundle: Path) -> tuple[Path, Path]:
    cli = select_bundle(env, bundle)
    setup_install_root(tmp_path, env)
    prepare_profile(env)
    result = run_cli(cli, ["setup", "--on-demand"], env)
    assert result.returncode == 0, result.stdout + result.stderr
    return cli, Path(env["SERIALWRAP_INSTALL_ROOT"])


def _start_rig(tmp_path: Path, env: dict[str, str], *, delay_ms: int = 1):
    profile_dir = prepare_profile(env)
    bridge = BridgeTarget.start(tmp_path / "bridge", env, delay_ms=delay_ms)
    try:
        daemon = start_python_daemon(env, profile_dir)
    except BaseException:
        bridge.stop()
        raise
    return bridge, daemon


def _stop_rig(bridge: BridgeTarget, daemon, cli: Path, env: dict[str, str]) -> None:
    finish_test_rig(bridge, daemon, cli, env)


@pytest.mark.rust_only
@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.process_rpc
@pytest.mark.pidns_rig
def test_fresh_install_blackbox(tmp_path: Path) -> None:
    """隔離 HOME＋空 install root＋stub PATH：setup --on-demand → daemon start → session list；profiles／config 存在。"""
    env = isolated_env(tmp_path)
    # 預設讀本測試專用的 mkbundle 產物 target/mkbundle-pytest（非共享的 target/mkbundle-dev，後者可能是舊版無標記目錄）。
    sw_bundle = os.environ.get("SW_BUNDLE", str(repo_root() / "target" / "mkbundle-pytest"))
    cli_bin = Path(sw_bundle) / "bin" / "serialwrap"
    assert cli_bin.is_file(), f"缺少 bundle CLI：{cli_bin}（請先 scripts/rust/mkbundle.sh --dev --out {sw_bundle}，或設 SW_BUNDLE）"
    daemon_started = False
    start_attempted = False
    try:
        res = subprocess.run([str(cli_bin), "setup", "--on-demand"], env=env, capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stdout + res.stderr
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml").exists()
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles" / "default.yaml").exists()
        start_attempted = True
        res_start = subprocess.run([str(cli_bin), "daemon", "start"], env=env, capture_output=True, text=True, timeout=30)
        daemon_started = res_start.returncode == 0
        assert res_start.returncode == 0, res_start.stdout + res_start.stderr
        res_list = subprocess.run([str(cli_bin), "session", "list"], env=env, capture_output=True, text=True, timeout=30)
        assert res_list.returncode == 0, res_list.stdout + res_list.stderr
    finally:
        active_error = sys.exc_info()[1]
        if start_attempted:
            try:
                status = subprocess.run(
                    [str(cli_bin), "daemon", "status"], env=env, capture_output=True, text=True,
                    timeout=30, check=False,
                )
                payload = json.loads(status.stdout) if status.stdout else {}
                if daemon_started or payload.get("ok") is True:
                    stopped = subprocess.run(
                        [str(cli_bin), "daemon", "stop"], env=env, capture_output=True, text=True,
                        timeout=30, check=False,
                    )
                    if stopped.returncode != 0:
                        raise RuntimeError(stopped.stdout + stopped.stderr)
            except BaseException as cleanup_error:
                if active_error is None:
                    raise
                active_error.add_note(
                    f"隔離 fresh-install daemon 清理失敗：{type(cleanup_error).__name__}: {cleanup_error}"
                )


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_rollback_ab_isolated(tmp_path: Path, env: dict[str, str]) -> None:
    """透過 bridge 以 0.4.0 安裝、更新至 0.4.1，再以真正 rollback 還原 0.4.0。"""
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    try:
        before_pid = daemon_pid(env)
        update = run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=180.0)
        assert update.returncode == 0, update.stdout + update.stderr
        assert installed_version(env) == "0.4.1"
        after_update_pid = daemon_pid(env)
        assert after_update_pid != before_pid
        assert wait_session(env)["state"] == "READY"

        rollback = run_cli(new_cli, ["setup", "--rollback"], env, timeout_s=180.0)
        assert rollback.returncode == 0, rollback.stdout + rollback.stderr
        assert installed_version(env) == "0.4.0"
        status = rpc(env, "health.status")
        assert status.get("ok") is True and status.get("version") == "0.4.0", status
        assert daemon_pid(env) != after_update_pid
    finally:
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_busy_l15_zero_tx(tmp_path: Path, env: dict[str, str]) -> None:
    """file push 正在 target 上處理 chunk 時更新回 SESSION_BUSY，更新期間 bridge DUT TX 無新增。

    時序設計（量測依據）：push 採 ACK-gated（`ack_mode=auto`，每個 chunk 等板端 `SW_XFER_CHUNK_42`），
    DUT 於首個 chunk 延遲 `BUSY_FIRST_ACK_DELAY_MS` 才回 ACK。CLI 在 ACK 到來前不送下一個 chunk，故首個 chunk TX 之後、
    ACK 之前的視窗內 TX 靜止；更新必須在該視窗內被拒，TX 差值為零。`wait_wire_contains` 對 TX 比對的是
    chunk 指令文字（`SW_XFER_CHUNK_` 前綴，數值由目標端 `$((6*7))` 展開），而非 ACK 字串 `SW_XFER_CHUNK_42`。

    檔案大小依量測：ACK-gated 路徑於 WSL2 bridge 實測約 2.2 KiB/s（64 KiB 約 30 s），因此本案例改用 16 KiB
    （4 個 chunk，約 8 s 完成＋首個 ACK 延遲 `BUSY_FIRST_ACK_DELAY_MS`），仍為多 chunk 且 push 全程在 join 逾時內結束。
    """
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=BUSY_FIRST_ACK_DELAY_MS)
    new_cli = select_bundle(env, new)
    source = tmp_path / "busy-push.bin"
    expected_md5, remote = make_push_file(source, size=BUSY_PUSH_BYTES)
    push_thread = None
    try:
        push_thread, push_rows = start_file_push(new_cli, env, source, remote, ack_mode="auto", chunk_timeout_s=BUSY_CHUNK_TIMEOUT_S)
        # 等整行 chunk 指令（含行尾換行）都已寫入 TX，避免快照落在半行上；此後 CLI 等待首個 ACK，TX 靜止。
        wait_wire_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_'$((6*7))\n", timeout_s=30.0)
        tx_before = tx_bytes(bridge.evidence_dir)
        result = run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=30.0)
        data = json_stdout(result)
        assert result.returncode == 2, result.stdout + result.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert tx_bytes(bridge.evidence_dir) == tx_before
        assert daemon_pid(env) == daemon.proc.pid
        push_thread.join(180.0)
        assert not push_thread.is_alive(), f"{BUSY_PUSH_BYTES} B file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == BUSY_PUSH_BYTES and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()
    finally:
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_file_push_after_last_probe_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """最後預檢後再開始 1 MiB push，交接拒絕且 Current／PID 不變；push 完成並驗 MD5。"""
    old, new = update_bundles()
    old_cli, root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=500)
    new_cli = select_bundle(env, new)
    source = tmp_path / "last-probe.bin"
    expected_md5, remote = make_push_file(source)
    pause_dir = tmp_path / "pause-safety-check"
    env.update({"SW_TEST_UPDATE_PAUSE_AFTER": "safety_check", "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir)})
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    push_thread = None
    old_pid = daemon_pid(env)
    current_bytes = current_release_path(root).read_bytes()
    try:
        update_thread.start()
        wait_pause(pause_dir, "safety_check")
        push_thread, push_rows = start_file_push(new_cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        assert push_thread.is_alive()
        resume_pause(pause_dir, "safety_check")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        update = update_rows[0]
        data = json_stdout(update)
        assert update.returncode == 2, update.stdout + update.stderr
        assert data.get("error_code") == "SESSION_BUSY", data
        assert data.get("handoff") == "refused", data
        assert daemon_pid(env) == old_pid
        assert installed_version(env) == "0.4.0"
        assert current_release_path(root).read_bytes() == current_bytes
        assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in wal_rows(env))
        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == 1024 * 1024 and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "safety_check")
            update_thread.join(15.0)
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_revert_new_daemon_push_not_aborted_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """新版 daemon 已啟動後 push 持續阻止回滾 handoff；回滾待復原，push 完成後由舊 bundle 恢復。"""
    old, new = update_bundles()
    old_cli, root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env, delay_ms=500)
    new_cli = select_bundle(env, new)
    pause_dir = tmp_path / "pause-new-daemon"
    env.update({
        "SW_TEST_UPDATE_PAUSE_AFTER": "start_and_verify:started",
        "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir),
        "SW_TEST_UPDATE_FORCE_VERIFY_FAIL": "smoke_failed",
    })
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    push_thread = None
    old_pid = daemon_pid(env)
    source = tmp_path / "revert-push.bin"
    expected_md5, remote = make_push_file(source)
    try:
        update_thread.start()
        wait_pause(pause_dir, "start_and_verify:started")
        new_pid = daemon_pid(env)
        assert new_pid != old_pid
        assert wait_session(env)["state"] == "READY"
        push_thread, push_rows = start_file_push(new_cli, env, source, remote)
        wait_rx_contains(bridge.evidence_dir, b"SW_XFER_CHUNK_42", timeout_s=30.0)
        assert push_thread.is_alive()
        resume_pause(pause_dir, "start_and_verify:started")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup 驗證失敗後未結束"
        update = update_rows[0]
        data = json_stdout(update)
        report = data.get("update", {})
        assert update.returncode == 2, update.stdout + update.stderr
        assert report.get("revert_pending") is True, data
        assert report.get("revert_error", {}).get("error_code") == "SESSION_BUSY", data
        assert daemon_pid(env) == new_pid
        assert installed_version(env) == "0.4.1"
        assert not any("shutdown" in json.dumps(row, ensure_ascii=False).lower() for row in wal_rows(env))
        push_thread.join(180.0)
        assert not push_thread.is_alive(), "1 MiB file push 未結束"
        assert len(push_rows) == 1
        push = json_stdout(push_rows[0])
        assert push_rows[0].returncode == 0, push_rows[0].stdout + push_rows[0].stderr
        assert push.get("bytes") == 1024 * 1024 and push.get("md5") == expected_md5
        assert (bridge.remote_root / Path(remote).name).read_bytes() == source.read_bytes()

        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        env.pop("SW_TEST_UPDATE_FORCE_VERIFY_FAIL", None)
        select_bundle(env, old)
        recovery = run_cli(old_cli, ["setup", "--on-demand"], env, timeout_s=180.0)
        assert recovery.returncode == 0, recovery.stdout + recovery.stderr
        assert installed_version(env) == "0.4.0"
        assert rpc(env, "health.status").get("version") == "0.4.0"
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "start_and_verify:started")
            update_thread.join(15.0)
        if push_thread is not None and push_thread.is_alive():
            push_thread.join(180.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        env.pop("SW_TEST_UPDATE_FORCE_VERIFY_FAIL", None)
        _stop_rig(bridge, daemon, old_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.pidns_rig
def test_update_push_after_freeze_zero_tx_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """handoff_stop 回 ok 後 file.push 回 FLUSHED_BY_SHUTDOWN；凍結期間 DUT TX 為零且無部分檔案。"""
    old, new = update_bundles()
    old_cli, _root = _install_old(tmp_path, env, old)
    bridge, daemon = _start_rig(tmp_path, env)
    new_cli = select_bundle(env, new)
    source = tmp_path / "frozen-push.bin"
    make_push_file(source)
    pause_dir = tmp_path / "pause-handoff"
    env.update({"SW_TEST_UPDATE_PAUSE_AFTER": "handoff_stop:replied", "SW_TEST_UPDATE_PAUSE_DIR": str(pause_dir)})
    update_rows = []
    update_thread = RaisingThread(
        lambda: update_rows.append(run_cli(new_cli, ["setup", "--on-demand"], env, timeout_s=240.0)),
    )
    try:
        update_thread.start()
        wait_pause(pause_dir, "handoff_stop:replied")
        bridge.mark("handoff-frozen", os.getpid())
        tx_at_freeze = tx_bytes(bridge.evidence_dir)
        push = run_cli(new_cli, ["file", "push", "--selector", "COM0", "--local", str(source), "--remote", "/tmp/frozen-push.bin", "--ack-mode", "echo"], env, timeout_s=30.0)
        data = json_stdout(push)
        assert push.returncode == 2, push.stdout + push.stderr
        assert data.get("error_code") == "FLUSHED_BY_SHUTDOWN", data
        assert data.get("reason") == "handoff_frozen", data
        assert tx_bytes(bridge.evidence_dir) == tx_at_freeze
        assert not list(bridge.remote_root.glob("*"))
        resume_pause(pause_dir, "handoff_stop:replied")
        update_thread.join(120.0)
        assert not update_thread.is_alive(), "setup update 未在 120 秒內結束"
        update = update_rows[0]
        assert update.returncode == 0, update.stdout + update.stderr
        assert installed_version(env) == "0.4.1"
        assert wait_session(env)["state"] == "READY"
    finally:
        if update_thread.is_alive():
            resume_pause(pause_dir, "handoff_stop:replied")
            update_thread.join(15.0)
        env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
        env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
        _stop_rig(bridge, daemon, new_cli, env)


@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.supervisor_runtime
def test_update_systemd_exit1_not_relaunched_l15(tmp_path: Path, env: dict[str, str]) -> None:
    """經 run-systemd.sh 實跑兩版 bundle：舊 daemon exit 1 時 NRestarts 為零、holder 不重疊且新版本就緒。"""
    old, new = update_bundles()
    run_id = f"g5-rust-refactor-{os.getpid()}-{time.time_ns()}"
    out = repo_root() / "target" / "g5-tmp" / f"systemd-evidence-{run_id}"
    script = repo_root() / "scripts" / "docker" / "run-systemd.sh"
    run_env = dict(os.environ)
    run_env["SW_DOCKER_RUN_ID"] = run_id
    try:
        result = subprocess.run(
            ["bash", str(script), "--mode", "systemd-user", "--scenario", "update-exit1",
             "--bundle-old", str(old), "--bundle-new", str(new), "--out", str(out)],
            cwd=repo_root(), env=run_env, capture_output=True, text=True, timeout=2400, check=False,
        )
        report_path = out / "l15-docker-report.json"
        assert report_path.is_file(), result.stdout + result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report.get("evidence_layer") == "L1.5-docker", report
        assert report.get("producer") == "run-systemd.sh", report
        assert report.get("results", [{}])[0].get("case_id") == "update-exit1", report
        assert report.get("results", [{}])[0].get("verdict") == "PASS", report
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        # 報告與 journal 先複製到 worktree 的穩定位置再刪暫存目錄；刪除失敗直接拋出，不靜默吞錯。
        if out.is_dir():
            keep = repo_root() / "target" / "systemd-evidence-kept"
            keep.mkdir(parents=True, exist_ok=True)
            for src in out.iterdir():
                if src.is_file():
                    shutil.copy2(src, keep / f"{out.name}-{src.name}")
            shutil.rmtree(out)
