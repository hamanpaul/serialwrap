"""#122 長跑編排（`lr-mixed`）＋事後分析器 `analyze()`。

- `analyze(snapshots, events) -> dict`：**純函式、無 I/O**，把長跑收集的兩條
  ndjson（快照序列＋事件序列）收斂成統計：per_source 命令計數、stuck_attached
  連續非 READY 區段、pid_changes、daemon_death_at、rss_trend。單測釘死行為。
- `lr-mixed` case（tier="longrun"）：4 個 agent worker thread（輪流對兩板送命令，
  mix line／background／interactive）＋1 個 human 模擬 thread（共用 human frontend，
  每 2-5 分鐘敲一行）＋1 個 snapshot thread（每 `snapshot_interval_s` 記狀態/RSS/pid），
  跑到 `duration_s` 或 SIGINT 停；重大事件（確認 daemon 行程已退出／兩板同時非 READY >15min）
  只停負載記事件、**不重啟 daemon**（保留現場）；收尾以 `analyze()` 產出分析報告。

無人環境穩健性優先：所有 thread 迴圈的單一動作包 try/except（記事件續跑、不殺整跑），
ndjson 開檔 append+flush，主/子迴圈以短 sleep 輪詢 stop flag（不 sleep 整段 duration）。
時鐘一律用相對秒（`time.monotonic() - start`），與 analyze 對齊。
"""
from __future__ import annotations

import json
import random
import signal
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import frontends, procobs
from ..harness import Case, CaseResult, register

# 重大事件／FAIL 級門檻（秒）
MAJOR_BOTH_DOWN_S = 15 * 60  # 兩板同時非 READY 持續超過此值＝重大事件
FAIL_STUCK_S = 15 * 60       # 單板連續非 READY ≥此值＝FAIL 級統計
_COUNTED_KINDS = ("submit", "done", "error")
BG_PENDING_MAX = 64
BG_POLL_INTERVAL_S = 1.0
BG_RESOLVE_WARN_S = 30.0
BG_FINAL_DRAIN_S = 30.0
LR_WORKER_JOIN_TIMEOUT_S = 30.0
BG_TERMINAL = frozenset({"done", "error", "canceled"})


# --------------------------------------------------------------------------- #
# 純分析器（無 I/O、可單測）
# --------------------------------------------------------------------------- #
def analyze(snapshots: list[dict], events: list[dict]) -> dict:
    """把快照/事件序列收斂成統計 dict（純函式）。

    回傳鍵：
      - per_source：{source: {"submit","done","error"}}（三鍵恆存，缺項為 0）
      - stuck_attached：[{"com","from_t","to_t","duration_s"}]（連續非 READY 區段）
      - pid_changes：相鄰快照間 MainPID 變更次數（僅計兩端皆非 0 的變更＝重啟）
      - daemon_death_at：pid 首次轉 0 的相對 t（無則 None）
      - rss_trend：{"first_kb","last_kb","delta_kb"}（首尾 VmRSS）
    """
    # per_source 命令計數（只計 submit/done/error；三鍵恆存）
    per_source: dict[str, dict[str, int]] = {}
    for ev in events:
        kind = ev.get("kind")
        if kind not in _COUNTED_KINDS and kind != "bg_result":
            continue
        src = ev.get("source") or "?"
        bucket = per_source.setdefault(src, {"submit": 0, "done": 0, "error": 0})
        if kind == "bg_result":
            bucket["done" if ev.get("status") == "done" else "error"] += 1
        else:
            bucket[kind] += 1

    # stuck_attached：每個 COM 的連續非 READY 區段（起訖 t 與時長）
    coms: list[str] = []
    seen: set[str] = set()
    for snap in snapshots:
        for com in (snap.get("sessions") or {}):
            if com not in seen:
                seen.add(com)
                coms.append(com)
    coms.sort()

    stuck: list[dict] = []
    for com in coms:
        open_from: int | None = None
        last_t = 0
        for snap in snapshots:
            t = snap.get("t", 0)
            last_t = t
            state = (snap.get("sessions") or {}).get(com)
            if state == "READY":
                if open_from is not None:
                    stuck.append({"com": com, "from_t": open_from, "to_t": t,
                                  "duration_s": t - open_from})
                    open_from = None
            elif open_from is None:
                open_from = t
        # 收尾仍非 READY→以最後快照 t 封口；僅 duration>0 才記，避免「最後一筆快照剛好
        # 非 READY 或 sessions 缺該 COM（如 daemon death 後清空）」產生無意義的 0 秒 stuck 段。
        if open_from is not None and last_t > open_from:
            stuck.append({"com": com, "from_t": open_from, "to_t": last_t,
                          "duration_s": last_t - open_from})

    # pid 變更與 daemon 死亡；新事件只在行程確認退出後記錄，兼容舊的 pid=0 快照。
    pid_changes = 0
    daemon_death_at: int | None = None
    for ev in events:
        if ev.get("kind") == "major" and ev.get("detail") == "daemon_death":
            value = ev.get("t_at", ev.get("t"))
            if isinstance(value, int):
                daemon_death_at = value
                break
    prev_pid: int | None = None
    for snap in snapshots:
        pid = snap.get("pid")
        if pid == 0 and daemon_death_at is None:
            daemon_death_at = snap.get("t")
        if pid is None:
            continue  # null 代表無觀測，不算 PID 變更，也不代表 daemon 死亡。
        if prev_pid and pid and pid != prev_pid:  # 兩端皆非 0 才算「重啟」變更
            pid_changes += 1
        prev_pid = pid

    # rss_trend（首尾）
    if snapshots:
        first_rss = snapshots[0].get("rss_kb", 0) or 0
        last_rss = snapshots[-1].get("rss_kb", 0) or 0
    else:
        first_rss = last_rss = 0

    return {
        "per_source": per_source,
        "stuck_attached": stuck,
        "pid_changes": pid_changes,
        "daemon_death_at": daemon_death_at,
        "rss_trend": {"first_kb": first_rss, "last_kb": last_rss,
                      "delta_kb": last_rss - first_rss},
    }


