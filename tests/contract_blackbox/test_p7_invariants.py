"""P7 Task 18（08-slice23-login-events.md）：§11.3 安全不變量黑箱（外部 CLI＋FakeTarget＋/proc 觀測）。

本檔承接：
- `test_rejected_requests_zero_tx`（§11.3-1）：quiet gate、`CREDENTIALS_UNRESOLVED`、`READY_UNCONFIRMED` 被拒時 target
  0 byte、WAL 無 TX（READY_UNCONFIRMED 子案經 180 s quiet，為慢案、不得 SKIP）。
- `test_release_during_login_waits_for_handle_close`（§11.3-4）。
- `test_no_auto_resend_after_partial_or_timeout`（§11.3-5；SC-30 子案 Rust-only，cases.yaml 平台限定）。
未承接（依賴 P6 human console，`session.console_attach` 於本分支 Rust 為 METHOD_NOT_FOUND，BLOCKED-UPSTREAM）：
`test_login_bytes_not_interleaved_with_agent_or_human`（§11.3-2）、`test_recovery_and_reboot_do_not_replay_stale_input`
（§11.3-3）；`test_evidence_insufficient_is_not_pass`（§11.3-6）依賴 `scripts/rust/run_p7_ab.sh`（尚未建立）。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import base64
import os
import threading
import time

import pytest

from bb_faketarget import FakeTarget
from bb_harness import is_rust
from bb_p7 import P7Rig, wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

BANNER = "\r\nU-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\r\nHit any key to stop autoboot:  3"


def _row(p7, sel: str = "COM0") -> dict:
    rc, j = p7.cli("session", "list")
    assert rc == 0 and j, (rc, j)
    return [x for x in j["sessions"] if x["com"] == sel][0]


def _tx_count(p7) -> int:
    return sum(1 for r in p7.wal_records("COM0", limit=5000) if r.get("dir") == "TX")


def _tx_bytes(p7) -> bytes:
    return b"".join(base64.b64decode(r.get("payload_b64") or "") for r in p7.wal_records("COM0", limit=5000)
                    if r.get("dir") == "TX")


def _daemon_fds_to(p7, path: str) -> int:
    pid = p7.daemon.proc.pid
    n = 0
    try:
        names = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return 0
    for name in names:
        try:
            if os.readlink(f"/proc/{pid}/fd/{name}") == path:
                n += 1
        except OSError:
            continue
    return n


def _reject_zero_tx(p7, tgt, want: str) -> None:
    mark, tx_before = tgt.rx_mark(), _tx_count(p7)
    rc, j = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "echo must-not-send")
    assert rc == 2 and j["ok"] is False and j["error_code"] == want, (rc, j)
    time.sleep(1.0)
    assert tgt.rx_log_since(mark) == b"", f"{want}：被拒請求不得送任何 byte"
    assert _tx_count(p7) == tx_before, f"{want}：被拒請求 WAL 不得有 TX"


def test_rejected_requests_zero_tx(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ", commands={"overrides": {"": {"response": "\r\n=> "}}})
    p7.start("prpl", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    tgt.emit(BANNER + "\r\n=> ")
    time.sleep(1.0)
    _reject_zero_tx(p7, tgt, "AUTOBOOT_QUIET")
    t0 = time.monotonic()
    while not _row(p7)["ready_reconfirm_failed"]:   # quiet 180 s 結束、D1 判定（慢案）
        assert time.monotonic() - t0 <= 185.0
        time.sleep(1.0)
    _reject_zero_tx(p7, tgt, "READY_UNCONFIRMED")


def test_rejected_requests_zero_tx_credentials_unresolved(p7, tmp_path):
    """同列（§11.3-1）的 CREDENTIALS_UNRESOLVED 子案：ATTACHED 終態下 submit 被拒、0 byte、WAL 無 TX。"""
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", login_steps=[{"expect": "", "respond": "\r\nop3 login: "}])
    p7.start("shell_unresolved", [tgt])
    wait_state(p7.env, "COM0", "ATTACHED")
    deadline = time.monotonic() + 15.0
    while _row(p7)["last_error"] != "CREDENTIALS_UNRESOLVED":
        assert time.monotonic() < deadline
        time.sleep(0.2)
    _reject_zero_tx(p7, tgt, "SESSION_NOT_READY")
    assert tgt.rx_log() == b"" and _tx_count(p7) == 0


def test_release_during_login_waits_for_handle_close(p7, tmp_path):
    """登入進行中 `device release`：回成功的當下 daemon 已無指向該 tty 的 fd；`device attach` 之前不重開；
    `session recover --force` 的 REATTACH 任何時刻至多持有一個 fd（舊 fd 關閉後才重開）。

    註：Python 基線（2026-10-29 實測）於本情境 `device attach` 後落 DETACHED＋`ATTACH_FAILED:TxIncompleteError`
    且不再自動重試，Rust 則回到 READY；此 A/B 差異已列 OWNER-BATCH，本案只斷言 §11.3-4 的 handle 時序。"""
    tgt = FakeTarget(tmp_path / "t0", delay_ms=1500)       # 每個回應延遲 1.5 s：login 進行中可插入 release
    p7.start("shell", [tgt])
    deadline = time.monotonic() + 20.0
    while b"\n" not in tgt.rx_log():
        assert time.monotonic() < deadline, "daemon 未開始 login probe"
        time.sleep(0.05)
    slave = tgt.slave_path
    assert _daemon_fds_to(p7, slave) >= 1, "login 進行中 daemon 應持有 tty"
    rc, j = p7.cli("device", "release", "--selector", "COM0", "--source", "agent:p7", "--reason", "flash")
    assert rc == 0 and j["ok"] is True, (rc, j)
    assert _daemon_fds_to(p7, slave) == 0, "release 回成功的當下 daemon 不得再持有該 tty 的 fd"
    t_end = time.monotonic() + 6.0
    while time.monotonic() < t_end:
        assert _daemon_fds_to(p7, slave) == 0, "device attach 之前不得重新開啟"
        time.sleep(0.1)
    rc, j = p7.cli("device", "attach", "--selector", "COM0")
    assert rc == 0 and j["ok"] is True, (rc, j)
    deadline = time.monotonic() + 10.0
    while _daemon_fds_to(p7, slave) == 0:
        assert time.monotonic() < deadline, "device attach 後應重新開啟 tty"
        time.sleep(0.05)

    # REATTACH：另一個 rig（READY 起手），RX 凍結下 recover --force，取樣 daemon 對 tty 的 fd 數峰值。
    rig = P7Rig(tmp_path / "p7-reattach")
    try:
        tgt2 = FakeTarget(tmp_path / "t1")
        rig.start("shell", [tgt2])
        wait_state(rig.env, "COM0", "READY", timeout_s=60.0)
        slave2 = tgt2.slave_path
        peak = [0]
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                peak[0] = max(peak[0], _daemon_fds_to(rig, slave2))
                time.sleep(0.005)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        try:
            tgt2.freeze_rx()
            rig.cli("session", "recover", "--selector", "COM0", "--force", timeout_s=60.0)
        finally:
            stop.set()
            th.join(timeout=5.0)
        assert peak[0] == 1, f"REATTACH 期間不得同時持有新舊 fd（且應持有過）：peak={peak[0]}"
    finally:
        rig.stop()


def test_no_auto_resend_after_partial_or_timeout(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    rc, sub = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 60", "--cmd-timeout", "2")
    assert sub and sub["ok"] is True, (rc, sub)
    deadline = time.monotonic() + 20.0
    while not ((p7.cli("cmd", "status", "--cmd-id", sub["cmd_id"])[1] or {}).get("command") or {}).get("done_at"):
        assert time.monotonic() < deadline
        time.sleep(0.3)
    time.sleep(10.0)                                          # 救援後不得自動重送
    sent = tgt.rx_log_since(mark)
    assert sent.count(b"sleep 60") == 1, f"逾時救援後 payload 必須恰一次：{sent!r}"
    assert _tx_bytes(p7).count(b"sleep 60") == 1
    if is_rust():
        # SC-30（Rust-only）：login 被 quiet banner 中止後，帳號只送一次、secret 不送、之後不重送。
        rig = P7Rig(tmp_path / "p7-sc30")
        try:
            tgt2 = FakeTarget(tmp_path / "t1", login_steps=[{"expect": "", "respond": "\r\nfake login: "},
                                                           {"expect": "root", "respond": BANNER}])
            rig.start("shell", [tgt2])
            deadline = time.monotonic() + 20.0
            while b"root" not in tgt2.rx_log():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            time.sleep(10.0)
            log = tgt2.rx_log()
            assert log.count(b"root") == 1 and tgt2.password.encode() not in log, log
        finally:
            rig.stop()
