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


# ── (d) flush 不主動 evict：剛 flush 的記錄仍可 get 到終端態，收斂交給後續 submit ──
def test_flush_keeps_flushed_records_gettable_then_converges(monkeypatch):
    """flush 內不呼叫 _evict_commands_locked（#128 review F3）：_commands 超量
    （> CMD_HISTORY_MAX）時 flush 當下 evict 可能把剛 flush 的記錄當場淘汰，
    client 隨後 get 只拿得到 CMD_NOT_FOUND 而非 FLUSHED_BY_RECOVERY。

    斷言：(1) flush 後所有被 flush 的 cmd_id 仍可 get 到 error + FLUSHED_BY_RECOVERY；
    (2) 後續 submit 觸發的 evict 能把 _commands 收斂回上限（增長有界）。
    """
    monkeypatch.setattr(arbiter_mod, "CMD_HISTORY_MAX", 3)
    monkeypatch.setattr(arbiter_mod, "CMD_PENDING_MAX", 100)
    release = threading.Event()
    arb = _blocking_arbiter(release)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
        queued_ids = []
        for i in range(10):
            r = arb.submit(session_id=sid, command=f"c{i}", source="t", mode="fg", timeout_s=1.0)
            queued_ids.append(r["cmd_id"])
        with arb._lock:
            assert len(arb._commands) == 11        # 遠超 CMD_HISTORY_MAX=3：flush 當下若 evict 必淘汰

        arb.unregister_session(sid)                # flush 終結 10 筆，但不得當場 evict

        # (1) 剛 flush 的記錄一律仍可 get 到終端態（不得 CMD_NOT_FOUND）
        for cid in queued_ids:
            rec = _status(arb, cid)
            assert rec["status"] == "error"
            assert rec["error_code"] == "FLUSHED_BY_RECOVERY"

        # (2) 收斂交給後續 submit 的 evict：re-register + submit 後 _commands 有界
        arb.register_session(sid)
        nxt = arb.submit(session_id=sid, command="next", source="t", mode="fg", timeout_s=1.0)
        assert nxt["ok"], nxt
        assert _wait_until(lambda: _status(arb, nxt["cmd_id"])["done_at"] is not None)
        with arb._lock:
            # 已終結記錄收斂回 CMD_HISTORY_MAX 上限；僅 in-flight blocking 額外保留
            assert len(arb._commands) <= 3 + 1
    finally:
        release.set()
        arb.unregister_session(sid)


