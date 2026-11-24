#!/usr/bin/env python3
"""離線計算兩次 Linux lr-mixed 24h 長跑的延遲、吞吐、錯誤與資源統計。

只讀 run 目錄內既有檔案；輸出寫到本腳本所在目錄的上一層。
用法：python3 -I latency.py <baseline_run_dir> <candidate_run_dir> <out_json>
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

DAY_S = 86400.0
PCTS = (50, 90, 99, 99.9)
EXPECTED_REJECT_KINDS = {"busy"}
EXPECTED_REJECT_CODES = {"SESSION_INTERACTIVE_BUSY"}
EXPECTED_REJECT_BY_MODE = {"interactive": {"SESSION_BUSY"}}


def ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


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


def analyze(run_dir: Path) -> dict:
    ev_path = run_dir / "realhw/lr-mixed/events.ndjson"
    snap_path = run_dir / "realhw/lr-mixed/snapshots.ndjson"
    obs_path = run_dir / "observer.ndjson"
    report_path = run_dir / "realhw/report.json"
    gate_path = run_dir / "release-gate.json"

    events = [json.loads(line) for line in open(ev_path, encoding="utf-8")]
    report = json.load(open(report_path, encoding="utf-8"))
    gate = json.load(open(gate_path, encoding="utf-8"))
    duration_s = next(r["duration_s"] for r in report["results"] if r["id"] == "lr-mixed")

    start_ev = next(e for e in events if e["kind"] == "start")
    stop_ev = next(e for e in events if e["kind"] == "stop")
    wall_span_s = ts(stop_ev["wall_ts"]) - ts(start_ev["wall_ts"])

    anomalies: dict[str, int] = collections.Counter()
    submits: dict[tuple, dict] = {}
    terminals: dict[tuple, list[dict]] = collections.defaultdict(list)
    accepted: dict[tuple, dict] = {}
    bg_by_id: dict[str, dict] = {}
    worker_errors: list[dict] = []
    for e in events:
        k = e["kind"]
        if k == "submit":
            key = (e["source"], e["round"])
            if key in submits:
                anomalies["duplicate_submit_key"] += 1
            submits[key] = e
        elif k in ("done", "error", "busy"):
            terminals[(e["source"], e["round"])].append(e)
        elif k == "submit_accepted":
            key = (e["source"], e["round"])
            if key in accepted:
                anomalies["duplicate_submit_accepted_key"] += 1
            accepted[key] = e
        elif k == "bg_result":
            if e["cmd_id"] in bg_by_id:
                anomalies["duplicate_bg_result_cmd_id"] += 1
            bg_by_id[e["cmd_id"]] = e
        elif k == "worker_error":
            worker_errors.append(e)
        elif k in ("bg_lost", "bg_slow", "bg_poll_error", "bg_drain", "bg_backpressure"):
            anomalies[k] += 1

    # 分組容器：lat[group][metric] -> list
    lat: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    completed = collections.Counter()
    completed_by_mode = collections.defaultdict(collections.Counter)
    rejects = collections.defaultdict(collections.Counter)
    unexpected: list[dict] = []
    negative = collections.Counter()

    def add(group_keys: list[str], metric: str, value: float) -> None:
        if value < 0:
            negative[metric] += 1
        for g in group_keys:
            lat[g][metric].append(value)

    for key, sub in submits.items():
        source, rnd = key
        com, mode = sub["com"], sub["mode"]
        cell = f"{com}|{source}"
        groups = ["*", cell]
        t_sub = ts(sub["wall_ts"])
        terms = terminals.get(key, [])
        if mode in ("line", "interactive"):
            if len(terms) != 1:
                anomalies[f"{mode}_terminal_count_{len(terms)}"] += 1
                if not terms:
                    unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                       "round": rnd, "submit_wall_ts": sub["wall_ts"]})
                    continue
            term = terms[0]
            d = ts(term["wall_ts"]) - t_sub
            if term["kind"] == "done":
                completed[cell] += 1
                completed_by_mode[cell][mode] += 1
                add([f"{g}|{mode}" for g in groups], "submit_to_done_s", d)
                add([f"{g}|all_modes" for g in groups], "submit_to_completion_s", d)
                if mode == "interactive" and term.get("detail") == "soft_preempted":
                    completed_by_mode[cell]["interactive_soft_preempted"] += 1
            elif is_expected_reject(term):
                label = "busy" if term["kind"] == "busy" else f"error:{term.get('detail')}"
                rejects[cell][f"{mode}:{label}"] += 1
                add([f"{g}|{mode}" for g in groups], "submit_to_reject_s", d)
            else:
                unexpected.append({"kind": "error", "mode": mode, "com": com, "source": source,
                                   "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                   "terminal_wall_ts": term["wall_ts"], "detail": term.get("detail"),
                                   "submit_to_terminal_s": round(d, 4)})
        elif mode == "background":
            acc = accepted.get(key)
            if acc is None:
                if len(terms) == 1 and terms[0]["kind"] == "error":
                    term = terms[0]
                    if is_expected_reject(term):
                        rejects[cell][f"background:error:{term.get('detail')}"] += 1
                    else:
                        unexpected.append({"kind": "error", "mode": mode, "com": com, "source": source,
                                           "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                           "terminal_wall_ts": term["wall_ts"], "detail": term.get("detail"),
                                           "submit_to_terminal_s": round(ts(term["wall_ts"]) - t_sub, 4)})
                else:
                    anomalies["background_no_accept"] += 1
                    unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                       "round": rnd, "submit_wall_ts": sub["wall_ts"]})
                continue
            if terms:
                anomalies["background_extra_terminal"] += 1
            t_acc = ts(acc["wall_ts"])
            add([f"{g}|background" for g in groups], "submit_to_accepted_s", t_acc - t_sub)
            bg = bg_by_id.get(acc["cmd_id"])
            if bg is None:
                anomalies["background_no_bg_result"] += 1
                unexpected.append({"kind": "unresolved", "mode": mode, "com": com, "source": source,
                                   "round": rnd, "submit_wall_ts": sub["wall_ts"], "cmd_id": acc["cmd_id"]})
                continue
            if (bg["source"], bg["round"], bg["com"]) != (source, rnd, com):
                anomalies["bg_result_key_mismatch"] += 1
            t_bg = ts(bg["wall_ts"])
            d_acc_bg = t_bg - t_acc
            d_sub_bg = t_bg - t_sub
            drift = abs(d_acc_bg - float(bg["latency_s"]))
            lat["_drift"]["latency_s_vs_wall_abs_diff_s"].append(drift)
            if bg.get("status") == "done":
                completed[cell] += 1
                completed_by_mode[cell]["background"] += 1
                add([f"{g}|background" for g in groups], "accepted_to_bg_result_wall_s", d_acc_bg)
                add([f"{g}|background" for g in groups], "bg_result_latency_s_field", float(bg["latency_s"]))
                add([f"{g}|background" for g in groups], "submit_to_bg_result_s", d_sub_bg)
                add([f"{g}|all_modes" for g in groups], "submit_to_completion_s", d_sub_bg)
            else:
                unexpected.append({"kind": "bg_result", "mode": mode, "com": com, "source": source,
                                   "round": rnd, "submit_wall_ts": sub["wall_ts"],
                                   "terminal_wall_ts": bg["wall_ts"], "cmd_id": acc["cmd_id"],
                                   "detail": f"status={bg.get('status')} error_code={bg.get('error_code')}",
                                   "submit_to_terminal_s": round(d_sub_bg, 4),
                                   "bg_result_latency_s": round(float(bg["latency_s"]), 4)})
        else:
            anomalies[f"unknown_mode_{mode}"] += 1

    # 每小時完成數（依完成事件 wall_ts 相對 start 分桶；最後不足一小時的尾段捨去）
    t0 = ts(start_ev["wall_ts"])
    full_hours = int(wall_span_s // 3600)
    hourly = collections.defaultdict(lambda: [0] * full_hours)
    for e in events:
        if e["kind"] == "done" or (e["kind"] == "bg_result" and e.get("status") == "done"):
            h = int((ts(e["wall_ts"]) - t0) // 3600)
            if 0 <= h < full_hours:
                hourly[f"{e['com']}|{e['source']}"][h] += 1
                hourly["*"][h] += 1

    # 長尾歸因：line 完成延遲 > 6 s 者，是否與同 COM 的非預期錯誤（submit→terminal 區間）重疊
    err_windows = collections.defaultdict(list)
    for u in unexpected:
        if u.get("submit_wall_ts") and u.get("terminal_wall_ts"):
            err_windows[u["com"]].append((ts(u["submit_wall_ts"]), ts(u["terminal_wall_ts"])))
    tail = collections.Counter()
    for key, sub in submits.items():
        if sub["mode"] != "line":
            continue
        terms = terminals.get(key, [])
        if len(terms) != 1 or terms[0]["kind"] != "done":
            continue
        a, b = ts(sub["wall_ts"]), ts(terms[0]["wall_ts"])
        if b - a <= 6.0:
            continue
        overlap = any(a <= we and b >= ws for ws, we in err_windows[sub["com"]])
        tail[f"{sub['com']}|{'overlaps_unexpected_error' if overlap else 'no_overlap'}"] += 1

    for we in worker_errors:
        unexpected.append({"kind": "worker_error", "source": we.get("source"), "com": we.get("com"),
                           "round": we.get("round"), "terminal_wall_ts": we.get("wall_ts"),
                           "detail": we.get("detail")})

    unexpected.sort(key=lambda u: u.get("terminal_wall_ts") or u.get("submit_wall_ts") or "")

    latency = {}
    for g in sorted(lat):
        if g == "_drift":
            continue
        latency[g] = {m: dist(v) for m, v in sorted(lat[g].items())}
    drift_vals = lat["_drift"]["latency_s_vs_wall_abs_diff_s"]

    # 吞吐：與 gate 相同，以 gate 的 effective_hour 為分母
    gate_counts = gate["counts"]
    throughput = {}
    cells = sorted({f"{s['com']}|{s['source']}" for s in submits.values()})
    for cell in cells:
        eff_h = gate_counts.get(cell, {}).get("effective_hour")
        n = completed[cell]
        throughput[cell] = {
            "completed": n,
            "completed_by_mode": dict(completed_by_mode[cell]),
            "effective_hour": eff_h,
            "completed_per_h": round(n / eff_h, 3) if eff_h else None,
            "gate_completed": gate_counts.get(cell, {}).get("completed"),
            "gate_throughput_per_h": gate_counts.get(cell, {}).get("throughput_per_h"),
        }
    tot = sum(completed.values())
    throughput["*"] = {"completed": tot, "effective_hour": gate_counts["*"]["effective_hour"],
                       "completed_per_h": round(tot / gate_counts["*"]["effective_hour"], 3),
                       "gate_completed": gate_counts["*"]["completed"]}

    reject_tot = collections.Counter()
    for c in rejects.values():
        reject_tot.update(c)
    rejects_out = {"by_cell": {c: dict(v) for c, v in sorted(rejects.items())},
                   "total": dict(reject_tot), "total_n": sum(reject_tot.values()),
                   "gate_expected_rejects": gate_counts["*"]["expected_rejects"]}

    # 資源：重用 gate._stats_resources 的定義
    snaps = [json.loads(line) for line in open(snap_path, encoding="utf-8")]
    rows = sorted((r for r in snaps if r.get("rss_kb") is not None), key=lambda r: float(r["t"]))
    last = [r for r in rows if float(r["t"]) >= duration_s - DAY_S]
    xs = [float(r["t"]) / 3600.0 for r in last]
    ys = [float(r["rss_kb"]) for r in last]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var if var > 0 else 0.0
    peak_row = max(rows, key=lambda r: r["rss_kb"])
    pids = sorted({r.get("pid") for r in snaps if r.get("pid") is not None})
    resources = {
        "source": "snapshots.ndjson（每 300 s；rss_kb = daemon 進程 VmRSS）",
        "samples": len(snaps), "samples_with_rss": len(rows),
        "rss_probe_errors": sum(1 for r in snaps if r.get("rss_probe_error")),
        "pid_probe_errors": sum(1 for r in snaps if r.get("pid_probe_error")),
        "daemon_pids": pids,
        "first_kb": rows[0]["rss_kb"], "first_t_s": rows[0]["t"],
        "peak_kb": peak_row["rss_kb"], "peak_t_s": peak_row["t"],
        "last_kb": rows[-1]["rss_kb"], "last_t_s": rows[-1]["t"],
        "steady_median_kb": statistics.median(ys), "steady_window": "t ≥ duration_s − 86400 s",
        "steady_samples": len(last),
        "slope_kb_per_h": round(slope, 3),
        "cpu": "沒有", "threads": "沒有", "fd": "沒有",
    }
    g_res = next((c for c in gate["checks"] if c.get("id") == "resources"), {})
    resources["gate_stats"] = (g_res.get("detail") or {}).get("stats")

    # observer：只有 daemon 健康與 session 狀態
    obs_samples = [json.loads(line) for line in open(obs_path, encoding="utf-8")]
    obs = [o for o in obs_samples if o.get("kind") == "sample"]
    non_ready = collections.Counter()
    for o in obs:
        for com, st in (o.get("sessions") or {}).items():
            if st != "READY":
                non_ready[com] += 1
    gaps = [ts(b["wall_ts"]) - ts(a["wall_ts"]) for a, b in zip(obs, obs[1:])]
    observer = {
        "samples": len(obs),
        "fields": sorted({k for o in obs for k in o}),
        "daemon_fields": sorted({k for o in obs for k in (o.get("daemon") or {})}),
        "daemon_ok_false": sum(1 for o in obs if (o.get("daemon") or {}).get("ok") is False),
        "pid_alive_false": sum(1 for o in obs if (o.get("daemon") or {}).get("pid_alive") is False),
        "wal_healthy_false": sum(1 for o in obs if (o.get("daemon") or {}).get("wal_healthy") is False),
        "multi_open_true": sum(1 for o in obs if (o.get("daemon") or {}).get("multi_open") is True),
        "other_serialwrapd_nonempty": sum(1 for o in obs if (o.get("procscan") or {}).get("other_serialwrapd")),
        "daemon_pids": sorted({(o.get("daemon") or {}).get("pid") for o in obs if (o.get("daemon") or {}).get("pid")}),
        "session_non_ready_samples": dict(non_ready),
        "interval_s": {"median": round(statistics.median(gaps), 3), "max": round(max(gaps), 3)},
        "cpu": "沒有", "threads": "沒有", "fd": "沒有",
    }

    # 補充：只有候選有 proc-detail.ndjson（本機另行記錄，非 harness 產物）
    proc_detail = "沒有"
    pd_path = run_dir / "proc-detail.ndjson"
    if pd_path.exists():
        pd = [json.loads(line) for line in open(pd_path, encoding="utf-8")]
        pd = [p for p in pd if "status" in p]

        def mm(vals):
            return {"min": min(vals), "max": max(vals), "first": vals[0], "last": vals[-1]}
        proc_detail = {
            "source": "proc-detail.ndjson（本機另行以 /proc 記錄，每 600 s；不在 harness 合約內，基線沒有對應資料）",
            "samples": len(pd), "first_wall_ts": pd[0]["wall_ts"], "last_wall_ts": pd[-1]["wall_ts"],
            "fd": mm([p["fd"] for p in pd]),
            "threads": mm([p["status"]["Threads"] for p in pd]),
            "vm_rss_kb": mm([p["status"]["VmRSS"] for p in pd]),
            "vm_hwm_kb_last": pd[-1]["status"]["VmHWM"],
            "rss_anon_kb": mm([p["status"]["RssAnon"] for p in pd]),
            "cpu": "沒有",
        }

    meta = report["meta"]
    return {
        "run_dir": run_dir.name,  # 只留目錄名稱，不寫測試機的絕對路徑
        "version": meta.get("version"),
        "started_at_utc": meta.get("started_at_utc"),
        "duration_s": duration_s,
        "events_wall_span_s": round(wall_span_s, 3),
        "inputs_sha256": {p.name: sha256(p) for p in (ev_path, snap_path, obs_path, report_path, gate_path)},
        "event_counts": dict(collections.Counter(e["kind"] for e in events)),
        "latency": latency,
        "bg_latency_field_vs_wall": dist(drift_vals),
        "negative_deltas": dict(negative),
        "throughput": throughput,
        "hourly_completed": {c: {"hours": len(v), "min": min(v), "median": statistics.median(v),
                                 "max": max(v)} for c, v in sorted(hourly.items())},
        "line_tail_gt_6s_attribution": dict(tail),
        "rejects": rejects_out,
        "unexpected": {"n": len(unexpected), "gate_unexpected_errors": gate_counts["*"]["unexpected_errors"],
                       "gate_unresolved": gate_counts["*"]["unresolved"], "items": unexpected},
        "anomalies": dict(anomalies),
        "resources": resources,
        "observer": observer,
        "proc_detail_supplementary": proc_detail,
    }


def main() -> int:
    base, cand, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    result = {
        "schema": "sw-longrun-latency.v1",
        "method": "見同目錄 METHOD.md",
        "runs": {"baseline_python_0.4.1": analyze(base), "candidate_rust_0.5.0_rc1": analyze(cand)},
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
