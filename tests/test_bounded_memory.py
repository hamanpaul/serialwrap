"""tests/test_bounded_memory.py — #81 無界記憶體上限/淘汰。

涵蓋 BackgroundCapture chunks 環形上限 + cursor 對齊、_background 數量淘汰、
arbiter _commands 數量淘汰、human deferred 輸入緩衝上限。
"""
import threading
import time

import sw_core.arbiter as arbiter_mod
import sw_core.session_manager as sm
import sw_core.uart_io as uio
from sw_core.arbiter import CommandArbiter
from sw_core.config import SessionProfile
from sw_core.session_manager import BackgroundCapture, SessionManager, SessionRuntime
from sw_core.wal import WalWriter


def _profile(**overrides):
    defaults = dict(
        profile_name="test", platform="shell", com="COM0", act_no=0, alias="t0",
        device_by_id="/dev/serial/by-id/test", prompt_regex=r"[$#] $",
        login_regex=r"(?mi)login:\s*$", password_regex=r"(?mi)password:\s*$",
        user_env="U", pass_env="P", ready_probe="echo __READY__${nonce}", timeout_s=5.0,
    )
    defaults.update(overrides)
    return SessionProfile(**defaults)


def _cap():
    return BackgroundCapture(cmd_id="c", session_id="s", from_seq=1, quiet_window_s=1.0, created_at="t")


# ── BackgroundCapture 環形上限 ───────────────────────────────────────────
def test_bg_capture_ring_drops_oldest_and_tracks_total():
    cap = _cap()
    for _ in range(10):
        cap.add_chunk("x" * 100, max_bytes=250)
    assert cap.dropped_chunks > 0
    assert cap.total_bytes <= 300            # 受控於上限附近（保留尾端最新）
    assert cap.total_bytes == sum(len(c) for c in cap.chunks)  # 增量計數與實際一致
    assert len(cap.chunks) >= 1


def test_bg_capture_keeps_single_oversize_chunk():
    cap = _cap()
    cap.add_chunk("y" * 1000, max_bytes=100)  # 單一超大 chunk > 上限
    assert len(cap.chunks) == 1 and cap.dropped_chunks == 0  # 至少保留一段，不無限丟


def test_bg_capture_empty_chunk_is_noop():
    cap = _cap()
    cap.add_chunk("", max_bytes=100)
    assert cap.chunks == [] and cap.total_bytes == 0


# ── get_background_result cursor 對齊 dropped_chunks ─────────────────────
def _mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STATE_PATH", str(tmp_path / "state.json"))
    return SessionManager([], WalWriter(wal_dir=str(tmp_path)),
                          on_ready=lambda _s: None, on_detached=lambda _s: None)


def test_get_background_result_cursor_accounts_for_dropped(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    cap = _cap()
    cap.chunks = ["a", "b", "c"]   # 絕對索引 5,6,7
    cap.dropped_chunks = 5
    cap.status = "done"
    mgr._background["c"] = cap
    r = mgr.get_background_result("c", from_chunk=5, limit=10)
    assert r["chunks"] == ["a", "b", "c"] and r["next_chunk"] == 8 and r["lost"] is False
    r2 = mgr.get_background_result("c", from_chunk=0, limit=10)   # 落後於已丟棄資料
    assert r2["lost"] is True and r2["chunks"] == ["a", "b", "c"] and r2["next_chunk"] == 8
    r3 = mgr.get_background_result("c", from_chunk=6, limit=10)
    assert r3["chunks"] == ["b", "c"] and r3["next_chunk"] == 8


def test_get_background_result_unchanged_when_nothing_dropped(tmp_path, monkeypatch):
    """dropped_chunks==0（常態）時 cursor 行為與原本一致（向後相容）。"""
    mgr = _mgr(tmp_path, monkeypatch)
    cap = _cap()
    cap.chunks = ["a", "b", "c"]
    cap.status = "done"
    mgr._background["c"] = cap
    r = mgr.get_background_result("c", from_chunk=1, limit=10)
    assert r["chunks"] == ["b", "c"] and r["from_chunk"] == 1 and r["next_chunk"] == 3 and r["lost"] is False


def test_background_count_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "BG_CAPTURE_MAX_COUNT", 3)
    mgr = _mgr(tmp_path, monkeypatch)
    for i in range(5):
        c = BackgroundCapture(cmd_id=f"d{i}", session_id="s", from_seq=1, quiet_window_s=1.0,
                              created_at=f"2026-01-0{i}")
        c.status = "done"
        mgr._background[f"d{i}"] = c
    mgr._background["live"] = BackgroundCapture(cmd_id="live", session_id="s", from_seq=1,
                                               quiet_window_s=1.0, created_at="2026-01-09")  # active
    with mgr._lock:
        mgr._evict_background_locked()
    assert "live" in mgr._background                 # 進行中保留
    assert "d0" not in mgr._background               # 最舊已終結被淘汰
    assert sum(1 for c in mgr._background.values() if c.status != "active") <= 3


