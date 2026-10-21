"""P4-T12 `supervision-mode set` 交接護欄黑箱契約（SC-22，R1-F3）。

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


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="FakeTarget 未 attach 並行 file push 到 daemon session；目前無法證明 SESSION_BUSY 或 --force 行為")
def test_supervision_set_force_push_continues_l15(tmp_path: Path) -> None:
    """真 Python daemon＋FakeTarget：client A file push 進行中，同時 supervision-mode set systemd-user --force → exit 2、error_code==SESSION_BUSY、phase: supervision_set_safety_check、force_ignored: true；push 完成、daemon pid 不變、config.yaml 未變。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["supervision-mode", "set", "systemd-user", "--force"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 2
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") == "SESSION_BUSY"
        assert data.get("phase") == "supervision_set_safety_check"
        assert data.get("force_ignored") is True
    finally:
        target.stop()


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.posix_pty
@pytest.mark.skip(reason="FakeTarget 未令 daemon session 進入 FLASHING；目前無法證明 FLASHING_BUSY 或零 TX")
def test_supervision_set_flashing_l15(tmp_path: Path) -> None:
    """FakeTarget 讓 session 進 FLASHING → supervision-mode set on-demand --force → FLASHING_BUSY；FLASHING 持續、daemon pid 不變、wire.ndjson 無主動寫出 byte。"""
    env = isolated_env(tmp_path)
    target = FakeTarget(tmp_path / "fake-dut")
    target.start(Path(env["SERIALWRAP_BY_ID_DIR"]))
    try:
        if target.slave_fd is not None:
            os.close(target.slave_fd)
            target.slave_fd = None

        cli = cli_prefix()
        res = subprocess.run(
            cli + ["supervision-mode", "set", "on-demand", "--force"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 2
        data = json.loads(res.stdout) if res.stdout else {}
        assert data.get("error_code") == "FLASHING_BUSY"
    finally:
        target.stop()


@pytest.mark.case("sc22-supervision-mode-set")
@pytest.mark.rust_only
@pytest.mark.process_rpc
@pytest.mark.skip(reason="未啟動既有 daemon；此命令 smoke 無法證明 handoff_stop、busy refusal 或不發送 daemon.stop")
def test_supervision_set_rust_only_switch(tmp_path: Path) -> None:
    """supervision_set_idle_uses_handoff_not_daemon_stop 與 supervision_set_handoff_unsupported_refuses_without_stop 黑箱驗證。"""
    env = isolated_env(tmp_path)
    cli = cli_prefix()
    res = subprocess.run(
        cli + ["supervision-mode", "set", "on-demand"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0
