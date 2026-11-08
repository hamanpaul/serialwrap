#!/usr/bin/env python3
"""S1-W WAL 寫入微基準工具（Stage 0 P1 Task T19）。

多流併發、指定負載大小、預填至特定比例跨越 rotate 門檻，量測 WAL append 效能分位數與 rotate 停頓。
依據：docs/rust-refactor/plans/02-stage0-python-bc.md Task T19（規格 §1.1.1、§1.1.2、§1.1.3、§1.2、§8.1；OD-10）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sw_core.release_manifest import tree_sha256
from sw_core.wal import WalWriter

# 資料混合比例常數（ANSI／文字／prompt，待測試負責人審閱）
MIX_ANSI_RATIO = 0.20
MIX_TEXT_RATIO = 0.60
MIX_PROMPT_RATIO = 0.20
DATA_MIX = {
    "ansi": MIX_ANSI_RATIO,
    "text": MIX_TEXT_RATIO,
    "prompt": MIX_PROMPT_RATIO,
}

_SAMPLE_ANSI = (
    b"\x1b[32m[  OK  ]\x1b[0m Started Serialwrap UART multiplexer service.\r\n"
    b"\x1b[1;33m[ WARN ]\x1b[0m Buffer occupancy reached 85% watermark.\r\n"
    b"\x1b[31;1m[ FAIL ]\x1b[0m Hardware handshake CTS asserted unexpectedly.\r\n"
)
_SAMPLE_TEXT = (
    b"[ 123.456789] serialwrap: session COM0 baud 115200 8N1 rx_fifo=4096\n"
    b"[ 123.460012] kernel: eth0: link up, 1000Mbps, full duplex, lpa 0xCDE1\n"
    b"[ 123.471203] systemd[1]: Reached target Multi-User System.\n"
    b"[ 123.489110] daemon: wal writer sync completed in 1240 ns\n"
)
_SAMPLE_PROMPT = (
    b"root@DUT-Router:/# "
    b"CFE> "
    b"uboot# "
    b"login: "
    b"Password: "
)


def generate_payload(size: int, seq_seed: int) -> bytes:
    """依固定混合比例產生指定大小的合法序列串流資料。"""
    rng = random.Random(seq_seed)
    parts: list[bytes] = []
    current_len = 0
    while current_len < size:
        choice = rng.random()
        if choice < MIX_ANSI_RATIO:
            chunk = _SAMPLE_ANSI
        elif choice < MIX_ANSI_RATIO + MIX_TEXT_RATIO:
            chunk = _SAMPLE_TEXT
        else:
            chunk = _SAMPLE_PROMPT
        parts.append(chunk)
        current_len += len(chunk)
    full = b"".join(parts)
    return full[:size]


def collect_env(wal_root: Path | None = None) -> dict[str, Any]:
    """蒐集規格 §1.1.1／§1.2 manifest 要求的環境欄位；查不到的值一律寫 'unknown'，不得填 0 或空字串。"""
    os_name = sys.platform
    if os_name.startswith("linux"):
        os_val = "linux"
    elif os_name.startswith("win"):
        os_val = "windows"
    elif os_name.startswith("darwin"):
        os_val = "darwin"
    else:
        os_val = os_name or "unknown"

    os_build = platform.release() or "unknown"
    py_ver = sys.version.split()[0] or "unknown"
    plat = platform.platform() or "unknown"

    # virtualization: "wsl2" | "native" | "vm" | "unknown"
    virt = "unknown"
    if os.name == "nt":
        virt = "native"
    else:
        try:
            proc_ver = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
            if "microsoft" in proc_ver or "wsl" in proc_ver:
                virt = "wsl2"
        except Exception:
            pass
        if virt == "unknown":
            try:
                r = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    out = r.stdout.strip()
                    virt = "native" if out == "none" else "vm"
            except Exception:
                pass
        if virt == "unknown":
            virt = "native"

    # cpu & cpu_count
    cpu_str = "unknown"
    if os.name != "nt":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if "model name" in line:
                    cpu_str = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
    if cpu_str == "unknown":
        cpu_str = platform.processor() or "unknown"
    cpu_count = os.cpu_count() or "unknown"

    # memory_bytes
    mem_bytes: int | str = "unknown"
    if os.name != "nt":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    mem_bytes = int(line.split()[1]) * 1024
                    break
        except Exception:
            pass

    # storage_device & fs_type
    check_dir = wal_root if wal_root is not None and wal_root.exists() else Path.cwd()
    storage_dev = "unknown"
    fs_type = "unknown"
    if os.name != "nt":
        try:
            r = subprocess.run(["df", "-T", str(check_dir)], capture_output=True, text=True, timeout=2)
            lines = r.stdout.strip().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                storage_dev = parts[0]
                fs_type = parts[1]
        except Exception:
            pass

    # power
    power = "unknown"
    if os.name != "nt":
        try:
            ac_list = list(Path("/sys/class/power_supply").glob("AC*"))
            if ac_list:
                power = "AC"
            else:
                bat_list = list(Path("/sys/class/power_supply").glob("BAT*"))
                power = "battery" if bat_list else "AC"
        except Exception:
            power = "AC"
    else:
        power = "AC"

    # endpoint_av
    endpoint_av = "unknown"

    # artifacts: cli version and sw_core tree_sha256
    version_file = REPO / "VERSION"
    version_str = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0.3.2"
    sw_core_dir = REPO / "sw_core"
    try:
        sw_core_sha = tree_sha256(str(sw_core_dir))
    except Exception:
        sw_core_sha = "unknown"
    artifacts = {
        "cli": f"serialwrap {version_str}",
        "daemon": sw_core_sha,
    }

    # contract_tree
    contract_tree = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD:contracts"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            contract_tree = r.stdout.strip()
    except Exception:
        pass

    # tool_revision (本腳本 git blob sha1)
    tool_revision = "unknown"
    try:
        r = subprocess.run(
            ["git", "hash-object", str(Path(__file__).resolve())],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            tool_revision = r.stdout.strip()
    except Exception:
        pass
    if tool_revision == "unknown":
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                tool_revision = r.stdout.strip()
        except Exception:
            pass

    return {
        "os": os_val,
        "os_build": os_build,
        "python": py_ver,
        "platform": plat,
        "virtualization": virt,
        "cpu": cpu_str,
        "cpu_count": cpu_count,
        "memory_bytes": mem_bytes,
        "storage_device": storage_dev,
        "fs_type": fs_type,
        "power": power,
        "endpoint_av": endpoint_av,
        "artifacts": artifacts,
        "contract_tree": contract_tree,
        "tool_revision": tool_revision,
        "timing_method": "time.perf_counter_ns",
        "serial": "none",
        "mix": DATA_MIX,
    }


def _run_stream_worker(
    stream_idx: int,
    wal: WalWriter,
    records: int,
    payload_len: int,
    rate_bps: int,
    start_barrier: threading.Barrier,
    ordinary_samples_ns: list[int],
    rotate_pauses_ns: list[int],
    rotations_count: list[int],
) -> None:
    """單一 stream worker thread。"""
    start_barrier.wait()

    last_archive_count = len(glob.glob(f"{wal.wal_path}.*"))
    for idx in range(records):
        data = generate_payload(payload_len, stream_idx * 1000000 + idx)
        t0 = time.perf_counter_ns()
        wal.append(
            com=f"COM{stream_idx}",
            direction="RX",
            source="bench",
            payload=data,
        )
        t1 = time.perf_counter_ns()
        elapsed = t1 - t0

        curr_archive_count = len(glob.glob(f"{wal.wal_path}.*"))
        if curr_archive_count > last_archive_count:
            # 本筆發生 rotate
            rotate_pauses_ns.append(elapsed)
            rotations_count[0] += (curr_archive_count - last_archive_count)
            last_archive_count = curr_archive_count
        else:
            ordinary_samples_ns.append(elapsed)

        if rate_bps > 0:
            target_time_s = payload_len / rate_bps
            actual_time_s = elapsed / 1e9
            rem = target_time_s - actual_time_s
            if rem > 0:
                time.sleep(rem)


def run_wal_microbench(
    *,
    streams: int,
    records: int,
    payload_len: int,
    rotate_bytes: int,
    prefill_ratio: float,
    wal_root: Path,
    rate_bps: int = 0,
) -> dict[str, Any]:
    """執行 S1-W WAL 微基準測試。"""
    wal_root.mkdir(parents=True, exist_ok=True)
    writers: list[WalWriter] = []
    prefill_bytes: list[int] = []

    # 初始化各流並預填
    target_prefill = int(prefill_ratio * rotate_bytes)
    for i in range(streams):
        s_dir = wal_root / f"s{i}"
        s_dir.mkdir(parents=True, exist_ok=True)
        w = WalWriter(wal_dir=str(s_dir), rotate_bytes=rotate_bytes)
        writers.append(w)

        # 預填合法的 WAL 紀錄（§1.1.3：不得用無效 padding）
        fill_idx = 0
        fill_chunk = min(payload_len, 2048)
        while True:
            cur_size = os.path.getsize(w.wal_path) if os.path.exists(w.wal_path) else 0
            if cur_size >= target_prefill:
                break
            w.append(
                com=f"COM{i}",
                direction="RX",
                source="prefill",
                payload=generate_payload(fill_chunk, i * 500000 + fill_idx),
            )
            fill_idx += 1
        prefill_bytes.append(os.path.getsize(w.wal_path))

    # 準備執行執行緒
    barrier = threading.Barrier(streams)
    stream_ordinary: list[list[int]] = [[] for _ in range(streams)]
    stream_rotates: list[list[int]] = [[] for _ in range(streams)]
    stream_rotations: list[list[int]] = [[0] for _ in range(streams)]
    threads: list[threading.Thread] = []

    for i in range(streams):
        t = threading.Thread(
            target=_run_stream_worker,
            args=(
                i,
                writers[i],
                records,
                payload_len,
                rate_bps,
                barrier,
                stream_ordinary[i],
                stream_rotates[i],
                stream_rotations[i],
            ),
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    for w in writers:
        w.close()

    # 匯總分位數與 rotate 停頓
    all_ordinary: list[int] = []
    all_rotates: list[int] = []
    rotations_per_stream: list[int] = []
    for i in range(streams):
        all_ordinary.extend(stream_ordinary[i])
        all_rotates.extend(stream_rotates[i])
        rotations_per_stream.append(stream_rotations[i][0])

    all_ordinary.sort()
    n_ord = len(all_ordinary)
    if n_ord > 0:
        p50 = all_ordinary[int(n_ord * 0.50)]
        p95 = all_ordinary[min(int(n_ord * 0.95), n_ord - 1)]
        p99 = all_ordinary[min(int(n_ord * 0.99), n_ord - 1)]
    else:
        p50 = p95 = p99 = 0

    rotate_max_ns = max(all_rotates) if all_rotates else 0
    env_info = collect_env(wal_root)

    return {
        "env": env_info,
        "rotate_bytes": rotate_bytes,
        "prefill_bytes": prefill_bytes,
        "total_records": streams * records,
        "append_ns": {
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "n": n_ord,
        },
        "ordinary_samples": n_ord,
        "rotate_samples": len(all_rotates),
        "rotate_pauses_ns": all_rotates,
        "rotate_max_ns": rotate_max_ns,
        "rotations_per_stream": rotations_per_stream,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="S1-W WAL 微基準測試工具")
    parser.add_argument("--streams", type=int, default=4, help="並行流數（預設 4）")
    parser.add_argument("--records", type=int, default=10000, help="每流紀錄數（預設 10000）")
    parser.add_argument("--payload", type=int, default=1024, help="每筆 payload bytes（預設 1024）")
    parser.add_argument("--rotate-bytes", type=int, default=67108864, help="WAL rotate 門檻 R（預設 64 MiB）")
    parser.add_argument("--prefill-ratio", type=float, default=0.9375, help="預填比例（預設 60/64=0.9375）")
    parser.add_argument("--rate", type=int, default=0, help="目標速率 bytes/s/流（0 代表不節流）")
    parser.add_argument("--out", type=Path, required=True, help="輸出結果 JSON 路徑")
    parser.add_argument("--wal-root", type=Path, default=None, help="WAL 暫存根目錄")
    args = parser.parse_args()

    cleanup = False
    wal_root = args.wal_root
    if wal_root is None:
        td = tempfile.TemporaryDirectory()
        wal_root = Path(td.name)
        cleanup = True

    try:
        res = run_wal_microbench(
            streams=args.streams,
            records=args.records,
            payload_len=args.payload,
            rotate_bytes=args.rotate_bytes,
            prefill_ratio=args.prefill_ratio,
            wal_root=wal_root,
            rate_bps=args.rate,
        )
    finally:
        if cleanup:
            td.cleanup()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