# ── arbiter per-session pending admission control（Codex 必修 [high]）──────
def test_submit_admission_control_rejects_when_pending_full(monkeypatch):
    """超過 per-session pending 上限即拒絕（SESSION_QUEUE_FULL），pending 不因持續 submit 無界成長。"""
    monkeypatch.setattr(arbiter_mod, "CMD_PENDING_MAX", 3)
    release = threading.Event()

    def _blocking_send(*_a, **_k):
        release.wait(timeout=5.0)  # 卡住 worker，使命令停在 accepted/running（done_at 仍 None＝pending）
        return {"ok": True, "stdout": ""}

    arb = CommandArbiter(send_cb=_blocking_send)
    arb.register_session("s")
    try:
        results = [arb.submit(session_id="s", command=f"c{i}", source="t", mode="fg", timeout_s=1.0)
                   for i in range(6)]
        accepted = [r for r in results if r.get("ok")]
        rejected = [r for r in results if not r.get("ok")]
        assert len(accepted) == 3, results                         # 恰好放行到上限
        assert rejected and all(r["error_code"] == "SESSION_QUEUE_FULL" for r in rejected)
        assert rejected[0]["limit"] == 3 and rejected[0]["pending"] >= 3
        with arb._lock:
            assert arb._count_pending_locked("s") <= 3             # 受 admission control 封頂
    finally:
        release.set()
        arb.unregister_session("s")


def test_submit_pending_drains_allows_new_after_completion(monkeypatch):
    """完成的命令不再計入 pending：drain 後可再次 submit（admission control 非永久封死）。"""
    monkeypatch.setattr(arbiter_mod, "CMD_PENDING_MAX", 2)
    arb = CommandArbiter(send_cb=lambda *a, **k: {"ok": True, "stdout": ""})
    arb.register_session("s")
    try:
        for _ in range(10):  # 同步 send_cb 立即完成，pending 隨即歸零，永不觸發 QUEUE_FULL
            r = arb.submit(session_id="s", command="c", source="t", mode="fg", timeout_s=1.0)
            assert r.get("ok"), r
            # 等該命令被 worker 完成（done_at 設定）
            for _ in range(200):
                got = arb.get(r["cmd_id"])
                if got["ok"] and got["command"].get("done_at"):
                    break
                time.sleep(0.005)
        with arb._lock:
            assert arb._count_pending_locked("s") == 0
    finally:
        arb.unregister_session("s")


# ── 淘汰 background capture 同步清 session.background_cmd_ids（Codex 必修 [medium]）──
def test_eviction_removes_bg_cmd_id_from_owning_session(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "BG_CAPTURE_MAX_COUNT", 2)
    mgr = _mgr(tmp_path, monkeypatch)
    sess = SessionRuntime(session_id="s", profile=_profile())
    mgr._sessions["s"] = sess
    # 三個已終結 capture 都屬於 session "s"，且 id 都登錄進 background_cmd_ids
    for i in range(3):
        cap = BackgroundCapture(cmd_id=f"b{i}", session_id="s", from_seq=1,
                                quiet_window_s=1.0, created_at=f"2026-01-0{i}")
        cap.status = "done"
        mgr._background[f"b{i}"] = cap
        sess.background_cmd_ids.append(f"b{i}")
    with mgr._lock:
        mgr._evict_background_locked()
    # 被淘汰的 capture id 不能殘留在 session list（否則 _on_bridge_rx 每筆 RX 仍會掃描死 id 而無界成長）
    assert set(mgr._background) == set(sess.background_cmd_ids)
    assert "b0" not in sess.background_cmd_ids          # 最舊已淘汰並同步清除
    assert len(sess.background_cmd_ids) <= 2


# ── arbiter _commands 數量淘汰 ──────────────────────────────────────────
def test_arbiter_commands_evicts_oldest_done(monkeypatch):
    monkeypatch.setattr(arbiter_mod, "CMD_HISTORY_MAX", 3)
    arb = CommandArbiter(send_cb=lambda *a, **k: {})
    for i in range(5):
        arb._commands[f"d{i}"] = {"done_at": f"2026-01-0{i}", "status": "done"}
    arb._commands["live"] = {"done_at": None, "status": "running"}
    with arb._lock:
        arb._evict_commands_locked()
    assert "live" in arb._commands                   # 進行中保留
    assert "d0" not in arb._commands                 # 最舊已完成被淘汰
    assert sum(1 for r in arb._commands.values() if r.get("done_at")) <= 3


# ── deferred 輸入緩衝上限 ───────────────────────────────────────────────
def test_deferred_buffer_capped(monkeypatch):
    monkeypatch.setattr(uio, "DEFERRED_INPUT_MAX_BYTES", 100)
    b = object.__new__(uio.UARTBridge)
    b._state_lock = threading.RLock()
    b._flash_mode = False
    b._interactive_owner = None
    b._agent_active = True
    b._suspended_owner = "human:cid1"
    b._deferred_buffers = {}
    client = uio.ConsoleClient(client_id="cid1", label="x", master_fd=-1, slave_fd=-1,
                               slave_path="", attached_at=0.0)
    b._handle_console_rx(client, b"A" * 500)
    assert len(b._deferred_buffers["cid1"]) == 100   # 受上限
    assert bytes(b._deferred_buffers["cid1"]) == b"A" * 100  # 保留最近輸入
