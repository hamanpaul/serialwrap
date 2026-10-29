#!/usr/bin/env python3
"""OD-45：event_latency 外部端到端量測（`contracts/resource_limits.yaml` `event_latency`，method_revision 2026-10-29.1）。

只量 `measure: external` 的三個事件（stop_request、command_cancel、release_request）；lease_expiry、drain_receipt
依 OD-45 只留 L1 結構測試（`l1_gate`），本工具不量。口徑為「RPC 送出 → RPC 回應」：client 先連上 daemon 端點，
於寫出請求前取 `time.monotonic_ns()`，讀到完整回應行後再取一次（不含連線建立；不讀 daemon 任何計時欄位、不改 WAL）。

負載：
- idle：DUT 不主動送資料。
- flood：DUT 以 `flood_load`（20000 B/s、每塊 256 bytes）持續送出 RX，涵蓋整輪量測（含觸發前的準備）。

每輪（repeat × load × event）`samples_per_run` 筆；每輪樣本最大值 ≤ `max_ms_<load>` 才通過（`gate_rule:
every_run_max_le_max_ms`，p99 只作診斷）。量測期間 `loadavg 1min > CPU 數` 的輪次作廢並重跑（最多 `--void-retries` 次），
重跑仍作廢即該輪 INSUFFICIENT_EVIDENCE。

用法：
  python3 scripts/rust/event_latency_measure.py --daemon <serialwrapd> --label rust --out <report.json>
      [--cli <serialwrap>] [--samples N] [--repeats N] [--events a,b] [--loads idle,flood] [--work-dir DIR]

`--samples`／`--repeats` 小於契約值時報告標 `diagnostic: true`，verdict 恆不為 PASS（不得作為 gate 證據）。
結束碼：PASS 0、FAIL 1、INSUFFICIENT_EVIDENCE 3、用法錯誤 2。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIMITS = ROOT / "contracts" / "resource_limits.yaml"
EXTERNAL_EVENTS = ("stop_request", "command_cancel", "release_request")
SCHEMA = "event_latency_external.v1"


def load_contract(path: pathlib.Path = LIMITS) -> dict[str, Any]:
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    lat = doc.get("event_latency")
    if not isinstance(lat, dict):
        raise RuntimeError(f"{path} 缺少 event_latency")
    return lat


def summarize(samples: list[float]) -> dict[str, Any]:
    if not samples:
        return {"count": 0, "max_ms": None, "p99_ms": None}
    ordered = sorted(samples)
    return {"count": len(ordered), "max_ms": ordered[-1], "p99_ms": ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]}


def host_load() -> dict[str, Any]:
    try:
        load1: float | None = float(pathlib.Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
    return {"loadavg_1min": load1, "cpu_count": os.cpu_count()}


def judge_run(samples: list[float], *, want: int, limit: float | None, load_before: dict, load_after: dict,
              errors: list[str]) -> dict[str, Any]:
    """單輪判定（純函式）：樣本數不足／有錯誤／host 過載 → INSUFFICIENT_EVIDENCE；最大值超過上限 → FAIL。"""
    summary = summarize(samples)
    cpu = load_after.get("cpu_count") or load_before.get("cpu_count")
    loads = [x for x in (load_before.get("loadavg_1min"), load_after.get("loadavg_1min")) if isinstance(x, (int, float))]
    void = isinstance(cpu, int) and bool(loads) and max(loads) > cpu
    if errors or summary["count"] != want or void or not isinstance(limit, (int, float)):
        verdict = "INSUFFICIENT_EVIDENCE"
    elif summary["max_ms"] <= limit:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return {**summary, "limit_ms": limit, "void": void, "verdict": verdict, "errors": errors[:5],
            "host_load": {"before": load_before, "after": load_after}}


def l1_gate_status(contract: dict[str, Any], l1_results: dict[str, Any] | None) -> dict[str, str]:
    """OD-45 (1)：各事件 `l1_gate` nodeid 在 linux_l1 l1-results 的 outcome（缺檔或未執行記為 missing）。"""
    outcomes: dict[str, str] = {}
    for row in (l1_results or {}).get("results") or []:
        if isinstance(row, dict) and row.get("nodeid"):
            outcomes[str(row["nodeid"])] = str(row.get("outcome"))
    status: dict[str, str] = {}
    for ev in contract.get("events") or []:
        for nid in ev.get("l1_gate") or []:
            status[nid] = outcomes.get(nid, "missing")
    return status


def overall_verdict(report: dict[str, Any]) -> tuple[str, list[str]]:
    """section 判定（純函式）：任一輪 FAIL 或 L1 gate failed → FAIL；全部輪次 PASS、覆蓋 repeats × loads × 外部事件、
    非診斷模式、L1 gate 全 passed 且上限值已凍結才 PASS；其餘 INSUFFICIENT_EVIDENCE（附原因）。"""
    runs = report.get("runs") or []
    l1 = report.get("l1_gate") or {}
    reasons: list[str] = []
    failed = [f"{r.get('repeat')}/{r.get('load')}/{r.get('event')} max {r.get('max_ms')} ms > {r.get('limit_ms')} ms"
              for r in runs if r.get("verdict") == "FAIL"]
    l1_failed = [n for n, o in l1.items() if o in ("failed", "error")]
    if failed or l1_failed:
        return "FAIL", [f"外部量測超過上限：{x}" for x in failed] + [f"L1 結構 gate 未通過：{n}" for n in l1_failed]
    want = {(rep, load, ev) for rep in range(1, int(report.get("repeats_required") or 0) + 1)
            for load in ("idle", "flood") for ev in EXTERNAL_EVENTS}
    got = {(r.get("repeat"), r.get("load"), r.get("event")) for r in runs if r.get("verdict") == "PASS"}
    if report.get("diagnostic"):
        reasons.append("診斷模式（samples／repeats 小於契約值），不得作為 gate 證據")
    if not want or not want <= got:
        reasons.append(f"外部量測輪次未全數 PASS：缺 {sorted(want - got)[:6]}")
    if not l1 or any(o != "passed" for o in l1.values()):
        reasons.append(f"L1 結構 gate 證據不足：{sorted(n for n, o in l1.items() if o != 'passed') or '未提供 l1-results'}")
    if report.get("freeze_status") != "frozen":
        reasons.append(f"event_latency 上限值未凍結（freeze_status={report.get('freeze_status')!r}，method_revision "
                       f"{report.get('method_revision')}）")
    return ("INSUFFICIENT_EVIDENCE", reasons) if reasons else ("PASS", [])


class _Rpc:
    """直接 NDJSON socket client；`timed()` 只計「寫出請求 → 讀到回應行」。"""

    def __init__(self, endpoint: str, timeout: float = 10.0) -> None:
        if endpoint.startswith("tcp://"):
            host, port = endpoint[6:].rsplit(":", 1)
            self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(endpoint)
        self.buf = b""
        self.n = 0

    def timed(self, method: str, params: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        self.n += 1
        line = (json.dumps({"id": f"lat-{self.n}", "method": method, "params": params},
                           separators=(",", ":")) + "\n").encode()
        t0 = time.monotonic_ns()
        self.sock.sendall(line)
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError(f"{method}：daemon 在回應前關閉連線")
            self.buf += chunk
        t1 = time.monotonic_ns()
        raw, self.buf = self.buf.split(b"\n", 1)
        resp = json.loads(raw)
        result = resp.get("result", resp) if isinstance(resp, dict) else {}
        return (t1 - t0) / 1_000_000, result if isinstance(result, dict) else {"_raw": result}

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.timed(method, params)[1]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Flood:
    """DUT 端持續送 RX：`bytes_per_s`、每塊 `chunk_bytes`（以時間表補償，維持平均速率）。"""

    def __init__(self, target: Any, bytes_per_s: int, chunk_bytes: int) -> None:
        self.target, self.rate, self.chunk = target, bytes_per_s, chunk_bytes
        self.stop_evt = threading.Event()
        self.sent = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        n = 0
        while not self.stop_evt.is_set():
            body = (b"LATFLOOD%08d_" % n).ljust(self.chunk - 2, b"x")[: self.chunk - 2] + b"\r\n"
            try:
                self.target.emit(body)
                self.sent += len(body)
            except (OSError, RuntimeError):
                pass
            n += 1
            due = start + self.sent / self.rate
            delay = due - time.monotonic()
            if delay > 0:
                self.stop_evt.wait(delay)

    def __enter__(self) -> "_Flood":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop_evt.set()
        self.thread.join(timeout=5)


def _import_harness():
    sys.path.insert(0, str(ROOT / "tests" / "contract_blackbox" / "slice21"))
    sys.path.insert(0, str(ROOT / "tests" / "contract_blackbox"))
    import harness  # noqa: PLC0415

    return harness


def _wait_state(rpc: _Rpc, state: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        r = rpc.call("session.get_state", {"selector": "COM0"})
        last = (r.get("session") or {}).get("state")
        if last == state:
            return
        time.sleep(0.05)
    raise TimeoutError(f"COM0 未於 {timeout}s 內進入 {state}（最後 {last}）")


def _sample_release(rpc: _Rpc) -> float:
    ms, r = rpc.timed("device.release", {"selector": "COM0", "source": "agent:lat"})
    if r.get("ok") is not True:
        raise RuntimeError(f"device.release 非 ok：{r}")
    r = rpc.call("device.attach", {"selector": "COM0"})
    if r.get("ok") is not True:
        raise RuntimeError(f"device.attach 非 ok：{r}")
    _wait_state(rpc, "READY")
    return ms


def _sample_cancel(rpc: _Rpc) -> float:
    r = rpc.call("command.submit", {"selector": "COM0", "cmd": "sleep 0.3", "source": "agent:lat", "timeout_s": 10})
    cmd_id = r.get("cmd_id")
    if r.get("ok") is not True or not cmd_id:
        raise RuntimeError(f"command.submit 非 ok：{r}")
    deadline = time.monotonic() + 10
    while True:
        st = (rpc.call("command.get", {"cmd_id": cmd_id}).get("command") or {}).get("status")
        if st == "running":
            break
        if st in ("done", "error", "canceled") or time.monotonic() > deadline:
            raise RuntimeError(f"命令未進入 running（{st}）")
        time.sleep(0.005)
    ms, r = rpc.timed("command.cancel", {"cmd_id": cmd_id})
    if "ok" not in r:
        raise RuntimeError(f"command.cancel 回應格式無效：{r}")
    _wait_state(rpc, "READY")
    deadline = time.monotonic() + 15
    while not (rpc.call("command.get", {"cmd_id": cmd_id}).get("command") or {}).get("done_at"):
        if time.monotonic() > deadline:
            raise RuntimeError("被取消的在途命令未結束")
        time.sleep(0.02)
    return ms


def measure(args: argparse.Namespace, contract: dict[str, Any], log: Callable[[str], None]) -> dict[str, Any]:
    harness = _import_harness()
    rows = {str(e.get("event")): e for e in contract.get("events") or [] if isinstance(e, dict)}
    flood_cfg = contract.get("flood_load") or {}
    samples_req, repeats_req = int(contract.get("samples_per_run") or 0), int(contract.get("repeats") or 0)
    samples, repeats = args.samples or samples_req, args.repeats or repeats_req
    events = [e for e in (args.events.split(",") if args.events else EXTERNAL_EVENTS)]
    for e in events:
        if e not in EXTERNAL_EVENTS or rows.get(e, {}).get("measure") != "external":
            raise SystemExit(f"事件 {e} 不是契約中的 external 量測事件")
    loads = args.loads.split(",") if args.loads else ["idle", "flood"]
    work = pathlib.Path(args.work_dir or tempfile.mkdtemp(prefix="evlat-"))
    daemon_cmd = [args.daemon] if not args.daemon.endswith(".py") else [sys.executable, args.daemon]
    cli_cmd = ([args.cli] if args.cli else None)
    report: dict[str, Any] = {
        "schema": SCHEMA, "method_revision": contract.get("method_revision"), "label": args.label,
        "daemon": args.daemon, "samples_per_run": samples, "samples_required": samples_req,
        "repeats": repeats, "repeats_required": repeats_req, "gate_rule": contract.get("gate_rule"),
        "diagnostic": samples < samples_req or repeats < repeats_req,
        "flood_load": flood_cfg, "freeze_status": contract.get("freeze_status"), "runs": [],
    }

    def start(root: pathlib.Path):
        d = harness.start_daemon(root, daemon=daemon_cmd, cli=cli_cmd, login=False)
        rpc = _Rpc(d.endpoint)
        _wait_state(rpc, "READY")
        return d, rpc

    for rep in range(1, repeats + 1):
        for load in loads:
            for event in events:
                limit = rows[event].get(f"max_ms_{load}")
                for attempt in range(args.void_retries + 1):
                    values: list[float] = []
                    errors: list[str] = []
                    before = host_load()
                    root = work / f"r{rep}-{load}-{event}-{attempt}"
                    if event == "stop_request":
                        setup_failures: list[str] = []
                        n = 0
                        while len(values) + len(errors) < samples:
                            n += 1
                            d = rpc = None
                            try:
                                try:                       # 準備（起 daemon 到 READY）不計時；失敗另記並重試，不算量測錯誤
                                    d = harness.start_daemon(root / f"s{n % 2}", daemon=daemon_cmd, cli=cli_cmd, login=False)
                                    rpc = _Rpc(d.endpoint)
                                    _wait_state(rpc, "READY")
                                except Exception as exc:  # noqa: BLE001
                                    setup_failures.append(f"setup #{n}: {type(exc).__name__}: {exc}")
                                    if len(setup_failures) > max(5, samples // 50):
                                        errors.append(f"準備失敗過多（{len(setup_failures)}）：{setup_failures[-1]}")
                                        break
                                    continue
                                tgt = d.targets["COM0"]
                                if load == "flood":
                                    with _Flood(tgt, int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"])):
                                        time.sleep(0.05)
                                        ms, r = rpc.timed("daemon.stop", {})
                                else:
                                    ms, r = rpc.timed("daemon.stop", {})
                                if r.get("ok") is not True:
                                    raise RuntimeError(f"daemon.stop 非 ok：{r}")
                                values.append(ms)
                            except Exception as exc:  # noqa: BLE001 — 逐筆記錄，不中斷整輪
                                errors.append(f"stop #{n}: {type(exc).__name__}: {exc}")
                                if len(errors) > 5:
                                    break
                            finally:
                                if rpc is not None:
                                    rpc.close()
                                if d is not None:
                                    d.stop()                       # 冪等：已停止者只回收 FakeTarget 與 run 目錄
                    else:
                        d, rpc = start(root)
                        try:
                            ctx = (_Flood(d.targets["COM0"], int(flood_cfg["bytes_per_s"]), int(flood_cfg["chunk_bytes"]))
                                   if load == "flood" else None)
                            if ctx is not None:
                                ctx.__enter__()
                            try:
                                fn = _sample_release if event == "release_request" else _sample_cancel
                                for n in range(samples):
                                    try:
                                        values.append(fn(rpc))
                                    except Exception as exc:  # noqa: BLE001
                                        errors.append(f"{event} #{n}: {type(exc).__name__}: {exc}")
                                        if len(errors) > 5:
                                            break
                            finally:
                                if ctx is not None:
                                    ctx.__exit__(None, None, None)
                        finally:
                            rpc.close()
                            d.stop()
                    run = judge_run(values, want=samples, limit=limit, load_before=before, load_after=host_load(),
                                    errors=errors)
                    run.update({"repeat": rep, "load": load, "event": event, "attempt": attempt})
                    if event == "stop_request":
                        run["setup_failures"] = setup_failures[:5]
                        run["setup_failure_count"] = len(setup_failures)
                    log(f"[{args.label}] r{rep} {load} {event}: {run['verdict']} max={run['max_ms']} p99={run['p99_ms']}"
                        f" n={run['count']} void={run['void']} err={len(errors)}")
                    if not args.keep_work:
                        shutil.rmtree(root, ignore_errors=True)
                    if run["void"] and attempt < args.void_retries:
                        report.setdefault("voided_runs", []).append(run)
                        continue
                    report["runs"].append(run)
                    break
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--daemon", required=True)
    ap.add_argument("--cli")
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int)
    ap.add_argument("--repeats", type=int)
    ap.add_argument("--events")
    ap.add_argument("--loads")
    ap.add_argument("--work-dir")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--void-retries", type=int, default=2)
    ap.add_argument("--l1-results", help="linux_l1 的 l1-results.json（OD-45 L1 結構 gate）")
    args = ap.parse_args(argv)
    contract = load_contract()
    report = measure(args, contract, lambda m: print(m, flush=True))
    l1 = None
    if args.l1_results and pathlib.Path(args.l1_results).is_file():
        l1 = json.loads(pathlib.Path(args.l1_results).read_text(encoding="utf-8"))
    report["l1_gate"] = l1_gate_status(contract, l1)
    report["verdict"], report["reasons"] = overall_verdict(report)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"verdict={report['verdict']} → {out}" + (f"（{'；'.join(report['reasons'])}）" if report["reasons"] else ""))
    return {"PASS": 0, "FAIL": 1}.get(report["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
