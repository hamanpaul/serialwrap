#!/usr/bin/env python3
"""S1-W WAL 短測工具（Stage 0 P1 Task T19）。

以 S1-W 相同資料混合與指定速率（預設 10,240 B/s/流）持續寫入指定秒數（預設 3600 秒／1 小時），
期間至少跨越 1 次 rotate，並驗證無寫入失敗、無遺失紀錄、無 seq 缺口，且達成率 ≥ 目標 95%。
依據：docs/rust-refactor/plans/02-stage0-python-bc.md Task T19（OD-10(2)）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from wal_microbench import collect_env, generate_payload
from sw_core.wal import WalWriter


def _run_shorttest_stream(
    stream_idx: int,
    wal: WalWriter,
    duration_s: float,
    rate_bps: int,
    payload_len: int,
    start_barrier: threading.Barrier,
    ordinary_samples_ns: list[int],
    rotate_pauses_ns: list[int],
    rotations_count: list[int],
    bytes_sent_box: list[int],
) -> None:
    """單一 stream 依目標速率持續寫入直到 duration_s 到期。"""
    start_barrier.wait()
    t_start = time.monotonic()
    t_deadline = t_start + duration_s

    last_archive_count = len(glob.glob(f"{wal.wal_path}.*"))
    idx = 0
    total_bytes = 0

    chunk_size = min(payload_len, rate_bps) if rate_bps > 0 else payload_len

    while time.monotonic() < t_deadline:
        data = generate_payload(chunk_size, stream_idx * 2000000 + idx)
        t0 = time.perf_counter_ns()
        wal.append(
            com=f"COM{stream_idx}",
            direction="RX",
            source="shorttest",
            payload=data,
        )
        t1 = time.perf_counter_ns()
        elapsed = t1 - t0
        total_bytes += len(data)
        idx += 1

        curr_archive_count = len(glob.glob(f"{wal.wal_path}.*"))
        if curr_archive_count > last_archive_count:
            rotate_pauses_ns.append(elapsed)
            rotations_count[0] += (curr_archive_count - last_archive_count)
            last_archive_count = curr_archive_count
        else:
            ordinary_samples_ns.append(elapsed)

        # 速率節流
        if rate_bps > 0:
            target_elapsed = total_bytes / rate_bps
            now_elapsed = time.monotonic() - t_start
            rem = target_elapsed - now_elapsed
            if rem > 0:
                time.sleep(rem)

    bytes_sent_box[0] = total_bytes


def _scan_stream_records(stream_dir: Path) -> tuple[int, int]:
    """掃描 stream 目錄下所有 WAL 檔案，回傳 (loss_records, seq_gaps)。"""
    wal_files = sorted(stream_dir.glob("raw.wal.ndjson*"))
    all_records: list[dict[str, Any]] = []

    for wf in wal_files:
        try:
            for line in wf.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and isinstance(obj.get("seq"), int):
                        all_records.append(obj)
                except Exception:
                    pass
        except Exception:
            pass

    # 依 seq 排序
    all_records.sort(key=lambda r: r["seq"])

    loss_records = sum(1 for r in all_records if r.get("loss_flag") is True)
    seq_gaps = 0
    if all_records:
        if all_records[0]["seq"] != 1:
            seq_gaps += 1
        for i in range(len(all_records) - 1):
            if all_records[i + 1]["seq"] != all_records[i]["seq"] + 1:
                seq_gaps += 1

    return loss_records, seq_gaps


def run_wal_shorttest(
    *,
    duration_s: float,
    rate_bps: int,
    streams: int,
    rotate_bytes: int,
    prefill_ratio: float,
    wal_root: Path,
    payload_len: int = 1024,
) -> tuple[dict[str, Any], bool]:
    """執行短測並判定是否滿足驗收條件。"""
    wal_root.mkdir(parents=True, exist_ok=True)
    writers: list[WalWriter] = []
    prefill_bytes: list[int] = []

    # 1. 預填至 prefill_ratio * rotate_bytes
    target_prefill = int(prefill_ratio * rotate_bytes)
    for i in range(streams):
        s_dir = wal_root / f"s{i}"
        s_dir.mkdir(parents=True, exist_ok=True)
        w = WalWriter(wal_dir=str(s_dir), rotate_bytes=rotate_bytes)
        writers.append(w)

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
                payload=generate_payload(fill_chunk, i * 700000 + fill_idx),
            )
            fill_idx += 1
        prefill_bytes.append(os.path.getsize(w.wal_path))

    # 2. 多流並行寫入
    barrier = threading.Barrier(streams)
    stream_ordinary: list[list[int]] = [[] for _ in range(streams)]
    stream_rotates: list[list[int]] = [[] for _ in range(streams)]
    stream_rotations: list[list[int]] = [[0] for _ in range(streams)]
    stream_bytes_sent: list[list[int]] = [[0] for _ in range(streams)]
    threads: list[threading.Thread] = []

    t_wall_start = time.monotonic()
    for i in range(streams):
        t = threading.Thread(
            target=_run_shorttest_stream,
            args=(
                i,
                writers[i],
                duration_s,
                rate_bps,
                payload_len,
                barrier,
                stream_ordinary[i],
                stream_rotates[i],
                stream_rotations[i],
                stream_bytes_sent[i],
            ),
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()
    t_wall_end = time.monotonic()
    actual_duration = max(t_wall_end - t_wall_start, 0.001)

    # 取得 health 計數後關閉
    write_failures = sum(w.health().get("write_failures", 0) for w in writers)
    recreated_count = sum(w.health().get("recreated_count", 0) for w in writers)
    for w in writers:
        w.close()

    # 3. 掃描所有 WAL 檔案檢查 gaps 與 loss
    total_loss_records = 0
    total_seq_gaps = 0
    for i in range(streams):
        s_dir = wal_root / f"s{i}"
        l_rec, s_gaps = _scan_stream_records(s_dir)
        total_loss_records += l_rec
        total_seq_gaps += s_gaps

    # 4. 匯總分位數與速率
    all_ordinary: list[int] = []
    all_rotates: list[int] = []
    rotations_per_stream: list[int] = []
    total_bytes_sent = sum(b[0] for b in stream_bytes_sent)

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
    achieved_rate_bps = (total_bytes_sent / streams) / actual_duration
    env_info = collect_env(wal_root)

    total_records = n_ord + len(all_rotates)

    out_data = {
        "env": env_info,
        "rotate_bytes": rotate_bytes,
        "prefill_bytes": prefill_bytes,
        "total_records": total_records,
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
        "duration_s": actual_duration,
        "achieved_rate_bps": achieved_rate_bps,
        "write_failures": write_failures,
        "recreated_count": recreated_count,
        "loss_records": total_loss_records,
        "seq_gaps": total_seq_gaps,
    }

    # 判定標準：write_failures==0、loss_records==0、seq_gaps==0、achieved_rate ≥ 目標 95%
    passed = (
        write_failures == 0
        and total_loss_records == 0
        and total_seq_gaps == 0
        and achieved_rate_bps >= (0.95 * rate_bps)
    )
    return out_data, passed


def main() -> None:
    parser = argparse.ArgumentParser(description="S1-W WAL 短測工具")
    parser.add_argument("--duration-s", type=float, default=3600.0, help="測試總秒數（預設 3600）")
    parser.add_argument("--rate", type=int, default=10240, help="每流目標速率 bytes/s（預設 10240）")
    parser.add_argument("--streams", type=int, default=4, help="並行流數（預設 4）")
    parser.add_argument("--rotate-bytes", type=int, default=67108864, help="WAL rotate 門檻（預設 64 MiB）")
    parser.add_argument("--prefill-ratio", type=float, default=0.9375, help="預填比例（預設 0.9375）")
    parser.add_argument("--payload", type=int, default=1024, help="每筆 payload 大小（預設 1024）")
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
        data, ok = run_wal_shorttest(
            duration_s=args.duration_s,
            rate_bps=args.rate,
            streams=args.streams,
            rotate_bytes=args.rotate_bytes,
            prefill_ratio=args.prefill_ratio,
            wal_root=wal_root,
            payload_len=args.payload,
        )
    finally:
        if cleanup:
            td.cleanup()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