# ── F1 epoch race 回歸：unregister 的 join 視窗內 re-register + 新 submit 不得被誤殺 ──
def test_reregister_submit_during_unregister_join_window_not_flushed():
    """epoch race 回歸（#128 review F1）：unregister_session 的 flush 必須與 pop queue
    在同一把鎖內原子完成。若延後到 join(1.0) 之後才 flush（舊行為），舊 worker 卡在
    send_cb 超過 join timeout 時，期間 re-register + 新 submit 的健康 accepted 命令
    會被遲到的 flush 誤標 FLUSHED_BY_RECOVERY、永不執行。

    重現法：blocking-old 卡住舊 worker（> join timeout）→ 背景執行 unregister →
    等第一段鎖完成（佇列已 pop）進入 join 視窗 → re-register + 提交 blocking-new 與
    fresh（fresh 排在 blocking-new 之後，保證跨越整個 join 視窗仍停留在 accepted）→
    等 unregister 返回 → 斷言 fresh 未被誤殺、放行後正常執行到 done。
    """
    release_old = threading.Event()
    release_new = threading.Event()

    def _send(_sess, command, *_a, **_k):
        if command == "blocking-old":
            release_old.wait(timeout=30.0)
        elif command == "blocking-new":
            release_new.wait(timeout=30.0)
        return {"ok": True, "stdout": ""}

    arb = CommandArbiter(send_cb=_send)
    sid = "s"
    try:
        arb.register_session(sid)
        blk = arb.submit(session_id=sid, command="blocking-old", source="t", mode="fg", timeout_s=1.0)
        assert _wait_until(lambda: _status(arb, blk["cmd_id"])["status"] == "running")
        stale = arb.submit(session_id=sid, command="stale", source="t", mode="fg", timeout_s=1.0)
        assert stale["ok"], stale

        th = threading.Thread(target=arb.unregister_session, args=(sid,))
        th.start()

        # 等 unregister 第一段鎖完成（佇列已 pop）；舊 worker 仍卡 send_cb，
        # unregister 停在 join(1.0) 視窗內。
        def _unregistered() -> bool:
            with arb._lock:
                return sid not in arb._queues
        assert _wait_until(_unregistered)

        # join 視窗內 re-register + 新 submit（epoch race 的觸發序列）
        arb.register_session(sid)
        blk2 = arb.submit(session_id=sid, command="blocking-new", source="t", mode="fg", timeout_s=1.0)
        assert blk2["ok"], blk2
        fresh = arb.submit(session_id=sid, command="fresh", source="t", mode="fg", timeout_s=1.0)
        assert fresh["ok"], fresh

        th.join(timeout=10.0)          # 舊碼在此之前執行遲到 flush → 誤殺 fresh
        assert not th.is_alive()

        # stale（舊佇列上的命令）已於第一段鎖內被原子 flush
        rec = _status(arb, stale["cmd_id"])
        assert rec["status"] == "error"
        assert rec["error_code"] == "FLUSHED_BY_RECOVERY"

        # fresh 不得被誤標終端態；放行 blocking-new 後照常執行到 done
        rec = _status(arb, fresh["cmd_id"])
        assert rec["error_code"] is None, rec   # 舊碼：FLUSHED_BY_RECOVERY（誤殺）
        release_new.set()
        assert _wait_until(lambda: _status(arb, fresh["cmd_id"])["done_at"] is not None)
        rec = _status(arb, fresh["cmd_id"])
        assert rec["status"] == "done"
        assert rec["error_code"] is None
    finally:
        release_old.set()
        release_new.set()
        arb.unregister_session(sid)


