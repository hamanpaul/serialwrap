"""restart 交接判準的負向驗證：持有者取樣觀測器（`bb_l15.HolderObserver`）與 oracle（`assert_holder_handoff`）。

舊判準於操作完成後才探測，新舊 holder 即使重疊，仍會得出 `old_close < new_open`（假綠）。
本檔分兩層驗證：
- 合成取樣時間線（core_portable／memory_only）：判準本身必須拒絕重疊、未釋放、取樣起點缺口與終點缺席。
- 真實 PTY 上的 holder 行程（posix_only）：刻意讓兩個 holder 同時持有同一 tty，觀測器必須判定重疊，
  oracle 必須拒絕；另以循序交接作正向對照，確認觀測器不產生假重疊。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

CONTRACT_DIR = Path(__file__).resolve().parent / "contract_blackbox"


def _bb_l15():
    if str(CONTRACT_DIR) not in sys.path:
        sys.path.insert(0, str(CONTRACT_DIR))
    import bb_l15

    return bb_l15


def _timeline(bb_l15, rows: list[set[int]]):
    return bb_l15.HolderTimeline([(index, frozenset(row)) for index, row in enumerate(rows)])


# ── 合成時間線：判準邏輯（不需 PTY 或子行程）──────────────────────────────────


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_timeline_edges_follow_sample_order() -> None:
    bb = _bb_l15()
    timeline = _timeline(bb, [{10}, {10}, set(), {10}])
    kinds = [(edge.kind, edge.sample) for edge in timeline.edges(10)]
    assert kinds == [("opened", 0), ("closed", 2), ("opened", 3)]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oracle_accepts_clean_sequential_handoff() -> None:
    bb = _bb_l15()
    bb.assert_holder_handoff(_timeline(bb, [{10}, {10}, set(), {20}, {20}]), 10, 20)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oracle_rejects_overlapping_holders() -> None:
    """新舊 holder 於同一取樣中同時存在，必須失敗（舊判準會在此情境假綠）。"""
    bb = _bb_l15()
    timeline = _timeline(bb, [{10}, {10, 20}, {20}])
    with pytest.raises(AssertionError, match="同時持有"):
        bb.assert_holder_handoff(timeline, 10, 20)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oracle_rejects_old_holder_never_released() -> None:
    bb = _bb_l15()
    with pytest.raises(AssertionError, match="未釋放"):
        bb.assert_holder_handoff(_timeline(bb, [{10}, {10}, {10}]), 10, 20)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oracle_rejects_missing_start_observation() -> None:
    """取樣起點未見舊 holder，無法證明其曾持有 tty，必須失敗。"""
    bb = _bb_l15()
    with pytest.raises(AssertionError, match="起點缺口"):
        bb.assert_holder_handoff(_timeline(bb, [set(), {10}, {20}]), 10, 20)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_oracle_rejects_new_holder_absent_at_end() -> None:
    bb = _bb_l15()
    with pytest.raises(AssertionError, match="取樣結束時新 holder 未持有"):
        bb.assert_holder_handoff(_timeline(bb, [{10}, set(), {20}, set()]), 10, 20)


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("denial", ["fd_directory", "fd_stat"])
def test_holder_observer_fails_closed_when_fd_access_is_denied(tmp_path: Path, monkeypatch, denial: str) -> None:
    """不可讀的 fd 可能是持有 tty 的 holder；目錄與項目權限錯誤都不得轉成空集合。"""
    bb = _bb_l15()
    tty_st = os.stat(tmp_path)
    def listdir(path):
        if path == "/proc/4242/fd":
            if denial == "fd_directory":
                raise PermissionError("fd directory permission denied")
            return ["3"]
        raise AssertionError(f"unexpected proc path: {path}")

    def denied_stat(path):
        if path == "/proc/4242/fd/3":
            raise PermissionError("permission denied")
        raise AssertionError(f"unexpected fd path: {path}")

    monkeypatch.setattr(bb, "os", type("ProcOS", (), {"listdir": staticmethod(listdir), "stat": staticmethod(denied_stat)}))
    expected = "fd directory permission denied" if denial == "fd_directory" else "permission denied"
    with pytest.raises(PermissionError, match=expected):
        bb.observe_holders(tty_st, pids=[4242])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_observer_error_does_not_mask_operation_failure(monkeypatch) -> None:
    """操作本身失敗時，後續觀測權限錯誤不得將原始失敗改成 evidence-insufficient。"""
    bb = _bb_l15()
    unavailable = threading.Event()
    calls = 0

    def observe(_tty_st, *, pids):
        nonlocal calls
        calls += 1
        if calls > 1:
            unavailable.set()
            raise PermissionError("candidate fd permission denied")
        return []

    fake_os = type("FakeOS", (), {"stat": staticmethod(lambda _path: object())})
    monkeypatch.setattr(bb, "os", fake_os)
    monkeypatch.setattr(bb, "observe_holders", observe)
    observer = bb.HolderObserver("unused", pids=[4242], period_s=0.01)

    with pytest.raises(AssertionError, match="original operation failure"):
        with observer:
            assert unavailable.wait(timeout=5.0), "測試觀測器沒有觸發權限錯誤"
            raise AssertionError("original operation failure")


# ── 真實 PTY 上的 holder 行程 ─────────────────────────────────────────────────


def _open_pty() -> tuple[int, str]:
    """建立 PTY pair；關閉 slave 端、保留 master 以維持 slave 節點存在，回傳 (master_fd, slave_path)。"""
    master, slave = os.openpty()
    try:
        return master, os.ttyname(slave)
    finally:
        os.close(slave)


def _spawn_holder(slave_path: str) -> subprocess.Popen:
    """以 bash 開啟 slave 並保留 fd 3，之後 exec 為 sleep；pid 不變，結束時 fd 隨行程釋放。"""
    return subprocess.Popen(
        ["bash", "-c", 'exec 3<>"$1"; exec sleep 120', "holder", slave_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_observer_flags_concurrent_holders_and_oracle_rejects_them(monkeypatch) -> None:
    """刻意重疊：第二個 holder 在舊 holder 仍持有 PTY 時開啟；觀測器必須判定重疊，oracle 必須拒絕。"""
    bb = _bb_l15()
    master, slave_path = _open_pty()
    old: subprocess.Popen | None = None
    new: subprocess.Popen | None = None
    try:
        old = _spawn_holder(slave_path)
        bb.wait_holder(slave_path, old.pid, present=True)
        candidates = [os.getpid(), old.pid]
        monkeypatch.setattr(bb, "numeric_proc_pids", lambda: tuple(candidates), raising=False)
        observer = bb.HolderObserver(slave_path, pids=[old.pid], discover_all=True)
        try:
            with observer:
                new = _spawn_holder(slave_path)
                candidates.append(new.pid)
                bb.wait_holder(slave_path, new.pid, present=True)
                time.sleep(0.2)
                _stop(old)
                bb.wait_holder(slave_path, old.pid, present=False)
                time.sleep(0.05)
        except bb.HolderObservationUnavailable as exc:
            pytest.skip(f"候選 PID fd 不可讀，未形成通過證據（evidence-insufficient）：{exc}")
        timeline = observer.timeline
        assert timeline is not None
        assert timeline.overlap_samples(old.pid, new.pid), "觀測器未偵測到刻意重疊的兩個 holder"
        with pytest.raises(AssertionError, match="同時持有"):
            bb.assert_holder_handoff(timeline, old.pid, new.pid)
    finally:
        _stop(new)
        _stop(old)
        os.close(master)


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_observer_sequential_handoff_passes_oracle(monkeypatch) -> None:
    """循序交接（舊 holder 完全釋放後才開啟新 holder）為正向對照：觀測器不得產生假重疊，oracle 必須通過。"""
    bb = _bb_l15()
    master, slave_path = _open_pty()
    old: subprocess.Popen | None = None
    new: subprocess.Popen | None = None
    try:
        old = _spawn_holder(slave_path)
        bb.wait_holder(slave_path, old.pid, present=True)
        candidates = [os.getpid(), old.pid]
        monkeypatch.setattr(bb, "numeric_proc_pids", lambda: tuple(candidates), raising=False)
        observer = bb.HolderObserver(slave_path, pids=[old.pid], discover_all=True)
        try:
            with observer:
                _stop(old)
                bb.wait_holder(slave_path, old.pid, present=False)
                new = _spawn_holder(slave_path)
                candidates.append(new.pid)
                bb.wait_holder(slave_path, new.pid, present=True)
                time.sleep(0.05)
        except bb.HolderObservationUnavailable as exc:
            pytest.skip(f"候選 PID fd 不可讀，未形成通過證據（evidence-insufficient）：{exc}")
        timeline = observer.timeline
        assert timeline is not None
        assert not timeline.overlap_samples(old.pid, new.pid)
        bb.assert_holder_handoff(timeline, old.pid, new.pid)
    finally:
        _stop(new)
        _stop(old)
        os.close(master)
