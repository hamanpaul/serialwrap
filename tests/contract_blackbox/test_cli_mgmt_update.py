"""P4-T12 `setup` 更新與 `setup --rollback` 黑箱契約（SC-23）。

規格來源：`docs/rust-refactor/plans/05-stage1b-rust-cli-mgmt.md` Task 12；盤點 `inventory/07-platform-mgmt.md` §12.1。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bb_faketarget import FakeTarget
from bb_harness import (
    cli_prefix,
    isolated_env,
    repo_root,
)

pytestmark = [
    pytest.mark.posix_only,
]


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.process_rpc
def test_fresh_install_blackbox(tmp_path: Path) -> None:
    """隔離 HOME＋空 install root＋stub PATH：bundle setup --on-demand → daemon start → session list ok；profiles／config 存在。"""
    env = isolated_env(tmp_path)
    sw_bundle = os.environ.get("SW_BUNDLE", str(repo_root() / "target" / "mkbundle-dev"))
    cli_bin = Path(sw_bundle) / "bin" / "serialwrap"
    daemon_started = False
    try:
        res = subprocess.run(
            [str(cli_bin), "setup", "--on-demand"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml").exists()
        assert (Path(env["SERIALWRAP_CONFIG_DIR"]) / "profiles" / "default.yaml").exists()

        res_start = subprocess.run(
            [str(cli_bin), "daemon", "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        daemon_started = res_start.returncode == 0
        assert res_start.returncode == 0, res_start.stdout + res_start.stderr

        res_list = subprocess.run(
            [str(cli_bin), "session", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_list.returncode == 0, res_list.stdout + res_list.stderr
    finally:
        if daemon_started:
            stopped = subprocess.run(
                [str(cli_bin), "daemon", "stop"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.process_rpc
@pytest.mark.skip(reason="尚未建置並切換 0.4.0／0.4.1 兩個獨立 bundle；此測試目前只重複 setup，不能證明回滾契約")
def test_update_rollback_ab_isolated(tmp_path: Path) -> None:
    """以 mkbundle.sh --dev --version-override 0.4.0／0.4.1 產兩個自含 bundle，安裝 0.4.0 → 更新 0.4.1 → 回滾 0.4.0。"""
    env = isolated_env(tmp_path)
    cli = cli_prefix()
    res_up = subprocess.run(
        cli + ["setup"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_up.returncode == 0, res_up.stdout + res_up.stderr

    res_rb = subprocess.run(
        cli + ["setup", "--rollback"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res_rb.returncode == 0, res_rb.stdout + res_rb.stderr


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="FakeTarget 尚未 attach 到 daemon session，也沒有執行中的 push；不能證明忙碌拒絕或零 TX")
def test_update_busy_l15_zero_tx(tmp_path: Path) -> None:
    """bridge＋DUT 模擬器上有背景命令進行中 → 更新被拒（SESSION_BUSY）；拒絕前後 DUT 端 wire.ndjson 無任何新增 TX。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["setup"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 2
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") in ("FLASHING_BUSY", "SESSION_BUSY")
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="產品未讀取 SW_TEST_UPDATE_PAUSE_AFTER；目前沒有同步屏障或並行 file push")
def test_update_file_push_after_last_probe_l15(tmp_path: Path) -> None:
    """真 Python daemon（Stage 1 bundle）＋bridge：以 SW_TEST_UPDATE_PAUSE_AFTER=safety_check 讓 setup 在預檢後暫停，期間另一 client 開始 1 MiB file push，再放行 → SESSION_BUSY、handoff: refused。"""
    env = isolated_env(tmp_path)
    env["SW_TEST_UPDATE_PAUSE_AFTER"] = "safety_check"
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["setup"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 2
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") == "SESSION_BUSY"
        assert data.get("handoff") == "refused"
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="產品未讀取 SW_TEST_UPDATE_PAUSE_AFTER／SW_TEST_UPDATE_FORCE_VERIFY_FAIL；目前沒有新 daemon 或並行 push")
def test_revert_new_daemon_push_not_aborted_l15(tmp_path: Path) -> None:
    """真 Python daemon（兩個 dev bundle）＋bridge：SW_TEST_UPDATE_PAUSE_AFTER=start_and_verify:started 與 SW_TEST_UPDATE_FORCE_VERIFY_FAIL=smoke_failed 執行 setup；暫停期間另一 client 開始 file push → setup exit 2、revert_pending:true、revert_error.error_code==SESSION_BUSY。"""
    env = isolated_env(tmp_path)
    env["SW_TEST_UPDATE_PAUSE_AFTER"] = "start_and_verify:started"
    env["SW_TEST_UPDATE_FORCE_VERIFY_FAIL"] = "smoke_failed"
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["setup"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 2
        data = json.loads(res.stdout) if res.stdout else {}
        u = data.get("update", {})
        assert u.get("revert_pending") is True
        assert u.get("revert_error", {}).get("error_code") == "SESSION_BUSY"
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="目前未建立 handoff_frozen 後的並行 file push，也未量測 DUT wire TX")
def test_update_push_after_freeze_zero_tx_l15(tmp_path: Path) -> None:
    """預檢後放行交接、daemon.handoff_stop 已回 ok（daemon 凍結中）時另一 client 送 file push → 回 FLUSHED_BY_SHUTDOWN、reason: handoff_frozen；DUT 端 wire.ndjson 自凍結起無任何新增 TX；更新照常完成。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["setup"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.supervisor_runtime
@pytest.mark.skip(reason="需 throwaway systemd Docker harness 與兩版 bundle；本測試未啟動該 harness，不能作為 systemd 產品證據")
def test_update_systemd_exit1_not_relaunched_l15(tmp_path: Path) -> None:
    """真 systemd --user 隔離 unit＋兩個 dev bundle，舊版 daemon 以 SW_TEST_DAEMON_SHUTDOWN_INCOMPLETE=1 執行 setup：全程 NRestarts 為 0、tty 無 holder；更新後 daemon status.version 為新版、drop-in 不存在、Restart=on-failure。"""
    env = isolated_env(tmp_path)
    env["SW_TEST_DAEMON_SHUTDOWN_INCOMPLETE"] = "1"
    cli = cli_prefix()
    res = subprocess.run(
        cli + ["setup"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0
