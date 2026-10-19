"""EvidenceWriter：多執行緒有界佇列與單一 writer 執行緒的 wire.ndjson 寫入器。"""

from __future__ import annotations

import collections
import errno
import hashlib
import os
import pathlib
import queue
import threading
import time
from typing import Any, Callable

from uart_stdio_bridge.evidence.records import (
    dumps_record,
    make_data_record,
    make_meta_record,
    wall_ts_now,
)


class EvidenceFailure(RuntimeError):
    """證據寫入重大失敗。"""

    def __init__(self, message: str, code: str = "HARNESS_EVIDENCE_WRITE_FAILED") -> None:
        super().__init__(message)
        self.code = code


class EvidenceWriter:
    """維護 wire.ndjson 循序寫入與有界背壓佇列。"""

    def __init__(
        self,
        path: pathlib.Path,
        *,
        run_id: str,
        case_id: str,
        clock_id: str,
        max_pending_records: int = 65_536,
        block_timeout_s: float = 5.0,
        os_write: Callable[[int, bytes], int] = os.write,
        os_fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self.case_id = case_id
        self.clock_id = clock_id
        self.max_pending_records = max_pending_records
        self.block_timeout_s = block_timeout_s
        self._os_write = os_write
        self._os_fsync = os_fsync

        self._seq = 0
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._offsets: dict[tuple[str, int, str], int] = collections.defaultdict(int)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=max_pending_records)
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        self._fd: int | None = None
        self._records_written = 0
        self._bytes_written = 0
        self._hasher = hashlib.sha256()
        self._closed = False

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            mode = 0o600
            try:
                self._fd = os.open(str(self.path), flags, mode)
            except OSError as exc:
                # 既有證據檔不得覆寫或續寫（O_EXCL）；任何開檔失敗一律以 harness 碼回報
                self._failure = "HARNESS_EVIDENCE_WRITE_FAILED"
                raise EvidenceFailure(
                    f"無法建立證據檔 {self.path}: {exc}", code="HARNESS_EVIDENCE_WRITE_FAILED"
                ) from exc
            self._thread = threading.Thread(
                target=self._worker, name="evidence-writer", daemon=True
            )
            self._thread.start()

    def _worker(self) -> None:
        assert self._fd is not None
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            try:
                total = len(item)
                written = 0
                while written < total:
                    n = self._os_write(self._fd, item[written:])
                    if n <= 0:
                        raise OSError("Evidence write returned <= 0")
                    written += n
                self._bytes_written += total
                self._records_written += 1
                self._hasher.update(item)
            except Exception as exc:
                with self._lock:
                    if self._failure is None:
                        err = getattr(exc, "errno", None)
                        if err in (28, 27, getattr(errno, "ENOSPC", 28), getattr(errno, "EFBIG", 27), getattr(errno, "EDQUOT", 122)):
                            self._failure = "HARNESS_DISK_FULL"
                        else:
                            self._failure = "HARNESS_EVIDENCE_WRITE_FAILED"
            finally:
                self._queue.task_done()

    def emit_data(
        self,
        *,
        port_id: str,
        endpoint_generation: int,
        dut_generation: int,
        observation: str,
        payload: bytes,
    ) -> int:
        if len(payload) == 0:
            raise ValueError("len(payload) 不得為 0")
        with self._emit_lock:
            with self._lock:
                if self._failure is not None:
                    raise EvidenceFailure(
                        f"Evidence writer failed: {self._failure}", code=self._failure
                    )
                self._seq += 1
                seq = self._seq
                key = (port_id, endpoint_generation, observation)
                offset = self._offsets[key]
                self._offsets[key] += len(payload)
                mono_ts = time.monotonic_ns()
                wall_ts = wall_ts_now()
                rec = make_data_record(
                    run_id=self.run_id,
                    case_id=self.case_id,
                    seq=seq,
                    mono_ts_ns=mono_ts,
                    wall_ts=wall_ts,
                    clock_id=self.clock_id,
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                    observation=observation,
                    stream_offset=offset,
                    payload=payload,
                )
            data = dumps_record(rec)
            try:
                self._queue.put(data, timeout=self.block_timeout_s)
            except queue.Full:
                with self._lock:
                    self._failure = "HARNESS_EVIDENCE_OVERFLOW"
                raise EvidenceFailure("Queue full", code="HARNESS_EVIDENCE_OVERFLOW")
            return seq

    def emit_meta(
        self,
        *,
        port_id: str,
        endpoint_generation: int,
        dut_generation: int,
        meta: dict[str, Any],
        loss_flag: bool = False,
    ) -> int:
        with self._emit_lock:
            with self._lock:
                if self._failure is not None:
                    raise EvidenceFailure(
                        f"Evidence writer failed: {self._failure}", code=self._failure
                    )
                self._seq += 1
                seq = self._seq
                mono_ts = time.monotonic_ns()
                wall_ts = wall_ts_now()
                rec = make_meta_record(
                    run_id=self.run_id,
                    case_id=self.case_id,
                    seq=seq,
                    mono_ts_ns=mono_ts,
                    wall_ts=wall_ts,
                    clock_id=self.clock_id,
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                    meta=meta,
                    loss_flag=loss_flag,
                )
            data = dumps_record(rec)
            try:
                self._queue.put(data, timeout=self.block_timeout_s)
            except queue.Full:
                with self._lock:
                    self._failure = "HARNESS_EVIDENCE_OVERFLOW"
                raise EvidenceFailure("Queue full", code="HARNESS_EVIDENCE_OVERFLOW")
            return seq

    def offsets(self, port_id: str, endpoint_generation: int) -> dict[str, int]:
        with self._lock:
            return {
                obs: self._offsets.get((port_id, endpoint_generation, obs), 0)
                for obs in (
                    "serial_read",
                    "dut_stdin_write",
                    "dut_stdout_read",
                    "serial_write",
                )
            }

    def flush(self, timeout_s: float = 5.0) -> None:
        self._queue.join()
        with self._lock:
            if self._failure is not None:
                raise EvidenceFailure(
                    f"Evidence failure: {self._failure}", code=self._failure
                )
            if self._fd is not None:
                try:
                    self._os_fsync(self._fd)
                except Exception as exc:
                    self._failure = "HARNESS_EVIDENCE_WRITE_FAILED"
                    raise EvidenceFailure(
                        str(exc), code="HARNESS_EVIDENCE_WRITE_FAILED"
                    ) from exc

    def close(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return {
                    "records": self._records_written,
                    "bytes": self._bytes_written,
                    "sha256": self._hasher.hexdigest(),
                }
            self._closed = True
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=10.0)
        with self._lock:
            if self._fd is not None:
                try:
                    self._os_fsync(self._fd)
                except OSError:
                    pass
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            return {
                "records": self._records_written,
                "bytes": self._bytes_written,
                "sha256": self._hasher.hexdigest(),
            }
