#!/usr/bin/env python3
"""Stage 0 3,000 ms drain 量測腳本（SC-04、規格 §5.2.5）：量測 release 的 Phase 1→回條耗時分佈與逾時次數。

entry: python3 scripts/stage0/drain_timing.py --device <by-id 路徑|COMx|alias> --iterations 50 --out <json>
       [--serialwrap <exe>] [--unplug-prompt] [--timeout-ms 3000] [--settle-timeout-s 60]
       Windows：py -3.12 scripts/stage0/drain_timing.py --device COM3 --iterations 50 --out <json>
       （Windows 端需與 Linux 相同方式 staging sw_core/ 與 realhw/；或於 Windows 端 clone repo 後執行。）
revision: 本檔 git blob sha1（執行時寫入輸出 JSON 的 tool_revision；等同 git rev-parse HEAD:scripts/stage0/drain_timing.py）
expected: 每輪流程為「等 READY → 送出持續 TX 的長命令並確認仍在途 → 量測 device release → device attach → 等回 READY」。
  輸出每次 release 的 daemon 端耗時（外部 first-byte 時間扣除 CLI 基準值 cli_overhead_ms）、ok 回應、drain_stalled 與
  DRAIN_STALLED 逾時次數，以及 p50／p95／max。只有達到 timeout_ms 的 DRAIN_STALLED 算逾時樣本；
  SESSION_RECOVERING 或早於期限的 DRAIN_STALLED（例如 handle close 失敗）標為 invalid、不進分佈；
  有 invalid 或準備步驟（READY、在途、attach 回 READY）失敗時 exit 非 0。逾時門檻 3000 ms 以 SC-04 為準，本腳本只量測不判定 PASS／FAIL。
  --unplug-prompt 時於第 k 次迭代「TX 進行中」提示拔除 USB，記錄 write 不返回情境下的 release 回應與錯誤碼。
owner: 擁有者實機（Linux 與 Windows 實體 USB-UART 依序；bench 需獨占該 COM／by-id 裝置）
evidence_dir: ~/b-log/rust-refactor/p1/drain-timing/<YYMMDD-HHMMSS>/（--out 指向該目錄下的 JSON）
rollback: 每次迭代後以 `serialwrap device attach --selector <COM>` 收回裝置；腳本中斷（Ctrl-C）或例外時會在 finally 中嘗試收回並寫出部分結果。
  若收回失敗，手動執行 `serialwrap device attach` 並以 `serialwrap session list` 確認 state 為 READY。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_MS = 3000
UNPLUG_AT_ITERATION = 1  # --unplug-prompt 時於此（0-based）迭代的 TX 進行中提示拔線
SESSION_RECOVERING = "SESSION_RECOVERING"
DRAIN_STALLED = "DRAIN_STALLED"
TERMINAL_CMD_STATES = ("done", "error", "timeout")
LONG_TX_BYTES = 4000  # 接近 CMD_WARN_BYTES（4 KB）：UART 寫入需數百毫秒，release 會落在持續 TX 中
LONG_TX_HEAD = "sleep 20; echo X; "


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _cli_timed(exe: str, *args: str, timeout: float = 30.0) -> tuple[dict[str, Any], dict[str, float | None]]:
    """呼叫 serialwrap CLI（JSON 輸出），同時量測 stdout 第一個位元組抵達時間與行程總時間（毫秒）。

    不以 subprocess.run 返回時間當回條時刻：那已含 CLI 行程退出與 pipe 收尾，會高估 drain。
    非 JSON 輸出以 _raw 保留，不吞錯。
    """
    t0 = time.perf_counter()
    proc = subprocess.Popen([exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first: list[float] = []
    chunks: list[bytes] = []

    def _reader() -> None:
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            if not first:
                first.append(time.perf_counter())
            chunks.append(chunk)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    reader.join(10)
    t_end = time.perf_counter()
    err = proc.stderr.read() if proc.stderr is not None else b""
    out = b"".join(chunks).decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        data = {"_raw": out}
    data["_rc"] = proc.returncode
    data["_stderr"] = err.decode("utf-8", errors="replace").strip()
    timing = {
        "first_byte_ms": round((first[0] - t0) * 1000.0, 3) if first else None,
        "total_ms": round((t_end - t0) * 1000.0, 3),
    }
    return data, timing


def _cli(exe: str, *args: str, timeout: float = 30.0) -> dict[str, Any]:
    return _cli_timed(exe, *args, timeout=timeout)[0]


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return round(ordered[k], 3)


def resolve_com(exe: str, device: str) -> str:
    """把 --device（by-id 路徑、COMx 或 alias）解析為 session 的 COM selector。

    release／attach 的 selector 只認 session_id、COMx 與 alias（SessionManager.get_session），
    直接傳 by-id 會每次 SESSION_NOT_FOUND，故須先經 session list 解析。找不到時 raise SystemExit。
    """
    listing = _cli(exe, "session", "list")
    for sess in listing.get("sessions") or []:
        if device in (sess.get("device_by_id"), sess.get("com"), sess.get("alias"), sess.get("session_id")):
            return str(sess.get("com") or device)
    raise SystemExit(f"無法解析 --device={device!r} 為 session（session list 無對應項）")


def _session_for(exe: str, com: str) -> dict[str, Any]:
    listing = _cli(exe, "session", "list")
    for sess in listing.get("sessions") or []:
        if sess.get("com") == com:
            return sess
    return {}


def wait_for_state(exe: str, com: str, want: tuple[str, ...], *, timeout_s: float,
                   poll_s: float = 0.25) -> tuple[bool, float, str | None]:
    """輪詢 session list 直到 state 落在 want；回傳 (是否達成, 等待毫秒, 最後 state)。"""
    t0 = time.perf_counter()
    last: str | None = None
    while time.perf_counter() - t0 < timeout_s:
        last = _session_for(exe, com).get("state")
        if last in want:
            return True, round((time.perf_counter() - t0) * 1000.0, 3), last
        time.sleep(poll_s)
    return False, round((time.perf_counter() - t0) * 1000.0, 3), last


def measure_cli_baseline(exe: str, com: str, n: int = 10) -> float:
    """CLI 行程啟動＋一次唯讀 RPC 的 first-byte 中位數（毫秒），用來扣除量測外的固定成本。"""
    samples = []
    for _ in range(n):
        _, timing = _cli_timed(exe, "session", "list")
        if timing["first_byte_ms"] is not None:
            samples.append(float(timing["first_byte_ms"]))
    return round(statistics.median(samples), 3) if samples else 0.0


def start_inflight_tx(exe: str, com: str) -> dict[str, Any]:
    """送出接近 4 KB 的長命令讓 UART 持續寫入，回傳 submit 回應與在途狀態。"""
    payload = LONG_TX_HEAD + "#" * (LONG_TX_BYTES - len(LONG_TX_HEAD))
    sub = _cli(exe, "cmd", "submit", "--selector", com, "--cmd", payload, "--cmd-timeout", "30")
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return {"ok": False, "cmd_id": None, "in_flight": False, "submit": sub}
    time.sleep(0.1)  # 讓寫入進行中
    status = (_cli(exe, "cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}).get("status")
    in_flight = status is not None and status not in TERMINAL_CMD_STATES
    return {"ok": in_flight, "cmd_id": cmd_id, "in_flight": in_flight, "status_before_release": status}


def measure_once(exe: str, com: str, timeout_ms: int, cli_overhead_ms: float,
                 *, unplug: bool = False) -> dict[str, Any]:
    """單輪：READY → 在途 TX → （可選拔線提示）→ release 量測 → attach → 等回 READY。

    回傳樣本 dict；準備步驟失敗時 valid=False 並附 invalid_reason（呼叫端決定是否停止）。
    """
    ready, ready_ms, state = wait_for_state(exe, com, ("READY",), timeout_s=60.0)
    sample: dict[str, Any] = {"ts": _now(), "ready_before": ready, "settle_before_ms": ready_ms,
                              "state_before": state, "valid": False}
    if not ready:
        sample["invalid_reason"] = f"release 前未回 READY（state={state!r}）"
        return sample
    tx = start_inflight_tx(exe, com)
    sample["tx"] = tx
    if not tx["in_flight"]:
        sample["invalid_reason"] = "長命令未在途（submit 失敗或已終態），非持續 TX 下的 release"
        return sample
    unplug_note = None
    if unplug:
        input("TX 進行中：請現在拔除 USB-UART 線，再按 Enter 送出 release 量測 ")
        unplug_note = _now()
    resp, timing = _cli_timed(exe, "device", "release", "--selector", com,
                              "--source", "agent:drain-timing", "--reason", "drain timing",
                              timeout=max(30.0, timeout_ms / 1000 * 4))
    first_byte = timing["first_byte_ms"]
    external_ms = first_byte if first_byte is not None else timing["total_ms"]
    daemon_ms = round(max(0.0, external_ms - cli_overhead_ms), 3)
    error_code = resp.get("error_code")
    stalled = bool(resp.get("drain_stalled"))
    timed_out = error_code == DRAIN_STALLED and stalled and daemon_ms >= timeout_ms
    sample.update({
        "external_first_byte_ms": first_byte,
        "external_total_ms": timing["total_ms"],
        "cli_overhead_ms": cli_overhead_ms,
        "daemon_drain_ms": daemon_ms,
        "ok": bool(resp.get("ok")),
        "error_code": error_code,
        "drain_stalled": stalled,
        "timed_out": timed_out,
        "unplug_prompted_at": unplug_note,
        "release_rc": resp.get("_rc"),
    })
    if not resp.get("ok") and not timed_out:
        sample["invalid_reason"] = (
            f"release 未達可量測的 {timeout_ms} ms DRAIN_STALLED 逾時"
            f"（error_code={error_code!r}, daemon_drain_ms={daemon_ms}）"
        )
        sample["valid"] = False
        # 收尾仍需嘗試 attach，避免留下 RELEASED
        _cli(exe, "device", "attach", "--selector", com)
        return sample
    sample["valid"] = True
    attach = _cli(exe, "device", "attach", "--selector", com)
    back, settle_ms, back_state = wait_for_state(exe, com, ("READY", "ATTACHED"), timeout_s=60.0)
    sample.update({"reattach_ok": bool(attach.get("ok")) and back, "settle_after_ms": settle_ms,
                   "state_after": back_state})
    if not sample["reattach_ok"]:
        sample["invalid_reason"] = f"device attach 後未回 READY／ATTACHED（state={back_state!r}）"
    return sample


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [s for s in samples if s.get("valid") and s.get("daemon_drain_ms") is not None]
    times = [float(s["daemon_drain_ms"]) for s in valid]
    return {
        "n": len(samples),
        "n_valid": len(valid),
        "n_invalid": sum(1 for s in samples if not s.get("valid")),
        "p50_ms": _percentile(times, 50),
        "p95_ms": _percentile(times, 95),
        "max_ms": max(times) if times else None,
        "mean_ms": round(statistics.fmean(times), 3) if times else None,
        "timeouts": sum(1 for s in valid if s.get("timed_out")),
        "reattach_failures": sum(1 for s in samples if s.get("valid") and not s.get("reattach_ok")),
    }


def run(args: argparse.Namespace) -> int:
    exe = args.serialwrap
    samples: list[dict[str, Any]] = []
    unplug_obs: dict[str, Any] | None = None
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    com = ""
    cli_overhead_ms = 0.0
    try:
        com = resolve_com(exe, args.device)
        cli_overhead_ms = measure_cli_baseline(exe, com)
        for i in range(args.iterations):
            unplug = bool(args.unplug_prompt and i == UNPLUG_AT_ITERATION)
            if unplug:
                unplug_obs = {"iteration": i, "prompted_at": _now()}
            sample = measure_once(exe, com, args.timeout_ms, cli_overhead_ms, unplug=unplug)
            sample["iteration"] = i
            samples.append(sample)
            if unplug_obs is not None and "result" not in unplug_obs:
                unplug_obs["result"] = sample
            if not sample.get("valid") or not sample.get("reattach_ok", True):
                break  # 前置條件失敗即停止，不把空轉或非穩態樣本計入分佈
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — 部分結果必須落盤後才 re-raise
        samples.append({"valid": False, "invalid_reason": f"{type(exc).__name__}: {exc}", "iteration": len(samples)})
        raise
    finally:
        if com:
            _cli(exe, "device", "attach", "--selector", com)  # 收回裝置，不留 RELEASED
        invalid = sum(1 for s in samples if not s.get("valid"))
        out = {
            "tool_revision": _git_blob_sha1(Path(__file__).resolve()),
            "run_at": _now(),
            "env": {"os": platform.system().lower(), "python": platform.python_version(),
                    "pid": os.getpid()},
            "device": args.device,
            "resolved_com": com or None,
            "timeout_ms": args.timeout_ms,
            "cli_overhead_ms": cli_overhead_ms,
            "summary": summarize(samples),
            "samples": samples,
            "unplug": unplug_obs,
            "complete": len(samples) == args.iterations and invalid == 0,
        }
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(out["summary"], ensure_ascii=False, sort_keys=True))
    return 0 if out["complete"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SC-04 3,000 ms drain 量測")
    p.add_argument("--device", required=True, help="by-id 路徑、COMx 或 alias（會先解析為 session COM）")
    p.add_argument("--iterations", type=int, default=50)
    p.add_argument("--out", required=True, help="輸出 JSON 路徑")
    p.add_argument("--serialwrap", default="serialwrap", help="serialwrap CLI 路徑（建議 pinned 絕對路徑）")
    p.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    p.add_argument("--unplug-prompt", action="store_true",
                   help="於 TX 進行中互動提示拔線，記錄 write 不返回時的 release 回應")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))
