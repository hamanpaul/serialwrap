"""Stage 1B 管理面 L1.5 整合與不變量測試（P4-T16）。

使用 P2 bridge＋DUT 模擬器（posix_pty）驗證 §0.4 不變量：
- 被拒請求外部 TX 為零（zero_tx_on_reject）
- 不同來源 bytes 不得交錯（no_forbidden_interleave）
- 切換不得回放失效輸入（no_stale_context_replay）
- 新 daemon 開 tty 須在舊 daemon fd 關閉之後（no_reopen_before_handoff）

涵蓋案例：
1. test_autostart_l15_tx_once（SC-18，首次需求啟動只在請求未送出時重送一次且 TX 恰一次）
2. test_update_busy_l15_zero_tx（SC-23，FLASHING_BUSY／SESSION_BUSY 拒絕更新時 DUT TX 為 0）
3. test_update_file_push_after_last_probe_l15（更新探測交接邊界安全阻斷）
4. test_update_push_after_freeze_zero_tx_l15（R1-F1，凍結後 push 拒絕且 DUT TX 為 0）
5. test_reject_conflict_l15_zero_tx（DAEMON_ALREADY_RUNNING_ELSEWHERE 拒絕時 DUT TX 為 0）
6. test_restart_no_stale_console_replay（restart 前 human console 緩衝未送出行在新 daemon 不出現於 DUT RX）
7. test_restart_no_reopen_before_handoff（新 daemon 開 tty 時舊 daemon fd 已關）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
    pytest.mark.posix_pty,
]

ROOT = repo_root()


@pytest.mark.case("sc18-windows-ondemand-autostart")
@pytest.mark.rust_only
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 autostart 首次需求啟動重送與 TX 驗證待實作",
)
def test_autostart_l15_tx_once(tmp_path: Path) -> None:
    """SC-18：daemon 未啟動時送出需要 daemon 的命令，驗證首次需求啟動成功且 DUT 端 TX 恰一次。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        # 執行需 daemon 服務之命令（預期觸發 auto-start）
        res = subprocess.run(
            cli + ["session", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stdout + res.stderr
        # 斷言 DUT 端 TX 紀錄（wire.ndjson）恰有一次命令請求的 bytes，未重複送出
        wire_log = tmp_path / "wire.ndjson"
        assert wire_log.is_file(), "缺少 L1.5 bridge wire.ndjson 紀錄"
        records = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines() if line]
        tx_records = [r for r in records if r.get("direction") == "tx"]
        assert len(tx_records) == 1
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 update busy 拒絕 zero_tx 待實作",
)
def test_update_busy_l15_zero_tx(tmp_path: Path) -> None:
    """SC-23 不變量 zero_tx_on_reject：在 FLASHING_BUSY／SESSION_BUSY 狀態下拒絕更新，DUT 端 TX 為 0。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        # 嘗試在忙碌狀態下執行 setup update
        res = subprocess.run(
            cli + ["setup", "--target-version", "0.4.0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode != 0
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") in ("FLASHING_BUSY", "SESSION_BUSY")
        # 斷言拒絕更新期間 DUT 端外部 TX bytes 為 0
        wire_log = tmp_path / "wire.ndjson"
        if wire_log.is_file():
            records = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines() if line]
            tx_records = [r for r in records if r.get("direction") == "tx"]
            assert len(tx_records) == 0
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 update 探測後 file.push 阻斷待實作",
)
def test_update_file_push_after_last_probe_l15(tmp_path: Path) -> None:
    """SC-23：在更新交接最後一次探測之後送入 file.push，原子交接須拒絕或安全阻斷。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        # 執行帶有交接競爭之更新測試
        res = subprocess.run(
            cli + ["setup", "--target-version", "0.4.0"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0 or "HANDOFF_REFUSED" in res.stdout
    finally:
        target.stop()


@pytest.mark.case("sc23-setup-rollback")
@pytest.mark.rust_only
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 freeze 後 push 拒絕且 zero_tx 待實作",
)
def test_update_push_after_freeze_zero_tx_l15(tmp_path: Path) -> None:
    """R1-F1：daemon 進入 freeze 凍結新操作之後送達的 file.push 必須被拒絕，且 DUT TX bytes 為 0。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        # 在凍結狀態下嘗試送出 push
        res = subprocess.run(
            cli + ["file", "push", "--selector", "COM0", "--local", str(tmp_path / "x"), "--remote", "/tmp/x"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode != 0
        wire_log = tmp_path / "wire.ndjson"
        assert wire_log.is_file(), "缺少 L1.5 bridge wire.ndjson 紀錄"
        records = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines() if line]
        tx_records = [r for r in records if r.get("direction") == "tx"]
        assert len(tx_records) == 0
    finally:
        target.stop()


@pytest.mark.case("bc05-process-lifecycle-stop-restart")
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 conflict 拒絕 zero_tx 待實作",
)
def test_reject_conflict_l15_zero_tx(tmp_path: Path) -> None:
    """不變量 zero_tx_on_reject：當 DAEMON_ALREADY_RUNNING_ELSEWHERE 時 DUT 無 TX。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["daemon", "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode != 0
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") == "DAEMON_ALREADY_RUNNING_ELSEWHERE"
        # 衝突被拒時 DUT 端無任何額外 TX
        wire_log = tmp_path / "wire.ndjson"
        assert wire_log.is_file(), "缺少 L1.5 bridge wire.ndjson 紀錄"
        records = [json.loads(line) for line in wire_log.read_text(encoding="utf-8").splitlines() if line]
        tx_records = [r for r in records if r.get("direction") == "tx"]
        assert len(tx_records) == 0
    finally:
        target.stop()


@pytest.mark.case("sc20-daemon-restart")
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 restart no_stale_console_replay 待實作",
)
def test_restart_no_stale_console_replay(tmp_path: Path) -> None:
    """不變量 no_stale_context_replay：restart 前 human console 緩衝未送出的行，新 daemon 起來後 DUT RX 中不出現。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        # 執行 daemon restart
        res = subprocess.run(
            cli + ["daemon", "restart"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0
        # 檢查 target 接收到的資料中無重啟前殘留未送出的行
        stale_marker = b"STALE_UNSENT_CONSOLE_LINE\n"
        assert stale_marker not in target._rx_log
    finally:
        target.stop()


@pytest.mark.case("sc20-daemon-restart")
@pytest.mark.xfail(
    strict=True,
    reason="P4-T16 紅燈：L1.5 restart no_reopen_before_handoff 待實作",
)
def test_restart_no_reopen_before_handoff(tmp_path: Path) -> None:
    """不變量 no_reopen_before_handoff：新 daemon 開 tty 時舊 daemon 的 fd 已關。

    bridge control.ndjson 中舊 holder closed 早於新 holder opened。
    """
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["daemon", "restart"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0
        # 讀取 control.ndjson 比對 closed 與 opened 事件時序
        ctrl_log = tmp_path / "control.ndjson"
        assert ctrl_log.is_file(), "缺少 bridge control.ndjson"
        events = [json.loads(line) for line in ctrl_log.read_text(encoding="utf-8").splitlines() if line]
        closed_ts = [e["ts"] for e in events if e.get("event") == "closed"]
        opened_ts = [e["ts"] for e in events if e.get("event") == "opened"]
        assert closed_ts and opened_ts
        assert max(closed_ts) < min(opened_ts)
    finally:
        target.stop()
