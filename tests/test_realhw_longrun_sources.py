"""P2-T14 red：lr-mixed 的 per-worker source、worker 存活事件、human frontend、
以及 background 終態在執行期收斂（BgResultTracker，對應 CMD_HISTORY_MAX=512 淘汰）。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1（longrun 變更 1、5、6、7）。
全部以替身 sw／human 執行，不碰 live；lr-mixed 只用執行緒與 ndjson。
"""
from __future__ import annotations

import ast
import json
import subprocess
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from realhw.cases import longrun

pytestmark = [pytest.mark.core_portable]


# --------------------------------------------------------------------------- #
# 替身與共用輔助
# --------------------------------------------------------------------------- #
def _fast_uniform(a, b):
    """agent 的 uniform(0.5,2.0) → 0.02 s；human 的 uniform(120,300) → 0.2 s。"""
    return 0.02 if a < 10 else 0.2


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _elog(directory: Path) -> "longrun._EventLog":
    directory.mkdir(parents=True, exist_ok=True)
    return longrun._EventLog(directory / "events.ndjson", time.monotonic())


def _events(directory: Path) -> list[dict]:
    return _read_jsonl(directory / "events.ndjson")


class _Systemd:
    """舊的 systemctl MainPID 路徑；新設計不得再呼叫（test_lr_mixed_pid_from_daemon_status 另行斷言）。

    預設回非零 pid，避免舊實作把 0 誤判為 daemon death 而提早收斂（會把失敗原因遮成假象）。
    """

    def __init__(self, pid: int = 4242) -> None:
        self._pid = pid
        self.calls = 0

    def main_pid(self) -> int:
        self.calls += 1
        return self._pid


class _FakeHuman:
    """HumanFrontend 替身：alive 恆 True，記錄 send_line。"""

    kind = "fake"

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.opened: list[str] = []

    def open(self, com: str) -> None:
        self.opened.append(com)

    def alive(self) -> bool:
        return True

    def send_line(self, text: str) -> None:
        self.lines.append(text)

    def send_text(self, text: str) -> None:
        self.lines.append(text)

    def send_key(self, key: str) -> None:
        pass

    def capture(self) -> str:
        return ""

    def client_id(self):
        return None

    def close(self) -> None:
        pass

    def crash(self) -> None:
        pass


class _LrSw:
    """lr-mixed 用的 sw 替身：記錄 submit_and_wait 的 source 與 run 的 argv。"""

    def __init__(self, pid: int = 77) -> None:
        self.pid = pid
        self.submits: list[tuple] = []  # (com, cmd, source)
        self.runs: list[tuple] = []     # run() 的 argv
        self._bg = 0
        self._lock = threading.Lock()

    def submit_and_wait(self, com, cmd, *, cmd_timeout=12.0, settle_s=1.5, poll_s=0.5, source=None):
        with self._lock:
            self.submits.append((com, cmd, source))
        marker = cmd.split(" ", 1)[1] if " " in cmd else cmd
        return {"status": "done", "stdout": marker + "\n"}

    def run(self, *args, **kwargs):
        with self._lock:
            self.runs.append(tuple(args))
            if args[:2] == ("cmd", "submit"):
                self._bg += 1
                return {"cmd_id": f"bg{self._bg:05d}"}
        if args[:2] == ("session", "interactive-open"):
            return {"ok": True, "interactive_id": 7}
        if args[:2] == ("session", "interactive-close"):
            return {"ok": True}
        if args[:2] == ("cmd", "status"):
            return {"ok": True, "command": {"status": "done"}}
        if tuple(args) == ("daemon", "status"):
            return {"ok": True, "running": True, "pid": self.pid}
        return {}

    def session(self, com):
        return {"state": "READY"}


