"""#122 長跑編排（`lr-mixed`）＋事後分析器 `analyze()`。

- `analyze(snapshots, events) -> dict`：**純函式、無 I/O**，把長跑收集的兩條
  ndjson（快照序列＋事件序列）收斂成統計：per_source 命令計數、stuck_attached
  連續非 READY 區段、pid_changes、daemon_death_at、rss_trend。單測釘死行為。
- `lr-mixed` case（tier="longrun"）：4 個 agent worker thread（輪流對兩板送命令，
  mix line／background／interactive）＋1 個 human 模擬 thread（tmux minicom COM0，
  每 2-5 分鐘敲一行）＋1 個 snapshot thread（每 `snapshot_interval_s` 記狀態/RSS/pid），
  跑到 `duration_s` 或 SIGINT 停；重大事件（daemon pid==0／兩板同時非 READY >15min）
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
from pathlib import Path
from typing import Any

from ..harness import Case, CaseResult, register

# 重大事件／FAIL 級門檻（秒）
MAJOR_BOTH_DOWN_S = 15 * 60  # 兩板同時非 READY 持續超過此值＝重大事件
FAIL_STUCK_S = 15 * 60       # 單板連續非 READY ≥此值＝FAIL 級統計
_COUNTED_KINDS = ("submit", "done", "error")


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
        if kind not in _COUNTED_KINDS:
            continue
        src = ev.get("source") or "?"
        bucket = per_source.setdefault(src, {"submit": 0, "done": 0, "error": 0})
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

    # pid 變更與 daemon 死亡
    pid_changes = 0
    daemon_death_at: int | None = None
    prev_pid: int | None = None
    for snap in snapshots:
        pid = snap.get("pid")
        if pid == 0 and daemon_death_at is None:
            daemon_death_at = snap.get("t")
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
def _read_rss_kb(pid: int) -> int:
    """讀 /proc/<pid>/status 的 VmRSS（kB）；pid 無效或讀不到回 0。"""
    if not pid or pid <= 0:
        return 0
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def _append_ndjson(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
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
                               "source": source, "kind": kind}
        rec.update(detail)
        line = json.dumps(rec, ensure_ascii=False)
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


def _tmux_alive(tmux: Any, session: str) -> bool:
    """以 capture 內容判斷 tmux session/minicom 是否還活著（死掉 capture 為空）。"""
    try:
        return bool((tmux.capture(session) or "").strip())
    except Exception:
        return False


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
        "同板多 worker 以 per-board lock 序列化，避免 FOREGROUND_BUSY 噪音；仍為多 source 競爭仲裁",
        "SIGINT 提前收斂＝記 sigint event、停負載，仍產出 longrun-analysis.md",
        "重大事件（daemon pid==0／兩板同時非 READY >15min）只停負載記事件、不自動重啟 daemon（保留現場）",
        "snapshot RSS 讀 /proc/<MainPID>/status VmRSS，MainPID 來自 systemctl show",
        "interactive 輪撞到 human raw owner 回 SESSION_INTERACTIVE_BUSY＝benign（不計 error）",
    ),
    requires=("tmux", "two_boards"),
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

    def rel_t() -> int:
        return int(time.monotonic() - start)

    # ---- agent worker 動作 ----
    def _do_line(source: str, board: str, n: int) -> None:
        tag = source.split(":")[-1]
        marker = f"LR_{tag}_{n}_{random.randint(1000, 9999)}"
        elog.emit(source, "submit", com=board, round=n, mode="line")
        with board_locks[board]:
            cmd = ctx.sw.submit_and_wait(board, f"echo {marker}", cmd_timeout=10.0)
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
                             "--mode", "background", "--cmd", f"echo {marker}")
        if sub.get("cmd_id"):
            elog.emit(source, "done", com=board, round=n, mode="background")
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
            _sleep_stop(stop, random.uniform(0.5, 2.0))

    def human_worker() -> None:
        ses = ctx.tmux.name("lrhuman")
        n = 0
        opened = False
        try:
            while not stop.is_set():
                try:
                    if not _tmux_alive(ctx.tmux, ses):
                        if opened:
                            elog.emit("human", "minicom_dead", com="COM0")
                        ctx.tmux.kill(ses)  # 清可能的殘留
                        ctx.tmux.new(ses, "serialwrap-minicom COM0")
                        opened = True
                        elog.emit("human", "minicom_open", com="COM0")
                        if _sleep_stop(stop, 6):  # 等 console-attach＋minicom 起來
                            break
                        continue
                    if _sleep_stop(stop, random.uniform(120, 300)):  # 2-5 分鐘
                        break
                    n += 1
                    ctx.tmux.send(ses, f"echo HUMAN_TICK_{n}")
                    elog.emit("human", "tick", com="COM0", n=n)
                except Exception as exc:
                    elog.emit("human", "human_error", detail=repr(exc))
                    _sleep_stop(stop, 5)
        finally:
            ctx.tmux.kill(ses)

    def snapshot_worker() -> None:
        both_down_since: int | None = None
        while not stop.is_set():
            try:
                pid = ctx.systemd.main_pid()
                sessions = {b: ctx.sw.session(b).get("state") for b in boards}
                rss = _read_rss_kb(pid)
                t = rel_t()
                _append_ndjson(snaps_path, {"t": t, "sessions": sessions,
                                            "rss_kb": rss, "pid": pid})
                # 重大事件偵測
                if pid == 0:
                    if not major:
                        major.update({"kind": "daemon_death", "t": t})
                        elog.emit("orchestrator", "major", detail="daemon_death", t_at=t)
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
            except Exception as exc:
                elog.emit("orchestrator", "snapshot_error", detail=repr(exc))
            _sleep_stop(stop, snap_interval)

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
               threading.Thread(target=human_worker, name="lr-human", daemon=True)]
    threads += [threading.Thread(target=agent_worker, args=(i,), name=f"lr-agent{i}",
                                 daemon=True) for i in range(n_workers)]
    for th in threads:
        th.start()

    deadline = start + duration_s
    try:
        while not stop.is_set():
            if duration_s > 0 and time.monotonic() >= deadline:
                break
            stop.wait(timeout=1.0)  # 短輪詢，不 sleep 整段 duration
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=30)
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

    reason_code = ""
    if analysis["daemon_death_at"] is not None or major.get("kind") == "daemon_death":
        reason_code = "daemon_died"
    elif major.get("kind") == "both_boards_stuck":
        reason_code = "both_boards_stuck"
    elif long_stuck:
        reason_code = "board_stuck"
    elif reasons:
        reason_code = "cmd_error_rate_high"

    verdict = "FAIL" if reasons else "PASS"
    evidence = {"analysis": _rel(analysis_path), "events": _rel(events_path),
                "snapshots": _rel(snaps_path)}
    return CaseResult(verdict, reason="；".join(reasons) or "長跑完成、無重大事件",
                      category="test" if reasons else "", reason_code=reason_code,
                      evidence=evidence)
