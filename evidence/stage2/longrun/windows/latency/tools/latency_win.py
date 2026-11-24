#!/usr/bin/env python3
"""離線計算兩次 Windows lr-mixed 長跑（0.4.1 基線、0.5.0-rc.1 候選）的延遲、服務時間、吞吐、錯誤與資源統計。

只讀 run 目錄內既有檔案；所有路徑都由命令列提供，輸出 JSON 不寫入任何輸入路徑，也不寫入 WAL 內容。
用法：
  python3 -I latency_win.py \\
      --baseline <基線 run 目錄> --baseline-name <基線目錄名稱> \\
      --candidate <候選 run 目錄> --candidate-name <候選目錄名稱> \\
      --cutoff <ISO 8601 截斷時間> \\
      [--od110-gate <release-gate-od110.json>] [--od110-receipt <truncation-receipt.json>] \\
      --out <輸出 JSON>

run 目錄內讀取的相對路徑：lr-mixed/events.ndjson、lr-mixed/snapshots.ndjson、observer.ndjson、
report.json、release-gate.json、excl/probes.ndjson、wal/raw.wal.ndjson*、wal/raw.mirror.log（只算雜湊）、
code/realhw/ 底下的引擎檔（只算雜湊）。
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

DAY_S = 86400.0
PCTS = (50, 90, 99, 99.9)
LINE_SETTLE_S = 1.5          # realhw/drivers.py submit_and_wait 的 settle_s 預設值
CLOCK_STEP_MIN_S = 0.1       # observer 相鄰樣本 (wall_ts − t_mono) 的變化超過此值，視為系統牆鐘跳變
WAL_SCAN_LIMIT_S = 10.0      # 從 WAL TX 往後找 RX 的最長範圍（牆鐘秒數）
PROMPT_TAIL = b"$ "          # shell prompt 結尾；只用來定位，不輸出任何 RX 內容
RSS_COMMON_FROM_S = 14400.0  # 共同比較窗起點：基線 WorkingSet 的每小時中位數在第 3、4 小時一次性下降（hourly_median_kb），4 h 後兩邊都已穩定
ENGINE_FILES = ("realhw/cases/longrun.py", "realhw/drivers.py", "realhw/harness.py",
                "realhw/procobs.py", "realhw/frontends.py")
EXPECTED_REJECT_KINDS = {"busy"}
EXPECTED_REJECT_CODES = {"SESSION_INTERACTIVE_BUSY"}
EXPECTED_REJECT_BY_MODE = {"interactive": {"SESSION_BUSY"}}
ITEMS_GROUP_MAX = 20         # 同一 (mode, detail) 超過此筆數時只給計數與首末時間，不逐筆列出
BG_MISC_KINDS = ("bg_lost", "bg_slow", "bg_poll_error", "bg_drain", "bg_backpressure")


# ---------------------------------------------------------------- 共用工具
def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nearest_rank(sorted_vals: list[float], p: float) -> float:
    k = max(1, math.ceil(p / 100.0 * len(sorted_vals)))
    return sorted_vals[k - 1]


def dist(vals: list[float]) -> dict | None:
    if not vals:
        return None
    v = sorted(vals)
    out = {"n": len(v), "min": round(v[0], 4)}
    for p in PCTS:
        key = "p" + (str(p).replace(".", "_") if p != int(p) else str(int(p)))
        out[key] = round(nearest_rank(v, p), 4)
    out["max"] = round(v[-1], 4)
    out["mean"] = round(statistics.fmean(v), 4)
    out["n_below_p99_9_resolution"] = len(v) < 1000  # p99.9 需 ≥1000 筆才不等於 max 附近的單點
    return out


def load_ndjson(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            if line.strip():
                rec = json.loads(line)
                rec["_ln"] = ln
                out.append(rec)
    return out


def is_expected_reject(ev: dict) -> bool:
    if ev["kind"] in EXPECTED_REJECT_KINDS:
        return True
    if ev["kind"] == "error":
        code = ev.get("error_code") or ev.get("detail")
        if code in EXPECTED_REJECT_CODES:
            return True
        if code in EXPECTED_REJECT_BY_MODE.get(ev.get("mode"), set()):
            return True
    return False


def slope_kb_per_h(rows: list[dict]) -> float | None:
    if len(rows) < 2:
        return None
    xs = [float(r["t"]) / 3600.0 for r in rows]
    ys = [float(r["rss_kb"]) for r in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 0 else 0.0


# ---------------------------------------------------------------- 牆鐘跳變
def observer_clock_steps(samples: list[dict]) -> list[dict]:
    """observer 每筆同時有 wall_ts 與 t_mono；兩者差值突變即系統牆鐘被調整。"""
    steps, prev = [], None
    for o in samples:
        off = ts(o["wall_ts"]) - float(o["t_mono"])
        if prev is not None and abs(off - prev[1]) > CLOCK_STEP_MIN_S:
            steps.append({"before_wall_ts": prev[0], "after_wall_ts": o["wall_ts"],
                          "offset_change_s": round(off - prev[1], 4)})
        prev = (o["wall_ts"], off)
    return steps


def wal_offset_changes(wal: list[dict]) -> tuple[list[dict], list[dict]]:
    """WAL 每筆有 wall_ts 與 mono_ts_ns。差值持續改變＝牆鐘跳變；只有單筆偏離、下一筆即回復＝該筆兩個時戳不是同一刻取的。"""
    offs = [r["_ts"] - r["_mono"] for r in wal]
    steps, spikes = [], []
    k = 1
    while k < len(offs):
        if abs(offs[k] - offs[k - 1]) > CLOCK_STEP_MIN_S:
            if k + 1 < len(offs) and abs(offs[k + 1] - offs[k - 1]) <= CLOCK_STEP_MIN_S:
                spikes.append({"wall_ts": wal[k]["wall_ts"], "dir": wal[k]["dir"], "source": wal[k]["src"],
                               "wall_minus_mono_excess_s": round(offs[k] - offs[k - 1], 4)})
                k += 2
                continue
            steps.append({"before_wall_ts": wal[k - 1]["wall_ts"], "after_wall_ts": wal[k]["wall_ts"],
                          "offset_change_s": round(offs[k] - offs[k - 1], 4)})
        k += 1
    return steps, spikes


def min_consecutive_diff(vals: list[float]) -> float | None:
    """依紀錄順序相鄰兩筆的最小正差值（觀察時戳解析度用）。"""
    d = [b - a for a, b in zip(vals, vals[1:]) if b - a > 0]
    return round(min(d), 6) if d else None


def share_near_tick(vals: list[float], tick: float = 1 / 64, tol: float = 0.0015) -> float | None:
    """落在 tick 整數倍 ±tol 內的比例；均勻分佈時約 2·tol/tick（≈0.19）。"""
    if not vals:
        return None
    near = sum(1 for v in vals if min(v % tick, tick - v % tick) <= tol)
    return round(near / len(vals), 4)


def mark_zones(records: list[dict], steps: list[dict]) -> list[dict]:
    """依檔案（真實時間）順序標記每筆紀錄的牆鐘 epoch 與是否落在跳變模糊區。

    跳變幅度 |D| 發生在 observer 兩個樣本之間（牆鐘 A、B）。牆鐘 < A−|D| 的紀錄必在跳變前，
    > B+|D| 的必在跳變後；中間（依檔案順序）一律標為模糊區 `_z`。模糊區之後 epoch 加 1。
    """
    times = [r["_ts"] for r in records]
    zones = []
    for st in steps:
        mag = abs(st["offset_change_s"])
        lo, hi = ts(st["before_wall_ts"]) - mag, ts(st["after_wall_ts"]) + mag
        i_lo = next((i for i, t in enumerate(times) if t >= lo), len(times))
        i_hi = next((i for i in range(len(times) - 1, -1, -1) if times[i] <= hi), -1)
        i_hi = max(i_hi, i_lo - 1)
        zones.append((i_lo, i_hi))
    for i, r in enumerate(records):
        r["_z"] = any(a <= i <= b for a, b in zones)
        r["_ep"] = sum(1 for _, b in zones if i > b)
    out = []
    for (a, b), st in zip(zones, steps):
        if a < len(records):
            out.append({"step": st, "records_in_zone": max(0, b - a + 1),
                        "zone_first_wall_ts": records[a]["wall_ts"] if b >= a else None,
                        "zone_last_wall_ts": records[b]["wall_ts"] if b >= a else None})
    return out


def same_epoch(*recs: dict) -> bool:
    return all(not r["_z"] for r in recs) and len({r["_ep"] for r in recs}) == 1


# ---------------------------------------------------------------- 載入 run
def load_run(run_dir: Path, name: str) -> dict:
    p = {
        "events": run_dir / "lr-mixed/events.ndjson",
        "snapshots": run_dir / "lr-mixed/snapshots.ndjson",
        "observer": run_dir / "observer.ndjson",
        "report": run_dir / "report.json",
        "gate": run_dir / "release-gate.json",
        "probes": run_dir / "excl/probes.ndjson",
    }
    wal_paths = sorted((run_dir / "wal").glob("raw.wal.ndjson*"))
    mirror = run_dir / "wal/raw.mirror.log"

    events = load_ndjson(p["events"])
    for i, e in enumerate(events):
        e["_ts"] = ts(e["wall_ts"])
        e["_i"] = i
    obs_all = load_ndjson(p["observer"])
    obs = [o for o in obs_all if o.get("kind") == "sample"]
    for o in obs:
        o["_ts"] = ts(o["wall_ts"])
    snaps = load_ndjson(p["snapshots"])
    for s in snaps:
        s["_ts"] = ts(s["wall_ts"])
    probes = load_ndjson(p["probes"])
    for q in probes:
        q["_ts"] = ts(q["wall_ts"])
    report = json.load(open(p["report"], encoding="utf-8"))
    gate = json.load(open(p["gate"], encoding="utf-8"))

    wal = []
    for wp in wal_paths:
        for r in load_ndjson(wp):
            wal.append({"seq": r["seq"], "dir": r["dir"], "src": r["source"].split(":")[0],
                        "wall_ts": r["wall_ts"], "_ts": ts(r["wall_ts"]),
                        "_mono": int(r["mono_ts_ns"]) / 1e9, "loss": bool(r.get("loss_flag")),
                        "pl": base64.b64decode(r["payload_b64"])})
    wal.sort(key=lambda r: r["seq"])

    obs_steps = observer_clock_steps(obs)
    ev_zones = mark_zones(events, obs_steps)
    wal_zones = mark_zones(wal, obs_steps)

    inputs = {"events.ndjson": sha256(p["events"]), "snapshots.ndjson": sha256(p["snapshots"]),
              "observer.ndjson": sha256(p["observer"]), "report.json": sha256(p["report"]),
              "release-gate.json": sha256(p["gate"]), "probes.ndjson": sha256(p["probes"]),
              "wal": {wp.name: sha256(wp) for wp in wal_paths}}
    if mirror.exists():
        inputs["wal"]["raw.mirror.log"] = sha256(mirror)
    gh = gate.get("evidence_hashes") or {}
    match = {k: inputs[k] == gh.get(k) for k in ("events.ndjson", "snapshots.ndjson", "observer.ndjson", "report.json")}
    match["wal"] = inputs["wal"] == gh.get("wal")
    engine = {}
    for rel in ENGINE_FILES:
        fp = run_dir / "code" / rel
        engine[rel] = sha256(fp) if fp.is_file() else None

    lr = next(r for r in report["results"] if r["id"] == "lr-mixed")
    start = next(e for e in events if e["kind"] == "start")
    stop = next((e for e in events if e["kind"] == "stop"), None)
    return {
        "name": name, "events": events, "obs": obs, "snaps": snaps, "probes": probes, "wal": wal,
        "report": report, "gate": gate, "lr": lr, "start": start, "stop": stop,
        "obs_steps": obs_steps, "wal_offsets": wal_offset_changes(wal),
        "ev_zones": ev_zones, "wal_zones": wal_zones,
        "inputs": inputs, "inputs_match_gate": match, "engine": engine,
    }


# ---------------------------------------------------------------- WAL 掃描
class WalIndex:
    def __init__(self, wal: list[dict]) -> None:
        self.wal = wal
        self.lr_tx: dict[str, list[int]] = collections.defaultdict(list)
        self.bg_tx: dict[str, list[int]] = collections.defaultdict(list)
        self.human_tx: dict[int, int] = {}
        for i, r in enumerate(wal):
            if r["dir"] != "TX":
                continue
            s = r["pl"].decode("utf-8", "replace").strip()
            if r["src"] == "agent" and s.startswith("echo LR_"):
                mk = s[5:]
                self.lr_tx[mk.rsplit("_", 1)[0] + "_"].append(i)
            elif r["src"] == "agent" and s.startswith("echo BG_"):
                self.bg_tx[s[5:]].append(i)
            elif r["src"] == "human" and "HUMAN_TICK_" in s:
                try:
                    n = int(s.split("HUMAN_TICK_")[1].strip().split()[0])
                except (IndexError, ValueError):
                    continue
                self.human_tx.setdefault(n, i)

    def token_of(self, i_tx: int) -> bytes:
        s = self.wal[i_tx]["pl"].decode("utf-8", "replace").strip()
        return (s.split("echo ", 1)[1] + "\r\n").encode()

    def scan(self, i_tx: int, token: bytes) -> tuple[int | None, int | None]:
        """從 TX 往後（seq 順序）串接 RX：token 第 2 次出現（第 1 次是 shell 回顯的命令行）為輸出，
        其後的 prompt 結尾為命令結束。遇到牆鐘跳變模糊區或 epoch 改變即停止（視為找不到）。

        基線（Python daemon）的 WAL mono_ts_ns 解析度只有約 15.6 ms，所以 WAL 內部區間一律用 wall_ts。"""
        tx = self.wal[i_tx]
        t0, ep = tx["_ts"], tx["_ep"]
        buf, pos, seen, out_j = b"", 0, 0, None
        j = i_tx + 1
        while j < len(self.wal) and self.wal[j]["_ts"] - t0 <= WAL_SCAN_LIMIT_S:
            r = self.wal[j]
            if r["_z"] or r["_ep"] != ep:
                return out_j, None
            if r["dir"] == "RX":
                buf += r["pl"]
                if out_j is None:
                    while True:
                        k = buf.find(token, pos)
                        if k < 0:
                            break
                        seen += 1
                        pos = k + len(token)
                        if seen == 2:
                            out_j = j
                            break
                if out_j is not None and buf.find(PROMPT_TAIL, pos) >= 0:
                    return out_j, j
            j += 1
        return out_j, None


# ---------------------------------------------------------------- 單一區段分析
def analyze_segment(R: dict, wend: float | None, wend_label: str, gate_for_xcheck: dict | None,
                    wix: WalIndex) -> dict:
    events = R["events"]
    t0 = R["start"]["_ts"]
    end_ts = wend if wend is not None else R["stop"]["_ts"]
    span_s = end_ts - t0

    def inseg(r: dict) -> bool:
        return wend is None or r["_ts"] < wend

    anomalies = collections.Counter()
    submits: dict[tuple, dict] = {}
    terms: dict[tuple, list[dict]] = collections.defaultdict(list)
    later_terms: dict[tuple, list[dict]] = collections.defaultdict(list)
    accepted: dict[tuple, dict] = {}
    later_acc: dict[tuple, dict] = {}
    bg_by_id: dict[str, dict] = {}
    later_bg: dict[str, dict] = {}
    worker_errors = []
    misc = collections.Counter()
    for e in events:
        k = e["kind"]
        s = inseg(e)
        key = (e.get("source"), e.get("round"))
        if k == "submit":
            if not s:
                continue
            if key in submits:
                anomalies["duplicate_submit_key"] += 1
            submits[key] = e
        elif k in ("done", "error", "busy"):
            (terms if s else later_terms)[key].append(e)
        elif k == "submit_accepted":
            if s:
                if key in accepted:
                    anomalies["duplicate_submit_accepted_key"] += 1
                accepted[key] = e
            else:
                later_acc[key] = e
        elif k == "bg_result":
            if s:
                if e["cmd_id"] in bg_by_id:
                    anomalies["duplicate_bg_result_cmd_id"] += 1
                bg_by_id[e["cmd_id"]] = e
            else:
                later_bg[e["cmd_id"]] = e
        elif k == "worker_error" and s:
            worker_errors.append(e)
        elif k in BG_MISC_KINDS and s:
            misc[k] += 1

    lat = collections.defaultdict(lambda: collections.defaultdict(list))
    negative = collections.Counter()
    step_excluded = collections.Counter()
    completed = collections.Counter()
    completed_by_mode = collections.defaultdict(collections.Counter)
    submitted = collections.Counter()
    submitted_by_mode = collections.defaultdict(collections.Counter)
    rejects = collections.defaultdict(collections.Counter)
    unexpected: list[dict] = []
    cut: list[dict] = []
    drift = []
    chain = collections.defaultdict(list)   # com -> [(file index of lock-release proxy, end_ev, sub_ev, mode, tag, key)]
    line_done_lat = []

    def add(groups: list[str], metric: str, value: float) -> None:
        if value < 0:
            negative[metric] += 1
        for g in groups:
            lat[g][metric].append(value)

    def later_outcome(key: tuple, mode: str) -> str:
        if mode == "background":
            acc = accepted.get(key) or later_acc.get(key)
            if acc is not None:
                bg = later_bg.get(acc["cmd_id"]) or bg_by_id.get(acc["cmd_id"])
                return f"bg_result:{bg.get('status')}" if bg else "none"
        lt = later_terms.get(key) or []
        if lt:
            t = lt[0]
            return t["kind"] + (f":{t.get('detail')}" if t.get("detail") else "")
        return "none"

    for key, sub in submits.items():
        source, rnd = key
        com, mode = sub["com"], sub["mode"]
        cell = f"{com}|{source}"
        groups = ["*", cell]
        submitted[cell] += 1
        submitted_by_mode[cell][mode] += 1
        t_sub = sub["_ts"]
        tl = terms.get(key, [])
        if mode in ("line", "interactive"):
            if not tl:
                if later_terms.get(key):
                    cut.append({"mode": mode, "com": com, "source": source, "round": rnd,
                                "submit_wall_ts": sub["wall_ts"], "outcome_after_end": later_outcome(key, mode)})
                else:
                    anomalies[f"{mode}_terminal_count_0"] += 1
                    unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                       "round": rnd, "submit_wall_ts": sub["wall_ts"]})
                continue
            if len(tl) != 1:
                anomalies[f"{mode}_terminal_count_{len(tl)}"] += 1
            term = tl[0]
            ok = same_epoch(sub, term)
            d = term["_ts"] - t_sub
            if not ok:
                step_excluded[mode] += 1
            if term["kind"] == "done":
                completed[cell] += 1
                completed_by_mode[cell][mode] += 1
                tag = "done"
                if ok:
                    add([f"{g}|{mode}" for g in groups], "submit_to_done_s", d)
                    add([f"{g}|all_modes" for g in groups], "submit_to_completion_s", d)
                    if mode == "line":
                        line_done_lat.append((d, sub["wall_ts"]))
                if mode == "interactive" and term.get("detail") == "soft_preempted":
                    completed_by_mode[cell]["interactive_soft_preempted"] += 1
            elif is_expected_reject(term):
                tag = "busy" if term["kind"] == "busy" else f"error:{term.get('detail')}"
                rejects[cell][f"{mode}:{tag}"] += 1
                if ok:
                    add([f"{g}|{mode}" for g in groups], "submit_to_reject_s", d)
            else:
                tag = f"error:{term.get('detail')}"
                unexpected.append({"kind": "error", "mode": mode, "com": com, "source": source,
                                   "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                   "terminal_wall_ts": term["wall_ts"], "detail": term.get("detail"),
                                   "submit_to_terminal_s": round(d, 4) if ok else None})
            chain[com].append((term["_i"], term, sub, mode, tag, key))
        elif mode == "background":
            acc = accepted.get(key)
            if acc is None:
                if len(tl) == 1 and tl[0]["kind"] == "error":
                    term = tl[0]
                    ok = same_epoch(sub, term)
                    if is_expected_reject(term):
                        tag = f"error:{term.get('detail')}"
                        rejects[cell][f"background:{tag}"] += 1
                    else:
                        tag = f"error:{term.get('detail')}"
                        unexpected.append({"kind": "error", "mode": mode, "com": com, "source": source,
                                           "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                           "terminal_wall_ts": term["wall_ts"], "detail": term.get("detail"),
                                           "submit_to_terminal_s": round(term["_ts"] - t_sub, 4) if ok else None})
                    chain[com].append((term["_i"], term, sub, mode, tag, key))
                elif later_acc.get(key) or later_terms.get(key):
                    cut.append({"mode": mode, "com": com, "source": source, "round": rnd,
                                "submit_wall_ts": sub["wall_ts"], "outcome_after_end": later_outcome(key, mode)})
                else:
                    anomalies["background_no_accept"] += 1
                    unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                       "round": rnd, "submit_wall_ts": sub["wall_ts"]})
                continue
            if tl:
                anomalies["background_extra_terminal"] += 1
            chain[com].append((acc["_i"], acc, sub, mode, "accepted", key))
            if same_epoch(sub, acc):
                add([f"{g}|background" for g in groups], "submit_to_accepted_s", acc["_ts"] - t_sub)
            else:
                step_excluded["background_submit_to_accepted"] += 1
            bg = bg_by_id.get(acc["cmd_id"])
            if bg is None:
                if acc["cmd_id"] in later_bg:
                    cut.append({"mode": mode, "com": com, "source": source, "round": rnd,
                                "submit_wall_ts": sub["wall_ts"], "accepted_wall_ts": acc["wall_ts"],
                                "outcome_after_end": later_outcome(key, mode)})
                else:
                    anomalies["background_no_bg_result"] += 1
                    unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                       "round": rnd, "submit_wall_ts": sub["wall_ts"]})
                continue
            if (bg["source"], bg["round"], bg["com"]) != (source, rnd, com):
                anomalies["bg_result_key_mismatch"] += 1
            ok = same_epoch(sub, acc, bg)
            d_acc_bg = bg["_ts"] - acc["_ts"]
            d_sub_bg = bg["_ts"] - t_sub
            if ok:
                drift.append(abs(d_acc_bg - float(bg["latency_s"])))
            else:
                step_excluded["background_bg_result"] += 1
            if bg.get("status") == "done":
                completed[cell] += 1
                completed_by_mode[cell]["background"] += 1
                # latency_s 由 harness 以 monotonic 時鐘計算，不受牆鐘跳變影響
                add([f"{g}|background" for g in groups], "bg_result_latency_s_field", float(bg["latency_s"]))
                if ok:
                    add([f"{g}|background" for g in groups], "accepted_to_bg_result_wall_s", d_acc_bg)
                    add([f"{g}|background" for g in groups], "submit_to_bg_result_s", d_sub_bg)
                    add([f"{g}|all_modes" for g in groups], "submit_to_completion_s", d_sub_bg)
            else:
                unexpected.append({"kind": "bg_result", "mode": mode, "com": com, "source": source,
                                   "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                   "terminal_wall_ts": bg["wall_ts"],
                                   "detail": f"status={bg.get('status')} error_code={bg.get('error_code')}",
                                   "submit_to_terminal_s": round(d_sub_bg, 4) if ok else None})
        else:
            anomalies[f"unknown_mode_{mode}"] += 1

    for we in worker_errors:
        unexpected.append({"kind": "worker_error", "source": we.get("source"), "com": we.get("com"),
                           "round": we.get("round"), "terminal_wall_ts": we.get("wall_ts"),
                           "detail": we.get("detail")})
    unexpected.sort(key=lambda u: (u.get("terminal_wall_ts") or u.get("submit_wall_ts") or "", u.get("source") or ""))

    # ---- 板鎖排隊估計：同一 com 的動作由 per-board lock 序列化；終態（或 submit_accepted）在釋放鎖後立即寫入。
    #      依檔案順序走訪，推定取得鎖時間 acq = max(submit, 前一動作的終態時間)。
    svc = collections.defaultdict(list)
    waits = collections.defaultdict(list)
    svc_excluded = 0
    busy_sum = 0.0
    acq_by_key: dict[tuple, float] = {}
    for com, items in chain.items():
        items.sort(key=lambda x: x[0])
        prev: tuple = ("start", None)
        for _, end_ev, sub_ev, mode, tag, key in items:
            if end_ev["_z"]:
                svc_excluded += 1
                prev = ("unknown", None)
                continue
            if not same_epoch(sub_ev, end_ev) or prev[0] == "unknown" or \
                    (prev[0] == "ok" and prev[1]["_ep"] != end_ev["_ep"]):
                svc_excluded += 1
                prev = ("ok", end_ev)
                continue
            acq = sub_ev["_ts"] if prev[0] == "start" else max(sub_ev["_ts"], prev[1]["_ts"])
            s_ = end_ev["_ts"] - acq
            svc[f"{mode}|{tag}"].append(s_)
            waits["*"].append(acq - sub_ev["_ts"])
            waits[f"{mode}|{tag}"].append(acq - sub_ev["_ts"])
            busy_sum += s_
            acq_by_key[key] = acq
            prev = ("ok", end_ev)
    service = {k: dist(v) for k, v in sorted(svc.items())}
    service["line|done_minus_settle_1.5s"] = dist([x - LINE_SETTLE_S for x in svc.get("line|done", [])])

    # ---- WAL 拆解（events 與 WAL 用同一個系統牆鐘；區間兩端都須在同一 epoch、不在模糊區）
    wal = wix.wal
    line_dec = collections.defaultdict(list)
    line_dec_miss = collections.Counter()
    line_acq_tx_top: list[tuple[float, str]] = []
    bg_acq_tx_top: list[tuple[float, str]] = []

    def top3(rows: list[tuple[float, str]]) -> list[dict]:
        return [{"wal_tx_wall_ts": w, "value_s": round(v, 4)} for v, w in sorted(rows, reverse=True)[:3]]
    for key, sub in submits.items():
        if sub["mode"] != "line" or key not in acq_by_key:
            continue
        tl = terms.get(key) or []
        if not tl or tl[0]["kind"] != "done":
            continue
        tag = key[0].split(":")[-1]
        cands = [i for i in wix.lr_tx.get(f"LR_{tag}_{key[1]}_", []) if not wal[i]["_z"] and wal[i]["_ep"] == sub["_ep"]]
        if not cands:
            line_dec_miss["done_without_wal_tx"] += 1
            continue
        if len(cands) > 1:
            line_dec_miss["multiple_wal_tx"] += 1
        acq = acq_by_key[key]
        i_tx = min(cands, key=lambda i: abs(wal[i]["_ts"] - acq))
        line_dec["lock_acq_to_wal_tx_s"].append(wal[i_tx]["_ts"] - acq)
        line_acq_tx_top.append((wal[i_tx]["_ts"] - acq, wal[i_tx]["wall_ts"]))
        j_out, j_pr = wix.scan(i_tx, wix.token_of(i_tx))
        if j_out is None:
            line_dec_miss["output_not_found"] += 1
            continue
        line_dec["wal_tx_to_output_rx_s"].append(wal[j_out]["_ts"] - wal[i_tx]["_ts"])
        if wal[j_out]["_ts"] - wal[i_tx]["_ts"] < 0.002:
            line_dec_miss["output_rx_within_2ms_of_tx"] += 1
        if j_pr is None:
            line_dec_miss["prompt_not_found"] += 1
            continue
        line_dec["wal_tx_to_prompt_rx_s"].append(wal[j_pr]["_ts"] - wal[i_tx]["_ts"])
    bg_dec = collections.defaultdict(list)
    bg_dec_miss = collections.Counter()
    for key, acc in accepted.items():
        if key not in acq_by_key or key not in submits:
            continue
        tag = key[0].split(":")[-1]
        cands = [i for i in wix.bg_tx.get(f"BG_{tag}_{key[1]}", []) if not wal[i]["_z"] and wal[i]["_ep"] == acc["_ep"]]
        if not cands:
            bg_dec_miss["accepted_without_wal_tx"] += 1
            continue
        i_tx = cands[0]
        bg_dec["lock_acq_to_wal_tx_s"].append(wal[i_tx]["_ts"] - acq_by_key[key])
        bg_acq_tx_top.append((wal[i_tx]["_ts"] - acq_by_key[key], wal[i_tx]["wall_ts"]))
        bg_dec["wal_tx_to_submit_accepted_event_s"].append(acc["_ts"] - wal[i_tx]["_ts"])
    tx2acc = bg_dec.get("wal_tx_to_submit_accepted_event_s", [])
    human = collections.defaultdict(list)
    human_miss = []
    ticks = [e for e in events if e["kind"] == "tick" and inseg(e)]
    for e in ticks:
        i_tx = wix.human_tx.get(e["n"])
        if i_tx is None or e["_z"] or wal[i_tx]["_z"] or wal[i_tx]["_ep"] != e["_ep"]:
            human_miss.append({"n": e["n"], "wall_ts": e["wall_ts"],
                               "reason": "no_wal_tx" if i_tx is None else "clock_step_zone"})
            continue
        human["tick_event_to_wal_tx_s"].append(wal[i_tx]["_ts"] - e["_ts"])
        j_out, j_pr = wix.scan(i_tx, f"HUMAN_TICK_{e['n']}\r\n".encode())
        if j_out is not None:
            human["wal_tx_to_output_rx_s"].append(wal[j_out]["_ts"] - wal[i_tx]["_ts"])
        if j_pr is not None:
            human["wal_tx_to_prompt_rx_s"].append(wal[j_pr]["_ts"] - wal[i_tx]["_ts"])

    # WAL 區段內紀錄（依 seq 順序，以牆鐘 [start, end) 取首末索引）
    i0 = next((i for i, r in enumerate(wal) if r["_ts"] >= t0), len(wal))
    i1 = next((i for i in range(len(wal) - 1, -1, -1) if wal[i]["_ts"] < end_ts or (wend is None and wal[i]["_ts"] <= end_ts)), -1)
    seg_wal = wal[i0:i1 + 1]
    gaps = sorted(((b["_ts"] - a["_ts"], b["wall_ts"], f"{a['dir']}:{a['src']}", f"{b['dir']}:{b['src']}")
                   for a, b in zip(seg_wal, seg_wal[1:]) if same_epoch(a, b)), reverse=True)[:5]
    wal_out = {
        "records_in_segment": len(seg_wal),
        "records_by_dir_source": dict(sorted(collections.Counter(f"{r['dir']}:{r['src']}" for r in seg_wal).items())),
        "loss_flag_records": sum(1 for r in seg_wal if r["loss"]),
        "largest_gaps_wall_s": [{"gap_s": round(g, 3), "next_record_wall_ts": w, "prev_record": pa, "next_record": nb}
                                for g, w, pa, nb in gaps],
    }

    # ---- 吞吐
    gate_counts = (gate_for_xcheck or {}).get("counts") or {}
    cells = sorted(submitted)
    unexp_by_cell = collections.Counter(f"{u.get('com')}|{u.get('source')}" for u in unexpected)
    throughput = {}
    for cell in cells:
        g = gate_counts.get(cell, {})
        throughput[cell] = {
            "expected_rejects": sum(rejects[cell].values()), "gate_expected_rejects": g.get("expected_rejects"),
            "unexpected": unexp_by_cell[cell], "gate_unexpected_errors": g.get("unexpected_errors"),
            "gate_unresolved": g.get("unresolved"),
            "submitted": submitted[cell], "submitted_by_mode": dict(sorted(submitted_by_mode[cell].items())),
            "completed": completed[cell], "completed_by_mode": dict(sorted(completed_by_mode[cell].items())),
            "completed_per_h": round(completed[cell] / (span_s / 3600.0), 3),
            "submitted_per_h": round(submitted[cell] / (span_s / 3600.0), 3),
            "gate_completed": g.get("completed"), "gate_effective_hour": g.get("effective_hour"),
            "gate_throughput_per_h": g.get("throughput_per_h"),
        }
    tot_c, tot_s = sum(completed.values()), sum(submitted.values())
    cbm, sbm = collections.Counter(), collections.Counter()
    for cell in cells:
        cbm.update(completed_by_mode[cell])
        sbm.update(submitted_by_mode[cell])
    throughput["*"] = {"submitted": tot_s, "completed": tot_c,
                       "submitted_by_mode": dict(sorted(sbm.items())), "completed_by_mode": dict(sorted(cbm.items())),
                       "completed_per_h_by_mode": {m: round(cbm[m] / (span_s / 3600.0), 3)
                                                   for m in ("line", "interactive", "background")},
                       "submitted_per_h_by_mode": {m: round(sbm[m] / (span_s / 3600.0), 3)
                                                   for m in ("line", "interactive", "background")},
                       "completed_per_h": round(tot_c / (span_s / 3600.0), 3),
                       "submitted_per_h": round(tot_s / (span_s / 3600.0), 3),
                       "gate_completed": gate_counts.get("*", {}).get("completed"),
                       "gate_effective_hour": gate_counts.get("*", {}).get("effective_hour")}

    full_hours = int(span_s // 3600)
    hourly = collections.defaultdict(lambda: [0] * full_hours)
    for e in events:
        if not inseg(e):
            continue
        if e["kind"] == "done" or (e["kind"] == "bg_result" and e.get("status") == "done"):
            h = int((e["_ts"] - t0) // 3600)
            if 0 <= h < full_hours:
                hourly[f"{e['com']}|{e['source']}"][h] += 1
                hourly["*"][h] += 1

    reject_tot = collections.Counter()
    for c in rejects.values():
        reject_tot.update(c)
    inter_sub = sum(submitted_by_mode[c]["interactive"] for c in cells)
    rejects_out = {"by_cell": {c: dict(sorted(v.items())) for c, v in sorted(rejects.items())},
                   "total": dict(sorted(reject_tot.items())), "total_n": sum(reject_tot.values()),
                   "interactive_submitted": inter_sub,
                   "interactive_session_busy_share": round(reject_tot["interactive:error:SESSION_BUSY"] / inter_sub, 5) if inter_sub else None,
                   "interactive_busy_share": round(reject_tot["interactive:busy"] / inter_sub, 5) if inter_sub else None,
                   "gate_expected_rejects": gate_counts.get("*", {}).get("expected_rejects")}

    by_md = collections.defaultdict(list)
    for u in unexpected:
        by_md[f"{u.get('mode', u['kind'])}|{u['kind']}:{u.get('detail')}"].append(u)
    unexp_out = {
        "n": len(unexpected),
        "rate_vs_completed_plus_unexpected": round(len(unexpected) / (tot_c + len(unexpected)), 8) if tot_c else None,
        "by_mode_detail": {k: {"n": len(v), "first_wall_ts": v[0].get("terminal_wall_ts") or v[0].get("submit_wall_ts"),
                               "last_wall_ts": v[-1].get("terminal_wall_ts") or v[-1].get("submit_wall_ts")}
                           for k, v in sorted(by_md.items())},
        "items": [u for k, v in sorted(by_md.items()) if len(v) <= ITEMS_GROUP_MAX for u in v],
        "items_omitted_groups": sorted(k for k, v in by_md.items() if len(v) > ITEMS_GROUP_MAX),
        "gate_unexpected_errors": gate_counts.get("*", {}).get("unexpected_errors"),
        "gate_unresolved": gate_counts.get("*", {}).get("unresolved"),
        "gate_error_rate": gate_counts.get("*", {}).get("error_rate"),
    }

    # ---- 資源（gate._stats_resources 定義）
    duration_s = float(R["lr"]["duration_s"])
    rows = sorted((s for s in R["snaps"] if inseg(s) and s.get("rss_kb") is not None), key=lambda r: float(r["t"]))
    last = [r for r in rows if float(r["t"]) >= duration_s - DAY_S]
    peak = max(rows, key=lambda r: r["rss_kb"])
    hourly_rss = collections.defaultdict(list)
    for r in rows:
        hourly_rss[int(float(r["t"]) // 3600)].append(r["rss_kb"])
    seg_snaps = [s for s in R["snaps"] if inseg(s)]
    resources = {
        "source": "snapshots.ndjson（每 300 s；rss_kb = daemon 行程 WorkingSetSize，GetProcessMemoryInfo）",
        "samples": len(seg_snaps), "samples_with_rss": len(rows),
        "rss_probe_errors": sum(1 for s in seg_snaps if s.get("rss_probe_error")),
        "pid_probe_errors": sum(1 for s in seg_snaps if s.get("pid_probe_error")),
        "daemon_pids": sorted({s.get("pid") for s in seg_snaps if s.get("pid") is not None}),
        "first_kb": rows[0]["rss_kb"], "first_t_s": rows[0]["t"],
        "peak_kb": peak["rss_kb"], "peak_t_s": peak["t"],
        "last_kb": rows[-1]["rss_kb"], "last_t_s": rows[-1]["t"],
        "steady_median_kb": statistics.median(float(r["rss_kb"]) for r in last),
        "steady_window": "t ≥ report.duration_s − 86400 s（gate._stats_resources）",
        "steady_samples": len(last),
        "slope_kb_per_h": round(slope_kb_per_h(last), 3),
        "hourly_median_kb": {str(h): statistics.median(v) for h, v in sorted(hourly_rss.items())},
        "cpu": "沒有", "threads": "沒有", "handles": "沒有",
    }
    g_res = next((c for c in (gate_for_xcheck or {}).get("checks", []) if c.get("id") == "resources"), {})
    gstats = (g_res.get("detail") or {}).get("stats") or (g_res.get("detail") or {}).get("calibration")
    resources["gate_stats"] = gstats

    # ---- observer、排他性探測
    if wend is None:
        obs = [o for o in R["obs"] if t0 <= o["_ts"] <= end_ts]
        prb = [q for q in R["probes"] if t0 <= q["_ts"] <= end_ts]
    else:
        obs = [o for o in R["obs"] if t0 <= o["_ts"] < wend]
        prb = [q for q in R["probes"] if t0 <= q["_ts"] < wend]
    non_ready = collections.Counter()
    for o in obs:
        for com, st in (o.get("sessions") or {}).items():
            if st != "READY":
                non_ready[f"{com}:{st}"] += 1
    mono_iv = [b["t_mono"] - a["t_mono"] for a, b in zip(obs, obs[1:])]
    observer = {
        "samples": len(obs),
        "daemon_ok_false": sum(1 for o in obs if (o.get("daemon") or {}).get("ok") is False),
        "pid_alive_false": sum(1 for o in obs if (o.get("daemon") or {}).get("pid_alive") is False),
        "wal_healthy_false": sum(1 for o in obs if (o.get("daemon") or {}).get("wal_healthy") is False),
        "multi_open_true": sum(1 for o in obs if (o.get("daemon") or {}).get("multi_open") is True),
        "holder_unknown_true": sum(1 for o in obs if (o.get("daemon") or {}).get("holder_unknown") is True),
        "foreign_holders_nonempty": sum(1 for o in obs if (o.get("daemon") or {}).get("foreign_holders")),
        "other_serialwrapd_nonempty": sum(1 for o in obs if (o.get("procscan") or {}).get("other_serialwrapd")),
        "daemon_pids": sorted({(o.get("daemon") or {}).get("pid") for o in obs if (o.get("daemon") or {}).get("pid")}),
        "session_non_ready_samples": dict(sorted(non_ready.items())),
        "interval_mono_s": {"median": round(statistics.median(mono_iv), 3), "max": round(max(mono_iv), 3)} if mono_iv else None,
        "cpu": "沒有", "threads": "沒有", "handles": "沒有",
    }
    probes = {"n": len(prb),
              "verdicts": dict(sorted(collections.Counter(f"{q.get('verdict')}|{q.get('reason')}" for q in prb).items()))}

    latency = {}
    for g in sorted(lat):
        latency[g] = {m: dist(v) for m, v in sorted(lat[g].items())}
    line_top = sorted(line_done_lat, reverse=True)[:5]
    seg_steps = [z for z in R["ev_zones"] if z["zone_first_wall_ts"] and (wend is None or ts(z["zone_first_wall_ts"]) < wend)]

    return {
        "end": wend_label,
        "start_wall_ts": R["start"]["wall_ts"],
        "span_s": round(span_s, 3),
        "span_h": round(span_s / 3600.0, 6),
        "span_s_clock_step_corrected": round(span_s - sum(z["step"]["offset_change_s"] for z in seg_steps), 3),
        "clock_step_zones": seg_steps,
        "clock_step_excluded_pairs": dict(sorted(step_excluded.items())),
        "latency": latency,
        "line_done_top5": [{"submit_wall_ts": w, "submit_to_done_s": round(d, 4)} for d, w in line_top],
        "bg_latency_field_vs_wall_abs_diff_s": dist(drift),
        "negative_deltas": dict(negative),
        "service_time_est_s": service,
        "lock_wait_est_s": {k: dist(v) for k, v in sorted(waits.items())},
        "lock_busy_fraction_est": round(busy_sum / span_s, 4),
        "service_chain_excluded": svc_excluded,
        "wal_decomposition": {
            "line_done": {k: dist(v) for k, v in sorted(line_dec.items())} | {
                "missing": dict(line_dec_miss), "lock_acq_to_wal_tx_top3": top3(line_acq_tx_top)},
            "background_accepted": {k: dist(v) for k, v in sorted(bg_dec.items())} | {
                "missing": dict(bg_dec_miss), "lock_acq_to_wal_tx_top3": top3(bg_acq_tx_top),
                "accepted_event_after_wal_tx_share": round(sum(1 for x in tx2acc if x >= 0) / len(tx2acc), 4) if tx2acc else None},
            "human_tick": {k: dist(v) for k, v in sorted(human.items())} | {"ticks": len(ticks), "excluded": human_miss},
        },
        "wal": wal_out,
        "throughput": throughput,
        "hourly_completed": {c: {"hours": len(v), "min": min(v), "median": statistics.median(v), "max": max(v)}
                             for c, v in sorted(hourly.items()) if v},
        "rejects": rejects_out,
        "unexpected": unexp_out,
        "cut_by_segment_end": cut,
        "anomalies": dict(anomalies),
        "harness_misc_events": dict(misc),
        "resources": resources,
        "observer": observer,
        "exclusivity_probes": probes,
    }


def rss_window(R: dict, lo: float, hi: float) -> dict:
    rows = sorted((s for s in R["snaps"] if s.get("rss_kb") is not None and lo <= float(s["t"]) < hi),
                  key=lambda r: float(r["t"]))
    vals = [r["rss_kb"] for r in rows]
    return {"t_from_s": lo, "t_to_s": round(hi, 3), "samples": len(rows), "median_kb": statistics.median(vals),
            "min_kb": min(vals), "max_kb": max(vals), "slope_kb_per_h": round(slope_kb_per_h(rows), 3)}


def run_meta(R: dict) -> dict:
    meta = R["report"].get("meta") or {}
    return {
        "run_dir": R["name"],  # 只留目錄名稱，不寫測試機路徑
        "version": meta.get("version"),
        "started_at_utc": meta.get("started_at_utc"),
        "engine_verdict": R["lr"].get("verdict"), "engine_reason_code": R["lr"].get("reason_code") or None,
        "report_duration_s": R["lr"].get("duration_s"),
        "events_start_wall_ts": R["start"]["wall_ts"],
        "events_stop_wall_ts": R["stop"]["wall_ts"] if R["stop"] else None,
        "events_wall_span_s": round(R["stop"]["_ts"] - R["start"]["_ts"], 3) if R["stop"] else None,
        "event_counts": dict(collections.Counter(e["kind"] for e in R["events"])),
        "gate_release_gate_result": R["gate"].get("release_gate_result"),
        "gate_baseline_result": R["gate"].get("baseline_result"),
        "inputs_sha256": R["inputs"],
        "inputs_match_gate_evidence_hashes": R["inputs_match_gate"],
        "engine_sha256": R["engine"],
        "clock_steps_observer": R["obs_steps"],
        "clock_steps_wal": R["wal_offsets"][0],
        "wal_single_record_mono_wall_spikes": {"n": len(R["wal_offsets"][1]), "items": R["wal_offsets"][1]},
        "timestamp_resolution": {
            "wal_mono_ts_ns_min_consecutive_diff_s": min_consecutive_diff([r["_mono"] for r in R["wal"]]),
            "wal_wall_ts_min_consecutive_diff_s": min_consecutive_diff([r["_ts"] for r in R["wal"]]),
            "events_wall_ts_min_consecutive_diff_s": min_consecutive_diff([e["_ts"] for e in R["events"]]),
            "bg_result_latency_s_share_within_1_5ms_of_1_64s_multiple": share_near_tick(
                [float(e["latency_s"]) for e in R["events"] if e["kind"] == "bg_result"]),
        },
        "wal_records_total": len(R["wal"]),
        "wal_loss_flag_total": sum(1 for r in R["wal"] if r["loss"]),
    }


def pick(d: dict | None, *keys: str) -> dict | None:
    return {k: d.get(k) for k in keys} if d else None


def ratio(a, b):
    return round(a / b, 5) if a is not None and b else None


def compare(base: dict, cand: dict) -> dict:
    """候選有效段 vs 基線全程的衍生數字（供 README 引用）。"""
    lat_keys = [("line", "submit_to_done_s"), ("interactive", "submit_to_done_s"),
                ("interactive", "submit_to_reject_s"), ("background", "submit_to_accepted_s"),
                ("background", "bg_result_latency_s_field"), ("background", "accepted_to_bg_result_wall_s"),
                ("background", "submit_to_bg_result_s")]
    lat = {}
    for mode, m in lat_keys:
        b = (base["latency"].get(f"*|{mode}") or {}).get(m)
        c = (cand["latency"].get(f"*|{mode}") or {}).get(m)
        fields = ("n", "min", "p50", "p90", "p99", "max", "mean")
        lat[f"{mode}.{m}"] = {"baseline": pick(b, *fields), "candidate": pick(c, *fields),
                              "p50_ratio": ratio(c and c["p50"], b and b["p50"]),
                              "p50_diff_ms": round((c["p50"] - b["p50"]) * 1000, 1) if b and c else None}
    svc = {}
    for k in ("line|done", "line|done_minus_settle_1.5s", "interactive|done", "background|accepted"):
        b, c = base["service_time_est_s"].get(k), cand["service_time_est_s"].get(k)
        svc[k] = {"baseline": pick(b, "n", "p50", "p90", "p99", "mean"), "candidate": pick(c, "n", "p50", "p90", "p99", "mean"),
                  "mean_diff_ms": round((c["mean"] - b["mean"]) * 1000, 1) if b and c else None,
                  "p50_diff_ms": round((c["p50"] - b["p50"]) * 1000, 1) if b and c else None}
    wd = {}
    for grp, m in (("line_done", "lock_acq_to_wal_tx_s"), ("line_done", "wal_tx_to_output_rx_s"),
                   ("line_done", "wal_tx_to_prompt_rx_s"), ("background_accepted", "lock_acq_to_wal_tx_s"),
                   ("human_tick", "wal_tx_to_output_rx_s")):
        b = base["wal_decomposition"][grp].get(m)
        c = cand["wal_decomposition"][grp].get(m)
        wd[f"{grp}.{m}"] = {"baseline": pick(b, "n", "p50", "p90", "p99", "max", "mean"),
                            "candidate": pick(c, "n", "p50", "p90", "p99", "max", "mean")}
    tp = {}
    for cell in sorted(set(base["throughput"]) | set(cand["throughput"])):
        b, c = base["throughput"].get(cell), cand["throughput"].get(cell)
        tp[cell] = {"baseline_completed_per_h": b and b["completed_per_h"],
                    "candidate_completed_per_h": c and c["completed_per_h"],
                    "completed_per_h_ratio": ratio(c and c["completed_per_h"], b and b["completed_per_h"]),
                    "submitted_per_h_ratio": ratio(c and c["submitted_per_h"], b and b["submitted_per_h"])}
    bt, ct = base["throughput"]["*"], cand["throughput"]["*"]
    tp_mode = {m: {"baseline_completed_per_h": bt["completed_per_h_by_mode"][m],
                   "candidate_completed_per_h": ct["completed_per_h_by_mode"][m],
                   "completed_per_h_ratio": ratio(ct["completed_per_h_by_mode"][m], bt["completed_per_h_by_mode"][m]),
                   "submitted_per_h_ratio": ratio(ct["submitted_per_h_by_mode"][m], bt["submitted_per_h_by_mode"][m])}
               for m in ("line", "interactive", "background")}
    return {
        "baseline_segment": "runs.baseline_python_0.4.1.segments.full",
        "candidate_segment": "runs.candidate_rust_0.5.0_rc1.segments.effective_od110",
        "latency_s": lat,
        "service_time_est_s": svc,
        "lock_busy_fraction_est": {"baseline": base["lock_busy_fraction_est"], "candidate": cand["lock_busy_fraction_est"]},
        "wal_decomposition_s": wd,
        "throughput": tp,
        "throughput_by_mode": tp_mode,
        "interactive_session_busy_share": {"baseline": base["rejects"]["interactive_session_busy_share"],
                                           "candidate": cand["rejects"]["interactive_session_busy_share"]},
        "unexpected": {"baseline": base["unexpected"]["n"], "candidate": cand["unexpected"]["n"],
                       "candidate_cut_by_segment_end": len(cand["cut_by_segment_end"])},
        "rss_gate_definition_kb": {"baseline": pick(base["resources"], "first_kb", "peak_kb", "steady_median_kb", "slope_kb_per_h", "steady_samples"),
                                   "candidate": pick(cand["resources"], "first_kb", "peak_kb", "steady_median_kb", "slope_kb_per_h", "steady_samples")},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--baseline-name", required=True)
    ap.add_argument("--candidate", required=True, type=Path)
    ap.add_argument("--candidate-name", required=True)
    ap.add_argument("--cutoff", required=True, help="候選有效段終點（OD-110 截斷時間，ISO 8601）")
    ap.add_argument("--od110-gate", type=Path, help="OD-110 重算判定檔，用於有效段交叉驗證")
    ap.add_argument("--od110-receipt", type=Path, help="OD-110 截斷收據，用於核對原始檔雜湊")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    cutoff = ts(a.cutoff)
    B = load_run(a.baseline, a.baseline_name)
    C = load_run(a.candidate, a.candidate_name)
    od110_gate = json.load(open(a.od110_gate, encoding="utf-8")) if a.od110_gate else None
    receipt = json.load(open(a.od110_receipt, encoding="utf-8")) if a.od110_receipt else None

    bw, cw = WalIndex(B["wal"]), WalIndex(C["wal"])
    base_full = analyze_segment(B, None, "stop 事件", B["gate"], bw)
    cand_eff = analyze_segment(C, cutoff, a.cutoff, od110_gate, cw)
    cand_full = analyze_segment(C, None, "stop 事件", C["gate"], cw)

    rss_hi = cand_eff["span_s"]
    rss_common = {"window": f"t ∈ [{RSS_COMMON_FROM_S:.0f} s, 候選有效段長度 {rss_hi:.3f} s)",
                  "baseline": rss_window(B, RSS_COMMON_FROM_S, rss_hi),
                  "candidate": rss_window(C, RSS_COMMON_FROM_S, rss_hi)}

    receipt_check = None
    if receipt:
        receipt_check = {rel.split("/")[-1]: receipt[rel]["src_sha256"] == C["inputs"][rel.split("/")[-1]]
                         for rel in ("lr-mixed/events.ndjson", "lr-mixed/snapshots.ndjson", "observer.ndjson")}
        receipt_check["cutoff_equal"] = ts(receipt["cutoff"]) == cutoff
    od110_xcheck = None
    if od110_gate:
        g = od110_gate["counts"]["*"]
        od110_xcheck = {
            "gate_report_json_sha256_equal": (od110_gate.get("evidence_hashes") or {}).get("report.json") == C["inputs"]["report.json"],
            "gate_wal_sha256_equal": (od110_gate.get("evidence_hashes") or {}).get("wal") == C["inputs"]["wal"],
            "gate_effective_hour": g.get("effective_hour"),
        }

    result = {
        "schema": "sw-longrun-latency-windows.v1",
        "method": "見同目錄 METHOD.md",
        "platform": "windows（Orange Pi 3，單板 COM0，4 個 agent worker 共用一把 per-board lock）",
        "cutoff": a.cutoff,
        "engine_files_identical": {rel: (B["engine"][rel] is not None and B["engine"][rel] == C["engine"][rel])
                                   for rel in ENGINE_FILES},
        "od110_receipt_src_sha256_match": receipt_check,
        "od110_gate_crosscheck": od110_xcheck,
        "runs": {
            "baseline_python_0.4.1": run_meta(B) | {"segments": {"full": base_full}},
            "candidate_rust_0.5.0_rc1": run_meta(C) | {"segments": {"effective_od110": cand_eff, "full": cand_full}},
        },
        "rss_common_window": rss_common,
        "comparison_effective_vs_baseline": compare(base_full, cand_eff),
    }
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
