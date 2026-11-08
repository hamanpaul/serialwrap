"""P8 Task 12（09-slice24-transfer-flash.md）：systemd 監管下的有序停止與失敗重啟黑箱（A/B：Python 基線與 Rust
artifact 同檔各跑一次）。

案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點）：
- `s24-sup-systemd-runtime`：`test_sigterm_ordered_shutdown`、`test_restart_on_failure_keeps_released_and_wal_seq`；
  計畫表另列 `test_stop_during_push_flushes_wal`（file push 進行中 `systemctl --user stop`）。

daemon 以 transient `systemd --user` unit 執行（`systemd-run --user`，`Restart=on-failure`、`ExecStopPost` 記錄
`$EXIT_CODE:$EXIT_STATUS`）；不改動擁有者既有的 `serialwrap` unit。throwaway daemon 的 RUN／STATE／WAL／BY_ID／
BY_PATH／CONFIG 全部隔離（`SwEnv.isolated_env()`），by-id 目錄只放 shelltarget 的 PTY slave。
`systemd --user` 不可用時 SKIP（cap:supervisor_runtime；證據不足，不得計為 PASS）。本檔在 docker systemd 容器
（P2 `scripts/docker/run-systemd.sh`）執行時屬 `L1.5-docker`，不得計為真板 L2／L3 或 OD-10 bench 證據。

SC-11 為 `ab_participation: diff`：Rust 停止序先取消進行中的長 RPC（Phase 1）再 join handler（Task 12），
Python 基線 `server.stop()` 先等 handler 自然結束；故 push 進行中停止的「回覆碼」與「停機耗時上限」只對 Rust 斷言，
其餘不變量（exit 0、WAL 完整、已收到的 chunk 都有 WAL TX、重啟後零重送）兩側相同。
外部 oracle：DUT 端 `shell_target.commands()`／`rx_log()`；WAL 只作關聯。stdlib＋pytest＋PTY，不得 import 產品碼。
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import time
import types
import zlib

import pytest

from pathlib import Path

from bb_harness import is_rust

pytestmark = [pytest.mark.posix_only, pytest.mark.supervisor_runtime, pytest.mark.posix_pty]

# 停止序上限：flash join 2.0 s＋步驟 3～5 共用 5 s（計畫 Task 12）。
STOP_CAP_S = 7.0
OD62_ABORT_CODES = {"COMMAND_ABORTED_IN_FLIGHT", "FLUSHED_BY_RECOVERY"}


def _systemd_user_ok() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
    return r.stdout.strip() in ("running", "degraded")


def _setenv_args(env: dict[str, str]) -> list[str]:
    # systemd-run --setenv 不能帶換行；隔離 env 的值一律為單行路徑或旗標。
    return [f"--setenv={k}={v}" for k, v in sorted(env.items()) if "\n" not in v and "=" not in k]


@pytest.fixture
def transient_unit(sw_env, tmp_path):
    if not _systemd_user_ok():
        pytest.skip("cap:supervisor_runtime 不可用（systemd --user 未執行）")
    name = f"sw-p8-test-{os.getpid()}-{int(time.time() * 1000)}"
    status = tmp_path / "exit-status"

    def start() -> None:
        cmd = [
            "systemd-run", "--user", f"--unit={name}", "--collect",
            "-p", "Type=simple", "-p", "Restart=on-failure", "-p", "RestartSec=2",
            "-p", f"ExecStopPost=/bin/sh -c 'echo $$EXIT_CODE:$$EXIT_STATUS >> {status}'",
            *_setenv_args(sw_env.isolated_env()),
            *sw_env.daemon_exe,
            "--profile-dir", str(sw_env.profile_dir),
            "--socket", sw_env.endpoint,
            "--lock", str(sw_env.lock),
        ]
        sw_env.lock.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True, capture_output=True)
        sw_env.wait_until(lambda: sw_env.rpc_ok("health.ping"), timeout=20)

    def stop() -> float:
        t0 = time.monotonic()
        subprocess.run(["systemctl", "--user", "stop", name], check=True, capture_output=True)
        return time.monotonic() - t0

    def last_status() -> str:
        lines = status.read_text(encoding="utf-8").strip().splitlines() if status.exists() else []
        return lines[-1] if lines else ""

    yield types.SimpleNamespace(name=name, start=start, stop=stop, status=status, last_status=last_status)
    subprocess.run(["systemctl", "--user", "stop", name], check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed", name], check=False, capture_output=True)


def _main_pid(name: str) -> int:
    r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", name],
                       capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def _assert_last_wal_crc(sw_env) -> None:
    raw = sw_env.wal_path.read_text(encoding="utf-8").rstrip("\r\n").split("\n")
    assert raw and raw[-1].strip(), "WAL 不得為空"
    records = sw_env.wal_records()           # 每一行都必須是完整 JSON
    last = records[-1]
    assert last["crc32"] == f"{zlib.crc32(base64.b64decode(last['payload_b64'])) & 0xFFFFFFFF:08x}", last


def test_sigterm_ordered_shutdown(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=20)
    took = transient_unit.stop()
    assert transient_unit.last_status() == "exited:0", (transient_unit.last_status(), took)
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == [], "停止後 tty 無殘留 holder"
    if os.name != "nt":
        assert not sw_env.socket.exists(), "有序停止後 socket 檔已移除"
    _assert_last_wal_crc(sw_env)


def test_restart_on_failure_keeps_released_and_wal_seq(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=20)
    rel = sw_env.rpc("device.release", {"selector": "COM0", "source": "agent:p8", "reason": "sup"})
    assert rel.get("ok") is True, rel
    seq0 = sw_env.rpc("wal.current_seq", {})["seq"]
    pid0 = _main_pid(transient_unit.name)
    assert pid0 > 0
    os.kill(pid0, signal.SIGKILL)
    sw_env.wait_until(lambda: _main_pid(transient_unit.name) not in (0, pid0), timeout=15)
    sw_env.wait_until(lambda: sw_env.rpc_ok("health.ping"), timeout=15)
    assert sw_env.session("COM0")["state"] == "RELEASED", "Restart=on-failure 拉起後 RELEASED 持久保留"
    assert sw_env.rpc("wal.current_seq", {})["seq"] >= seq0, "WAL seq 不倒退"


def test_stop_during_push_flushes_wal(sw_env, shell_target, transient_unit, tmp_path):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=20)
    src = tmp_path / "push.bin"
    src.write_bytes(bytes(range(256)) * 256)               # 64 KiB
    remote = f"/tmp/sw-p8t12-{os.getpid()}.bin"
    fut = sw_env.rpc_async("file.push", {"selector": "COM0", "local_path": str(src), "remote_path": remote})
    try:
        sw_env.wait_until(lambda: any("SW_XFER_CHUNK" in c for c in shell_target.commands()), timeout=20)
        took = transient_unit.stop()
        assert transient_unit.last_status() == "exited:0", (transient_unit.last_status(), took)
        reply = fut.result(timeout=30)
        if is_rust():
            # Task 12：Phase 1 先取消進行中的傳輸，join 不等 600 s 上限（OD-62 比照 BC-03 的中止碼）。
            assert took <= STOP_CAP_S, f"停止耗時 {took:.2f}s 超過 2.0 s＋5 s"
            assert reply.get("ok") is False and reply.get("error_code") in OD62_ABORT_CODES, reply
        _assert_last_wal_crc(sw_env)
        tx_payloads = b"".join(base64.b64decode(r["payload_b64"]) for r in sw_env.wal_tx("COM0"))
        received = [c for c in shell_target.commands() if "SW_XFER_CHUNK" in c]
        assert received, "停止前 DUT 已收到 chunk"
        for line in received:
            assert line.encode() in tx_payloads, f"DUT 已收到的 chunk 必有對應 WAL TX：{line[:80]}"
        # 重啟後不自動重送（no_auto_resend）：DUT 端零新增 chunk。
        before = len(received)
        transient_unit.start()
        sw_env.wait_state("COM0", "READY", timeout=20)
        time.sleep(1.0)
        assert len([c for c in shell_target.commands() if "SW_XFER_CHUNK" in c]) == before, "重啟後不得重送 chunk"
    finally:
        # 中止的 push 不送 cleanup（OD-62）：自行移除 shelltarget（本機 /bin/sh）留下的上傳暫存檔與目的檔。
        Path(remote).unlink(missing_ok=True)
        for name in set(re.findall(rb"/tmp/\.sw_upload_[0-9a-f]{12}", shell_target.rx_log())):
            Path(name.decode("ascii")).unlink(missing_ok=True)
