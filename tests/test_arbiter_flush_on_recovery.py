"""tests/test_arbiter_flush_on_recovery.py — #128 recovery/re-attach 時 flush 佇列命令。

背景：PROMPT_TIMEOUT → _transition_to_attached → _on_detached → arbiter.unregister_session
會丟棄整個 PriorityQueue，但佇列中「尚未啟動（status=accepted、done_at=None）」的命令記錄
從未被終結——stale accepted 永久計入 _count_pending_locked，佔用 CMD_PENDING_MAX 額度，
數次 recovery episode 後該 session 直到 daemon 重啟前一律 SESSION_QUEUE_FULL。

修法：unregister_session 時 flush_session() 把未啟動的 queued 命令以
status=error、error_code=FLUSHED_BY_RECOVERY 終結（可被 evict、client 可辨識重送）；
in-flight（running）命令不重複標記，留給 worker 以真實結果終結。
"""
from __future__ import annotations

import threading
import time

import sw_core.arbiter as arbiter_mod
from sw_core.arbiter import CommandArbiter


def _wait_until(pred, timeout_s: float = 5.0, interval_s: float = 0.005) -> bool:
    """輪詢等待條件成立；逾時回 False。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval_s)
    return False


def _blocking_arbiter(release: threading.Event) -> CommandArbiter:
    """send_cb 對 command == "blocking" 卡住直到 release；其餘立即成功。"""

    def _send(_sess, command, *_a, **_k):
        if command == "blocking":
            release.wait(timeout=10.0)
        return {"ok": True, "stdout": ""}

    return CommandArbiter(send_cb=_send)


def _status(arb: CommandArbiter, cmd_id: str) -> dict:
    got = arb.get(cmd_id)
    assert got["ok"], got
    return got["command"]


# ── (a) 多次 recovery episode 後 stale pending 歸零、新 submit 背靠背全 OK ──
def test_unregister_flushes_stale_pending_across_episodes(monkeypatch):
    """3 個 recovery episode（阻塞 worker + backlog + unregister/re-register）後：
    stale pending 必須為 0，且新 submit 背靠背（不等 worker 消化）全數放行。

    舊行為：每個 episode 遺留 backlog 筆 stale accepted（永無 done_at），
    3×5=15 > CMD_PENDING_MAX=8 → 新 submit 一律 SESSION_QUEUE_FULL。
    """
    monkeypatch.setattr(arbiter_mod, "CMD_PENDING_MAX", 8)
    release = threading.Event()
    arb = _blocking_arbiter(release)
    sid = "s"
    try:
        for _ in range(3):
            release.clear()
            arb.register_session(sid)
            blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
            assert blk["ok"], blk
            # 等 worker 取走 blocking（進入 running），確保 backlog 停在 accepted
            assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
            for i in range(5):
                arb.submit(session_id=sid, command=f"c{i}", source="t", mode="fg", timeout_s=1.0)
            # recovery：worker 仍卡著時 unregister（對應 service._on_detached）
            arb.unregister_session(sid)
            release.set()
            # 等 in-flight blocking 由 worker 以真實結果終結，避免 episode 間互相干擾
            assert _wait_until(lambda: _status(arb, blk["cmd_id"])["done_at"] is not None)
        # recovery 成功 re-attach → re-register（對應 service._on_ready）
        arb.register_session(sid)
        with arb._lock:
            assert arb._count_pending_locked(sid) == 0  # stale accepted 已全數終結
        results = [arb.submit(session_id=sid, command=f"n{i}", source="t", mode="fg", timeout_s=1.0)
                   for i in range(8)]
        assert all(r.get("ok") for r in results), results  # 額度完全恢復，背靠背全放行
    finally:
        release.set()
        arb.unregister_session(sid)


# ── (b) 被 flush 命令的 client 可見語意：終端態 error + FLUSHED_BY_RECOVERY ──
def test_flushed_command_get_reports_flushed_by_recovery():
    release = threading.Event()
    arb = _blocking_arbiter(release)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
        queued = arb.submit(session_id=sid, command="queued", source="t", mode="fg", timeout_s=1.0)
        assert queued["ok"], queued

        arb.unregister_session(sid)

        rec = _status(arb, queued["cmd_id"])
        assert rec["status"] == "error"                      # 終端態（非永遠 accepted）
        assert rec["error_code"] == "FLUSHED_BY_RECOVERY"    # client 據此得知未執行、可重送
        assert rec["done_at"] is not None
        # 已終結 → 不可再 cancel
        c = arb.cancel(queued["cmd_id"])
        assert not c["ok"] and c["error_code"] == "CMD_NOT_CANCELABLE"
    finally:
        release.set()
        arb.unregister_session(sid)


# ── (c) in-flight running 命令不被 flush 標記、由 worker 以真實結果終結 ──
def test_inflight_running_command_survives_flush():
    release = threading.Event()
    arb = _blocking_arbiter(release)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")

        arb.unregister_session(sid)  # flush 發生時 blocking 仍 in-flight

        rec = _status(arb, blk["cmd_id"])
        assert rec["status"] == "running"          # 不得被 flush 重複標記
        assert rec["error_code"] is None

        release.set()                              # send_cb 返回 → worker 以真實結果終結
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["done_at"] is not None)
        rec = _status(arb, blk["cmd_id"])
        assert rec["status"] == "done"             # 真實結果，非 FLUSHED_BY_RECOVERY
        assert rec["error_code"] is None
    finally:
        release.set()


# ── (d) flush 後 _evict 生效：_commands 大小受控 ──
def test_flush_triggers_eviction_keeps_commands_bounded(monkeypatch):
    monkeypatch.setattr(arbiter_mod, "CMD_HISTORY_MAX", 3)
    monkeypatch.setattr(arbiter_mod, "CMD_PENDING_MAX", 100)
    release = threading.Event()
    arb = _blocking_arbiter(release)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
        for i in range(10):
            arb.submit(session_id=sid, command=f"c{i}", source="t", mode="fg", timeout_s=1.0)
        with arb._lock:
            assert len(arb._commands) == 11        # flush 前：全 pending、無一可淘汰

        arb.unregister_session(sid)                # flush 終結 10 筆 → 立即可被 evict

        with arb._lock:
            # 已終結記錄收斂回 CMD_HISTORY_MAX 上限；僅 in-flight blocking 額外保留
            assert sum(1 for r in arb._commands.values() if r.get("done_at")) <= 3
            assert len(arb._commands) <= 3 + 1
    finally:
        release.set()


# ── flush/consume race 防線：已終結（done_at 非 None）的取件一律跳過 ──
def test_worker_skips_already_flushed_item():
    """flush 與 worker 取件的 race：若 queued 記錄已被 flush 終結，worker 之後取到
    同一筆 item 時必須跳過（不得重設 running、不得執行 send_cb）。

    以 flush_session() 直呼模擬（佇列不丟棄）：blocking 卡住 worker → queued 進佇列
    → flush 終結 queued → 放行 worker → worker 取到 queued 的 item。
    """
    release = threading.Event()
    executed: list[str] = []

    def _send(_sess, command, *_a, **_k):
        executed.append(command)
        if command == "blocking":
            release.wait(timeout=10.0)
        return {"ok": True, "stdout": ""}

    arb = CommandArbiter(send_cb=_send)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
        queued = arb.submit(session_id=sid, command="queued", source="t", mode="fg", timeout_s=1.0)

        flushed = arb.flush_session(sid)
        assert flushed == 1

        release.set()  # worker 繼續 → 取到 queued 的 item → 必須跳過
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["done_at"] is not None)
        time.sleep(0.1)  # 給 worker 消化佇列的餘裕

        rec = _status(arb, queued["cmd_id"])
        assert rec["status"] == "error"                     # 終端態不被 worker 覆寫回 running
        assert rec["error_code"] == "FLUSHED_BY_RECOVERY"
        assert "queued" not in executed                     # send_cb 未被執行
    finally:
        release.set()
        arb.unregister_session(sid)
