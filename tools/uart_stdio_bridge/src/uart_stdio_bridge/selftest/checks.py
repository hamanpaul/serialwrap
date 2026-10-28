"""bridge 自測項目檢查器與實作（§15.4.3／Spike 4）。

包含自測項目與規格關鍵詞對照表：
- ST-01: 完整 byte 值域 雙向傳輸驗證（0～255 全值域往返、四個觀測串流全等與比較器驗證）
- ST-02: 多區塊 資料傳輸（4 MiB 隨機分塊寫入、連續性與 seq/offset 斷裂偵測）
- ST-03: 短讀 ／部分寫入行為（精確記錄每筆實際接受長度且串接無重複無遺失）
- ST-04: stderr 大量輸出隔離與日誌排空（DUT flood 50 MB 同時 1 MiB UART 往返）
- ST-05: EOF 偵測與世代保持（stdout 關閉後留下 dut_stdout_eof META 且 TX 仍可轉送）
- ST-06: 程序退出 與非預期離開 fail-stop（DUT 提前 exit 觸發 bridge exit 3 與 HARNESS_DUT_EXITED）
- ST-07: 故障控制 通道動作與回條記錄（Task 5 表中每個 action 的 requested/completed 稽核）
- ST-08: CRC 與 offset 驗證（正常結束證據執行 load_wire 與 bridge-manifest digest 檢驗）
- ST-09: 破壞證據 竄改拒絕判定（套用 Task 7 的 13 種竄改均拒絕並符合原因碼）
- ST-10: 空間不足 與磁碟滿 fail-stop（RLIMIT_FSIZE / EFBIG / ENOSPC 觸發 HARNESS_DISK_FULL）
- ST-11: DUT restart 世代累加與 offset 保持（兩次 restart 世代 1→2→3 且 offset 不重設）
- ST-12: transport fault 斷線與重連世代遞增（peer EIO、世代累加且新世代 offset 從 0 起）
- ST-13: 不確定寫入 fail-stop（端點 write_some 拋出 EndpointWriteUncertain 留 loss_flag: true）
- ST-14: 非預期 overflow 與有界背壓佇列（writer 阻塞觸發 HARNESS_EVIDENCE_OVERFLOW 與 fail-stop）
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import errno
import hashlib
import json
import os
import pathlib
import random
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence
import zlib

from uart_stdio_bridge.bridge.core import Bridge, BridgeConfig
from uart_stdio_bridge.bridge.dut_process import DutProcess
from uart_stdio_bridge.compare.comparator import CompareInputs, compare
from uart_stdio_bridge.compare.wire_reader import load_wire
from uart_stdio_bridge.dut.control_file import DutControlClient
from uart_stdio_bridge.dut.model import TargetConfig
from uart_stdio_bridge.endpoints import open_endpoint
from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain
from uart_stdio_bridge.evidence.clock import clock_id
from uart_stdio_bridge.evidence.manifest import ensure_private_dir
from uart_stdio_bridge.evidence.writer import EvidenceWriter


@dataclass(frozen=True)
class CheckResult:
    """單一自測項目判定結果。"""

    id: str
    verdict: str  # "PASS" | "FAIL" | "NOT_RUN"
    reason: str
    evidence_dir: str


CHECK_IDS: tuple[str, ...] = (
    "ST-01",
    "ST-02",
    "ST-03",
    "ST-04",
    "ST-05",
    "ST-06",
    "ST-07",
    "ST-08",
    "ST-09",
    "ST-10",
    "ST-11",
    "ST-12",
    "ST-13",
    "ST-14",
)


@dataclass(frozen=True)
class SelftestContext:
    """自測執行環境與參數設定。"""

    endpoint: str                     # "posix-pty" | "win-serial"
    com_pair: tuple[str, str] | None  # win-serial：(受測端 COMa, bridge 端 COMb)
    transport_mode: str
    work_dir: pathlib.Path
    seed: int = 20261015
    com_driver: dict[str, Any] | None = None
    com_driver_install_method: str | None = None


# -----------------------------------------------------------------------------
# 輔助函式
# -----------------------------------------------------------------------------

_SRC_DIR = pathlib.Path(__file__).resolve().parents[2]


def _dut_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_SRC_DIR}{os.pathsep}{existing}" if existing else str(_SRC_DIR)
    return env


def _make_bridge_config(
    run_id: str,
    case_id: str,
    port_id: str,
    evidence_dir: pathlib.Path,
    *,
    read_chunk: int = 4096,
    max_inflight_bytes: int = 1_048_576,
    write_timeout_s: float = 5.0,
    barrier_quiet_ms: int = 500,
    stderr_cap_bytes: int = 268_435_456,
    allow_dut_exit: bool = False,
    test_overrides: dict[str, str] | None = None,
) -> BridgeConfig:
    overrides = {
        k: v for k, v in os.environ.items() if k.startswith("UART_STDIO_BRIDGE_TEST_")
    }
    if test_overrides is not None:
        overrides.update(test_overrides)
    return BridgeConfig(
        run_id=run_id,
        case_id=case_id,
        port_id=port_id,
        evidence_dir=evidence_dir,
        read_chunk=read_chunk,
        max_inflight_bytes=max_inflight_bytes,
        write_timeout_s=write_timeout_s,
        barrier_quiet_ms=barrier_quiet_ms,
        stderr_cap_bytes=stderr_cap_bytes,
        allow_dut_exit=allow_dut_exit,
        test_overrides=overrides,
    )


def _wait_bridge(bridge: Bridge, timeout_s: float = 10.0) -> int:
    """等待 bridge 結束並附帶逾時保護。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if bridge._done_event.wait(timeout=0.05):
            return bridge.wait()
    bridge.handle_action("shutdown", {}, "watchdog_timeout")
    return bridge.wait()


def _read_exact(fd: int, n: int, timeout_s: float = 10.0) -> bytes:
    """從檔案描述子讀取恰好 n 個位元組。"""
    buf = bytearray()
    deadline = time.monotonic() + timeout_s
    poll = select.poll()
    poll.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"read_exact 逾時：已讀取 {len(buf)}/{n} 位元組")
        events = poll.poll(int(max(remaining, 0.001) * 1000))
        if not events:
            continue
        for _, ev in events:
            if ev & (select.POLLIN | select.POLLHUP):
                try:
                    chunk = os.read(fd, n - len(buf))
                    if chunk:
                        buf.extend(chunk)
                    elif ev & select.POLLHUP:
                        raise EOFError(f"讀取至 EOF，僅取得 {len(buf)}/{n} 位元組")
                except BlockingIOError:
                    pass
            if ev & select.POLLERR:
                raise OSError("POLLERR 發生於檔案描述子")
    return bytes(buf)


def _write_all(fd: int, data: bytes, timeout_s: float = 10.0) -> None:
    """將所有位元組寫入檔案描述子。"""
    written = 0
    total = len(data)
    deadline = time.monotonic() + timeout_s
    poll = select.poll()
    poll.register(fd, select.POLLOUT | select.POLLERR)
    while written < total:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"write_all 逾時：已寫入 {written}/{total} 位元組")
        events = poll.poll(int(max(remaining, 0.001) * 1000))
        if not events:
            continue
        for _, ev in events:
            if ev & select.POLLOUT:
                try:
                    n = os.write(fd, data[written:])
                    written += n
                except BlockingIOError:
                    pass
            if ev & select.POLLERR:
                raise OSError("POLLERR 發生於檔案描述子")


# ST-03(a) Windows 分支的有界期限：寫入與讀取皆須於期限內完成，否則 fail closed（不無限等待）。
ST03_WIN_WRITE_TIMEOUT_S = 120.0
ST03_WIN_READ_DEADLINE_S = 120.0
# ST-03(a) Windows 分支的驅動層收發佇列（pySerial `set_buffer_size`，須於開啟後呼叫）。
# 依據（2026-10-22 於 COM20:COM21、com0com 實測）：未設定時每方向僅接受 4096 位元組即阻塞（對端不讀）；
# 設為 4 MiB 後可整段接受 4 MiB。ST-03 在測試端讀取前須寫入 2 MiB，回聲因而需停在測試端的接收佇列；
# POSIX 分支的 PTY 核心緩衝可提供同類的餘裕，Windows 端須以此明示等價餘裕。
# 不影響位元組相等與短讀斷言，也不改變 bridge 的 max_inflight_bytes（1 MiB）與 write_timeout_s（5 s）。
ST03_WIN_QUEUE_BYTES = 4 * 1024 * 1024


def _win_write_all(ser: Any, data: bytes, *, timeout_s: float) -> None:
    """pySerial 版寫入：以 write_timeout 有界寫入整段；回報長度不足或逾時即失敗。"""
    ser.write_timeout = timeout_s
    written = int(ser.write(data))
    if written != len(data):
        raise OSError(f"寫入回報 {written}/{len(data)} bytes（寫入不完整）")


def _win_read_exact(ser: Any, size: int, *, timeout_s: float) -> bytes:
    """pySerial 版讀取：以整體期限讀滿 size bytes；期限內未讀滿即 TimeoutError（不回傳部分資料當成功）。"""
    deadline = time.monotonic() + timeout_s
    out = bytearray()
    while len(out) < size:
        if time.monotonic() > deadline:
            raise TimeoutError(f"讀取期限逾 {timeout_s:.0f} 秒，已接收 {len(out)}/{size} 位元組")
        out.extend(bytes(ser.read(size - len(out))))
    return bytes(out)


