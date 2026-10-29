"""F2 背壓與有界資源（Slice 2.1 黑箱，承接 f2-* 案例）。

上限依 `contracts/resource_limits.yaml`：CMD_PENDING_MAX=256、CMD_HISTORY_MAX=512、RX 視窗與 FakeTarget 大量輸出。
"""
import time

import pytest

pytestmark = pytest.mark.cap_posix_pty


def _vmrss_kib(pid: int) -> int:
    with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    raise AssertionError(f"/proc/{pid}/status 缺少 VmRSS")


# CLI `cmd submit` 的參數預設值（sw_core/cli.py `p_cs`）；填佇列的 RPC 與 CLI 送出同一組參數。
_CLI_SUBMIT_DEFAULTS = {"source": "agent", "mode": "line", "priority": 10, "timeout_s": 10.0}


def test_queue_full_backpressure(shell_daemon):
    # P5 第 7 輪（案例設計修正，編排者裁決允許）：原案以 CLI 逐筆送 300 筆，Python 基線每次 CLI 行程約 0.12～0.18 s，
    # 256 筆需 33～40 s，超過前景 sleep 30，佇列在填滿前即開始消化而恆不回 SESSION_QUEUE_FULL（送出速率前提錯，
    # 非 daemon 行為）。改以 command.submit RPC（與 CLI 同參數）在前景執行期間填佇列，再以 CLI 驗證溢位回應。
    fg = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 30", "--cmd-timeout", "60")
    accepted = [fg["cmd_id"]]
    full: list[dict] = []
    for i in range(300):
        r = shell_daemon.rpc("command.submit", {"selector": "COM0", "cmd": f"echo p{i}", **_CLI_SUBMIT_DEFAULTS})
        if r.get("error_code") == "SESSION_QUEUE_FULL":
            full.append(r)
        elif "cmd_id" in r:
            accepted.append(r["cmd_id"])
    assert full, "前景 sleep 30 期間連送 300 筆，至少一筆應回 SESSION_QUEUE_FULL"
    assert full[0]["pending"] == 256
    assert full[0]["limit"] == 256
    over = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo over")   # CLI 層的溢位回應
    assert over.get("error_code") == "SESSION_QUEUE_FULL", over
    assert over["pending"] == 256 and over["limit"] == 256
    for cid in accepted:
        shell_daemon.sw("cmd", "cancel", "--cmd-id", cid)
    ok = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo ok")
    done = shell_daemon.wait_command_done(ok["cmd_id"], timeout=30)
    assert done["status"] == "done"


def test_history_bounded(shell_daemon):
    pid = shell_daemon.proc.pid
    rss0 = _vmrss_kib(pid)
    all_ids: list[str] = []
    for start in range(0, 600, 100):                 # 分批送出，避免超過 CMD_PENDING_MAX
        batch = [
            shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo {i}")["cmd_id"]
            for i in range(start, start + 100)
        ]
        all_ids.extend(batch)
        for cid in batch:
            done = shell_daemon.wait_command_done(cid, timeout=30)
            assert done["status"] == "done"
    st = shell_daemon.sw("daemon", "status")
    assert st["commands"] <= 512
    rss1 = _vmrss_kib(pid)
    assert (rss1 - rss0) * 1024 < 32 * 1024 * 1024, f"VmRSS 增量 {(rss1 - rss0)} KiB 超過 32 MiB"


def test_recovery_flushes_queue(shell_daemon):
    """DF-21（OD-37）：recover flush 原子化——Ctrl-C 後佇列全部 FLUSHED_BY_RECOVERY 且佇列命令零 TX。
    Python 輪逐列標記 DF 差異（ab_rows.PY_DF_DIFFERENCE），只在 Rust 判定。"""
    from ab_rows import gate_row

    gate_row("test_recovery_flushes_queue")
    t = shell_daemon.targets["COM0"]
    shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 30", "--cmd-timeout", "60")
    queued = [
        shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo q{i}")["cmd_id"]
        for i in range(5)
    ]
    shell_daemon.sw("session", "recover", "--selector", "COM0")
    for cid in queued:
        done = shell_daemon.wait_command_done(cid, timeout=20)
        assert done.get("error_code") == "FLUSHED_BY_RECOVERY"
    written = t.written()
    assert not any(f"echo q{i}".encode() in written for i in range(5)), "被 flush 的佇列命令不得送出任何 byte"
    t0 = time.monotonic()
    ok = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo ok")
    done = shell_daemon.wait_command_done(ok["cmd_id"], timeout=10)
    assert done["status"] == "done"
    assert time.monotonic() - t0 <= 10.0


def test_rx_window_crossing_prompt(shell_daemon):
    big = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "yes | head -c 200000", "--cmd-timeout", "60")
    done = shell_daemon.wait_command_done(big["cmd_id"], timeout=60)
    assert done["status"] == "done"
    for _ in range(3):
        sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo x")
        res = shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
        assert res["status"] == "done"
        assert "x" in [ln.strip() for ln in res["stdout"].splitlines()]
