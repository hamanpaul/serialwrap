"""DUT 標準 I/O 適配器（run_stdio 與 run_echo）。

連接 stdin/stdout 與 DutModel，處理 backpressure、時間排程與背景控制通道。
"""
from __future__ import annotations

import json
import os
import queue
import select
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from uart_stdio_bridge.dut.control_file import ControlFileChannel
from uart_stdio_bridge.dut.model import DutModel, Emit


def run_echo(
    *,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
    stderr_fd: int = 2,
    stderr_flood_bytes: int = 0,
    exit_after_bytes: int | None = None,
    exit_code: int = 0,
    close_stdout_after_bytes: int | None = None,
    read_delay_ms: int = 0,
    write_chunk: int = 4096,
) -> int:
    """純二進位回顯模式（自測與效能測試專用）。"""

    def _flood_stderr() -> None:
        remaining = stderr_flood_bytes
        chunk = b"E" * min(65536, remaining)
        while remaining > 0:
            to_write = min(len(chunk), remaining)
            try:
                n = os.write(stderr_fd, chunk[:to_write])
                if n <= 0:
                    break
                remaining -= n
            except OSError:
                break

    flood_thread: threading.Thread | None = None
    if stderr_flood_bytes > 0:
        flood_thread = threading.Thread(target=_flood_stderr, daemon=True)
        flood_thread.start()

    total_written = 0
    stdout_closed = False

    while True:
        if read_delay_ms > 0:
            time.sleep(read_delay_ms / 1000.0)
        try:
            data = os.read(stdin_fd, write_chunk)
        except OSError:
            break
        if not data:
            break

        if not stdout_closed:
            to_write = data
            if exit_after_bytes is not None:
                remaining = exit_after_bytes - total_written
                if len(to_write) > remaining:
                    to_write = to_write[: max(0, remaining)]
            if close_stdout_after_bytes is not None:
                remaining = close_stdout_after_bytes - total_written
                if len(to_write) > remaining:
                    to_write = to_write[: max(0, remaining)]

            if to_write:
                offset = 0
                while offset < len(to_write):
                    try:
                        n = os.write(stdout_fd, to_write[offset:])
                        if n <= 0:
                            stdout_closed = True
                            break
                        offset += n
                    except OSError:
                        stdout_closed = True
                        break
                total_written += offset

            if exit_after_bytes is not None and total_written >= exit_after_bytes:
                if flood_thread is not None:
                    flood_thread.join(timeout=30)
                return exit_code

            if close_stdout_after_bytes is not None and total_written >= close_stdout_after_bytes:
                try:
                    os.close(stdout_fd)
                except OSError:
                    pass
                stdout_closed = True

    if flood_thread is not None:
        flood_thread.join(timeout=30)

    if exit_after_bytes is not None and total_written >= exit_after_bytes:
        return exit_code
    return 0


