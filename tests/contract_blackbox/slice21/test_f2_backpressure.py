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


def test_queue_full_backpressure(shell_daemon):
    fg = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 30", "--cmd-timeout", "60")
    accepted = [fg["cmd_id"]]
    full: list[dict] = []
    for i in range(300):
        r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo p{i}")
        if r.get("error_code") == "SESSION_QUEUE_FULL":
            full.append(r)
        elif "cmd_id" in r:
            accepted.append(r["cmd_id"])
    assert full, "前景 sleep 30 期間連送 300 筆，至少一筆應回 SESSION_QUEUE_FULL"
    assert full[0]["pending"] == 256
    assert full[0]["limit"] == 256
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
    shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 30", "--cmd-timeout", "60")
    queued = [
        shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo q{i}")["cmd_id"]
        for i in range(5)
    ]
    shell_daemon.sw("session", "recover", "--selector", "COM0")
    for cid in queued:
        done = shell_daemon.wait_command_done(cid, timeout=20)
        assert done.get("error_code") == "FLUSHED_BY_RECOVERY"
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