def _ctx(tmp_path: Path, sw, *, human=None, duration_s: int = 2, workers: int = 4,
         snap: float = 1, systemd=None):
    cfg = {
        "boards": [{"com": "COM0"}, {"com": "COM1"}],
        "longrun": {"agent_workers": workers, "snapshot_interval_s": snap},
        "duration_s": duration_s,
        "_version": "serialwrap 0.3.2",
        "_git": "abc1234",
    }
    return SimpleNamespace(
        cfg=cfg, report_dir=tmp_path, case_dir=tmp_path / "lr-mixed", sw=sw,
        tmux=None, usbipd=None, systemd=systemd or _Systemd(), win=None,
        human=human if human is not None else (lambda tag, role: _FakeHuman()),
    )


class _ScriptedSw:
    """cmd status 替身：每個 cmd_id 依序回傳腳本，最後一筆重複。未設定者恆 running。"""

    def __init__(self) -> None:
        self._script: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def set(self, cid: str, *responses: dict) -> None:
        with self._lock:
            self._script[cid] = [dict(r) for r in responses]

    def run(self, *args, **kwargs):
        cid = args[args.index("--cmd-id") + 1]
        with self._lock:
            self.calls.append(cid)
            seq = self._script.get(cid)
            if not seq:
                return {"ok": True, "command": {"status": "running"}}
            resp = seq.pop(0) if len(seq) > 1 else seq[0]
            return dict(resp)


def _running() -> dict:
    return {"ok": True, "command": {"status": "running"}}


def _done() -> dict:
    return {"ok": True, "command": {"status": "done"}}


def _error(code: str) -> dict:
    return {"ok": True, "command": {"status": "error", "error_code": code}}


def _canceled() -> dict:
    return {"ok": True, "command": {"status": "canceled"}}


class _EvictingCmdStore:
    """依 sw/arbiter.py:233-251 的語意：完成紀錄超過 history_max 時淘汰 done_at 最舊者。

    line 命令提交即完成；background 命令第 1 次 cmd status 回 running，第 2 次起回 done。
    被淘汰的 cmd_id 回 {"ok":false,"error_code":"CMD_NOT_FOUND","cmd_id":id}。
    """

    def __init__(self, history_max: int = 512) -> None:
        self.history_max = history_max
        self._recs: "OrderedDict[str, dict]" = OrderedDict()
        self._seq = 0
        self.evicted: set[str] = set()
        self.status_calls: list[str] = []

    @property
    def evicted_count(self) -> int:
        return len(self.evicted)

    def _admit(self, kind: str) -> str:
        self._seq += 1
        cid = f"c{self._seq:06d}"
        self._recs[cid] = {"kind": kind, "polls": 0}  # 提交即完成，done_at 單調遞增
        while len(self._recs) > self.history_max:
            old, _ = self._recs.popitem(last=False)  # 最舊的已完成紀錄
            self.evicted.add(old)
        return cid

    def submit_line(self) -> str:
        return self._admit("line")

    def submit_bg(self) -> str:
        return self._admit("bg")

    def handle(self, argv: tuple) -> dict:
        if tuple(argv[:2]) != ("cmd", "status"):
            return {}
        cid = argv[argv.index("--cmd-id") + 1]
        self.status_calls.append(cid)
        rec = self._recs.get(cid)
        if rec is None:
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cid}
        if rec["kind"] == "bg":
            rec["polls"] += 1
            status = "running" if rec["polls"] == 1 else "done"
        else:
            status = "done"
        return {"ok": True, "command": {"status": status, "cmd_id": cid, "error_code": None}}


class _StoreSw:
    def __init__(self, store: _EvictingCmdStore) -> None:
        self.store = store
        self.argvs: list[tuple] = []

    def run(self, *args, **kwargs):
        self.argvs.append(tuple(args))
        return self.store.handle(args)


