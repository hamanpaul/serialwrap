"""長跑唯讀 observer：每個取樣週期只呼叫 `daemon status` 與 `session list`。

絕不呼叫互動狀態類或任何有副作用的命令。Windows 另可依 `excl_dir` 週期性執行
`winexcl.second_open_probe`（本身只讀 RPC＋零 I/O 的獨占開啟探測），結果寫入 `probes.ndjson`。
Linux 不做獨占探測、不建立 `probes.ndjson`。

觀測器本身的失敗（單次 RPC、單次探測、列舉程序）一律記為該筆的 `ok:false`／`inconclusive`／`null`，
不中斷長跑；既有 observer 紀錄檔不覆寫（非空即拒絕）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from realhw import procobs

from .winexcl import ProbeLock, second_open_probe

# 與 sw_core.multi_open._serialwrapd_status 的四條規則等價（stdlib 重寫，不 import 產品碼），
# 另加 Windows 原生 `serialwrapd.exe` 名稱。
_SERIALWRAPD_BASENAMES = frozenset({"serialwrapd", "serialwrapd.exe"})
_PATH_SPLIT = re.compile(r"[\\/]")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _platform_name() -> str:
    return "windows" if sys.platform.startswith("win") else "linux"


def _basename(token: str) -> str:
    return _PATH_SPLIT.split(token)[-1].lower()


def is_serialwrapd_argv(tokens: list[str]) -> bool:
    """判定命令列是否為 serialwrapd daemon（直接執行、console_script 路徑、薄 shim、-m 形式）。"""
    if not tokens:
        return False
    # 1. argv0 的 basename 為 serialwrapd（含 Windows .exe）
    if _basename(tokens[0]) in _SERIALWRAPD_BASENAMES:
        return True
    # 2. 任一參數的 basename 為 serialwrapd.py（薄 shim）。與產品的後綴比對不同：以 basename 精確比對，
    #    避免 `pytest tests/test_serialwrapd.py` 之類無關程序被誤判為第二個 daemon（誤判會讓健康 run 直接 FAIL）
    if any(_basename(token) == "serialwrapd.py" for token in tokens):
        return True
    # 3. 路徑型參數（含 / 或 \）的 basename 為 serialwrapd（python /venv/bin/serialwrapd）
    for token in tokens[1:]:
        if ("/" in token or "\\" in token) and _basename(token) in _SERIALWRAPD_BASENAMES:
            return True
    # 4. python -m sw_core.daemon／-m serialwrapd（-m 與模組名必須相鄰）
    return any(a == "-m" and b in ("sw_core.daemon", "serialwrapd") for a, b in zip(tokens, tokens[1:]))


def _all_serialwrapd_pids() -> list[int] | None:
    """列出本機所有 serialwrapd 程序（不含觀測者自身）；無法列舉回 None。"""
    rows = procobs.list_processes()
    if rows is None:
        return None
    own = os.getpid()
    found: list[int] = []
    for row in rows:
        pid = int(row.get("pid") or 0)
        if pid <= 0 or pid == own:
            continue
        # procobs 以空白串接 argv；路徑含空白時 argv0 切分可能不精確（本工具鏈的 daemon 路徑無此形式）
        tokens = str(row.get("cmdline") or "").split()
        name = str(row.get("name") or "").lower()
        if name in _SERIALWRAPD_BASENAMES or is_serialwrapd_argv(tokens):
            found.append(pid)
    return sorted(found)


def _session_rows(sw: Any) -> list[dict[str, Any]]:
    response = sw.run("session", "list")
    rows = response.get("sessions") if isinstance(response, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _row_com(row: dict[str, Any]) -> str | None:
    return row.get("selector") or row.get("com")


class _ProcScan:
    """多 daemon 程序掃描的節流快取。

    排除判定一律以「目前 daemon pid」為準；RPC 失敗時沿用最後一次成功取得的 pid，
    避免把本 daemon 自身誤報成「其他 serialwrapd」。
    """

    def __init__(self, period_s: float = 10.0) -> None:
        self._period = float(period_s)
        self._last_at: float | None = None
        self._all: list[int] | None = None
        self._last_good_pid: int | None = None

    @property
    def last_good_pid(self) -> int | None:
        return self._last_good_pid

    def note_daemon_pid(self, pid: int | None) -> None:
        if pid is not None:
            self._last_good_pid = int(pid)

    def scan(self, daemon_pid: int | None) -> list[int] | None:
        now = time.monotonic()
        if self._last_at is None or now - self._last_at >= self._period:
            self._all = _all_serialwrapd_pids()
            self._last_at = now
        if self._all is None:
            return None
        effective = daemon_pid if daemon_pid is not None else self._last_good_pid
        if effective is None:
            # 無法辨識哪一個是本 daemon：有其他 serialwrapd 時無法判定（證據不足），否則確定為空
            return None if self._all else []
        return [pid for pid in self._all if pid != effective]


def _holder_unknown(foreign: Any) -> bool | None:
    """由 `foreign_holders` 推導：任一持有者為 holder_unknown 即 True（診斷用，不作判定依據）。"""
    if not isinstance(foreign, dict):
        return None
    return any(isinstance(entry, dict) and entry.get("holder_unknown") is True for entry in foreign.values())


def _wal_healthy(status: dict[str, Any]) -> bool | None:
    wal = status.get("wal")
    if isinstance(wal, dict) and "healthy" in wal:
        return bool(wal["healthy"])
    return None


def sample(sw: Any, boards: list[str], *, procscan: _ProcScan | None = None) -> dict[str, Any]:
    """取一筆唯讀樣本；RPC 失敗時記 `daemon.ok=false`（附最後成功 pid 的存活判定）並續跑。"""
    procscan = procscan if procscan is not None else _ProcScan(period_s=0.0)
    row: dict[str, Any] = {"wall_ts": _now_iso(), "t_mono": time.monotonic()}
    daemon: dict[str, Any] = {"ok": False, "pid": None, "pid_alive": None, "version": None,
                              "multi_open": None, "foreign_holders": None, "holder_unknown": None,
                              "wal_healthy": None}
    try:
        status = sw.run("daemon", "status")
        if (not isinstance(status, dict) or status.get("ok") is False or status.get("_error")
                or status.get("_rc", 0) not in (0, None)):
            raise RuntimeError(str((status or {}).get("error_code") or "daemon status 失敗"))
        raw_pid = status.get("pid")
        pid = int(raw_pid) if raw_pid is not None else None
        foreign = status.get("foreign_holders")
        daemon = {"ok": True,
                  "pid": pid,
                  "pid_alive": True if pid is not None else None,
                  "version": status.get("version"),
                  "multi_open": status.get("multi_open"),
                  "foreign_holders": foreign if isinstance(foreign, dict) else None,
                  "holder_unknown": _holder_unknown(foreign),
                  "wal_healthy": _wal_healthy(status)}
        procscan.note_daemon_pid(pid)
    except Exception:  # noqa: BLE001 - 唯讀觀測不因單次 RPC 失敗停止
        last = procscan.last_good_pid
        daemon["pid_alive"] = procobs.pid_alive(last) if last is not None else None
    row["daemon"] = daemon

    sessions: dict[str, Any] = {com: None for com in boards}
    try:
        for session in _session_rows(sw):
            com = _row_com(session)
            if com in sessions:
                sessions[com] = session.get("state")
    except Exception:  # noqa: BLE001
        pass
    row["sessions"] = sessions

    try:
        other = procscan.scan(daemon.get("pid"))
    except Exception:  # noqa: BLE001
        other = None
    row["procscan"] = {"other_serialwrapd": other}
    return row


def header_record(*, supervision: dict[str, Any], platform: str,
                  device_binding: dict[str, str | None] | None = None) -> dict[str, Any]:
    """observer.ndjson 第一行。`supervision` 原樣保留擁有者提供的證據欄位。"""
    return {
        "kind": "header",
        "platform": platform,
        "supervision": dict(supervision),
        "device_binding": dict(device_binding) if device_binding is not None else None,
    }


def _device_binding(sw: Any, boards: list[str]) -> dict[str, str | None]:
    binding: dict[str, str | None] = {com: None for com in boards}
    try:
        for session in _session_rows(sw):
            com = _row_com(session)
            if com in binding:
                binding[com] = session.get("attached_real_path")
    except Exception:  # noqa: BLE001 - 取不到時 header 留 None，gate 會判 device_unbound
        pass
    return binding


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()


def run_observer(sw: Any, boards: list[str], *, interval_s: float, out: Path, stop: threading.Event,
                 duration_s: float | None, supervision: dict[str, Any] | None = None,
                 excl_dir: Path | None = None, excl_probe_interval_s: float | None = None) -> int:
    out = Path(out)
    if out.exists() and out.stat().st_size > 0:
        # 既有觀測紀錄是證據，不得被重新啟動的 observer 截斷
        raise FileExistsError(f"observer 輸出已存在且非空，拒絕覆寫：{out}")
    platform = _platform_name()
    binding = _device_binding(sw, boards)
    supervision = supervision or {"mode": "unknown", "auto_restart_disabled": None, "evidence": ""}
    out.parent.mkdir(parents=True, exist_ok=True)
    _append(out, header_record(supervision=supervision, platform=platform, device_binding=binding))

    procscan = _ProcScan(period_s=10.0)
    probe_enabled = platform == "windows" and excl_dir is not None and excl_probe_interval_s is not None
    probe_lock = ProbeLock(Path(excl_dir) / ".probe.lock") if probe_enabled else None
    probes_path = Path(excl_dir) / "probes.ndjson" if probe_enabled else None
    next_probe = 0.0

    started = time.monotonic()
    while not stop.is_set():
        now = time.monotonic()
        elapsed = now - started
        if duration_s is not None and elapsed >= duration_s:
            break
        _append(out, {"kind": "sample", **sample(sw, boards, procscan=procscan)})
        if probe_enabled and now >= next_probe:
            for com in boards:
                try:
                    record = second_open_probe(sw, com, expected_device=binding.get(com), lock=probe_lock)
                except Exception as exc:  # noqa: BLE001 - 單次探測失敗記 inconclusive，觀測照常續跑
                    record = {"wall_ts": _now_iso(), "expected_device": binding.get(com),
                              "verdict": "inconclusive", "reason": "rpc_error",
                              "error": type(exc).__name__}
                _append(probes_path, {"kind": "excl_probe", "com": com, **record})
            next_probe = now + float(excl_probe_interval_s)
        wait = float(interval_s)
        if duration_s is not None:
            wait = min(wait, max(0.0, duration_s - (time.monotonic() - started)))
        if probe_enabled:
            wait = min(wait, max(0.0, next_probe - time.monotonic()))
        stop.wait(timeout=max(0.0, wait))
    return 0


def _parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    unit = text[-1].lower()
    scale = {"s": 1.0, "m": 60.0, "h": 3600.0}
    if unit in scale:
        return float(text[:-1]) * scale[unit]
    return float(text)


def _load_supervision(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("--supervision-attest 必須是 JSON 物件")
    return {"mode": value.get("mode", "unknown"),
            "auto_restart_disabled": value.get("auto_restart_disabled"),
            "evidence": value.get("evidence", "")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="長跑唯讀 observer（只讀 daemon status／session list）")
    parser.add_argument("--serialwrap-exe", default="serialwrap")
    parser.add_argument("--boards", required=True, help="逗號分隔的受測 COM，例如 COM0,COM1")
    parser.add_argument("--interval-s", type=float, default=10.0)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--duration", default=None, help="例如 48h、90m、3600s；未給則持續到中斷")
    parser.add_argument("--supervision-attest", default=None, help="擁有者填寫的監管證據 JSON")
    parser.add_argument("--excl-dir", type=Path, default=None)
    parser.add_argument("--excl-probe-interval-s", type=float, default=None)
    args = parser.parse_args(argv)

    try:
        supervision = _load_supervision(args.supervision_attest)
    except (OSError, ValueError) as exc:
        print(f"supervision 證據檔無法使用：{exc}", file=sys.stderr)
        return 2

    from realhw.drivers import SwCli

    boards = [item.strip() for item in args.boards.split(",") if item.strip()]
    stop = threading.Event()

    def _on_signal(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)
    try:
        return run_observer(
            SwCli(args.serialwrap_exe), boards,
            interval_s=args.interval_s, out=args.out, stop=stop,
            duration_s=_parse_duration(args.duration),
            supervision=supervision,
            excl_dir=args.excl_dir, excl_probe_interval_s=args.excl_probe_interval_s,
        )
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