def run_stdio(
    model: DutModel,
    *,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
    stderr_fd: int = 2,
    control: ControlFileChannel | None = None,
    clock_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000,
    read_chunk: int = 4096,
) -> int:
    """以標準串流驅動 DutModel，stdout 僅輸出 DUT bytes，診斷一律以 JSON lines 輸出至 stderr。"""

    def log_diag(event: str, **extra: Any) -> None:
        payload = {"t_ms": clock_ms(), "event": event, **extra}
        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            os.write(stderr_fd, line)
        except OSError:
            pass

    def write_emits(emits: list[Emit]) -> None:
        for e in emits:
            data = e.data
            if not data:
                continue
            offset = 0
            while offset < len(data):
                try:
                    n = os.write(stdout_fd, data[offset:])
                    if n <= 0:
                        break
                    offset += n
                except (BlockingIOError, InterruptedError):
                    if sys.platform != "win32":
                        try:
                            select.select([], [stdout_fd], [], 0.05)
                        except OSError:
                            time.sleep(0.01)
                    else:
                        time.sleep(0.01)
                except OSError as exc:
                    log_diag("stdout_write_failed", error=str(exc))
                    raise

    now_ms = clock_ms()
    log_diag("boot", generation=model.generation)
    boot_emits = model.boot(now_ms)
    try:
        write_emits(boot_emits)
    except OSError:
        return 1

    input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
    stop_event = threading.Event()
    stdin_resume_event = threading.Event()
    stdin_resume_event.set()
    stdin_eof_event = threading.Event()

    def stdin_worker() -> None:
        while not stop_event.is_set():
            if not stdin_resume_event.is_set():
                if sys.platform != "win32":
                    try:
                        r, _, _ = select.select([stdin_fd], [], [], 0.05)
                        if not r:
                            continue
                    except OSError:
                        pass
                else:
                    time.sleep(0.01)

            if stop_event.is_set():
                break
            try:
                chunk = os.read(stdin_fd, read_chunk)
            except (BlockingIOError, InterruptedError):
                time.sleep(0.01)
                continue
            except OSError:
                chunk = b""
            if not chunk:
                stdin_eof_event.set()
                input_queue.put(None)
                break
            input_queue.put(chunk)

    reader_thread = threading.Thread(target=stdin_worker, daemon=True, name="dut-stdin")
    reader_thread.start()

    last_control_poll_ms = 0
    stdin_eof = False

    try:
        while True:
            now_ms = clock_ms()

            if model.stdin_paused:
                stdin_resume_event.clear()
            else:
                stdin_resume_event.set()

            if control is not None and (
                now_ms - last_control_poll_ms >= 50 or last_control_poll_ms == 0
            ):
                msgs = control.poll()
                for msg in msgs:
                    ctl_emits, ack = model.apply_control(msg, now_ms)
                    try:
                        control.ack(ack)
                    except OSError as exc:
                        log_diag("control_ack_failed", error=str(exc))
                    if ctl_emits:
                        try:
                            write_emits(ctl_emits)
                        except OSError:
                            return 1
                last_control_poll_ms = now_ms
                if model.stdin_paused:
                    stdin_resume_event.clear()
                else:
                    stdin_resume_event.set()

            poll_emits = model.poll(now_ms)
            if poll_emits:
                try:
                    write_emits(poll_emits)
                except OSError:
                    return 1

            next_d = model.next_deadline_ms()
            if next_d is not None:
                timeout_s = max(0.0, (next_d - now_ms) / 1000.0)
                if control is not None:
                    timeout_s = min(timeout_s, 0.05)
            else:
                timeout_s = 0.05 if control is not None else 0.1

            if stdin_eof_event.is_set():
                stdin_eof = True

            if not model.stdin_paused:
                try:
                    item = input_queue.get(timeout=min(timeout_s, 0.05))
                    if item is None:
                        stdin_eof = True
                    else:
                        now_ms = clock_ms()
                        feed_emits = model.feed(item, now_ms)
                        if feed_emits:
                            try:
                                write_emits(feed_emits)
                            except OSError:
                                return 1
                except queue.Empty:
                    pass
            else:
                time.sleep(min(timeout_s, 0.05))

            if stdin_eof:
                if not model.stdin_paused:
                    while not input_queue.empty():
                        try:
                            rem = input_queue.get_nowait()
                            if rem is not None:
                                now_ms = clock_ms()
                                feed_emits = model.feed(rem, now_ms)
                                if feed_emits:
                                    try:
                                        write_emits(feed_emits)
                                    except OSError:
                                        return 1
                        except queue.Empty:
                            break

                now_ms = clock_ms()
                final_emits = model.poll(now_ms)
                if final_emits:
                    try:
                        write_emits(final_emits)
                    except OSError:
                        return 1
                log_diag("exit", code=0)
                return 0
    except OSError as exc:
        log_diag("stdio_error", error=str(exc))
        return 1
    finally:
        stop_event.set()
        stdin_resume_event.set()
        reader_thread.join(timeout=1.0)