# ── flush/consume race 防線：已終結（done_at 非 None）的取件一律跳過 ──
def test_worker_skips_already_flushed_item():
    """flush 與 worker 取件的 race：若 queued 記錄已被 flush 終結，worker 之後取到
    同一筆 item 時必須跳過（不得重設 running、不得執行 send_cb）。

    以 flush_session() 直呼模擬（佇列不丟棄）：blocking 卡住 worker → queued 進佇列
    → flush 終結 queued → 在 queued 之後入列 marker（同 priority、counter 較大 →
    PriorityQueue FIFO 保證 marker 在 queued 之後被消費）→ 放行 worker → 等 marker
    done，即確定性證明 worker 已消化（跳過）queued 的 item——取代 sleep 的假綠風險
    （#128 review F4）。
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

        marker = arb.submit(session_id=sid, command="marker", source="t", mode="fg", timeout_s=1.0)
        assert marker["ok"], marker

        release.set()  # worker 繼續 → 取到 queued 的 item（必須跳過）→ 再取 marker
        assert _wait_until(lambda: _status(arb, marker["cmd_id"])["status"] == "done")

        rec = _status(arb, queued["cmd_id"])
        assert rec["status"] == "error"                     # 終端態不被 worker 覆寫回 running
        assert rec["error_code"] == "FLUSHED_BY_RECOVERY"
        assert "queued" not in executed                     # send_cb 未被執行
        assert "marker" in executed                         # marker 已消費 → queued 必已被處理過
    finally:
        release.set()
        arb.unregister_session(sid)


# ══ service 層釘測（#128 review F2 / F5-2）══════════════════════════════════
# 手法沿用 tests/test_issue28_bg_lifecycle.py：mock 掉 I/O 元件（WalWriter /
# SessionManager / DeviceWatcher），但保留**真實 CommandArbiter** 走完整 flush 路徑。

def _make_mock_service():
    """建立最小化 SerialwrapService：I/O 元件以 mock 替代、arbiter 為真實實例。"""
    from unittest.mock import patch

    from sw_core.config import SessionProfile

    profiles: list[SessionProfile] = []
    with (
        patch("sw_core.service.WalWriter"),
        patch("sw_core.service.SessionManager"),
        patch("sw_core.service.DeviceWatcher"),
    ):
        from sw_core.service import SerialwrapService

        svc = SerialwrapService(profiles)
    return svc


def _mock_exec(release: threading.Event):
    """SessionManager.execute_command 替身：command == "blocking" 卡住直到 release。"""

    def _exec(session_id, command, source, cmd_id, *, timeout_s, mode, expected_duration_s=None):
        if command == "blocking":
            release.wait(timeout=10.0)
        return {"ok": True, "stdout": ""}

    return _exec


# ── F2：daemon shutdown（service.stop）以 FLUSHED_BY_SHUTDOWN 終結未啟動命令 ──
def test_service_stop_flushes_with_shutdown_code():
    """service.stop() 對每個 session 的 unregister 帶 error_code=FLUSHED_BY_SHUTDOWN
    （#128 review F2）：與 recovery/detach 類路徑（FLUSHED_BY_RECOVERY）區隔終結碼，
    語意相同＝命令未執行、可於 READY 後重送。
    """
    from unittest.mock import MagicMock

    svc = _make_mock_service()
    sid = "COM0"
    release = threading.Event()
    svc._sessions.execute_command = _mock_exec(release)
    try:
        svc._arbiter.register_session(sid)
        blk = svc._arbiter.submit(session_id=sid, command="blocking", source="t", mode="fg", timeout_s=5.0)
        assert _wait_until(lambda: svc._arbiter.get(blk["cmd_id"])["command"]["status"] == "running")
        queued = svc._arbiter.submit(session_id=sid, command="queued", source="t", mode="fg", timeout_s=5.0)
        assert queued["ok"], queued

        # stop() 的其餘停機面向（engine/watcher/flash endpoint）以 mock 隔離，聚焦 arbiter 終結碼
        svc._running = True
        svc._engine = MagicMock()
        svc._watcher = MagicMock()
        svc._flash_endpoint = MagicMock()
        svc._sessions.list_sessions = MagicMock(return_value=[{"session_id": sid}])
        svc.stop()

        rec = svc._arbiter.get(queued["cmd_id"])["command"]
        assert rec["status"] == "error"
        assert rec["error_code"] == "FLUSHED_BY_SHUTDOWN"   # 非 FLUSHED_BY_RECOVERY
        assert rec["done_at"] is not None
    finally:
        release.set()


# ── F5-2：bg 命令被 flush 後 result_tail 走 arbiter fallback 回 FLUSHED_BY_RECOVERY ──
def test_result_tail_flushed_bg_falls_back_with_flushed_by_recovery():
    """bg 命令尚未啟動即被 flush（BackgroundCapture 從未建立）時，command.result_tail
    經 _bg_fallback_from_arbiter 合成回應：ok + status=error +
    error_code=FLUSHED_BY_RECOVERY + 空 chunks——而非 CMD_NOT_FOUND（#128 review F5-2）。
    """
    from unittest.mock import MagicMock

    svc = _make_mock_service()
    sid = "COM0"
    release = threading.Event()
    svc._sessions.execute_command = _mock_exec(release)
    try:
        svc._arbiter.register_session(sid)
        blk = svc._arbiter.submit(session_id=sid, command="blocking", source="t", mode="bg", timeout_s=5.0)
        assert _wait_until(lambda: svc._arbiter.get(blk["cmd_id"])["command"]["status"] == "running")
        queued = svc._arbiter.submit(session_id=sid, command="sleep 999", source="t", mode="bg", timeout_s=5.0)
        assert queued["ok"], queued

        svc._arbiter.unregister_session(sid)   # flush：queued 終結為 FLUSHED_BY_RECOVERY

        # BackgroundCapture 從未建立 → SessionManager 回 CMD_NOT_FOUND → 走 arbiter fallback
        svc._sessions.get_background_result = MagicMock(
            return_value={"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": queued["cmd_id"]},
        )
        result = svc.rpc("command.result_tail", {"cmd_id": queued["cmd_id"], "from_chunk": 0})
        assert result["ok"], result
        assert result["status"] == "error"
        assert result["error_code"] == "FLUSHED_BY_RECOVERY"
        assert result["chunks"] == []
    finally:
        release.set()
