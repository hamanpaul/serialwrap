"""P8 Task 12（09-slice24-transfer-flash.md）：systemd 監管下的有序停止與失敗重啟黑箱（A/B：Python 基線與 Rust
artifact 同檔各跑一次）。

案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點）：
- `s24-sup-systemd-runtime`：`test_sigterm_ordered_shutdown`、`test_restart_on_failure_keeps_released_and_wal_seq`；
  計畫表另列 `test_stop_during_push_flushes_wal`（file push 進行中 `systemctl --user stop`）與
  `test_stop_during_flashing_removes_ttymcu`（FLASHING 中 `systemctl --user stop`，fakemcu＋fakeflasher）。

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
import warnings
import zlib

import pytest

from pathlib import Path

from bb_harness import is_rust
from p8_fixtures import FakeFlasher

pytestmark = [pytest.mark.posix_only, pytest.mark.supervisor_runtime, pytest.mark.posix_pty]

# 停止序上限：flash join 2.0 s＋步驟 3～5 共用 5 s（計畫 Task 12）。
STOP_CAP_S = 7.0
OD62_ABORT_CODES = {"COMMAND_ABORTED_IN_FLIGHT", "FLUSHED_BY_RECOVERY"}
# fakemcu（ti-cc26xx）的 sync 與 ACK（同 test_s24_flash_endpoint.py）。
SYNC = b"\x55\x55"
ACK = b"\x00\xcc"


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


def _settled_rx_len(shell_target, quiet_s: float = 2.0, timeout_s: float = 10.0) -> int:
    """等 DUT PTY relay 把已送達的 UART bytes 記錄完成，再回傳穩定長度。"""
    last = len(shell_target.rx_log())
    deadline = time.monotonic() + timeout_s
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = len(shell_target.rx_log())
        if now != last:
            last, stable_since = now, time.monotonic()
        elif time.monotonic() - stable_since >= quiet_s:
            return last
    raise AssertionError(f"DUT RX 在 {timeout_s:.1f}s 內未靜定：{len(shell_target.rx_log())} bytes")


# 測試觀察上限（非產品期限）。hosted runner 上本檔從啟動到 READY 實測不到 3 s（PR #278 第 3、7 輪 junit：
# test_sigterm_ordered_shutdown 0.13～0.47 s、test_restart_on_failure_… 2.3～2.9 s），啟動與登入並不慢。
# 會超過 20 s 的只有 test_stop_during_push_flushes_wal「重啟後」那一次等待，原因是產品行為（issue #282，
# 擁有者裁決 0.5.1 修、不阻擋 0.5.0）：push 進行中停機時，echo-paced 已送出、尚未送行尾的 chunk 命令本文留在
# DUT 輸入列；新 daemon attach 後的第一個探測換行把殘留內容送進 shell。殘留若停在引號內，shell 進入續行、
# 探測得不到 prompt（PR #278 第 6 輪傾印：ATTACHED、last_error=PROMPT_UNAVAILABLE、reprobe_attempts=0），
# 要等 reprobe（RX 閒置 3 s、退避 2 s 起）才回 READY。60 s 是把這段恢復延遲算進去的上限；#282 修正後應改回 20 s。
READY_WAIT_S = 60


def _wait_ready_observing(sw_env, com: str, timeout: float) -> list[str]:
    """通過條件與期限同 `sw_env.wait_state(com, "READY", timeout)`；另回傳等待期間看過的 last_error（純觀察）。"""
    seen: list[str] = []
    last: dict = {}

    def _pred() -> bool:
        last["session"] = session = sw_env.session(com)
        err = session.get("last_error")
        if err and err not in seen:
            seen.append(str(err))
        return session.get("state") == "READY"

    try:
        sw_env.wait_until(_pred, timeout, interval=0.1)
    except AssertionError as exc:
        raise AssertionError(
            f"{com} 未在 {timeout}s 內進入 READY；最後觀察：{last.get('session')!r}；看過的 last_error：{seen}；{exc}"
        ) from None
    return seen


def _restart_residue_report(
    rx_at_restart: bytes, after_restart: bytes, chunk_commands_before: int, commands_after: list[str],
    errors_seen: list[str],
) -> dict:
    """issue #282 的可觀察量（純計算，不含判定）。

    - `residual_input_bytes`：停機靜定後留在 DUT 輸入列、尚無行尾的 bytes（`rx_at_restart` 最後一個 `\\n` 之後）。
    - `residual_submitted_after_restart`：重啟後 DUT 收到的第一個換行之前沒有清行控制字元（Ctrl-U／Ctrl-C），
      也就是殘留內容被那個換行原樣送進 shell。chunk 命令的 sentinel 在行尾（`… && printf '%s\\n' 'SW_XFER_CHUNK_'…`），
      殘留若斷在 sentinel 之前，補成的那一行不含 `SW_XFER_CHUNK`、`extra_chunk_commands` 仍為 0，所以另記這一項。
    - `extra_chunk_commands`：重啟後 DUT 完整輸入行中含 chunk sentinel 的行數，比停機靜定後多幾行。
    - `prompt_unavailable_seen`：重啟後的 READY 等待期間是否出現過 PROMPT_UNAVAILABLE（殘留停在引號內、shell 進入續行）。
    """
    residual = rx_at_restart.rsplit(b"\n", 1)[-1]
    before_first_newline, newline, _ = after_restart.partition(b"\n")
    cleared = any(ctrl in before_first_newline for ctrl in (b"\x15", b"\x03"))
    chunk_commands_after = sum(1 for c in commands_after if "SW_XFER_CHUNK" in c)
    return {
        "residual_input_bytes": len(residual),
        "residual_has_chunk_sentinel": b"SW_XFER_CHUNK" in residual,
        "residual_submitted_after_restart": bool(residual) and bool(newline) and not cleared,
        "extra_chunk_commands": chunk_commands_after - chunk_commands_before,
        "prompt_unavailable_seen": "PROMPT_UNAVAILABLE" in errors_seen,
        "errors_seen": list(errors_seen),
    }

def test_sigterm_ordered_shutdown(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=READY_WAIT_S)
    took = transient_unit.stop()
    assert transient_unit.last_status() == "exited:0", (transient_unit.last_status(), took)
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == [], "停止後 tty 無殘留 holder"
    if os.name != "nt":
        assert not sw_env.socket.exists(), "有序停止後 socket 檔已移除"
    _assert_last_wal_crc(sw_env)


def test_restart_on_failure_keeps_released_and_wal_seq(sw_env, shell_target, transient_unit):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=READY_WAIT_S)
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


def test_stop_during_push_flushes_wal(sw_env, shell_target, transient_unit, tmp_path, record_property):
    sw_env.prepare(targets={"COM0": shell_target})
    transient_unit.start()
    sw_env.wait_state("COM0", "READY", timeout=READY_WAIT_S)
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
        # daemon 已退出後，先讓 DUT relay 記完停機前已進入 PTY 的 bytes，避免把延遲觀察誤認為重啟重送。
        _settled_rx_len(shell_target)
        tx_payloads = b"".join(base64.b64decode(r["payload_b64"]) for r in sw_env.wal_tx("COM0"))
        received = [c for c in shell_target.commands() if "SW_XFER_CHUNK" in c]
        assert received, "停止前 DUT 已收到 chunk"
        for line in received:
            assert line.encode() in tx_payloads, f"DUT 已收到的 chunk 必有對應 WAL TX：{line[:80]}"
        # 重啟後不自動重送（no_auto_resend）：重啟後到達 DUT 的 bytes 不得含任何 chunk 命令。
        # 以 DUT 端位元組位移切出「重啟後才收到」的部分（判準仍是 DUT 端，不用受測 daemon 自己寫的 WAL）：
        # 停機時寫到一半、尚無行尾的 chunk 行，會被新 daemon 的第一個換行補成完整行；舊的「完整行數」比對因此會
        # 把它誤算成重送（PR #278 第七輪 hosted CI：39 vs 38）。新 daemon 若真的重送，chunk sentinel 必出現在位移之後。
        rx_before_restart = _settled_rx_len(shell_target)
        rx_at_restart = shell_target.rx_log()[:rx_before_restart]
        transient_unit.start()
        errors_seen = _wait_ready_observing(sw_env, "COM0", READY_WAIT_S)
        # 保留原有 1 秒 no-auto-resend 觀察窗，再排空 relay 後檢查重啟後收到的 bytes。
        time.sleep(1.0)
        _settled_rx_len(shell_target)
        after_restart = shell_target.rx_log()[rx_before_restart:]
        assert b"SW_XFER_CHUNK" not in after_restart, f"重啟後不得重送 chunk：{after_restart[:240]!r}"
        assert b"base64 -d >>" not in after_restart, f"重啟後不得重送 chunk 資料：{after_restart[:240]!r}"
        # issue #282（0.5.1 修）：上面的位移判準看不到「停機殘留的半行被新 daemon 的探測換行送出」。這裡把它記成
        # 可觀察量（junit property＋warning），並限制在已知現象的範圍內：殘留至多一行，所以重啟後 DUT 至多多執行
        # 一個 chunk 命令；超過 1 就不是 #282，而是真的重送。
        residue = _restart_residue_report(
            rx_at_restart, after_restart, len(received), shell_target.commands(), errors_seen,
        )
        for key, value in residue.items():
            record_property(f"issue282_{key}", value)
        if (residue["residual_submitted_after_restart"] or residue["extra_chunk_commands"]
                or residue["prompt_unavailable_seen"]):
            warnings.warn(
                f"#282：push 進行中停機後 DUT 輸入列殘留被重啟後的探測換行送出（{residue}）", stacklevel=1,
            )
        assert residue["extra_chunk_commands"] <= 1, (
            f"重啟後 DUT 多執行 {residue['extra_chunk_commands']} 個 chunk 命令，超出 #282 已知的至多 1 個：{residue}"
        )
    finally:
        # 中止的 push 不送 cleanup（OD-62）：自行移除 shelltarget（本機 /bin/sh）留下的上傳暫存檔與目的檔。
        Path(remote).unlink(missing_ok=True)
        for name in set(re.findall(rb"/tmp/\.sw_upload_[0-9a-f]{12}", shell_target.rx_log())):
            Path(name.decode("ascii")).unlink(missing_ok=True)


def test_stop_during_flashing_removes_ttymcu(sw_env, fake_mcu, transient_unit):
    """FLASHING 中 `systemctl --user stop`：exit 0、`ttyMCU` link 已移除；重啟後 session 為 ATTACHED（FLASHING 不持久化）。"""
    sw_env.prepare(targets={"COM0": fake_mcu}, profile="passthrough")
    transient_unit.start()
    sw_env.wait_state("COM0", "ATTACHED", timeout=20)
    link = sw_env.run_dir / "dev" / "ttyMCU"
    sw_env.wait_until(link.is_symlink, timeout=10)
    fl = FakeFlasher(link, baud=115200)
    try:
        fl.write(SYNC)
        assert fl.read_exact(2, timeout=3.0) == ACK, "認線後回放 MCU ACK"
        assert sw_env.session("COM0")["state"] == "FLASHING"
        took = transient_unit.stop()               # flasher 仍持有端點
        assert transient_unit.last_status() == "exited:0", (transient_unit.last_status(), took)
        assert not link.is_symlink() and not link.exists(), "停止後 ttyMCU link 已移除"
    finally:
        fl.close()
    transient_unit.start()
    sw_env.wait_state("COM0", "ATTACHED", timeout=20)
    assert sw_env.rpc("mcu.status", {})["flashing"] is False, "FLASHING 不跨重啟持久化"
    sw_env.wait_until(link.is_symlink, timeout=10)
