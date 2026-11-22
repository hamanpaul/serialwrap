"""OD-110 v2：排除 [CUTOFF, WINDOW_END] 內的命令層事件（submit／done／error／busy／bg_result／tick 等），
保留長跑生命週期事件（start、stop、worker_*、major、minicom_open）；快照與 observer 取樣截在 CUTOFF 之前。
不改寫任何紀錄內容；report.json、WAL、排他性證據不截、不改。"""
import datetime as dt, json, pathlib, shutil, sys, hashlib
SRC = pathlib.Path(sys.argv[1]); DST = pathlib.Path(sys.argv[2])
CUTOFF = dt.datetime.fromisoformat("2026-11-22T05:29:16+00:00")
WINDOW_END = dt.datetime.fromisoformat("2026-11-22T05:44:58.842+00:00")
CMD_KINDS = {"submit", "submit_accepted", "done", "error", "busy", "bg_result", "tick"}
def keep(line, events):
    try: r = json.loads(line)
    except Exception: return True
    ts = r.get("wall_ts")
    if not ts: return True          # 檔頭等無時間戳紀錄保留
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if events:
        return not (CUTOFF <= t <= WINDOW_END and r.get("kind") in CMD_KINDS)
    return t < CUTOFF
out = {}
for rel in ("lr-mixed/events.ndjson", "lr-mixed/snapshots.ndjson", "observer.ndjson"):
    src = (SRC / rel).read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [l for l in src if not l.strip() or keep(l, rel.endswith("events.ndjson"))]
    (DST / rel).write_text("".join(kept), encoding="utf-8")
    out[rel] = {"src_lines": len(src), "kept": len(kept), "dropped": len(src) - len(kept),
                "src_sha256": hashlib.sha256((SRC / rel).read_bytes()).hexdigest(),
                "dst_sha256": hashlib.sha256((DST / rel).read_bytes()).hexdigest()}
for rel in ("report.json", "lr-mixed/longrun-analysis.md", "supervision-attest.json"):
    if (SRC / rel).exists():
        shutil.copy2(SRC / rel, DST / rel)
        out[rel] = {"copied_unchanged": True, "sha256": hashlib.sha256((DST / rel).read_bytes()).hexdigest()}
out["cutoff"] = CUTOFF.isoformat(); out["window_end"] = WINDOW_END.isoformat(); out["excluded_event_kinds"] = sorted(CMD_KINDS)
(DST / "truncation-receipt.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
print(json.dumps(out, ensure_ascii=False, indent=1)[:1500])
