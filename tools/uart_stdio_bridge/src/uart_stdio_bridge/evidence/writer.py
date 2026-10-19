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
from dataclasses import dataclass
from typing import Any, Callable

from uart_stdio_bridge.evidence.manifest import ensure_private_dir
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


@dataclass(frozen=True)
class _PendingRecord:
    """佇列項目及建立失敗標記所需的來源資訊。"""

    payload: bytes
    consumes_slot: bool
    port_id: str
    endpoint_generation: int
    dut_generation: int


@dataclass
class _FlushRequest:
    """在單一 writer 執行緒中同步前序紀錄的 barrier。"""

    done: threading.Event
    port_id: str
    endpoint_generation: int
    dut_generation: int
    error_code: str | None = None


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
        close_timeout_s: float = 10.0,
        os_write: Callable[[int, bytes], int] = os.write,
        os_fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        if max_pending_records < 1:
            raise ValueError("max_pending_records 必須大於 0")
        if block_timeout_s < 0 or close_timeout_s < 0:
            raise ValueError("writer timeout 不得為負數")
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self.case_id = case_id
        self.clock_id = clock_id
        self.max_pending_records = max_pending_records
        self.block_timeout_s = block_timeout_s
        self.close_timeout_s = close_timeout_s
        self._os_write = os_write
        self._os_fsync = os_fsync

        self._seq = 0
        self._last_context: tuple[str, int, int] | None = None
        self._lock = threading.Lock()
        # emit 與 close/flush 共用此鎖，保證檔案 seq 順序與生命週期邊界一致。
        self._emit_lock = threading.Lock()
        self._offsets: dict[tuple[str, int, str], int] = collections.defaultdict(int)
        # 普通紀錄最多 max_pending_records；另外保留 flush barrier 與 evidence_failure META 各一格。
        self._queue: queue.Queue[_PendingRecord | _FlushRequest | None] = queue.Queue(
            maxsize=max_pending_records + 2
        )
        self._pending_slots = threading.BoundedSemaphore(max_pending_records)
        self._flush_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        self._worker_failed = False
        self._failure_marker_attempted = False
        self._fd: int | None = None
        self._records_written = 0
        self._bytes_written = 0
        self._hasher = hashlib.sha256()
        self._state = "new"
        self._sentinel_enqueued = False

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    @staticmethod
    def _classify_error(exc: BaseException) -> str:
        err = getattr(exc, "errno", None)
        if err in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
            return "HARNESS_DISK_FULL"
        return "HARNESS_EVIDENCE_WRITE_FAILED"

    def _set_failure_locked(self, code: str, *, worker_failed: bool = False) -> None:
        if self._failure is None:
            self._failure = code
        if worker_failed:
            self._worker_failed = True

    def _raise_failure(self) -> None:
        code = self.failure or "HARNESS_EVIDENCE_WRITE_FAILED"
        raise EvidenceFailure(f"Evidence writer failed: {code}", code=code)

    def _require_running_locked(self) -> None:
        if self._state != "running":
            raise EvidenceFailure(
                f"Evidence writer state is {self._state}; expected running",
                code=self._failure or "HARNESS_EVIDENCE_WRITE_FAILED",
            )
        if self._failure is not None:
            raise EvidenceFailure(
                f"Evidence writer failed: {self._failure}", code=self._failure
            )

    def start(self) -> None:
        with self._emit_lock:
            with self._lock:
                if self._state == "running":
                    return
                if self._state != "new":
                    raise EvidenceFailure(
                        f"Evidence writer cannot start from state {self._state}",
                        code=self._failure or "HARNESS_EVIDENCE_WRITE_FAILED",
                    )
            try:
                # 證據可能含登入資料；父目錄由 ensure_private_dir 建立並限制權限。
                ensure_private_dir(self.path.parent)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                fd = os.open(str(self.path), flags, 0o600)
            except OSError as exc:
                code = self._classify_error(exc)
                with self._lock:
                    self._set_failure_locked(code)
                    self._state = "closed"
                raise EvidenceFailure(
                    f"無法建立證據檔 {self.path}: {exc}", code=code
                ) from exc

            with self._lock:
                self._fd = fd
                self._state = "running"
                self._thread = threading.Thread(
                    target=self._worker, name="evidence-writer", daemon=True
                )
                thread = self._thread
            try:
                thread.start()
            except Exception as exc:
                with self._lock:
                    self._set_failure_locked(self._classify_error(exc), worker_failed=True)
                    self._state = "closed"
                    self._fd = None
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise EvidenceFailure(str(exc), code=self.failure or "HARNESS_EVIDENCE_WRITE_FAILED") from exc

    def _write_complete(self, payload: bytes) -> None:
        fd = self._fd
        if fd is None:
            raise OSError("Evidence file is not open")
        written = 0
        while written < len(payload):
            n = self._os_write(fd, payload[written:])
            if type(n) is not int or n <= 0 or n > len(payload) - written:
                raise OSError("Evidence write returned an invalid length")
            chunk = payload[written : written + n]
            with self._lock:
                self._hasher.update(chunk)
                self._bytes_written += n
            written += n
        with self._lock:
            self._records_written += 1

    def _best_effort_failure_marker(
        self,
        *,
        port_id: str,
        endpoint_generation: int,
        dut_generation: int,
        code: str,
    ) -> None:
        with self._lock:
            if self._failure_marker_attempted or self._fd is None:
                return
            self._failure_marker_attempted = True
            seq = self._seq + 1
            self._seq = seq
            record = make_meta_record(
                run_id=self.run_id,
                case_id=self.case_id,
                seq=seq,
                mono_ts_ns=time.monotonic_ns(),
                wall_ts=wall_ts_now(),
                clock_id=self.clock_id,
                port_id=port_id,
                endpoint_generation=endpoint_generation,
                dut_generation=dut_generation,
                meta={"event": "evidence_failure", "code": code},
                loss_flag=True,
            )
        try:
            self._write_complete(dumps_record(record))
        except Exception:
            # 原始寫入已失敗；標記只能 best-effort，failure 仍會讓本次證據失效。
            return

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                break
            if isinstance(item, _FlushRequest):
                try:
                    with self._lock:
                        if self._worker_failed:
                            item.error_code = self._failure or "HARNESS_EVIDENCE_WRITE_FAILED"
                            continue
                        fd = self._fd
                    if fd is not None:
                        self._os_fsync(fd)
                except Exception as exc:
                    code = self._classify_error(exc)
                    with self._lock:
                        self._set_failure_locked(code, worker_failed=True)
                    item.error_code = code
                    self._best_effort_failure_marker(
                        port_id=item.port_id,
                        endpoint_generation=item.endpoint_generation,
                        dut_generation=item.dut_generation,
                        code=code,
                    )
                finally:
                    item.done.set()
                    self._queue.task_done()
                continue
            if item.consumes_slot:
                # 有界上限計算尚未開始寫出的 queue backlog；目前寫入項目不占佇列容量。
                self._pending_slots.release()
            try:
                with self._lock:
                    skip = self._worker_failed
                if skip:
                    continue
                self._write_complete(item.payload)
            except Exception as exc:
                code = self._classify_error(exc)
                with self._lock:
                    self._set_failure_locked(code, worker_failed=True)
                self._best_effort_failure_marker(
                    port_id=item.port_id,
                    endpoint_generation=item.endpoint_generation,
                    dut_generation=item.dut_generation,
                    code=code,
                )
            finally:
                self._queue.task_done()

    def _enqueue_overflow_marker_locked(
        self,
        *,
        port_id: str,
        endpoint_generation: int,
        dut_generation: int,
    ) -> None:
        code = "HARNESS_EVIDENCE_OVERFLOW"
        with self._lock:
            self._require_running_locked()
            seq = self._seq + 1
            marker = make_meta_record(
                run_id=self.run_id,
                case_id=self.case_id,
                seq=seq,
                mono_ts_ns=time.monotonic_ns(),
                wall_ts=wall_ts_now(),
                clock_id=self.clock_id,
                port_id=port_id,
                endpoint_generation=endpoint_generation,
                dut_generation=dut_generation,
                meta={"event": "evidence_failure", "code": code},
                loss_flag=True,
            )
            encoded = dumps_record(marker)
            self._set_failure_locked(code)
            self._seq = seq
            try:
                self._queue.put_nowait(
                    _PendingRecord(
                        payload=encoded,
                        consumes_slot=False,
                        port_id=port_id,
                        endpoint_generation=endpoint_generation,
                        dut_generation=dut_generation,
                    )
                )
            except queue.Full:
                # 正常項目使用 semaphore 限額，額外位置只留給此 marker；若不變量破裂仍 fail-stop。
                self._set_failure_locked("HARNESS_EVIDENCE_WRITE_FAILED")
                self._seq = seq - 1

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
                self._require_running_locked()
                seq = self._seq + 1
                key = (port_id, endpoint_generation, observation)
                offset = self._offsets.get(key, 0)
                mono_ts = time.monotonic_ns()
                wall_ts = wall_ts_now()
                record = make_data_record(
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
            encoded = dumps_record(record)
            if not self._pending_slots.acquire(timeout=self.block_timeout_s):
                self._enqueue_overflow_marker_locked(
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                )
                raise EvidenceFailure("Queue full", code="HARNESS_EVIDENCE_OVERFLOW")
            try:
                with self._lock:
                    self._require_running_locked()
                    # _emit_lock 串行化 producer，且 semaphore 保留容量，入列不會等待。
                    self._queue.put_nowait(
                        _PendingRecord(
                            payload=encoded,
                            consumes_slot=True,
                            port_id=port_id,
                            endpoint_generation=endpoint_generation,
                            dut_generation=dut_generation,
                        )
                    )
                    self._seq = seq
                    self._offsets[key] += len(payload)
                    self._last_context = (port_id, endpoint_generation, dut_generation)
            except Exception:
                self._pending_slots.release()
                raise
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
                self._require_running_locked()
                seq = self._seq + 1
                mono_ts = time.monotonic_ns()
                wall_ts = wall_ts_now()
                record = make_meta_record(
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
            encoded = dumps_record(record)
            if not self._pending_slots.acquire(timeout=self.block_timeout_s):
                self._enqueue_overflow_marker_locked(
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                )
                raise EvidenceFailure("Queue full", code="HARNESS_EVIDENCE_OVERFLOW")
            try:
                with self._lock:
                    self._require_running_locked()
                    self._queue.put_nowait(
                        _PendingRecord(
                            payload=encoded,
                            consumes_slot=True,
                            port_id=port_id,
                            endpoint_generation=endpoint_generation,
                            dut_generation=dut_generation,
                        )
                    )
                    self._seq = seq
                    self._last_context = (port_id, endpoint_generation, dut_generation)
            except Exception:
                self._pending_slots.release()
                raise
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
        if timeout_s < 0:
            raise ValueError("timeout_s 不得為負數")
        deadline = time.monotonic() + timeout_s
        if not self._flush_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            with self._lock:
                self._set_failure_locked(
                    "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                )
            self._raise_failure()
        try:
            if not self._emit_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._set_failure_locked(
                        "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                    )
                self._raise_failure()
            try:
                with self._lock:
                    self._require_running_locked()
                    context = self._last_context or ("unknown", 1, 1)
                    request = _FlushRequest(
                        done=threading.Event(),
                        port_id=context[0],
                        endpoint_generation=context[1],
                        dut_generation=context[2],
                    )
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    self._queue.put(request, timeout=remaining)
                except queue.Full:
                    with self._lock:
                        self._set_failure_locked(
                            "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                        )
                    self._raise_failure()
            finally:
                self._emit_lock.release()

            remaining = max(0.0, deadline - time.monotonic())
            if not request.done.wait(remaining):
                with self._lock:
                    self._set_failure_locked(
                        "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                    )
                self._raise_failure()
            if request.error_code is not None:
                raise EvidenceFailure(
                    f"Evidence flush failed: {request.error_code}",
                    code=request.error_code,
                )
            with self._lock:
                failure = self._failure
            if failure is not None:
                raise EvidenceFailure(f"Evidence writer failed: {failure}", code=failure)
        finally:
            self._flush_lock.release()

    def _summary_locked(self) -> dict[str, Any]:
        return {
            "records": self._records_written,
            "bytes": self._bytes_written,
            "sha256": self._hasher.hexdigest(),
        }

    def close(self) -> dict[str, Any]:
        with self._emit_lock:
            with self._lock:
                if self._state == "closed":
                    return self._summary_locked()
                if self._state == "new":
                    self._state = "closed"
                    return self._summary_locked()
                self._state = "closing"
                thread = self._thread
                should_enqueue_sentinel = thread is not None and not self._sentinel_enqueued

            deadline = time.monotonic() + self.close_timeout_s
            if should_enqueue_sentinel:
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    self._queue.put(None, timeout=remaining)
                    with self._lock:
                        self._sentinel_enqueued = True
                except queue.Full:
                    with self._lock:
                        self._set_failure_locked(
                            "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                        )
                    return self._summary()

            if thread is not None:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    with self._lock:
                        self._set_failure_locked(
                            "HARNESS_EVIDENCE_WRITE_FAILED", worker_failed=True
                        )
                    return self._summary()

            with self._lock:
                fd = self._fd
            if fd is not None:
                try:
                    self._os_fsync(fd)
                except Exception as exc:
                    with self._lock:
                        self._set_failure_locked(self._classify_error(exc))
                try:
                    os.close(fd)
                except OSError as exc:
                    with self._lock:
                        self._set_failure_locked(self._classify_error(exc))
                with self._lock:
                    # close(2) failure leaves descriptor ownership uncertain; never retry it.
                    self._fd = None
            with self._lock:
                self._state = "closed"
                return self._summary_locked()

    def _summary(self) -> dict[str, Any]:
        with self._lock:
            return self._summary_locked()