def _make_wal_and_correlation(
    ev_dir: pathlib.Path,
    tx_data: bytes,
    rx_data: bytes,
    port_id: str = "COM0",
) -> pathlib.Path:
    """建立自測比對所需之 WAL 替身與 correlation.ndjson。"""
    wal_dir = ev_dir / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    wal_file = wal_dir / "raw.wal.ndjson"

    records: list[dict[str, Any]] = []
    if tx_data:
        records.append({
            "cmd_id": "c1",
            "com": port_id,
            "crc32": f"{zlib.crc32(tx_data) & 0xFFFFFFFF:08x}",
            "dir": "TX",
            "len": len(tx_data),
            "loss_flag": False,
            "meta": {},
            "mono_ts_ns": 1_000_000,
            "payload_b64": base64.b64encode(tx_data).decode("ascii"),
            "seq": 1,
            "source": "TX",
            "wall_ts": "2026-10-16T00:00:00.000000Z",
        })
    if rx_data:
        records.append({
            "cmd_id": "c1",
            "com": port_id,
            "crc32": f"{zlib.crc32(rx_data) & 0xFFFFFFFF:08x}",
            "dir": "RX",
            "len": len(rx_data),
            "loss_flag": False,
            "meta": {},
            "mono_ts_ns": 2_000_000,
            "payload_b64": base64.b64encode(rx_data).decode("ascii"),
            "seq": len(records) + 1,
            "source": "RX",
            "wall_ts": "2026-10-16T00:00:00.000000Z",
        })

    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in records]
    wal_file.write_text("".join(lines), encoding="utf-8")

    wire_file = ev_dir / "wire.ndjson"
    final_wire_seq = 0
    final_offsets = {"serial_read": 0, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 0}
    if wire_file.is_file():
        wire_lines = []
        for line in wire_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                wire_lines.append(json.loads(line))
            except Exception:
                pass
        if wire_lines:
            final_wire_seq = max(r.get("seq", 0) for r in wire_lines)
            for r in wire_lines:
                obs = r.get("observation")
                if obs in final_offsets:
                    final_offsets[obs] = max(final_offsets[obs], r.get("stream_offset", 0) + r.get("len", 0))

    corr_file = ev_dir / "correlation.ndjson"
    corr_records = [
        {
            "action": "command.submit",
            "argv": ["echo"],
            "case_id": "st",
            "clock_id": "clk",
            "cmd_id": "c1",
            "kind": "op_start",
            "mono_ts_ns": 1_000_000,
            "op_id": "op-1",
            "rc": None,
            "response_sha256": None,
            "run_id": "st",
            "schema_version": "correlation.v1",
            "seq": 1,
            "settled": True,
            "step_index": 0,
            "wal_seq": 0,
            "wall_ts": "2026-10-16T00:00:00.000000Z",
            "wire": {
                "dut_generation": 1,
                "endpoint_generation": 1,
                "offsets": {"serial_read": 0, "dut_stdin_write": 0, "dut_stdout_read": 0, "serial_write": 0},
                "wire_seq": 0,
            },
        },
        {
            "action": "command.submit",
            "argv": ["echo"],
            "case_id": "st",
            "clock_id": "clk",
            "cmd_id": "c1",
            "kind": "op_end",
            "mono_ts_ns": 2_000_000,
            "op_id": "op-1",
            "rc": 0,
            "response_sha256": hashlib.sha256(rx_data).hexdigest(),
            "run_id": "st",
            "schema_version": "correlation.v1",
            "seq": 2,
            "settled": True,
            "step_index": 1,
            "wal_seq": len(records),
            "wall_ts": "2026-10-16T00:00:00.000000Z",
            "wire": {
                "dut_generation": 1,
                "endpoint_generation": 1,
                "offsets": final_offsets,
                "wire_seq": final_wire_seq,
            },
        },
    ]
    corr_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in corr_records]
    corr_file.write_text("".join(corr_lines), encoding="utf-8")

    return wal_dir


# -----------------------------------------------------------------------------
# 各自測項目實作
# -----------------------------------------------------------------------------