def _schedule_1600() -> list[str]:
    """1600 筆：1100 line＋500 background。

    前 1000 筆交錯（偶數位 bg，i<998），之後 593 筆 line，尾段 5 line＋1 bg＋1 line。
    尾段的 bg（倒數第二筆）在最後一次 poll 時已查過一次 running，收尾 final_drain 恰好只需查 1 次。
    控制組的 bg 全部位於前 1088 筆，會被淘汰（bg_lost ≥ 400）。
    """
    seq = ["bg" if (i % 2 == 0 and i < 998) else "line" for i in range(1000)]
    seq += ["line"] * 593
    seq += ["line"] * 5 + ["bg", "line"]
    assert len(seq) == 1600 and seq.count("bg") == 500 and seq.count("line") == 1100
    return seq


# --------------------------------------------------------------------------- #
# lr-mixed 編排：source、存活事件、human frontend、背景終態
# --------------------------------------------------------------------------- #
@pytest.mark.memory_only
def test_lr_mixed_agents_use_distinct_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    sw = _LrSw()
    longrun.lr_mixed(_ctx(tmp_path, sw, duration_s=2, workers=4))

    expected = {f"agent:rhw{i}" for i in range(4)}
    line_sources = {src for (_com, _cmd, src) in sw.submits}
    bg_sources = [argv[argv.index("--source") + 1] if "--source" in argv else None
                  for argv in sw.runs if argv[:2] == ("cmd", "submit")]
    owners = [argv[argv.index("--owner") + 1] if "--owner" in argv else None
              for argv in sw.runs if argv[:2] == ("session", "interactive-open")]

    assert line_sources == expected  # 四個 worker 的 line source 兩兩不同且齊全
    assert bg_sources and None not in bg_sources and set(bg_sources) <= expected
    assert owners and None not in owners and set(owners) <= expected