# --------------------------------------------------------------------------- #
# I/O helpers（編排用；純函式部分保持無副作用）
# --------------------------------------------------------------------------- #
def _append_ndjson(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()


def _read_ndjson(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _sleep_stop(stop: threading.Event, secs: float) -> bool:
    """可被 stop 打斷的 sleep；回傳 True 表示 stop 已被設定。"""
    return stop.wait(timeout=max(0.0, secs))


class _EventLog:
    """執行緒安全的事件 ndjson 記錄器（append + 即時 flush）。"""

    def __init__(self, path: Path, start: float) -> None:
        self._start = start
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, source: str, kind: str, **detail: Any) -> None:
        rec: dict[str, Any] = {"t": int(time.monotonic() - self._start),
                               "wall_ts": datetime.now(timezone.utc).isoformat(),
                               "source": source, "kind": kind}
        rec.update(detail)
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except (OSError, ValueError):
                pass


def _human_frontend(ctx: Any, tag: str, role: str) -> Any:
    """取得共用 frontend；相容未提供 factory 的舊測試 context。"""
    factory = getattr(ctx, "human", None)
    if callable(factory):
        return factory(tag, role)
    factory = frontends.make_frontend_factory(
        {"human_frontend": "tmux"}, tmux=ctx.tmux, sw=ctx.sw)
    return factory(tag, role)


@dataclass
class _PendingBackground:
    cmd_id: str
    source: str
    com: str
    round_: int
    submitted_at: float
    slow_reported: bool = False
    poll_error_reported: bool = False


class BgResultTracker:
    """長跑期間輪詢 background 命令，避免產品 history 淘汰未擷取結果。"""

    def __init__(self, sw: Any, elog: _EventLog, *, pending_max: int = BG_PENDING_MAX,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if pending_max < 1:
            raise ValueError("pending_max 必須大於零")
        self._sw = sw
        self._elog = elog
        self._pending_max = int(pending_max)
        self._clock = clock
        self._condition = threading.Condition()
        self._pending: OrderedDict[str, _PendingBackground] = OrderedDict()
        self.max_pending_seen = 0

    def add(self, cmd_id: str, *, source: str, com: str, round_: int,
            stop: threading.Event) -> None:
        started = self._clock()
        waited = False
        with self._condition:
            while len(self._pending) >= self._pending_max and not stop.is_set():
                waited = True
                self._condition.wait(timeout=0.05)
            wait_s = max(0.0, self._clock() - started) if waited else 0.0
            pending = _PendingBackground(str(cmd_id), source, com, int(round_), self._clock())
            self._pending[pending.cmd_id] = pending
            self.max_pending_seen = max(self.max_pending_seen, len(self._pending))
            self._condition.notify_all()
        if waited:
            self._elog.emit(source, "bg_backpressure", wait_s=wait_s)

    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def _record_poll_error(self, pending: _PendingBackground, detail: str) -> None:
        if pending.poll_error_reported:
            return
        pending.poll_error_reported = True
        self._elog.emit(pending.source, "bg_poll_error", cmd_id=pending.cmd_id,
                        detail=detail, com=pending.com, round=pending.round_)

    def _finish(self, pending: _PendingBackground, *, lost: bool,
                status: str | None = None, error_code: Any = None) -> None:
        age_s = max(0.0, self._clock() - pending.submitted_at)
        if lost:
            self._elog.emit(pending.source, "bg_lost", cmd_id=pending.cmd_id,
                            reason="CMD_NOT_FOUND", age_s=age_s,
                            com=pending.com, round=pending.round_)
        else:
            self._elog.emit(pending.source, "bg_result", cmd_id=pending.cmd_id,
                            status=status, error_code=error_code, latency_s=age_s,
                            com=pending.com, round=pending.round_)
        with self._condition:
            if self._pending.get(pending.cmd_id) is pending:
                del self._pending[pending.cmd_id]
                self._condition.notify_all()

    def _poll_one(
        self,
        pending: _PendingBackground,
        *,
        deadline: float | None = None,
        timeout_limit_s: float | None = None,
    ) -> bool:
        kwargs: dict[str, float] = {}
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if timeout_limit_s is not None:
                remaining = min(remaining, timeout_limit_s)
            kwargs["timeout"] = remaining
        try:
            response = self._sw.run("cmd", "status", "--cmd-id", pending.cmd_id, **kwargs)
        except Exception as exc:
            self._record_poll_error(pending, repr(exc))
            return False
        if not isinstance(response, dict):
            self._record_poll_error(pending, f"cmd status 回應非物件：{type(response).__name__}")
            return False
        error_code = response.get("error_code")
        command = response.get("command") or response
        if not isinstance(command, dict):
            self._record_poll_error(pending, f"cmd status command 非物件：{type(command).__name__}")
            return False
        error_code = command.get("error_code") or error_code
        if error_code == "CMD_NOT_FOUND":
            self._finish(pending, lost=True)
            return True
        if response.get("_error") or response.get("ok") is False:
            self._record_poll_error(pending, str(response.get("_error") or error_code or "RPC error"))
            return False
        status = command.get("status")
        if status in BG_TERMINAL:
            self._finish(pending, lost=False, status=str(status), error_code=error_code)
            return True
        if status not in {"accepted", "running", "queued"}:
            self._record_poll_error(pending, f"未知或缺少 status：{status!r}")
        age_s = max(0.0, self._clock() - pending.submitted_at)
        if age_s >= BG_RESOLVE_WARN_S and not pending.slow_reported:
            pending.slow_reported = True
            self._elog.emit(pending.source, "bg_slow", cmd_id=pending.cmd_id,
                            age_s=age_s, com=pending.com, round=pending.round_)
        return False

    def poll_once(self, *, stop: threading.Event | None = None,
                  deadline: float | None = None,
                  timeout_limit_s: float | None = None) -> int:
        """依提交順序查詢 pending；停止或到期時不再開始下一筆 RPC。"""
        with self._condition:
            pending = list(self._pending.values())
        removed = 0
        for item in pending:
            if stop is not None and stop.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if self._poll_one(item, deadline=deadline, timeout_limit_s=timeout_limit_s):
                removed += 1
        return removed

    def run(self, stop: threading.Event) -> None:
        source = "bg_resolver"
        started = self._clock()
        self._elog.emit(source, "worker_start")
        next_alive = started + 300.0
        reason = "stop"
        try:
            while not stop.is_set():
                try:
                    self.poll_once(stop=stop,
                                   deadline=time.monotonic() + BG_POLL_INTERVAL_S)
                except Exception as exc:
                    reason = "error"
                    self._elog.emit(source, "worker_error", detail=repr(exc))
                    stop.set()
                    with self._condition:
                        self._condition.notify_all()
                    break
                now = self._clock()
                if now >= next_alive:
                    self._elog.emit(source, "worker_alive")
                    next_alive = now + 300.0
                stop.wait(BG_POLL_INTERVAL_S)
        finally:
            self._elog.emit(source, "worker_stop", reason=reason)

    def final_drain(self, timeout_s: float = BG_FINAL_DRAIN_S) -> int:
        """停止負載後只輪詢仍 pending 的命令至終態或逾時。"""
        timeout_limit_s = max(0.0, float(timeout_s))
        deadline = time.monotonic() + timeout_limit_s
        while self.pending_count() and time.monotonic() < deadline:
            self.poll_once(deadline=deadline, timeout_limit_s=timeout_limit_s)
            remaining_s = deadline - time.monotonic()
            if self.pending_count() and remaining_s > 0:
                time.sleep(min(BG_POLL_INTERVAL_S, remaining_s))
        remaining = self.pending_count()
        if remaining:
            self._elog.emit("bg_resolver", "bg_drain", remaining=remaining)
        return remaining


def _join_workers(threads: list[threading.Thread], elog: _EventLog,
                  timeout_s: float = LR_WORKER_JOIN_TIMEOUT_S) -> list[str]:
    """以單一總期限等待 worker，未結束者寫入 evidence 並回傳名稱。"""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    timed_out: list[str] = []
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            timed_out.append(thread.name)
            elog.emit("orchestrator", "worker_join_timeout", worker=thread.name,
                      timeout_s=max(0.0, float(timeout_s)))
    return timed_out


def _render_analysis_md(analysis: dict, meta: dict, major: dict,
                        events: list[dict], elapsed_s: int) -> str:
    per_source = analysis["per_source"]
    stuck = analysis["stuck_attached"]
    rss = analysis["rss_trend"]
    tot_submit = sum(d["submit"] for d in per_source.values())
    tot_done = sum(d["done"] for d in per_source.values())
    tot_error = sum(d["error"] for d in per_source.values())

    lines = [
        "# realhw 長跑分析（lr-mixed，#122）",
        "",
        f"- 版本：{meta.get('version')}（git {meta.get('git')}）",
        f"- 目標時長：{meta.get('duration_s')}s；實際執行：約 {elapsed_s}s",
        f"- 重大事件：{major.get('kind') + '@t=' + str(major.get('t')) + 's' if major else '無'}",
        f"- 命令總計：submit {tot_submit}／done {tot_done}／error {tot_error}",
        f"- daemon pid_changes：{analysis['pid_changes']}"
        f"；daemon_death_at：{analysis['daemon_death_at']}",
        f"- RSS：{rss['first_kb']} → {rss['last_kb']} kB（Δ{rss['delta_kb']} kB）",
        "",
        "## per-source 命令計數",
        "",
        "| source | submit | done | error |",
        "|---|---|---|---|",
    ]
    for src in sorted(per_source):
        d = per_source[src]
        lines.append(f"| {src} | {d['submit']} | {d['done']} | {d['error']} |")

    lines += ["", "## stuck_attached（連續非 READY 區段）", ""]
    if stuck:
        lines += ["| com | from_t | to_t | duration_s |", "|---|---|---|---|"]
        for s in stuck:
            lines.append(f"| {s['com']} | {s['from_t']} | {s['to_t']} | {s['duration_s']} |")
    else:
        lines.append("（無：全程兩板皆 READY）")

    # 事件時間線——只列「值得注意」事件（error／worker_error／major／minicom_dead／
    # snapshot_error／sigint），常態 submit/done/busy/tick 只入計數不入時間線（避免長跑爆量）。
    notable_kinds = {"error", "worker_error", "major", "minicom_dead",
                     "minicom_open", "snapshot_error", "sigint"}
    notable = [e for e in events if e.get("kind") in notable_kinds]
    lines += ["", f"## 事件時間線（值得注意者，共 {len(notable)} 筆，最多列 200）", ""]
    if notable:
        for e in notable[:200]:
            detail = e.get("detail") or e.get("error_code") or ""
            lines.append(f"- t={e.get('t')}s [{e.get('source')}] {e.get('kind')} {detail}".rstrip())
    else:
        lines.append("（無值得注意事件）")

    # 與歷史基線對照句
    lines += [
        "",
        "## 歷史基線對照",
        "",
        f"歷史基線：長跑期間 daemon 應全程存活（pid_changes 期望 0、無 daemon_death）、"
        f"RSS 不應單調暴漲（記憶體洩漏哨兵）、兩板不應長時間卡非 READY；#52 曾見 file.* "
        f"期間 health ping 阻塞 19.8s。本輪 pid_changes={analysis['pid_changes']}、"
        f"daemon_death_at={analysis['daemon_death_at']}、RSS Δ{rss['delta_kb']} kB、"
        f"最長 stuck={max((s['duration_s'] for s in stuck), default=0)}s。",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# lr-mixed 編排 case
# --------------------------------------------------------------------------- #
def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="longrun", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


@_case(
    "lr-mixed",
    "混合負載長跑＋事後分析（agent×4＋human＋快照）",
    hints=(
        "同板多 worker 以 per-board lock 序列化；仍為多 source 競爭仲裁",
        "SIGINT 提前收斂＝記 sigint event、停負載，仍產出 longrun-analysis.md",
        "重大事件（daemon 已退出／兩板同時非 READY >15min）只停負載記事件、不自動重啟 daemon（保留現場）",
        "snapshot PID 取 daemon status；RSS 使用平台 adapter 觀測",
        "interactive 輪撞到 human raw owner 回 SESSION_INTERACTIVE_BUSY＝benign（不計 error）",
    ),
    requires=("human_frontend", "two_boards"),
)
def lr_mixed(ctx) -> CaseResult:
    cfg = ctx.cfg
    lr = cfg.get("longrun") or {}
    duration_s = int(cfg.get("duration_s") or lr.get("duration_s") or 0)
    n_workers = int(lr.get("agent_workers") or 4)
    snap_interval = float(lr.get("snapshot_interval_s") or 300)
    boards = [b["com"] for b in cfg["boards"]]

    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    events_path = ctx.case_dir / "events.ndjson"
    snaps_path = ctx.case_dir / "snapshots.ndjson"
    analysis_path = ctx.case_dir / "longrun-analysis.md"

    start = time.monotonic()
    stop = threading.Event()
    major: dict = {}  # {"kind":..., "t":...}——第一個重大事件
    elog = _EventLog(events_path, start)
    board_locks = {b: threading.Lock() for b in boards}
    tracker = BgResultTracker(ctx.sw, elog)

    def rel_t() -> int:
        return int(time.monotonic() - start)

    # ---- agent worker 動作 ----
    def _do_line(source: str, board: str, n: int) -> None:
        tag = source.split(":")[-1]
        marker = f"LR_{tag}_{n}_{random.randint(1000, 9999)}"
        elog.emit(source, "submit", com=board, round=n, mode="line")
        with board_locks[board]:
            cmd = ctx.sw.submit_and_wait(board, f"echo {marker}", cmd_timeout=10.0,
                                         source=source)
        if cmd.get("status") == "done" and marker in (cmd.get("stdout") or ""):
            elog.emit(source, "done", com=board, round=n, mode="line")
        else:
            code = cmd.get("error_code") or cmd.get("status") or cmd.get("_error")
            elog.emit(source, "error", com=board, round=n, mode="line", detail=str(code))

    def _do_background(source: str, board: str, n: int) -> None:
        tag = source.split(":")[-1]
        marker = f"BG_{tag}_{n}"
        elog.emit(source, "submit", com=board, round=n, mode="background")
        with board_locks[board]:
            sub = ctx.sw.run("cmd", "submit", "--selector", board,
                             "--mode", "background", "--cmd", f"echo {marker}",
                             "--source", source)
        if sub.get("cmd_id"):
            cmd_id = str(sub["cmd_id"])
            elog.emit(source, "submit_accepted", com=board, round=n, mode="background",
                      cmd_id=cmd_id)
            tracker.add(cmd_id, source=source, com=board, round_=n, stop=stop)
        else:
            code = sub.get("error_code") or sub.get("_error")
            elog.emit(source, "error", com=board, round=n, mode="background", detail=str(code))

    def _do_interactive(source: str, board: str, n: int) -> None:
        elog.emit(source, "submit", com=board, round=n, mode="interactive")
        with board_locks[board]:
            resp = ctx.sw.run("session", "interactive-open", "--selector", board,
                              "--owner", source, "--timeout", "10")
            iid = resp.get("interactive_id")
            if resp.get("ok") and iid:
                ctx.sw.run("session", "interactive-close", "--interactive-id", str(iid))
        if resp.get("ok") and iid:
            elog.emit(source, "done", com=board, round=n, mode="interactive",
                      detail="soft_preempted" if resp.get("soft_preempted") else "")
        elif resp.get("error_code") == "SESSION_INTERACTIVE_BUSY":
            elog.emit(source, "busy", com=board, round=n, mode="interactive")  # benign
        else:
            elog.emit(source, "error", com=board, round=n, mode="interactive",
                      detail=str(resp.get("error_code")))

    def agent_worker(i: int) -> None:
        source = f"agent:rhw{i}"
        n = 0
        reason = "stop"
        elog.emit(source, "worker_start")
        next_alive = time.monotonic() + 300.0
        try:
            while not stop.is_set():
                n += 1
                board = boards[(i + n) % len(boards)]  # 輪流對兩板
                try:
                    if n % 10 == 0:        # 每第 10 輪：interactive open/close
                        _do_interactive(source, board, n)
                    elif n % 3 == 0:       # 每第 3 輪：background echo
                        _do_background(source, board, n)
                    else:                  # 其餘：line submit_and_wait
                        _do_line(source, board, n)
                except Exception as exc:   # 單一動作例外＝記事件續跑，不殺整跑
                    elog.emit(source, "worker_error", com=board, round=n, detail=repr(exc))
                if time.monotonic() >= next_alive:
                    elog.emit(source, "worker_alive")
                    next_alive = time.monotonic() + 300.0
                _sleep_stop(stop, random.uniform(0.5, 2.0))
        except Exception as exc:
            reason = "error"
            elog.emit(source, "worker_error", detail=repr(exc))
        finally:
            elog.emit(source, "worker_stop", reason=reason)

    def human_worker() -> None:
        n = 0
        opened = False
        human = None
        reason = "stop"
        elog.emit("human", "worker_start")
        next_alive = time.monotonic() + 300.0
        try:
            while not stop.is_set():
                try:
                    if human is None or not human.alive():
                        if opened:
                            elog.emit("human", "minicom_dead", com="COM0")
                            human.close()
                        human = _human_frontend(ctx, "lrhuman", "minicom")
                        human.open("COM0")
                        opened = True
                        elog.emit("human", "minicom_open", com="COM0")
                        delay = 6.0 if human.kind == "tmux-minicom" else 0.0
                        if _sleep_stop(stop, delay):
                            break
                        continue
                    if _sleep_stop(stop, random.uniform(120, 300)):  # 2-5 分鐘
                        break
                    n += 1
                    human.send_line(f"echo HUMAN_TICK_{n}")
                    elog.emit("human", "tick", com="COM0", n=n)
                    if time.monotonic() >= next_alive:
                        elog.emit("human", "worker_alive")
                        next_alive = time.monotonic() + 300.0
                except Exception as exc:
                    elog.emit("human", "human_error", detail=repr(exc))
                    _sleep_stop(stop, 5)
        except Exception as exc:
            reason = "error"
            elog.emit("human", "worker_error", detail=repr(exc))
        finally:
            if human is not None:
                try:
                    human.close()
                except Exception as exc:
                    elog.emit("human", "human_error", detail=repr(exc))
            elog.emit("human", "worker_stop", reason=reason)

    def snapshot_worker() -> None:
        both_down_since: int | None = None
        last_pid: int | None = None
        failed_probes = 0
        elog.emit("orchestrator", "worker_start")
        next_alive = time.monotonic() + 300.0
        reason = "stop"
        try:
            while not stop.is_set():
                try:
                    try:
                        status = ctx.sw.run("daemon", "status")
                        raw_pid = status.get("pid")
                        if status.get("_error") or status.get("ok") is False:
                            raise RuntimeError(str(status.get("error_code") or
                                                   status.get("_error") or "RPC error"))
                        pid = int(raw_pid) if raw_pid is not None and int(raw_pid) > 0 else None
                        if pid is None:
                            raise ValueError("daemon status 缺少有效 pid")
                        if last_pid is not None and pid != last_pid:
                            elog.emit("orchestrator", "pid_change", old_pid=last_pid, pid=pid)
                        last_pid = pid
                        failed_probes = 0
                        pid_error = None
                    except Exception as exc:
                        pid = None
                        failed_probes += 1
                        pid_error = str(exc)

                    sessions = {b: ctx.sw.session(b).get("state") for b in boards}
                    rss: int | None = None
                    rss_error: str | None = None
                    if pid is not None:
                        rss_value = procobs.rss_kb(pid)
                        if isinstance(rss_value, int) and rss_value > 0:
                            rss = rss_value
                        else:
                            rss_error = "process working-set query returned no value"
                    t = rel_t()
                    snapshot = {"t": t, "wall_ts": datetime.now(timezone.utc).isoformat(),
                                "sessions": sessions, "rss_kb": rss, "pid": pid}
                    if pid_error:
                        snapshot["pid_probe_error"] = pid_error
                    if rss_error:
                        snapshot["rss_probe_error"] = rss_error
                    _append_ndjson(snaps_path, snapshot)

                    if failed_probes >= 3 and last_pid is not None:
                        try:
                            dead = not procobs.pid_alive(last_pid)
                        except Exception:
                            dead = False
                        if dead:
                            if not major:
                                major.update({"kind": "daemon_death", "t": t})
                                elog.emit("orchestrator", "major", detail="daemon_death", t_at=t,
                                          pid=last_pid)
                            stop.set()
                            break

                    both_down = all(sessions.get(b) != "READY" for b in boards)
                    if both_down:
                        if both_down_since is None:
                            both_down_since = t
                        elif t - both_down_since >= MAJOR_BOTH_DOWN_S:
                            if not major:
                                major.update({"kind": "both_boards_stuck", "t": t})
                                elog.emit("orchestrator", "major",
                                          detail="both_boards_stuck", t_at=t)
                            stop.set()
                            break
                    else:
                        both_down_since = None
                    if time.monotonic() >= next_alive:
                        elog.emit("orchestrator", "worker_alive")
                        next_alive = time.monotonic() + 300.0
                    _sleep_stop(stop, snap_interval)
                except Exception as exc:
                    elog.emit("orchestrator", "snapshot_error", detail=repr(exc), t=rel_t())
                    _sleep_stop(stop, snap_interval)
        except Exception as exc:
            reason = "error"
            elog.emit("orchestrator", "worker_error", detail=repr(exc))
        finally:
            elog.emit("orchestrator", "worker_stop", reason=reason)

    # ---- SIGINT → 提前收斂 ----
    def _on_sigint(signum, frame):
        elog.emit("orchestrator", "sigint")
        stop.set()

    prev_handler: Any = None
    try:
        prev_handler = signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        prev_handler = None  # 非主執行緒等情境：仍以 duration 收斂

    elog.emit("orchestrator", "start", duration_s=duration_s, workers=n_workers)

    threads = [threading.Thread(target=snapshot_worker, name="lr-snap", daemon=True),
               threading.Thread(target=human_worker, name="lr-human", daemon=True),
               threading.Thread(target=tracker.run, args=(stop,), name="lr-bg-resolver", daemon=True)]
    threads += [threading.Thread(target=agent_worker, args=(i,), name=f"lr-agent{i}",
                                 daemon=True) for i in range(n_workers)]
    for th in threads:
        th.start()

    deadline = start + duration_s
    worker_join_timeouts: list[str] = []
    bg_remaining = 0
    try:
        while not stop.is_set():
            if duration_s > 0 and time.monotonic() >= deadline:
                break
            stop.wait(timeout=1.0)  # 短輪詢，不 sleep 整段 duration
    finally:
        stop.set()
        worker_join_timeouts = _join_workers(threads, elog)
        resolver = next((th for th in threads if th.name == "lr-bg-resolver"), None)
        if resolver is not None and resolver.is_alive():
            bg_remaining = tracker.pending_count()
            if bg_remaining:
                elog.emit("bg_resolver", "bg_drain", remaining=bg_remaining,
                          reason="resolver_join_timeout")
        else:
            bg_remaining = tracker.final_drain(BG_FINAL_DRAIN_S)
        elog.emit("orchestrator", "stop")
        elog.close()
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    # ---- 收尾：analyze → 報告 → CaseResult ----
    elapsed = rel_t()
    snapshots = _read_ndjson(snaps_path)
    events = _read_ndjson(events_path)
    analysis = analyze(snapshots, events)
    meta = {"version": ctx.cfg.get("_version", ""),
            "git": ctx.cfg.get("_git", ""), "duration_s": duration_s}
    analysis_path.write_text(
        _render_analysis_md(analysis, meta, major, events, elapsed), encoding="utf-8")

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(ctx.report_dir))
        except ValueError:
            return str(p)

    reasons: list[str] = []
    if major:
        reasons.append(f"重大事件：{major.get('kind')}@t={major.get('t')}s")
    if analysis["daemon_death_at"] is not None:
        reasons.append(f"daemon 死亡於 t={analysis['daemon_death_at']}s")
    long_stuck = [s for s in analysis["stuck_attached"] if s["duration_s"] >= FAIL_STUCK_S]
    if long_stuck:
        reasons.append(f"板卡長時間非 READY（≥{FAIL_STUCK_S}s）：{long_stuck}")
    tot_submit = sum(d["submit"] for d in analysis["per_source"].values())
    tot_error = sum(d["error"] for d in analysis["per_source"].values())
    if tot_submit >= 10 and tot_error / max(tot_submit, 1) > 0.5:
        reasons.append(f"命令錯誤率過高：{tot_error}/{tot_submit}")

    monitor_gaps: list[str] = []
    stop_event = next((event for event in events if event.get("kind") == "stop"), None)
    stop_t = stop_event.get("t", float("inf")) if stop_event else float("inf")
    if not snapshots:
        monitor_gaps.append("沒有快照")
    if any(event.get("kind") == "snapshot_error" for event in events):
        monitor_gaps.append("快照輪次失敗")
    if any(snapshot.get("pid_probe_error") or snapshot.get("rss_probe_error")
           for snapshot in snapshots):
        monitor_gaps.append("PID 或 RSS 觀測失敗")
    if any(event.get("kind") == "worker_stop" and event.get("reason") == "error"
           and event.get("t", float("inf")) <= stop_t for event in events):
        monitor_gaps.append("worker 提前結束")
    if worker_join_timeouts or any(event.get("kind") == "worker_join_timeout"
                                   for event in events):
        monitor_gaps.append("worker 未在期限內結束")
    if bg_remaining:
        monitor_gaps.append(f"background 結果未收斂：{bg_remaining}")
    if monitor_gaps:
        reasons.append("長跑觀測證據不完整：" + "；".join(monitor_gaps))

    reason_code = ""
    if analysis["daemon_death_at"] is not None or major.get("kind") == "daemon_death":
        reason_code = "daemon_died"
    elif major.get("kind") == "both_boards_stuck":
        reason_code = "both_boards_stuck"
    elif long_stuck:
        reason_code = "board_stuck"
    elif monitor_gaps:
        reason_code = "monitor_died"
    elif reasons:
        reason_code = "cmd_error_rate_high"

    verdict = "FAIL" if reasons else "PASS"
    evidence = {"analysis": _rel(analysis_path), "events": _rel(events_path),
                "snapshots": _rel(snaps_path)}
    return CaseResult(verdict, reason="；".join(reasons) or "長跑完成、無重大事件",
                      category="test" if reasons else "", reason_code=reason_code,
                      evidence=evidence)