def _run_st01(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-01: 完整 byte 值域 (0~255 全值域往返與四觀測全等比對)。"""
    if ctx.endpoint == "win-serial" and not ctx.com_pair:
        return CheckResult("ST-01", "NOT_RUN", "win-serial 缺少 com_pair", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st01", case_id="st01", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st01", case_id="st01", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    data = bytes(range(256)) * 16
    recvd = b""
    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                _write_all(fd, data)
                recvd = _read_exact(fd, len(data), timeout_s=10.0)
            finally:
                os.close(fd)
        else:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=10.0) as ser:
                ser.write(data)
                recvd = ser.read(len(data))
    finally:
        bridge.handle_action("barrier", {"quiet_ms": 100}, "a1")
        bridge.handle_action("shutdown", {}, "a2")
        rc = _wait_bridge(bridge, timeout_s=10.0)

    if rc != 0 or bridge.failure is not None:
        return CheckResult("ST-01", "FAIL", f"bridge 未乾淨結束 rc={rc} failure={bridge.failure}", str(ev_dir))

    if recvd != data:
        return CheckResult("ST-01", "FAIL", f"A 端接收資料與傳送資料不符（長度 {len(recvd)} vs {len(data)}）", str(ev_dir))

    wire_lines = [
        json.loads(line)
        for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"):
        obs_payload = b"".join(
            base64.b64decode(r["payload_b64"])
            for r in wire_lines
            if r.get("observation") == obs
        )
        if obs_payload != data:
            return CheckResult("ST-01", "FAIL", f"觀測位置 {obs} 資料不全等", str(ev_dir))

    wal_dir = _make_wal_and_correlation(ev_dir, data, data)
    cmp_res = compare(CompareInputs(evidence_dir=ev_dir, wal_dir=wal_dir, port_map={"COM0": ("COM0",)}))
    if cmp_res.get("verdict") != "PASS":
        return CheckResult("ST-01", "FAIL", f"WAL↔wire 比較器未通過: {cmp_res.get('reasons')}", str(ev_dir))

    return CheckResult("ST-01", "PASS", "完整 byte 值域 4096B 往返與四觀測串流比對全等", str(ev_dir))


def _run_st02(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-02: 多區塊 資料傳輸 (4 MiB 隨機分塊寫入與 seq/offset 連續性檢驗)。"""
    if ctx.endpoint == "win-serial" and not ctx.com_pair:
        return CheckResult("ST-02", "NOT_RUN", "win-serial 缺少 com_pair", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st02", case_id="st02", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st02", case_id="st02", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    rng = random.Random(ctx.seed)
    total_size = 4 * 1024 * 1024
    data = rng.randbytes(total_size)
    recvd = bytearray()
    read_err: list[Exception] = []

    def _reader_loop(read_fn: Callable[[int], bytes]) -> None:
        try:
            deadline = time.monotonic() + 300.0
            last_progress = time.monotonic()
            while len(recvd) < total_size and time.monotonic() < deadline:
                needed = min(65536, total_size - len(recvd))
                try:
                    chunk = read_fn(needed)
                except BlockingIOError:
                    chunk = None
                if chunk:
                    recvd.extend(chunk)
                    last_progress = time.monotonic()
                else:
                    if time.monotonic() - last_progress > 30.0:
                        raise TimeoutError(f"逾 30 秒無進展，已接收 {len(recvd)}/{total_size}")
                    time.sleep(0.001)
        except Exception as exc:
            read_err.append(exc)

    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                t = threading.Thread(target=_reader_loop, args=(lambda n: os.read(fd, n),), daemon=True)
                t.start()
                offset = 0
                while offset < total_size:
                    chunk_len = rng.randint(1, 8192)
                    chunk = data[offset : offset + chunk_len]
                    _write_all(fd, chunk, timeout_s=120.0)
                    offset += len(chunk)
                t.join(timeout=300.0)
            finally:
                os.close(fd)
        else:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=1.0) as ser:
                t = threading.Thread(target=_reader_loop, args=(lambda n: ser.read(n),), daemon=True)
                t.start()
                offset = 0
                while offset < total_size:
                    chunk_len = rng.randint(1, 8192)
                    chunk = data[offset : offset + chunk_len]
                    ser.write(chunk)
                    offset += len(chunk)
                t.join(timeout=300.0)
    finally:
        bridge.handle_action("barrier", {"quiet_ms": 100}, "a1")
        bridge.handle_action("shutdown", {}, "a2")
        rc = _wait_bridge(bridge, timeout_s=10.0)

    if rc != 0 or bridge.failure is not None:
        return CheckResult("ST-02", "FAIL", f"bridge 未乾淨結束 rc={rc} failure={bridge.failure}", str(ev_dir))

    # 若環境帶有 drop_every_1000th 故障注入，資料勢必不符，判定為 FAIL
    if bytes(recvd) != data:
        err_msg = f"，錯誤: {read_err[0]}" if read_err else ""
        return CheckResult("ST-02", "FAIL", f"A 端接收資料不符或漏失（實得 {len(recvd)} vs 預期 {total_size}{err_msg}）", str(ev_dir))

    wire_lines = [
        json.loads(line)
        for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # seq 連續性檢驗
    seqs = [r["seq"] for r in wire_lines]
    if seqs != list(range(1, len(seqs) + 1)):
        return CheckResult("ST-02", "FAIL", "wire.ndjson seq 未嚴格自 1 連續遞增", str(ev_dir))

    # 四觀測位置 offset 連續性與內容比對
    for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"):
        obs_records = [r for r in wire_lines if r.get("observation") == obs]
        expected_offset = 0
        obs_payloads = []
        for r in obs_records:
            if r["stream_offset"] != expected_offset:
                return CheckResult("ST-02", "FAIL", f"觀測位置 {obs} offset 不連續: 預期 {expected_offset}，實得 {r['stream_offset']}", str(ev_dir))
            payload = base64.b64decode(r["payload_b64"])
            expected_offset += len(payload)
            obs_payloads.append(payload)
        joined = b"".join(obs_payloads)
        if joined != data:
            return CheckResult("ST-02", "FAIL", f"觀測位置 {obs} 串接資料與 4 MiB 亂數不符", str(ev_dir))

    wal_dir = _make_wal_and_correlation(ev_dir, data, data)
    cmp_res = compare(CompareInputs(evidence_dir=ev_dir, wal_dir=wal_dir, port_map={"COM0": ("COM0",)}))
    if cmp_res.get("verdict") != "PASS":
        return CheckResult("ST-02", "FAIL", f"比較器比對失敗: {cmp_res.get('reasons')}", str(ev_dir))

    return CheckResult("ST-02", "PASS", "4 MiB 多區塊隨機寫入與四觀測及 seq/offset 連續性檢驗通過", str(ev_dir))


def _run_st03(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-03: 短讀 ／部分寫入行為 ((a) 真 pipe 延遲讀取與 (b) in-process 腳本模擬)。"""
    # (a) 真 pipe 測試
    pipe_dir = ev_dir / "real_pipe"
    pipe_dir.mkdir(parents=True, exist_ok=True)
    link_path = pipe_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    elif ctx.com_pair:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}
    else:
        ep_spec = None

    if ep_spec is not None:
        endpoint_a = open_endpoint(ep_spec)
        dut_cmd_a = [
            sys.executable,
            "-m",
            "uart_stdio_bridge.dut",
            "--mode",
            "echo",
            "--read-delay-ms",
            "20",
        ]
        dut_a = DutProcess(lambda _g: dut_cmd_a, mode="local-process", cwd=str(pipe_dir), env=_dut_env())
        writer_a = EvidenceWriter(pipe_dir / "wire.ndjson", run_id="st03a", case_id="st03a", clock_id=clock_id())
        cfg_a = _make_bridge_config(run_id="st03a", case_id="st03a", port_id="COM0", evidence_dir=pipe_dir)
        bridge_a = Bridge(cfg_a, endpoint_a, dut_a, writer_a, dut_control=None)
        bridge_a.start()

        data_2mib = b"0123456789ABCDEF" * (128 * 1024)  # 2 MiB
        recvd_2mib = bytearray()
        win_io_error: str | None = None
        try:
            if ctx.endpoint == "posix-pty":
                fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                try:
                    # 一次寫 2 MiB 且不讀
                    _write_all(fd, data_2mib, timeout_s=30.0)
                    # 之後再讀回
                    recvd_2mib = _read_exact(fd, len(data_2mib), timeout_s=30.0)
                finally:
                    os.close(fd)
            else:
                import serial
                # 與 POSIX 分支同序：一次寫 2 MiB 且不讀，之後再讀；寫入與讀取皆有界，逾時即 fail closed。
                try:
                    with serial.Serial(ctx.com_pair[0], 115200, timeout=0.5) as ser:
                        ser.set_buffer_size(rx_size=ST03_WIN_QUEUE_BYTES, tx_size=ST03_WIN_QUEUE_BYTES)
                        _win_write_all(ser, data_2mib, timeout_s=ST03_WIN_WRITE_TIMEOUT_S)
                        recvd_2mib = bytearray(_win_read_exact(ser, len(data_2mib), timeout_s=ST03_WIN_READ_DEADLINE_S))
                except (OSError, TimeoutError, AttributeError) as exc:
                    # AttributeError：本 pySerial 後端不支援 set_buffer_size 時同樣 fail closed，不得略過緩衝設定。
                    win_io_error = str(exc)
        finally:
            bridge_a.handle_action("barrier", {"quiet_ms": 100}, "a1")
            bridge_a.handle_action("shutdown", {}, "a2")
            rc_a = _wait_bridge(bridge_a, timeout_s=10.0)

        if rc_a != 0 or bridge_a.failure is not None:
            return CheckResult("ST-03", "FAIL", f"(a) bridge 未乾淨結束 rc={rc_a} failure={bridge_a.failure}", str(ev_dir))

        if win_io_error is not None:
            return CheckResult("ST-03", "FAIL", f"(a) Windows COM 往返失敗（期限內未完成，fail closed）：{win_io_error}", str(ev_dir))

        if bytes(recvd_2mib) != data_2mib:
            return CheckResult("ST-03", "FAIL", f"(a) 真 pipe 2 MiB 往返資料不符（實收 {len(recvd_2mib)} vs 預期 {len(data_2mib)}）", str(ev_dir))

        wire_lines_a = [
            json.loads(line)
            for line in (pipe_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        stdin_writes = [r for r in wire_lines_a if r.get("observation") == "dut_stdin_write"]
        serial_reads = [r for r in wire_lines_a if r.get("observation") == "serial_read"]
        if sum(r["len"] for r in stdin_writes) != len(data_2mib):
            return CheckResult("ST-03", "FAIL", "(a) dut_stdin_write 長度總和不等於實收 2 MiB", str(ev_dir))
        if len(serial_reads) <= 1:
            return CheckResult("ST-03", "FAIL", f"(a) serial_read 未發生短讀（筆數 {len(serial_reads)} <= 1）", str(ev_dir))

    # (b) in-process 腳本模擬
    dut_script = [10, 20, 5, 15, 30, 25, 40]
    dut_script_copy = list(dut_script)
    ep_script = [12, 18, 7, 23, 11, 29, 35]
    ep_script_copy = list(ep_script)

    class _StubDut:
        def __init__(self, script: list[int]) -> None:
            self.script = list(script)
            self.generation = 1
            self.stdin_written = bytearray()
            self.stdout_buf = bytearray()

        def stdin_write_some(self, data: bytes) -> int:
            if not self.script:
                return len(data)
            n = self.script.pop(0)
            accepted = min(n, len(data))
            self.stdin_written.extend(data[:accepted])
            return accepted

        def stdout_read(self, n: int, timeout_s: float = 0.05) -> bytes:
            if not self.stdout_buf:
                return b""
            chunk = bytes(self.stdout_buf[:n])
            del self.stdout_buf[:n]
            return chunk

        def stderr_read(self, _n: int) -> bytes:
            return b""

        def poll(self) -> int | None:
            return None

        def terminate(self, _grace_s: float = 0.5) -> int:
            return 0

        def kill(self) -> int:
            return 0

        def describe(self) -> dict[str, Any]:
            return {"mode": "stub", "generation": 1}

        def spawn(self) -> None:
            pass

    class _StubEndpoint:
        def __init__(self, script: list[int]) -> None:
            self.script = list(script)
            self.generation = 1
            self.read_buf = bytearray()
            self.written = bytearray()
            self.kind = "stub"
            self.transport_mode = "virtual_pty"

        def open(self) -> None:
            pass

        def read(self, n: int, _timeout_s: float) -> bytes:
            if not self.read_buf:
                return b""
            chunk = bytes(self.read_buf[:n])
            del self.read_buf[:n]
            return chunk

        def write_some(self, data: bytes, _timeout_s: float) -> int:
            if not self.script:
                return len(data)
            n = self.script.pop(0)
            accepted = min(n, len(data))
            self.written.extend(data[:accepted])
            return accepted

        def peer_open(self) -> bool:
            return True

        def capabilities(self) -> dict[str, bool]:
            return {"transport_disconnect": True}

        def describe(self) -> dict[str, Any]:
            return {"kind": "stub", "generation": 1}

        def close(self) -> None:
            pass

    dut = _StubDut(dut_script)
    endpoint = _StubEndpoint(ep_script)
    inproc_dir = ev_dir / "in_process"
    inproc_dir.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(inproc_dir / "wire.ndjson", run_id="st03b", case_id="st03b", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st03b", case_id="st03b", port_id="COM0", evidence_dir=inproc_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)

    test_payload = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" * 2
    endpoint.read_buf.extend(test_payload)
    dut.stdout_buf.extend(test_payload)
    bridge.start()

    # 等候 pump 處理完成
    deadline = time.monotonic() + 5.0
    while (len(dut.stdin_written) < len(test_payload) or len(endpoint.written) < len(test_payload)) and time.monotonic() < deadline:
        time.sleep(0.02)

    bridge.handle_action("barrier", {"quiet_ms": 50}, "a1")
    bridge.handle_action("shutdown", {}, "a2")
    rc_b = _wait_bridge(bridge, timeout_s=5.0)
    if rc_b != 0 or bridge.failure is not None:
        return CheckResult("ST-03", "FAIL", f"(b) bridge 未乾淨結束 rc={rc_b} failure={bridge.failure}", str(ev_dir))

    wire_lines_b = [
        json.loads(line)
        for line in (inproc_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    def _sim(script: list[int], total: int) -> list[int]:
        out: list[int] = []
        rem = total
        for n in script:
            if rem <= 0:
                break
            acc = min(n, rem)
            out.append(acc)
            rem -= acc
        if rem > 0:
            out.append(rem)
        return out

    dut_stdin_records = [r for r in wire_lines_b if r.get("observation") == "dut_stdin_write"]
    if [r["len"] for r in dut_stdin_records] != _sim(dut_script_copy, len(test_payload)):
        return CheckResult("ST-03", "FAIL", "(b) dut_stdin_write 各筆長度不符腳本", str(ev_dir))

    serial_write_records = [r for r in wire_lines_b if r.get("observation") == "serial_write"]
    if [r["len"] for r in serial_write_records] != _sim(ep_script_copy, len(test_payload)):
        return CheckResult("ST-03", "FAIL", "(b) serial_write 各筆長度不符腳本", str(ev_dir))

    # 串接無遺失無重複檢驗
    joined_stdin = b"".join(base64.b64decode(r["payload_b64"]) for r in dut_stdin_records)
    if joined_stdin != test_payload:
        return CheckResult("ST-03", "FAIL", "(b) dut_stdin_write 串接結果與原資料不符", str(ev_dir))

    joined_sw = b"".join(base64.b64decode(r["payload_b64"]) for r in serial_write_records)
    if joined_sw != test_payload:
        return CheckResult("ST-03", "FAIL", "(b) serial_write 串接結果與原資料不符", str(ev_dir))

    return CheckResult("ST-03", "PASS", "短讀與部分寫入精確記錄實際接受長度且串接無遺失無重複", str(ev_dir))


def _run_st04(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-04: stderr 大量輸出 (DUT flood 50 MB 同時往返 1 MiB，wire 中無 stderr bytes)。"""
    if ctx.endpoint == "win-serial" and not ctx.com_pair:
        return CheckResult("ST-04", "NOT_RUN", "win-serial 缺少 com_pair", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo", "--stderr-flood-bytes", "50000000"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st04", case_id="st04", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st04", case_id="st04", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    data = b"0123456789ABCDEF" * 65536  # 1 MiB
    recvd = bytearray()
    t_start = time.monotonic()

    def _reader(read_fn: Callable[[int], bytes]) -> None:
        while len(recvd) < len(data) and (time.monotonic() - t_start) < 30.0:
            try:
                chunk = read_fn(min(65536, len(data) - len(recvd)))
            except BlockingIOError:
                chunk = None
            if chunk:
                recvd.extend(chunk)
            else:
                time.sleep(0.005)

    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                t = threading.Thread(target=_reader, args=(lambda n: os.read(fd, n),), daemon=True)
                t.start()
                _write_all(fd, data, timeout_s=25.0)
                t.join(timeout=30.0)
            finally:
                os.close(fd)
        else:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=1.0) as ser:
                t = threading.Thread(target=_reader, args=(lambda n: ser.read(n),), daemon=True)
                t.start()
                ser.write(data)
                t.join(timeout=30.0)
    finally:
        bridge.handle_action("barrier", {"quiet_ms": 100}, "a1")
        bridge.handle_action("shutdown", {}, "a2")
        rc = _wait_bridge(bridge, timeout_s=10.0)

    if rc != 0 or bridge.failure is not None:
        return CheckResult("ST-04", "FAIL", f"bridge 未乾淨結束 rc={rc} failure={bridge.failure}", str(ev_dir))

    elapsed = time.monotonic() - t_start
    if elapsed >= 30.0:
        return CheckResult("ST-04", "FAIL", f"1 MiB 往返耗時 {elapsed:.2f}s 超過 30 秒上限", str(ev_dir))

    if bytes(recvd) != data:
        return CheckResult("ST-04", "FAIL", "UART 傳輸資料受 stderr 影響發生不一致", str(ev_dir))

    stderr_log = ev_dir / "dut.stderr.log"
    if not stderr_log.is_file() or stderr_log.stat().st_size != 50_000_000:
        return CheckResult("ST-04", "FAIL", f"dut.stderr.log 大小不是 50000000 (實際 {stderr_log.stat().st_size if stderr_log.is_file() else 0})", str(ev_dir))

    wire_lines = [
        json.loads(line)
        for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for obs in ("serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"):
        joined = b"".join(base64.b64decode(r["payload_b64"]) for r in wire_lines if r.get("observation") == obs)
        if joined != data:
            return CheckResult("ST-04", "FAIL", f"觀測位置 {obs} 串流與 1 MiB 資料不符（可能混入 stderr）", str(ev_dir))

    manifest = json.loads((ev_dir / "bridge-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dut_stderr_bytes", 0) < 50000000:
        return CheckResult("ST-04", "FAIL", f"manifest dut_stderr_bytes {manifest.get('dut_stderr_bytes')} 小於 50000000", str(ev_dir))

    return CheckResult("ST-04", "PASS", "stderr 大量輸出隔離正常，1 MiB 往返耗時合格且 wire 中無 stderr bytes", str(ev_dir))


def _run_st05(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-05: EOF 偵測 (DUT --close-stdout-after-bytes 1024 留 META dut_stdout_eof 且 TX 仍轉送)。"""
    if ctx.endpoint == "win-serial" and not ctx.com_pair:
        return CheckResult("ST-05", "NOT_RUN", "win-serial 缺少 com_pair", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo", "--close-stdout-after-bytes", "1024"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st05", case_id="st05", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st05", case_id="st05", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    data_first = b"A" * 1024
    data_second = b"B" * 512
    recvd = b""

    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                _write_all(fd, data_first)
                recvd = _read_exact(fd, 1024, timeout_s=5.0)
                time.sleep(0.2)
                _write_all(fd, data_second)
                time.sleep(0.2)
            finally:
                os.close(fd)
        else:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=5.0) as ser:
                ser.write(data_first)
                recvd = ser.read(1024)
                time.sleep(0.2)
                ser.write(data_second)
                time.sleep(0.2)
    finally:
        bridge.handle_action("barrier", {"quiet_ms": 100}, "a1")
        bridge.handle_action("shutdown", {}, "a2")
        rc = _wait_bridge(bridge, timeout_s=10.0)

    if rc != 0 or bridge.failure is not None:
        return CheckResult("ST-05", "FAIL", f"bridge 未乾淨結束 rc={rc} failure={bridge.failure}", str(ev_dir))

    if recvd != data_first:
        return CheckResult("ST-05", "FAIL", "關閉 stdout 前的 1024 位元組接收不完整", str(ev_dir))

    wire_lines = [
        json.loads(line)
        for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    has_eof_meta = any(r.get("meta", {}).get("event") == "dut_stdout_eof" for r in wire_lines)
    if not has_eof_meta:
        return CheckResult("ST-05", "FAIL", "wire.ndjson 缺少 dut_stdout_eof META 紀錄", str(ev_dir))

    tx_total = sum(r.get("len", 0) for r in wire_lines if r.get("observation") == "dut_stdin_write")
    if tx_total < 1024 + 512:
        return CheckResult("ST-05", "FAIL", f"stdout 關閉後 TX 未繼續轉送（實送 {tx_total} vs 預期 {1024 + 512}）", str(ev_dir))

    return CheckResult("ST-05", "PASS", "stdout 關閉後正確留存 dut_stdout_eof META 且 TX 繼續轉送", str(ev_dir))


def _run_st06(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-06: 程序退出 (DUT exit 7 未加 --allow-dut-exit 觸發 exit 3、HARNESS_DUT_EXITED)。"""
    if ctx.endpoint == "win-serial" and not ctx.com_pair:
        return CheckResult("ST-06", "NOT_RUN", "win-serial 缺少 com_pair", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo", "--exit-after-bytes", "512", "--exit-code", "7"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st06", case_id="st06", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st06", case_id="st06", port_id="COM0", evidence_dir=ev_dir, allow_dut_exit=False)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    data = b"X" * 512
    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
        else:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=2.0) as ser:
                ser.write(data)
    except Exception:
        pass

    exit_code = _wait_bridge(bridge, timeout_s=10.0)
    if exit_code != 3:
        return CheckResult("ST-06", "FAIL", f"bridge 結束代碼為 {exit_code}（預期 3）", str(ev_dir))
    if bridge.failure != "HARNESS_DUT_EXITED":
        return CheckResult("ST-06", "FAIL", f"bridge failure 原因為 {bridge.failure}（預期 HARNESS_DUT_EXITED）", str(ev_dir))

    wal_dir = _make_wal_and_correlation(ev_dir, data, data)
    cmp_res = compare(CompareInputs(evidence_dir=ev_dir, wal_dir=wal_dir, port_map={"COM0": ("COM0",)}))
    if cmp_res.get("verdict") != "INSUFFICIENT_EVIDENCE":
        return CheckResult("ST-06", "FAIL", f"比較器回傳判定為 {cmp_res.get('verdict')}（預期 INSUFFICIENT_EVIDENCE）", str(ev_dir))

    return CheckResult("ST-06", "PASS", "DUT 非預期退出觸發 bridge exit 3、HARNESS_DUT_EXITED 且比較器判定 INSUFFICIENT_EVIDENCE", str(ev_dir))


def _run_st07(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-07: 故障控制 通道動作依序呼叫並驗證 control.ndjson。"""
    if ctx.endpoint != "win-serial" or not ctx.com_pair:
        return _run_st07_body(ctx, ev_dir)
    import serial
    # Windows 對端（COM20）於整段檢查期間保持開啟，模擬實體連線：實測 com0com 對端關閉時寫入會阻塞直到 bridge 的 5 秒
    # 寫入逾時，隨即以 HARNESS_WRITE_UNCERTAIN fail closed（POSIX 端由 peer gate 處理，Windows 端無 peer 狀態可取樣）。
    peer = serial.Serial(ctx.com_pair[0], 115200, timeout=0.05)
    try:
        return _run_st07_body(ctx, ev_dir)
    finally:
        peer.close()


def _run_st07_body(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-07 本體（不含 Windows 對端保持開啟之包裝）。"""
    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        if not ctx.com_pair:
            return CheckResult("ST-07", "NOT_RUN", "win-serial 需要指定 --com-pair", str(ev_dir))
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    try:
        endpoint = open_endpoint(ep_spec)
    except Exception:
        if ctx.endpoint == "win-serial":
            return CheckResult("ST-07", "NOT_RUN", "win-serial 開啟端點失敗", str(ev_dir))
        raise

    control_dir = ev_dir / "dut_ctl"
    control_dir.mkdir(parents=True, exist_ok=True)
    target_json = ev_dir / "target.json"
    cfg_target = TargetConfig.from_yaml({"noise": {"enabled": False}})
    target_json.write_text(json.dumps(cfg_target.to_json(), ensure_ascii=False), encoding="utf-8")

    dut_cmd = [
        sys.executable,
        "-m",
        "uart_stdio_bridge.dut",
        "--mode",
        "dut",
        "--target-json",
        str(target_json),
        "--control-dir",
        str(control_dir),
    ]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    dut_control = DutControlClient(control_dir)
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st07", case_id="st07", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st07", case_id="st07", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=dut_control)
    bridge.start()

    actions_to_call: list[tuple[str, dict[str, Any], str]] = [
        ("status", {}, "act_status"),
        ("mark", {"label": "m1", "op_id": "op-1"}, "act_mark"),
        ("pause_pump", {"dir": "TX"}, "act_pause_tx"),
        ("resume_pump", {"dir": "TX"}, "act_resume_tx"),
        ("barrier", {"mode": "passive", "quiet_ms": 50}, "act_barrier_passive"),
        ("barrier", {"mode": "quiesce"}, "act_barrier_quiesce"),
        ("resume_pump", {"dir": "RX"}, "act_resume_rx"),
        ("checkpoint", {}, "act_checkpoint"),
        (
            "dut_control",
            {"msg": {"action": "emit", "payload_b64": base64.b64encode(b"PING").decode("ascii")}},
            "act_dut_control",
        ),
        ("dut_restart", {"queue_policy": "deliver"}, "act_restart_deliver"),
        ("dut_restart", {"queue_policy": "discard"}, "act_restart_discard"),
        ("dut_kill", {}, "act_dut_kill"),
        ("dut_spawn", {}, "act_dut_spawn"),
    ]

    for act, params, aid in actions_to_call:
        res = bridge.handle_action(act, params, aid)
        if res.get("ok") is False:
            bridge.handle_action("shutdown", {}, "act_err_shut")
            _wait_bridge(bridge, timeout_s=5.0)
            return CheckResult("ST-07", "FAIL", f"動作 {act} 執行失敗: {res}", str(ev_dir))

    if ctx.endpoint == "posix-pty":
        actions_to_call.extend([
            ("by_id_link_remove", {}, "act_by_id_link_remove"),
            ("by_id_link_add", {}, "act_by_id_link_add"),
        ])
        cap_actions: list[tuple[str, dict[str, Any], str]] = []
        for act, params, aid in actions_to_call[13:]:
            res = bridge.handle_action(act, params, aid)
            if res.get("ok") is False:
                bridge.handle_action("shutdown", {}, "act_err_shut")
                _wait_bridge(bridge, timeout_s=5.0)
                return CheckResult("ST-07", "FAIL", f"動作 {act} 執行失敗: {res}", str(ev_dir))
    else:
        cap_actions = [
            ("by_id_link_remove", {}, "act_by_id_link_remove"),
            ("by_id_link_add", {}, "act_by_id_link_add"),
            ("transport_disconnect", {"remove_link": True}, "act_transport_disconnect"),
            ("transport_reconnect", {}, "act_transport_reconnect"),
        ]
        for act, params, aid in cap_actions:
            res = bridge.handle_action(act, params, aid)
            if res.get("ok") is not False or res.get("error") != "HARNESS_CAPABILITY_UNAVAILABLE":
                bridge.handle_action("shutdown", {}, "act_err_shut")
                _wait_bridge(bridge, timeout_s=5.0)
                return CheckResult(
                    "ST-07",
                    "FAIL",
                    f"win-serial 下 {act} 預期回傳 HARNESS_CAPABILITY_UNAVAILABLE，實得: {res}",
                    str(ev_dir),
                )

    bridge.handle_action("shutdown", {}, "act_shutdown")
    exit_code = _wait_bridge(bridge, timeout_s=10.0)
    if exit_code != 0:
        return CheckResult("ST-07", "FAIL", f"bridge 退出代碼為 {exit_code}（預期 0）", str(ev_dir))
    if bridge.failure is not None:
        return CheckResult("ST-07", "FAIL", f"bridge failure 為 {bridge.failure}（預期 None）", str(ev_dir))

    ctrl_file = ev_dir / "control.ndjson"
    if not ctrl_file.is_file():
        return CheckResult("ST-07", "FAIL", "control.ndjson 不存在", str(ev_dir))

    ctrl_lines = [json.loads(line) for line in ctrl_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    all_called = actions_to_call + cap_actions + [("shutdown", {}, "act_shutdown")]
    by_aid: dict[str, list[dict[str, Any]]] = {}
    for r in ctrl_lines:
        aid = r.get("action_id")
        if aid:
            by_aid.setdefault(aid, []).append(r)

    for act, _params, aid in all_called:
        recs = by_aid.get(aid, [])
        if len(recs) != 2:
            return CheckResult(
                "ST-07",
                "FAIL",
                f"action {act} ({aid}) 記錄筆數不為 2（實得 {len(recs)} 筆）",
                str(ev_dir),
            )
        req, res_rec = recs[0], recs[1]
        if req.get("phase") != "requested":
            return CheckResult("ST-07", "FAIL", f"action {act} 首筆 phase 為 {req.get('phase')}（預期 requested）", str(ev_dir))
        expected_phase = "failed" if (ctx.endpoint == "win-serial" and act in {a[0] for a in cap_actions}) else "completed"
        if res_rec.get("phase") != expected_phase:
            return CheckResult(
                "ST-07",
                "FAIL",
                f"action {act} 末筆 phase 為 {res_rec.get('phase')}（預期 {expected_phase}）",
                str(ev_dir),
            )
        if req.get("wire_seq", 0) > res_rec.get("wire_seq", 0):
            return CheckResult("ST-07", "FAIL", f"action {act} wire_seq 倒退: {req.get('wire_seq')} > {res_rec.get('wire_seq')}", str(ev_dir))

    return CheckResult("ST-07", "PASS", "故障控制通道動作完整呼叫、驗證且 control.ndjson 正確配對", str(ev_dir))


def _run_st08(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-08: CRC 與 offset 驗證 (正常結束證據跑 load_wire)。"""
    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        if not ctx.com_pair:
            return CheckResult("ST-08", "NOT_RUN", "win-serial 需要指定 --com-pair", str(ev_dir))
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    try:
        endpoint = open_endpoint(ep_spec)
    except Exception:
        if ctx.endpoint == "win-serial":
            return CheckResult("ST-08", "NOT_RUN", "win-serial 開啟端點失敗", str(ev_dir))
        raise

    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st08", case_id="st08", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st08", case_id="st08", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    data = b"ST08_VERIFY_DATA" * 32
    total_data = bytearray()
    if ctx.endpoint == "posix-pty":
        fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            for _ in range(3):
                _write_all(fd, data)
                total_data.extend(_read_exact(fd, len(data), timeout_s=5.0))
                time.sleep(0.01)
        finally:
            os.close(fd)
    elif ctx.com_pair:
        import serial
        with serial.Serial(ctx.com_pair[0], 115200, timeout=5.0) as ser:
            for _ in range(3):
                ser.write(data)
                total_data.extend(ser.read(len(data)))
                time.sleep(0.01)

    bridge.handle_action("barrier", {"quiet_ms": 50}, "a1")
    bridge.handle_action("shutdown", {}, "a2")
    exit_code = _wait_bridge(bridge, timeout_s=10.0)
    if exit_code != 0:
        return CheckResult("ST-08", "FAIL", f"bridge 結束代碼為 {exit_code}（預期 0）", str(ev_dir))
    if bridge.failure is not None:
        return CheckResult("ST-08", "FAIL", f"bridge failure 為 {bridge.failure}（預期 None）", str(ev_dir))

    _make_wal_and_correlation(ev_dir, bytes(total_data), bytes(total_data))

    records, problems = load_wire(ev_dir)
    if problems:
        return CheckResult("ST-08", "FAIL", f"load_wire 檢出問題: {problems}", str(ev_dir))

    manifest_path = ev_dir / "bridge-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wire_bytes = (ev_dir / "wire.ndjson").read_bytes()
    expected_digest = manifest.get("evidence", {}).get("wire", {}).get("sha256")
    actual_digest = hashlib.sha256(wire_bytes).hexdigest()
    if actual_digest != expected_digest:
        return CheckResult("ST-08", "FAIL", f"manifest wire digest 不符: {actual_digest} vs {expected_digest}", str(ev_dir))

    return CheckResult("ST-08", "PASS", "CRC、offset 與 manifest digest 驗證通過無任何問題", str(ev_dir))


def _run_st09(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-09: 破壞證據 竄改拒絕判定 (套用 Task 7 13 種竄改均拒絕)。"""
    base_dir = ev_dir / "baseline"
    ensure_private_dir(base_dir)
    res_base = _run_st08(ctx, base_dir)
    if res_base.verdict != "PASS":
        return CheckResult("ST-09", "FAIL", f"ST-09 基線證據產生失敗: {res_base.reason}", str(ev_dir))

    base_cmp = compare(CompareInputs(evidence_dir=base_dir, wal_dir=base_dir / "wal", port_map={"COM0": ("COM0",)}))
    if base_cmp.get("verdict") != "PASS":
        return CheckResult("ST-09", "FAIL", f"ST-09 基線未通過比較判定: {base_cmp.get('verdict')} reasons={base_cmp.get('reasons')}", str(ev_dir))

    tampers: dict[str, tuple[str, ...]] = {
        "flip_payload_keep_crc": ("HARNESS_CRC_MISMATCH",),
        "flip_payload_fix_crc": ("HARNESS_TAMPER_DETECTED",),
        "delete_middle_record": ("HARNESS_SEQ_GAP", "HARNESS_OFFSET_GAP"),
        "duplicate_record": ("HARNESS_SEQ_GAP",),
        "swap_two_records": ("HARNESS_SEQ_GAP",),
        "change_len": ("HARNESS_CRC_MISMATCH",),
        "set_meta_loss_flag": ("HARNESS_LOSS_FLAG",),
        "truncate_last_line": ("HARNESS_SCHEMA_INVALID",),
        "remove_bridge_manifest": ("HARNESS_TAMPER_DETECTED",),
        "manifest_closed_uncleanly": ("HARNESS_INSUFFICIENT_EVIDENCE",),
        "wal_flip_payload": ("HARNESS_CRC_MISMATCH",),
        "wal_drop_required_key": ("HARNESS_WAL_UNREADABLE",),
        "correlation_unsettled": ("HARNESS_WINDOW_UNSETTLED",),
    }

    for tamper_name, expected_codes in sorted(tampers.items()):
        sub_dir = ev_dir / tamper_name
        if sub_dir.exists():
            shutil.rmtree(sub_dir)
        shutil.copytree(base_dir, sub_dir)

        wire_path = sub_dir / "wire.ndjson"
        manifest_path = sub_dir / "bridge-manifest.json"
        wal_path = sub_dir / "wal" / "raw.wal.ndjson"
        corr_path = sub_dir / "correlation.ndjson"
        wire = [json.loads(l) for l in wire_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        wal = [json.loads(l) for l in wal_path.read_text(encoding="utf-8").splitlines() if l.strip()] if wal_path.is_file() else []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

        def _save_wire() -> None:
            wire_path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in wire), encoding="utf-8")

        def _save_wal() -> None:
            wal_path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in wal), encoding="utf-8")

        def _save_manifest() -> None:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        def _refresh_wire_digest() -> None:
            if manifest_path.is_file():
                manifest.setdefault("evidence", {}).setdefault("wire", {})["sha256"] = hashlib.sha256(wire_path.read_bytes()).hexdigest()
                _save_manifest()

        data_rec = next((r for r in wire if r.get("observation") == "serial_read"), None)
        if tamper_name == "flip_payload_keep_crc":
            assert data_rec is not None
            p = base64.b64decode(data_rec["payload_b64"])
            changed = p[:1] + bytes([p[1] ^ 1]) + p[2:]
            data_rec["payload_b64"] = base64.b64encode(changed).decode("ascii")
            _save_wire()
        elif tamper_name == "flip_payload_fix_crc":
            assert data_rec is not None
            p = base64.b64decode(data_rec["payload_b64"])
            changed = p[:1] + bytes([p[1] ^ 1]) + p[2:]
            data_rec["payload_b64"] = base64.b64encode(changed).decode("ascii")
            data_rec["crc32"] = f"{zlib.crc32(changed) & 0xFFFFFFFF:08x}"
            data_rec["len"] = len(changed)
            _save_wire()
        elif tamper_name == "delete_middle_record":
            sr_indices = [i for i, r in enumerate(wire) if r.get("observation") == "serial_read"]
            if len(sr_indices) >= 2:
                del wire[sr_indices[1]]
            else:
                del wire[len(wire) // 2]
            _save_wire()
            _refresh_wire_digest()
        elif tamper_name == "duplicate_record":
            wire.insert(2, dict(wire[1]))
            _save_wire()
            _refresh_wire_digest()
        elif tamper_name == "swap_two_records":
            wire[1], wire[2] = wire[2], wire[1]
            _save_wire()
            _refresh_wire_digest()
        elif tamper_name == "change_len":
            assert data_rec is not None
            data_rec["len"] += 1
            _save_wire()
        elif tamper_name == "set_meta_loss_flag":
            meta = next((r for r in wire if r.get("dir") == "META"), None)
            if not meta:
                meta = {"dir": "META", "loss_flag": True, "meta": {"event": "test"}}
                wire.append(meta)
            else:
                meta["loss_flag"] = True
            _save_wire()
            _refresh_wire_digest()
        elif tamper_name == "truncate_last_line":
            c = wire_path.read_bytes()
            wire_path.write_bytes(c[:-1])
        elif tamper_name == "remove_bridge_manifest":
            manifest_path.unlink()
        elif tamper_name == "manifest_closed_uncleanly":
            manifest["closed_cleanly"] = False
            _save_manifest()
        elif tamper_name == "wal_flip_payload":
            assert wal
            p = base64.b64decode(wal[0]["payload_b64"])
            changed = p[:1] + bytes([p[1] ^ 1]) + p[2:]
            wal[0]["payload_b64"] = base64.b64encode(changed).decode("ascii")
            _save_wal()
        elif tamper_name == "wal_drop_required_key":
            assert wal
            wal[0].pop("source", None)
            _save_wal()
        elif tamper_name == "correlation_unsettled":
            corrs = [json.loads(l) for l in corr_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            for r in corrs:
                r["settled"] = False
            corr_path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in corrs), encoding="utf-8")

        out = compare(CompareInputs(evidence_dir=sub_dir, wal_dir=sub_dir / "wal", port_map={"COM0": ("COM0",)}))
        if out.get("verdict") == "PASS":
            return CheckResult("ST-09", "FAIL", f"竄改 {tamper_name} 未被拒絕（仍為 PASS）", str(ev_dir))
        reasons = set(out.get("reasons", []))
        if not (set(expected_codes) <= reasons):
            return CheckResult("ST-09", "FAIL", f"竄改 {tamper_name} 原因碼未包含預期 {expected_codes}（實得 {reasons}）", str(ev_dir))

    return CheckResult("ST-09", "PASS", "13 種證據竄改皆成功拒絕並符合預期原因碼", str(ev_dir))


def _run_st10(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-10: 空間不足 與磁碟滿 fail-stop (RLIMIT_FSIZE / ENOSPC)。"""
    link_path = ev_dir / "fake-uart"
    base_args = [
        "run",
        "--run-id",
        "st10",
        "--case-id",
        "st10",
        "--port-id",
        "COM0",
        "--evidence-dir",
        str(ev_dir),
        "--endpoint",
        "posix-pty" if ctx.endpoint == "posix-pty" else "win-serial",
        "--dut-mode",
        "local-process",
        "--dut-argv-json",
        json.dumps([sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]),
    ]
    if ctx.endpoint == "posix-pty":
        base_args.extend(["--by-id-link", str(link_path)])
    else:
        if not ctx.com_pair:
            return CheckResult("ST-10", "NOT_RUN", "win-serial 需要指定 --com-pair", str(ev_dir))
        base_args.extend(["--com", ctx.com_pair[1]])

    env = _dut_env()
    if sys.platform != "win32":
        runner_cmd = [
            sys.executable,
            "-c",
            (
                "import sys, resource, signal, runpy; "
                "signal.signal(signal.SIGXFSZ, signal.SIG_IGN); "
                "resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024)); "
                "sys.argv = sys.argv[1:]; "
                "runpy.run_module('uart_stdio_bridge', run_name='__main__')"
            ),
            "uart-stdio-bridge",
            *base_args,
        ]
    else:
        env["UART_STDIO_BRIDGE_TEST_FAULT"] = "enospc_after_1mib"
        runner_cmd = [sys.executable, "-m", "uart_stdio_bridge", *base_args]

    proc = subprocess.Popen(
        runner_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ev_dir),
        env=env,
    )

    ready = False
    stdout_buf = bytearray()
    deadline = time.monotonic() + 10.0
    fd = proc.stdout.fileno()
    if sys.platform != "win32":
        os.set_blocking(fd, False)
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            chunk = os.read(fd, 4096)
            if chunk:
                stdout_buf.extend(chunk)
                if b"BRIDGE_READY" in stdout_buf:
                    ready = True
                    break
        except (BlockingIOError, InterruptedError):
            pass
        time.sleep(0.02)

    if not ready and proc.poll() is None:
        proc.kill()
        return CheckResult("ST-10", "FAIL", "等待 BRIDGE_READY 逾時", str(ev_dir))

    flood_data = b"D" * 65536
    try:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                for _ in range(32):  # 2 MiB 數據足以觸發 1 MiB 上限
                    if proc.poll() is not None:
                        break
                    try:
                        _write_all(fd, flood_data, timeout_s=1.0)
                    except Exception:
                        break
                    time.sleep(0.01)
            finally:
                os.close(fd)
        elif ctx.com_pair:
            import serial
            # 寫入須有界（同 POSIX 分支的 _write_all 1.0 秒期限）：bridge 因磁碟滿退出後不再讀取，無 write_timeout 時會永久阻塞。
            with serial.Serial(ctx.com_pair[0], 115200, timeout=1.0, write_timeout=1.0) as ser:
                for _ in range(32):
                    if proc.poll() is not None:
                        break
                    ser.write(flood_data)
                    time.sleep(0.01)
    except Exception:
        pass

    try:
        proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)

    if proc.returncode != 3:
        return CheckResult("ST-10", "FAIL", f"bridge 退出代碼為 {proc.returncode}（預期 3）", str(ev_dir))

    manifest_file = ev_dir / "bridge-manifest.json"
    if not manifest_file.is_file():
        return CheckResult("ST-10", "FAIL", "bridge-manifest.json 未產出", str(ev_dir))

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("failure") != "HARNESS_DISK_FULL":
        return CheckResult("ST-10", "FAIL", f"manifest failure 為 {manifest.get('failure')}（預期 HARNESS_DISK_FULL）", str(ev_dir))

    wal_dir = _make_wal_and_correlation(ev_dir, flood_data, flood_data)
    cmp_res = compare(CompareInputs(evidence_dir=ev_dir, wal_dir=wal_dir, port_map={"COM0": ("COM0",)}))
    if cmp_res.get("verdict") != "INSUFFICIENT_EVIDENCE":
        return CheckResult("ST-10", "FAIL", f"比較器回傳判定為 {cmp_res.get('verdict')}（預期 INSUFFICIENT_EVIDENCE）", str(ev_dir))

    return CheckResult("ST-10", "PASS", "空間不足正確觸發 HARNESS_DISK_FULL 與 exit code 3", str(ev_dir))


def _run_st11(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-11: DUT restart (dut_restart 兩次，世代 1→2→3 且 offset 不重設)。"""
    link_path = ev_dir / "fake-uart"
    if ctx.endpoint == "posix-pty":
        ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    else:
        if not ctx.com_pair:
            return CheckResult("ST-11", "NOT_RUN", "win-serial 需要指定 --com-pair", str(ev_dir))
        ep_spec = {"kind": "win-serial", "com": ctx.com_pair[1], "transport_mode": ctx.transport_mode}

    try:
        endpoint = open_endpoint(ep_spec)
    except Exception:
        if ctx.endpoint == "win-serial":
            return CheckResult("ST-11", "NOT_RUN", "win-serial 開啟端點失敗", str(ev_dir))
        raise

    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st11", case_id="st11", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st11", case_id="st11", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    def _roundtrip(data: bytes) -> None:
        if ctx.endpoint == "posix-pty":
            fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                _write_all(fd, data)
                _read_exact(fd, len(data), timeout_s=5.0)
            finally:
                os.close(fd)
        elif ctx.com_pair:
            import serial
            with serial.Serial(ctx.com_pair[0], 115200, timeout=5.0) as ser:
                ser.write(data)
                ser.read(len(data))

    # 世代 1 傳輸
    _roundtrip(b"GEN1_DATA_PAYLOAD_1234567890\n")

    res1 = bridge.handle_action("dut_restart", {}, "a1")
    gen1 = res1.get("dut_generation")
    if gen1 != 2:
        bridge.handle_action("shutdown", {}, "a_err")
        _wait_bridge(bridge, timeout_s=5.0)
        return CheckResult("ST-11", "FAIL", f"第 1 次 restart 後世代為 {gen1}（預期 2）", str(ev_dir))

    # 世代 2 傳輸
    _roundtrip(b"GEN2_DATA_PAYLOAD_ABCDEFGHIJ\n")

    res2 = bridge.handle_action("dut_restart", {}, "a2")
    gen2 = res2.get("dut_generation")
    if gen2 != 3:
        bridge.handle_action("shutdown", {}, "a_err")
        _wait_bridge(bridge, timeout_s=5.0)
        return CheckResult("ST-11", "FAIL", f"第 2 次 restart 後世代為 {gen2}（預期 3）", str(ev_dir))

    # 世代 3 傳輸
    _roundtrip(b"GEN3_DATA_PAYLOAD_KLMNOPQRST\n")

    bridge.handle_action("barrier", {"quiet_ms": 50}, "a_bar")
    bridge.handle_action("shutdown", {}, "a3")
    exit_code = _wait_bridge(bridge, timeout_s=10.0)
    if exit_code != 0:
        return CheckResult("ST-11", "FAIL", f"bridge 退出代碼為 {exit_code}（預期 0）", str(ev_dir))
    if bridge.failure is not None:
        return CheckResult("ST-11", "FAIL", f"bridge failure 為 {bridge.failure}（預期 None）", str(ev_dir))

    wire_lines = [json.loads(line) for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
    gen_events = [r for r in wire_lines if r.get("meta", {}).get("event") == "dut_generation"]
    if len(gen_events) < 2:
        return CheckResult("ST-11", "FAIL", f"wire.ndjson 缺少 dut_generation META 紀錄（實得 {len(gen_events)} 筆）", str(ev_dir))

    # 驗證 stream_offset 跨世代連續遞增不重設
    for obs in ("serial_read", "dut_stdin_write"):
        records = [r for r in wire_lines if r.get("observation") == obs]
        if not records:
            return CheckResult("ST-11", "FAIL", f"wire.ndjson 缺少 {obs} 紀錄", str(ev_dir))
        gens_present = {r.get("dut_generation") for r in records}
        if not ({1, 2, 3} <= gens_present):
            return CheckResult("ST-11", "FAIL", f"{obs} 未涵蓋世代 1, 2, 3（實得 {gens_present}）", str(ev_dir))
        for i in range(len(records) - 1):
            curr_rec = records[i]
            next_rec = records[i + 1]
            curr_end = curr_rec.get("stream_offset", 0) + curr_rec.get("len", 0)
            next_start = next_rec.get("stream_offset", 0)
            if next_start < curr_end:
                return CheckResult("ST-11", "FAIL", f"{obs} stream_offset 跨紀錄倒退或重設: {next_start} < {curr_end}", str(ev_dir))

    return CheckResult("ST-11", "PASS", "dut_restart 兩次世代遞增至 3 且 stream offset 保持不重設", str(ev_dir))


def _run_st12(ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-12: transport fault (斷線造成 peer EIO，重連後世代與 offset 正確)。"""
    if ctx.endpoint == "win-serial":
        return CheckResult("ST-12", "NOT_RUN", "cap_missing:transport_disconnect", str(ev_dir))

    link_path = ev_dir / "fake-uart"
    ep_spec = {"kind": "posix-pty", "by_id_link": str(link_path), "peer_gate": True}
    endpoint = open_endpoint(ep_spec)
    dut_cmd = [sys.executable, "-m", "uart_stdio_bridge.dut", "--mode", "echo"]
    dut = DutProcess(lambda _g: dut_cmd, mode="local-process", cwd=str(ev_dir), env=_dut_env())
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st12", case_id="st12", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st12", case_id="st12", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        _write_all(fd, b"hello")
        _read_exact(fd, 5, timeout_s=5.0)
        bridge.handle_action("transport_disconnect", {"remove_link": True}, "a_disc")
        time.sleep(0.05)
        saw_eio = False
        try:
            chunk = os.read(fd, 1024)
            if not chunk:
                saw_eio = True
        except OSError as e:
            if e.errno == errno.EIO:
                saw_eio = True
        os.close(fd)
        fd = -1

        if not saw_eio:
            return CheckResult("ST-12", "FAIL", "transport_disconnect 未造成 peer EIO", str(ev_dir))

        reconn_res = bridge.handle_action("transport_reconnect", {}, "a_reconn")
        new_gen = reconn_res.get("endpoint_generation")
        if new_gen != 2:
            return CheckResult("ST-12", "FAIL", f"reconnect 後端點世代為 {new_gen}（預期 2）", str(ev_dir))

        fd = os.open(str(link_path), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        _write_all(fd, b"world")
        _read_exact(fd, 5, timeout_s=5.0)
    finally:
        if fd >= 0:
            os.close(fd)
        bridge.handle_action("barrier", {"quiet_ms": 50}, "a_bar")
        bridge.handle_action("shutdown", {}, "a_shut")
        exit_code = _wait_bridge(bridge, timeout_s=10.0)

    if exit_code != 0:
        return CheckResult("ST-12", "FAIL", f"bridge 退出代碼為 {exit_code}（預期 0）", str(ev_dir))
    if bridge.failure is not None:
        return CheckResult("ST-12", "FAIL", f"bridge failure 為 {bridge.failure}（預期 None）", str(ev_dir))

    wire_lines = [json.loads(line) for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
    gen2_reads = [r for r in wire_lines if r.get("observation") == "serial_read" and r.get("endpoint_generation") == 2]
    if not gen2_reads:
        return CheckResult("ST-12", "FAIL", "新世代 2 未記錄 serial_read", str(ev_dir))
    if gen2_reads[0].get("stream_offset") != 0:
        return CheckResult("ST-12", "FAIL", f"新世代 serial_read 首筆 offset 為 {gen2_reads[0].get('stream_offset')}（預期 0）", str(ev_dir))

    return CheckResult("ST-12", "PASS", "transport fault 斷線造成 peer EIO 且重連後世代與 offset 正確", str(ev_dir))


def _run_st13(_ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-13: 不確定寫入 (FakeEndpoint raise EndpointWriteUncertain 留 loss_flag: true)。"""
    class _UncertainEndpoint:
        def __init__(self) -> None:
            self.kind = "posix-pty"
            self.transport_mode = "virtual_pty"
            self.generation = 1

        def open(self) -> None:
            pass

        def read(self, _n: int, _timeout_s: float) -> bytes:
            return b""

        def write_some(self, data: bytes, _timeout_s: float) -> int:
            raise EndpointWriteUncertain(min(10, len(data)), "write timeout")

        def peer_open(self) -> bool:
            return True

        def capabilities(self) -> dict[str, bool]:
            return {}

        def describe(self) -> dict[str, Any]:
            return {"kind": "uncertain", "generation": 1}

        def close(self) -> None:
            pass

    class _StubDut:
        def __init__(self) -> None:
            self.generation = 1
            self.stdout_buf = bytearray(b"0123456789")

        def stdin_write_some(self, data: bytes) -> int:
            return len(data)

        def stdout_read(self, n: int, _timeout_s: float = 0.05) -> bytes:
            if not self.stdout_buf:
                return b""
            chunk = bytes(self.stdout_buf[:n])
            del self.stdout_buf[:n]
            return chunk

        def stderr_read(self, _n: int) -> bytes:
            return b""

        def poll(self) -> int | None:
            return None

        def terminate(self, _grace_s: float = 0.5) -> int:
            return 0

        def kill(self) -> int:
            return 0

        def describe(self) -> dict[str, Any]:
            return {"mode": "stub", "generation": 1}

        def spawn(self) -> None:
            pass

    endpoint = _UncertainEndpoint()
    dut = _StubDut()
    writer = EvidenceWriter(ev_dir / "wire.ndjson", run_id="st13", case_id="st13", clock_id=clock_id())
    cfg = _make_bridge_config(run_id="st13", case_id="st13", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    exit_code = _wait_bridge(bridge, timeout_s=10.0)
    if exit_code != 3:
        return CheckResult("ST-13", "FAIL", f"bridge 退出代碼為 {exit_code}（預期 3）", str(ev_dir))
    if bridge.failure != "HARNESS_WRITE_UNCERTAIN":
        return CheckResult("ST-13", "FAIL", f"bridge failure 為 {bridge.failure}（預期 HARNESS_WRITE_UNCERTAIN）", str(ev_dir))

    wire_lines = [json.loads(line) for line in (ev_dir / "wire.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
    unc_records = [r for r in wire_lines if r.get("meta", {}).get("event") == "write_uncertain"]
    if not unc_records or unc_records[0].get("loss_flag") is not True:
        return CheckResult("ST-13", "FAIL", "缺少 loss_flag: true 之 write_uncertain META 紀錄", str(ev_dir))

    return CheckResult("ST-13", "PASS", "端點不確定寫入正確 fail-stop 且記錄 loss_flag: true", str(ev_dir))


def _run_st14(_ctx: SelftestContext, ev_dir: pathlib.Path) -> CheckResult:
    """ST-14: 非預期 overflow (writer os_write 阻塞觸發 HARNESS_EVIDENCE_OVERFLOW)。"""
    block_event = threading.Event()

    def _blocking_write(_fd: int, data: bytes) -> int:
        block_event.wait(5.0)
        return len(data)

    class _StubEndpoint:
        def __init__(self) -> None:
            self.kind = "posix-pty"
            self.transport_mode = "virtual_pty"
            self.generation = 1

        def open(self) -> None:
            pass

        def read(self, n: int, _timeout_s: float) -> bytes:
            return b"X" * min(n, 64)

        def write_some(self, data: bytes, _timeout_s: float) -> int:
            return len(data)

        def peer_open(self) -> bool:
            return True

        def capabilities(self) -> dict[str, bool]:
            return {}

        def describe(self) -> dict[str, Any]:
            return {"kind": "stub", "generation": 1}

        def close(self) -> None:
            pass

    class _StubDut:
        def __init__(self) -> None:
            self.generation = 1

        def stdin_write_some(self, data: bytes) -> int:
            return len(data)

        def stdout_read(self, _n: int, _timeout_s: float = 0.05) -> bytes:
            return b""

        def stderr_read(self, _n: int) -> bytes:
            return b""

        def poll(self) -> int | None:
            return None

        def terminate(self, _grace_s: float = 0.5) -> int:
            return 0

        def kill(self) -> int:
            return 0

        def describe(self) -> dict[str, Any]:
            return {"mode": "stub", "generation": 1}

        def spawn(self) -> None:
            pass

    endpoint = _StubEndpoint()
    dut = _StubDut()
    writer = EvidenceWriter(
        ev_dir / "wire.ndjson",
        run_id="st14",
        case_id="st14",
        clock_id=clock_id(),
        max_pending_records=8,
        block_timeout_s=0.2,
        os_write=_blocking_write,
    )
    cfg = _make_bridge_config(run_id="st14", case_id="st14", port_id="COM0", evidence_dir=ev_dir)
    bridge = Bridge(cfg, endpoint, dut, writer, dut_control=None)
    bridge.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and bridge.failure is None:
        time.sleep(0.02)

    block_event.set()
    exit_code = _wait_bridge(bridge, timeout_s=5.0)

    if exit_code != 3:
        return CheckResult("ST-14", "FAIL", f"bridge 退出代碼為 {exit_code}（預期 3）", str(ev_dir))
    if bridge.failure != "HARNESS_EVIDENCE_OVERFLOW":
        return CheckResult("ST-14", "FAIL", f"bridge failure 為 {bridge.failure}（預期 HARNESS_EVIDENCE_OVERFLOW）", str(ev_dir))

    wal_dir = _make_wal_and_correlation(ev_dir, b"X" * 10, b"")
    cmp_res = compare(CompareInputs(evidence_dir=ev_dir, wal_dir=wal_dir, port_map={"COM0": ("COM0",)}))
    if cmp_res.get("verdict") != "INSUFFICIENT_EVIDENCE":
        return CheckResult("ST-14", "FAIL", f"比較器回傳判定為 {cmp_res.get('verdict')}（預期 INSUFFICIENT_EVIDENCE）", str(ev_dir))

    return CheckResult("ST-14", "PASS", "證據 writer 佇列溢位正確觸發 HARNESS_EVIDENCE_OVERFLOW 與 fail-stop", str(ev_dir))


_CHECK_RUNNERS: dict[str, Callable[[SelftestContext, pathlib.Path], CheckResult]] = {
    "ST-01": _run_st01,
    "ST-02": _run_st02,
    "ST-03": _run_st03,
    "ST-04": _run_st04,
    "ST-05": _run_st05,
    "ST-06": _run_st06,
    "ST-07": _run_st07,
    "ST-08": _run_st08,
    "ST-09": _run_st09,
    "ST-10": _run_st10,
    "ST-11": _run_st11,
    "ST-12": _run_st12,
    "ST-13": _run_st13,
    "ST-14": _run_st14,
}


def run_check(check_id: str, ctx: SelftestContext) -> CheckResult:
    """執行單一自測項目並回傳 CheckResult。"""
    if check_id not in _CHECK_RUNNERS:
        return CheckResult(check_id, "FAIL", f"未知自測代碼: {check_id}", str(ctx.work_dir))

    evidence_dir = ctx.work_dir / check_id
    ensure_private_dir(evidence_dir)

    try:
        return _CHECK_RUNNERS[check_id](ctx, evidence_dir)
    except Exception as exc:
        return CheckResult(check_id, "FAIL", f"執行例外: {exc}", str(evidence_dir))
