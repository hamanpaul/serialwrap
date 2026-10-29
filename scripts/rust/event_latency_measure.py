#!/usr/bin/env python3
"""OD-45：event_latency 外部端到端量測（`contracts/resource_limits.yaml` `event_latency`，method_revision 見契約）。

只量 `measure: external` 的三個事件（stop_request、command_cancel、release_request）；lease_expiry、drain_receipt
依 OD-45 只留 L1 結構測試（`l1_gate`），本工具不量。口徑為「RPC 送出 → RPC 回應」：client 先連上 daemon 端點，
於寫出請求前取 `time.monotonic_ns()`，讀到完整回應行後再取一次（不含連線建立；不讀 daemon 任何計時欄位、不改 WAL）。

負載：
- idle：DUT 不主動送資料。
- flood：DUT 以 `flood_load`（20000 B/s、每塊 256 bytes）持續送出 RX，涵蓋整輪量測（含觸發前的準備）。
  實際負載須經驗證（method_revision 2026-10-29.2；方法審閱 rejected 後修正）：送出失敗、送出端中途結束、
  實際速率低於 `bytes_per_s × FLOOD_MIN_RATE_RATIO`、或持續時間不足（release／cancel 每輪至少 `seconds`；
  stop 每樣本暖機 `stop_warmup_s`）→ 該輪 `load_problems`，不得當成有效 flood（證據不足）。

每輪（repeat × load × event）`samples_per_run` 筆；每輪樣本最大值 ≤ `max_ms_<load>` 才通過（`gate_rule:
every_run_max_le_max_ms`，p99 只作診斷）。有效樣本已超限即 FAIL，不因同輪的錯誤、缺樣或負載問題降級（明確作廢的輪次除外）。
量測期間以 `_HostMonitor` 每秒取樣 loadavg（含前後），峰值 `> CPU 數` 的輪次作廢並重跑（最多 `--void-retries` 次），
重跑仍作廢即該輪 INSUFFICIENT_EVIDENCE；監測資料缺失（取不到 loadavg 或 CPU 數）同為證據不足。

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
FLOOD_MIN_RATE_RATIO = 0.9          # 實際平均速率須 ≥ bytes_per_s 的 90%（送出端排程抖動容許值）


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


class _HostMonitor:
    """量測期間每 `interval` 秒取樣一次 host_load（方法審閱 [Medium]：作廢規則不得只看輪次兩端）。"""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while True:
            self.samples.append(host_load())
            if self._stop.wait(self.interval):
                return

    def __enter__(self) -> "_HostMonitor":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _sha256_file(path: str) -> str | None:
    import hashlib

    h = hashlib.sha256()                       # 分塊 update（`hashlib.file_digest` 需 Python 3.11+；sol P5-2472f78-1）
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def report_identity(daemon: str, *, run_id: str, build_report: str | None = None,
                    artifact_source_commit: str | None = None, artifact_build_run_id: str | None = None,
                    artifact_sha256: str | None = None) -> dict[str, Any]:
    """sol P5-3084388-2：量測證據的受測身分——daemon binary sha256、artifact 建置來源 commit、量測工具所在 commit
    （與工作樹是否乾淨）、本工具與契約檔的 sha256、method_revision、run ID。daemon 只記檔名（policy R-21）。

    artifact 建置來源不取工具工作樹 HEAD：以 `build_report`（建置該 artifact 的 gate `report.json`，其
    `identity.daemon_sha256` 須等於受測 binary 的 sha256）取得 `git_revision`；或由建置與量測同一 run 的 gate
    明示 `artifact_source_commit`／`artifact_build_run_id`，此時須一併給出建置成功當下記錄的 `artifact_sha256`，且須等於
    受測 binary 的 sha256（方法審閱 [High]：建置失敗留下的舊 binary 不得被綁到目前 HEAD）。兩者皆無或 sha256 不符時
    `artifact_source_commit` 為 None。"""
    import subprocess

    def _git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    dirty = _git("status", "--porcelain", "--untracked-files=no")
    daemon_sha = _sha256_file(daemon)
    mismatch = None
    if build_report:
        try:
            rep = json.loads(pathlib.Path(build_report).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep = {}
        built_sha = (rep.get("identity") or {}).get("daemon_sha256")
        if daemon_sha and built_sha == daemon_sha and rep.get("git_revision"):
            artifact_source_commit, artifact_build_run_id = rep["git_revision"], rep.get("run_id")
        else:
            artifact_source_commit = artifact_build_run_id = None
            mismatch = f"建置紀錄 daemon_sha256={built_sha} 與受測 binary {daemon_sha} 不符或缺 git_revision"
    elif artifact_source_commit or artifact_build_run_id:
        if not artifact_sha256 or not daemon_sha or artifact_sha256 != daemon_sha:
            mismatch = (f"gate 明示的建置來源未附建置成功當下的 artifact sha256，或與受測 binary 不符"
                        f"（build {artifact_sha256}、受測 {daemon_sha}）")
            artifact_source_commit = artifact_build_run_id = None
    return {
        "artifact_source_commit": artifact_source_commit,
        "artifact_build_run_id": artifact_build_run_id,
        "artifact_build_mismatch": mismatch,
        "run_id": run_id,
        "daemon": pathlib.Path(daemon).name,
        "daemon_sha256": daemon_sha,
        "source_commit": _git("rev-parse", "HEAD"),           # 量測工具所在 commit
        "source_tree_dirty": bool(dirty),
        "tool_sha256": _sha256_file(__file__),
        "contract_sha256": _sha256_file(str(LIMITS)),
        "method_revision": load_contract().get("method_revision"),
    }


def judge_run(samples: list[float], *, want: int, limit: float | None, load_before: dict, load_after: dict,
              errors: list[str], load_problems: list[str] | tuple[str, ...] = (),
              host_samples: list[dict] | None = None) -> dict[str, Any]:
    """單輪判定（純函式）。

    - 作廢：量測期間（`host_samples`＋前後）loadavg 峰值 > CPU 數 → void，該輪 INSUFFICIENT_EVIDENCE（不判 FAIL）。
    - 未作廢且上限有效時，有效樣本最大值 > 上限 → FAIL（方法審閱 [Medium]：不因錯誤、缺樣或負載問題降級）。
    - 其餘：錯誤、樣本數不足、負載無效（`load_problems`）、host 監測資料缺失、上限缺漏 → INSUFFICIENT_EVIDENCE。
    """
    summary = summarize(samples)
    seen = [load_before, *(host_samples or []), load_after]
    cpus = [x.get("cpu_count") for x in seen if isinstance(x.get("cpu_count"), int)]
    loads = [x.get("loadavg_1min") for x in seen if isinstance(x.get("loadavg_1min"), (int, float))]
    cpu = cpus[-1] if cpus else None
    peak = max(loads) if loads else None
    monitor_missing = cpu is None or peak is None
    void = not monitor_missing and peak > cpu
    has_limit = isinstance(limit, (int, float))
    if void:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif has_limit and summary["count"] and summary["max_ms"] > limit:
        verdict = "FAIL"
    elif errors or summary["count"] != want or load_problems or monitor_missing or not has_limit:
        verdict = "INSUFFICIENT_EVIDENCE"
    else:
        verdict = "PASS"
    return {**summary, "limit_ms": limit, "void": void, "verdict": verdict, "errors": errors[:5],
            "load_problems": list(load_problems)[:5], "host_monitor_missing": monitor_missing,
            "host_load": {"before": load_before, "after": load_after, "samples": len(host_samples or []),
                          "peak_loadavg_1min": peak, "cpu_count": cpu}}


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
    ident = report.get("identity") or {}
    if not ident.get("daemon_sha256") or not ident.get("source_commit"):
        reasons.append("受測身分不全（daemon sha256／來源 commit），量測證據無法追溯")
    elif not ident.get("artifact_source_commit"):
        reasons.append("受測 artifact 的建置來源未綁定（需 --artifact-build-report 或 gate 同 run 建置）："
                       f"{ident.get('artifact_build_mismatch') or '未提供'}")
    elif ident.get("source_tree_dirty"):
        reasons.append(f"來源工作樹不乾淨（{ident.get('source_commit')}），量測證據無法對應 commit")
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
    """DUT 端持續送 RX：`bytes_per_s`、每塊 `chunk_bytes`（以時間表補償，維持平均速率）。

    方法審閱 [High]：送出失敗不得被吞掉——逐次計數 `emit_errors`（保留第一筆），並記錄起訖時刻與送出端在結束前
    是否仍存活；`stats()` 給 `flood_problems()` 驗證實際負載。送出失敗時以排程速率退避，避免空轉。"""

    def __init__(self, target: Any, bytes_per_s: int, chunk_bytes: int) -> None:
        self.target, self.rate, self.chunk = target, bytes_per_s, chunk_bytes
        self.stop_evt = threading.Event()
        self.sent = 0
        self.emit_errors = 0
        self.first_error: str | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.alive_at_exit: bool | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        start = time.monotonic()
        n = 0
        while not self.stop_evt.is_set():
            body = (b"LATFLOOD%08d_" % n).ljust(self.chunk - 2, b"x")[: self.chunk - 2] + b"\r\n"
            try:
                self.target.emit(body)
                self.sent += len(body)
            except (OSError, RuntimeError) as exc:
                self.emit_errors += 1
                if self.first_error is None:
                    self.first_error = f"{type(exc).__name__}: {exc}"
            n += 1
            due = start + n * self.chunk / self.rate
            delay = due - time.monotonic()
            if delay > 0:
                self.stop_evt.wait(delay)

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.stopped_at or time.monotonic()) - self.started_at

    def stats(self) -> dict[str, Any]:
        seconds = self.elapsed()
        return {"sent": self.sent, "seconds": round(seconds, 3),
                "rate_bytes_per_s": round(self.sent / seconds, 1) if seconds > 0 else 0.0,
                "emit_errors": self.emit_errors, "first_error": self.first_error,
                "thread_alive": bool(self.alive_at_exit if self.alive_at_exit is not None else self.thread.is_alive())}

    def __enter__(self) -> "_Flood":
        self.started_at = time.monotonic()
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.alive_at_exit = self.thread.is_alive()
        self.stop_evt.set()
        self.thread.join(timeout=5)
        self.stopped_at = time.monotonic()


def flood_problems(stats: dict[str, Any], *, bytes_per_s: float, min_seconds: float) -> list[str]:
    """flood 實際負載驗證（純函式；方法審閱 [High]）：送出失敗、送出端中途結束、實際平均速率低於
    `bytes_per_s × FLOOD_MIN_RATE_RATIO`、持續時間短於 `min_seconds` → 回傳問題（空串列即有效 flood）。"""
    problems: list[str] = []
    seconds = float(stats.get("seconds") or 0.0)
    sent = int(stats.get("sent") or 0)
    if stats.get("emit_errors"):
        problems.append(f"flood 送出失敗 {stats['emit_errors']} 次（{stats.get('first_error')}）")
    if not stats.get("thread_alive"):
        problems.append("flood 送出端在量測結束前已停止")
    if seconds + 1e-9 < min_seconds:
        problems.append(f"flood 持續 {seconds:.3f}s < 要求 {min_seconds}s")
    rate = sent / seconds if seconds > 0 else 0.0
    if rate < bytes_per_s * FLOOD_MIN_RATE_RATIO:
        problems.append(f"flood 實際速率 {rate:.0f} B/s < {bytes_per_s * FLOOD_MIN_RATE_RATIO:.0f} B/s（送出 {sent} bytes）")
    return problems


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


def _sample_release(rpc: _Rpc) -> tuple[float, str | None]:
    """回 (release 耗時, 之後重新 attach 的準備錯誤或 None)。release 成功後的重新 attach 不計時；其失敗另記為準備錯誤，
    不得遺失已取得的 release 耗時（方法審閱 [Medium]）。release 本身非 ok 時拋例外（不是有效樣本）。"""
    ms, r = rpc.timed("device.release", {"selector": "COM0", "source": "agent:lat"})
    if r.get("ok") is not True:
        raise RuntimeError(f"device.release 非 ok：{r}")
    try:
        r = rpc.call("device.attach", {"selector": "COM0"})
        if r.get("ok") is not True:
            raise RuntimeError(f"device.attach 非 ok：{r}")
        _wait_state(rpc, "READY")
    except Exception as exc:  # noqa: BLE001 — 準備錯誤，不吞掉 release 耗時
        return ms, f"{type(exc).__name__}: {exc}"
    return ms, None


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
    # sol P5-3084388-1：取消須成功（ok:true、status canceled）才是有效樣本；命令已自然完成而被拒（CMD_NOT_CANCELABLE）
    # 或最終不是 canceled 者記為錯誤（該輪 INSUFFICIENT_EVIDENCE），不得把拒絕回應的耗時算成取消延遲。
    if r.get("ok") is not True or r.get("status") != "canceled":
        raise RuntimeError(f"command.cancel 未成功取消在途命令：{r}")
    _wait_state(rpc, "READY")
    deadline = time.monotonic() + 15
    while True:
        cmd = rpc.call("command.get", {"cmd_id": cmd_id}).get("command") or {}
        if cmd.get("done_at"):
            break
        if time.monotonic() > deadline:
            raise RuntimeError("被取消的在途命令未結束")
        time.sleep(0.02)
    if cmd.get("status") != "canceled":
        raise RuntimeError(f"取消後命令最終狀態不是 canceled：{cmd.get('status')}")
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

    rate, chunk = int(flood_cfg.get("bytes_per_s") or 0), int(flood_cfg.get("chunk_bytes") or 0)
    run_seconds = float(flood_cfg.get("seconds") or 0)          # release／cancel：每輪 flood 至少持續秒數
    warmup = float(flood_cfg.get("stop_warmup_s") or 0)          # stop：每樣本（新 daemon）送出前的 flood 暖機
    if "flood" in loads and (rate <= 0 or chunk <= 2 or run_seconds <= 0 or warmup <= 0):
        raise SystemExit("契約 flood_load 缺 bytes_per_s／chunk_bytes／seconds／stop_warmup_s")

    for rep in range(1, repeats + 1):
        for load in loads:
            for event in events:
                limit = rows[event].get(f"max_ms_{load}")
                for attempt in range(args.void_retries + 1):
                    values: list[float] = []
                    errors: list[str] = []
                    load_problems: list[str] = []
                    flood_stats: list[dict[str, Any]] = []
                    before = host_load()
                    monitor = _HostMonitor()
                    monitor.__enter__()
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
                                    fl = _Flood(tgt, rate, chunk)
                                    with fl:
                                        fl.stop_evt.wait(warmup)  # 暖機：daemon 已在持續 RX 下才送 stop
                                        ms, r = rpc.timed("daemon.stop", {})
                                    st = fl.stats()
                                    flood_stats.append(st)
                                    bad = flood_problems(st, bytes_per_s=rate, min_seconds=warmup)
                                    if bad:
                                        load_problems.append(f"stop #{n}：{'；'.join(bad)}")
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
                            fl = _Flood(d.targets["COM0"], rate, chunk) if load == "flood" else None
                            if fl is not None:
                                fl.__enter__()
                            try:
                                for n in range(samples):
                                    try:
                                        if event == "release_request":
                                            ms, post = _sample_release(rpc)
                                            values.append(ms)
                                            if post:
                                                errors.append(f"{event} #{n}（release 已計時）準備失敗：{post}")
                                        else:
                                            values.append(_sample_cancel(rpc))
                                    except Exception as exc:  # noqa: BLE001
                                        errors.append(f"{event} #{n}: {type(exc).__name__}: {exc}")
                                    if len(errors) > 5:
                                        break
                                if fl is not None and fl.elapsed() < run_seconds:
                                    fl.stop_evt.wait(run_seconds - fl.elapsed())   # 每輪 flood 至少持續 seconds
                            finally:
                                if fl is not None:
                                    fl.__exit__(None, None, None)
                                    st = fl.stats()
                                    flood_stats.append(st)
                                    load_problems.extend(flood_problems(st, bytes_per_s=rate, min_seconds=run_seconds))
                        finally:
                            rpc.close()
                            d.stop()
                    monitor.__exit__(None, None, None)
                    run = judge_run(values, want=samples, limit=limit, load_before=before, load_after=host_load(),
                                    errors=errors, load_problems=load_problems, host_samples=monitor.samples)
                    run.update({"repeat": rep, "load": load, "event": event, "attempt": attempt})
                    if flood_stats:
                        run["flood"] = {
                            "periods": len(flood_stats),
                            "sent_bytes": sum(x["sent"] for x in flood_stats),
                            "seconds": round(sum(x["seconds"] for x in flood_stats), 3),
                            "min_rate_bytes_per_s": min(x["rate_bytes_per_s"] for x in flood_stats),
                            "emit_errors": sum(x["emit_errors"] for x in flood_stats),
                            "invalid_periods": len(load_problems),
                        }
                    if event == "stop_request":
                        run["setup_failures"] = setup_failures[:5]
                        run["setup_failure_count"] = len(setup_failures)
                    log(f"[{args.label}] r{rep} {load} {event}: {run['verdict']} max={run['max_ms']} p99={run['p99_ms']}"
                        f" n={run['count']} void={run['void']} err={len(errors)} load_problems={len(load_problems)}")
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
    ap.add_argument("--run-id", help="量測 run ID（預設依時間產生）")
    ap.add_argument("--artifact-build-report", help="建置受測 artifact 的 gate report.json（核對 daemon_sha256 後取 git_revision）")
    ap.add_argument("--artifact-source-commit", help="gate 同一 run 建置 artifact 時的來源 commit")
    ap.add_argument("--artifact-build-run-id", help="gate 同一 run 建置 artifact 時的 run ID")
    ap.add_argument("--artifact-sha256", help="gate 建置成功當下記錄的受測 daemon sha256（明示來源時必填，須與受測 binary 相符）")
    args = ap.parse_args(argv)
    contract = load_contract()
    report = measure(args, contract, lambda m: print(m, flush=True))
    l1 = None
    if args.l1_results and pathlib.Path(args.l1_results).is_file():
        l1 = json.loads(pathlib.Path(args.l1_results).read_text(encoding="utf-8"))
    report["l1_gate"] = l1_gate_status(contract, l1)
    report["identity"] = report_identity(
        args.daemon, run_id=args.run_id or time.strftime("evlat-%Y%m%dT%H%M%SZ", time.gmtime()),
        build_report=args.artifact_build_report, artifact_source_commit=args.artifact_source_commit,
        artifact_build_run_id=args.artifact_build_run_id, artifact_sha256=args.artifact_sha256)
    report["daemon"] = report["identity"]["daemon"]
    report["verdict"], report["reasons"] = overall_verdict(report)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"verdict={report['verdict']} → {out}" + (f"（{'；'.join(report['reasons'])}）" if report["reasons"] else ""))
    return {"PASS": 0, "FAIL": 1}.get(report["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