@pytest.mark.memory_only
def test_lr_mixed_worker_liveness_events(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    sw = _LrSw()
    ctx = _ctx(tmp_path, sw, duration_s=2, workers=4)
    longrun.lr_mixed(ctx)

    events = _events(ctx.case_dir)
    snaps = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    workers = {f"agent:rhw{i}" for i in range(4)} | {"human", "orchestrator", "bg_resolver"}
    for worker in sorted(workers):
        starts = [e for e in events if e["kind"] == "worker_start" and e["source"] == worker]
        stops = [e for e in events if e["kind"] == "worker_stop" and e["source"] == worker]
        assert len(starts) == 1, worker
        assert len(stops) == 1, worker
    assert events and all("wall_ts" in e for e in events)
    assert snaps and all("wall_ts" in s for s in snaps)
    assert all(datetime.fromisoformat(e["wall_ts"]).tzinfo is not None for e in events)


@pytest.mark.memory_only
def test_lr_mixed_human_uses_frontend(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    human = _FakeHuman()
    sw = _LrSw()
    ctx = _ctx(tmp_path, sw, human=lambda tag, role: human, duration_s=2)
    longrun.lr_mixed(ctx)

    assert "echo HUMAN_TICK_1" in human.lines  # 人類模擬經 ctx.human 的 frontend，不經 tmux
    snaps = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    analysis = longrun.analyze(snaps, _events(ctx.case_dir))
    assert set(analysis) == {"per_source", "stuck_attached", "pid_changes",
                             "daemon_death_at", "rss_trend"}


@pytest.mark.memory_only
def test_lr_mixed_background_resolved_during_run(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    sw = _LrSw()
    ctx = _ctx(tmp_path, sw, duration_s=2)
    longrun.lr_mixed(ctx)

    events = _events(ctx.case_dir)
    stop_t = next(e["t"] for e in events if e["kind"] == "stop")
    submitted = [e["cmd_id"] for e in events if e["kind"] == "submit_accepted"]
    resolved = [e for e in events if e["kind"] == "bg_result"]

    assert submitted  # background 送出後應拿到 cmd_id 並受理
    # 執行期寫入：至少一筆 bg_result 早於 stop 事件（不是全部等收尾才查）
    assert any(e["t"] < stop_t for e in resolved)
    # 不得留下「已受理但無 bg_result」的 cmd_id：每個受理者恰一筆 bg_result
    assert len(resolved) == len(submitted)
    assert {e["cmd_id"] for e in resolved} == set(submitted)
    assert any(e["kind"] == "worker_start" and e["source"] == "bg_resolver" for e in events)
    assert any(e["kind"] == "worker_stop" and e["source"] == "bg_resolver" for e in events)


# --------------------------------------------------------------------------- #
# BgResultTracker 單元
# --------------------------------------------------------------------------- #
@pytest.mark.memory_only
def test_background_results_survive_history_eviction(tmp_path):
    schedule = _schedule_1600()

    # 主線：每 8 筆 poll 一次，最後 final_drain
    store = _EvictingCmdStore(history_max=512)
    sw = _StoreSw(store)
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path / "a"))
    stop = threading.Event()
    bg_ids: list[str] = []
    for i, kind in enumerate(schedule):
        if kind == "bg":
            cid = store.submit_bg()
            bg_ids.append(cid)
            tracker.add(cid, source="agent:rhw0", com="COM0", round_=i, stop=stop)
        else:
            store.submit_line()
        if (i + 1) % 8 == 0:
            tracker.poll_once()

    assert store.evicted_count >= 1600 - 512                              # (a) 確實發生淘汰
    pre = tracker.pending_count()
    before = len(store.status_calls)
    tracker.final_drain()
    during = len(store.status_calls) - before
    assert during == pre and pre <= 8                                     # (e) 收尾只處理未完成項

    ev = _events(tmp_path / "a")
    results = [e for e in ev if e["kind"] == "bg_result"]
    counts = Counter(e["cmd_id"] for e in results)
    assert len(bg_ids) == 500
    assert set(counts) == set(bg_ids) and all(v == 1 for v in counts.values())  # (b)
    assert all(e["status"] == "done" for e in results)                           # (b)
    assert not [e for e in ev if e["kind"] == "bg_lost"]                         # (c)
    assert tracker.max_pending_seen <= longrun.BG_PENDING_MAX                    # (d)
    assert tracker.max_pending_seen <= 16                                        # (d)

    assert store.history_max == 512                                              # (f)
    assert not any("history" in str(a).lower() for argv in sw.argvs for a in argv)
    src = Path(longrun.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assigned = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    assert "CMD_HISTORY_MAX" not in assigned
    upper_strings = [n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and "HISTORY" in n.value]
    assert upper_strings == []

    # 對照組（g）：同一店與負載、全程不 poll、只在最後 final_drain → 被淘汰者必定 bg_lost，不得算成功
    store2 = _EvictingCmdStore(history_max=512)
    sw2 = _StoreSw(store2)
    tracker2 = longrun.BgResultTracker(sw2, _elog(tmp_path / "control"), pending_max=10_000)
    for i, kind in enumerate(schedule):
        if kind == "bg":
            cid = store2.submit_bg()
            tracker2.add(cid, source="agent:rhw0", com="COM0", round_=i, stop=threading.Event())
        else:
            store2.submit_line()
    tracker2.final_drain(timeout_s=1.0)
    ev2 = _events(tmp_path / "control")
    lost = [e for e in ev2 if e["kind"] == "bg_lost"]
    lost_ids = {e["cmd_id"] for e in lost}
    assert len(lost_ids) >= 400
    assert all(e["reason"] == "CMD_NOT_FOUND" for e in lost)
    assert not [e for e in ev2 if e["kind"] in ("bg_result", "done") and e["cmd_id"] in lost_ids]


class _FakeClock:
    """執行緒安全的假時鐘：只由測試端推進，供 BgResultTracker 的 clock= 注入。

    `read_seen` 在任一執行緒讀取時置位，讓測試能確認第五筆已記下 started，才推進時間。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 0.0
        self.read_seen = threading.Event()

    def __call__(self) -> float:
        with self._lock:
            self.read_seen.set()
            return self._now

    def advance(self, dt: float) -> None:
        with self._lock:
            self._now += dt


@pytest.mark.memory_only
def test_bg_pending_bounded_backpressure(tmp_path):
    sw = _ScriptedSw()
    for cid in ("c1", "c2", "c3", "c4", "c5"):
        sw.set(cid, _running())
    # Windows 的 time.monotonic 解析度約 15.6 ms，故注入假時鐘以確定性量測等待時間。
    clock = _FakeClock()
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path), pending_max=4, clock=clock)
    stop = threading.Event()
    for cid in ("c1", "c2", "c3", "c4"):
        tracker.add(cid, source="agent:rhw0", com="COM0", round_=1, stop=stop)
    assert tracker.pending_count() == 4

    fifth_done = threading.Event()

    def _fifth() -> None:
        tracker.add("c5", source="agent:rhw0", com="COM0", round_=1, stop=stop)
        fifth_done.set()

    clock.read_seen.clear()
    th = threading.Thread(target=_fifth, daemon=True)
    th.start()
    assert clock.read_seen.wait(2.0)  # 第五筆已在 add 內讀取 started，之後才推進假時鐘
    time.sleep(0.2)
    assert not fifth_done.is_set()  # 滿載時 add 阻塞（測試端背壓）
    clock.advance(0.25)  # 模擬等待 0.25 s；tracker 只透過注入的假時鐘量測
    sw.set("c1", _done())
    tracker.poll_once()  # c1 終態 → 移出 pending，騰出位置
    assert fifth_done.wait(2.0)
    th.join(2.0)

    assert tracker.max_pending_seen <= 4
    assert tracker.pending_count() <= 4
    bp = [e for e in _events(tmp_path) if e["kind"] == "bg_backpressure"]
    assert len(bp) == 1 and bp[0]["wait_s"] == pytest.approx(0.25)

    for cid in ("c2", "c3", "c4", "c5"):
        sw.set(cid, _done())
    tracker.final_drain(timeout_s=1.0)
    resolved = {e["cmd_id"] for e in _events(tmp_path) if e["kind"] == "bg_result"}
    assert resolved == {"c1", "c2", "c3", "c4", "c5"}  # 已受理者一個都不丟


@pytest.mark.memory_only
def test_bg_result_carries_submit_pair_key(tmp_path):
    sw = _ScriptedSw()
    sw.set("c1", _done())
    sw.set("c2", _error("SESSION_NOT_READY"))
    sw.set("c3", _canceled())
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    stop = threading.Event()
    tracker.add("c1", source="agent:rhw2", com="COM1", round_=9, stop=stop)
    tracker.add("c2", source="agent:rhw3", com="COM0", round_=4, stop=stop)
    tracker.add("c3", source="agent:rhw1", com="COM1", round_=5, stop=stop)
    tracker.poll_once()

    br = {e["cmd_id"]: e for e in _events(tmp_path) if e["kind"] == "bg_result"}
    assert br["c1"]["source"] == "agent:rhw2"
    assert br["c1"]["com"] == "COM1"
    assert br["c1"]["round"] == 9
    assert br["c1"]["status"] == "done"
    assert br["c1"]["latency_s"] >= 0
    assert br["c2"]["status"] == "error" and br["c2"]["error_code"] == "SESSION_NOT_READY"
    assert br["c2"]["source"] == "agent:rhw3" and br["c2"]["round"] == 4
    assert br["c3"]["status"] == "canceled"


@pytest.mark.memory_only
def test_bg_lost_never_counted_done(tmp_path):
    sw = _ScriptedSw()
    sw.set("c1", {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": "c1"})
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    tracker.add("c1", source="agent:rhw0", com="COM0", round_=1, stop=threading.Event())
    tracker.poll_once()

    ev = _events(tmp_path)
    lost = [e for e in ev if e["kind"] == "bg_lost"]
    assert len(lost) == 1
    assert lost[0]["cmd_id"] == "c1" and lost[0]["reason"] == "CMD_NOT_FOUND"
    assert not [e for e in ev if e["kind"] in ("bg_result", "done")]
    assert tracker.pending_count() == 0
    before = len(sw.calls)
    tracker.poll_once()
    assert len(sw.calls) == before  # 已移出 pending，不再查 c1


@pytest.mark.memory_only
def test_bg_poll_error_keeps_pending(tmp_path):
    sw = _ScriptedSw()
    sw.set("c1", {"_error": "TIMEOUT"}, _done())
    sw.set("c2", {"ok": True, "command": {"status": "weird"}})
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    stop = threading.Event()

    tracker.add("c1", source="agent:rhw0", com="COM0", round_=1, stop=stop)
    tracker.poll_once()
    assert tracker.pending_count() == 1  # RPC 失敗：留在 pending
    errs = [e for e in _events(tmp_path) if e["kind"] == "bg_poll_error"]
    assert len(errs) == 1 and errs[0]["cmd_id"] == "c1"

    tracker.poll_once()
    res = [e for e in _events(tmp_path) if e["kind"] == "bg_result"]
    assert len(res) == 1 and res[0]["cmd_id"] == "c1" and res[0]["status"] == "done"
    assert tracker.pending_count() == 0

    tracker.add("c2", source="agent:rhw0", com="COM0", round_=2, stop=stop)
    for _ in range(3):
        tracker.poll_once()
    assert tracker.pending_count() == 1  # 未知 status 不得視為終態
    weird = [e for e in _events(tmp_path) if e["kind"] == "bg_poll_error" and e["cmd_id"] == "c2"]
    assert len(weird) == 1
    tracker.final_drain(timeout_s=0.1)
    assert not [e for e in _events(tmp_path) if e["kind"] == "bg_result" and e["cmd_id"] == "c2"]


@pytest.mark.memory_only
def test_final_drain_only_pending(tmp_path):
    sw = _ScriptedSw()
    ids = [f"c{i}" for i in range(1, 24)]
    for cid in ids[:20]:
        sw.set(cid, _done())
    for cid in ids[20:]:
        sw.set(cid, _running())
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    stop = threading.Event()
    for cid in ids:
        tracker.add(cid, source="agent:rhw0", com="COM0", round_=1, stop=stop)

    tracker.poll_once()
    resolved_now = {e["cmd_id"] for e in _events(tmp_path) if e["kind"] == "bg_result"}
    assert resolved_now == set(ids[:20])

    sw.calls.clear()
    remaining = tracker.final_drain(timeout_s=0.5)
    assert remaining == 3
    assert set(sw.calls) == set(ids[20:])  # 只對仍在 pending 的 3 筆查詢；已結案者零查詢
    drains = [e for e in _events(tmp_path) if e["kind"] == "bg_drain"]
    assert drains and drains[-1]["remaining"] == 3
    final = {e["cmd_id"] for e in _events(tmp_path) if e["kind"] == "bg_result"}
    assert final == set(ids[:20])


@pytest.mark.memory_only
def test_bg_tracker_rejects_malformed_command_and_keeps_pending(tmp_path):
    sw = _ScriptedSw()
    sw.set("c1", {"ok": True, "command": "not-an-object"}, _done())
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    tracker.add("c1", source="agent:rhw0", com="COM0", round_=1,
                stop=threading.Event())

    tracker.poll_once()
    assert tracker.pending_count() == 1
    errors = [event for event in _events(tmp_path) if event["kind"] == "bg_poll_error"]
    assert len(errors) == 1 and errors[0]["cmd_id"] == "c1"

    tracker.poll_once()
    assert tracker.pending_count() == 0
    assert [event["kind"] for event in _events(tmp_path)].count("bg_result") == 1


@pytest.mark.memory_only
def test_bg_tracker_poll_respects_stop_between_items(tmp_path):
    sw = _ScriptedSw()
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    stop = threading.Event()
    for cid in ("c1", "c2"):
        tracker.add(cid, source="agent:rhw0", com="COM0", round_=1, stop=stop)
    stop.set()

    assert tracker.poll_once(stop=stop) == 0
    assert sw.calls == []


@pytest.mark.memory_only
def test_bg_tracker_final_drain_obeys_wall_deadline(tmp_path):
    class _SlowSw:
        def __init__(self):
            self.timeouts = []

        def run(self, *args, timeout=30.0):
            self.timeouts.append(timeout)
            time.sleep(min(0.01, max(0.0, timeout)))
            return _running()

    sw = _SlowSw()
    tracker = longrun.BgResultTracker(sw, _elog(tmp_path))
    stop = threading.Event()
    for index in range(20):
        tracker.add(f"c{index}", source="agent:rhw0", com="COM0", round_=index,
                    stop=stop)

    started = time.monotonic()
    remaining = tracker.final_drain(timeout_s=0.05)
    elapsed = time.monotonic() - started

    assert remaining == 20
    assert elapsed < 0.2
    assert 0 < len(sw.timeouts) < 20
    assert max(sw.timeouts) <= 0.05


@pytest.mark.memory_only
def test_bg_tracker_worker_error_is_reported_and_stops_load(tmp_path, monkeypatch):
    tracker = longrun.BgResultTracker(_ScriptedSw(), _elog(tmp_path))
    stop = threading.Event()

    def _boom(**kwargs):
        raise RuntimeError("poll loop failed")

    monkeypatch.setattr(tracker, "poll_once", _boom)
    tracker.run(stop)

    events = _events(tmp_path)
    assert stop.is_set()
    assert any(event["kind"] == "worker_error" and event["source"] == "bg_resolver"
               for event in events)
    assert any(event["kind"] == "worker_stop" and event["source"] == "bg_resolver"
               and event["reason"] == "error" for event in events)


@pytest.mark.memory_only
def test_longrun_worker_join_timeout_is_recorded(tmp_path):
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="blocked-worker", daemon=True)
    thread.start()
    elog = _elog(tmp_path)
    try:
        timed_out = longrun._join_workers([thread], elog, timeout_s=0.01)
        assert timed_out == ["blocked-worker"]
        assert any(event["kind"] == "worker_join_timeout"
                   for event in _events(tmp_path))
    finally:
        release.set()
        thread.join(timeout=1.0)


@pytest.mark.memory_only
def test_lr_mixed_snapshot_error_does_not_end_monitor_or_pass(tmp_path, monkeypatch):
    class _OneSessionFailureSw(_LrSw):
        def __init__(self):
            super().__init__()
            self.failed = False

        def session(self, com):
            if not self.failed:
                self.failed = True
                raise TimeoutError("single session-list timeout")
            return {"state": "READY"}

    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    ctx = _ctx(tmp_path, _OneSessionFailureSw(), duration_s=1, workers=1, snap=0.02)
    result = longrun.lr_mixed(ctx)

    events = _events(ctx.case_dir)
    snapshots = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    assert any(event["kind"] == "snapshot_error" for event in events)
    assert len(snapshots) > 0
    assert result.verdict == "FAIL"
    assert result.reason_code == "monitor_died"


@pytest.mark.memory_only
def test_lr_mixed_zero_rss_is_recorded_as_missing_observation(tmp_path, monkeypatch):
    from realhw.cases import longrun as module

    monkeypatch.setattr(longrun.random, "uniform", _fast_uniform)
    monkeypatch.setattr(module.procobs, "rss_kb", lambda pid: 0)
    ctx = _ctx(tmp_path, _LrSw(), duration_s=1, workers=1, snap=0.02)
    result = longrun.lr_mixed(ctx)

    snapshots = _read_jsonl(ctx.case_dir / "snapshots.ndjson")
    assert snapshots
    assert all(snapshot["rss_kb"] is None for snapshot in snapshots)
    assert all(snapshot["rss_probe_error"] for snapshot in snapshots)
    assert result.verdict == "FAIL"
    assert result.reason_code == "monitor_died"
