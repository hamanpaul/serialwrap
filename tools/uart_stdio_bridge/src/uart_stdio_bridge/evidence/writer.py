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

    def __init__(
        self,
        message: str,
        code: str = "HARNESS_EVIDENCE_WRITE_FAILED",
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class _ProgressWait:
    """同一次等待中，追蹤最後一次觀察到的 worker 進度。"""

    last_progress_at: float
    generation: int


# close/flush 的停滯期限可以被持續進度重設，但單次操作最多等待五分鐘。
# 這讓大證據佇列有機會排空，同時避免持續產生工作或異常排程造成無界等待。
_ABSOLUTE_WAIT_CAP_S = 300.0


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
        self._progress_changed = threading.Condition()
        self._progress_lock = threading.Lock()
        self._progress_generation = 0
        self._progress_at = time.monotonic()
        self._thread: threading.Thread | None = None
        self._failure: str | None = None
        self._failure_detail: str | None = None
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

    @property
    def failure_detail(self) -> str | None:
        with self._lock:
            return self._failure_detail

    @staticmethod
    def _classify_error(exc: BaseException) -> str:
        err = getattr(exc, "errno", None)
        if err in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
            return "HARNESS_DISK_FULL"
        return "HARNESS_EVIDENCE_WRITE_FAILED"

    def _set_failure_locked(
        self,
        code: str,
        *,
        worker_failed: bool = False,
        detail: str | None = None,
    ) -> None:
        if self._failure is None:
            self._failure = code
            self._failure_detail = detail
        if worker_failed:
            self._worker_failed = True

    def _raise_failure(self) -> None:
        code = self.failure or "HARNESS_EVIDENCE_WRITE_FAILED"
        raise EvidenceFailure(
            f"Evidence writer failed: {code}",
            code=code,
            detail=self.failure_detail,
        )

    def _progress_wait_state(self) -> _ProgressWait:
        now = time.monotonic()
        with self._progress_lock:
            return _ProgressWait(
                last_progress_at=now,
                generation=self._progress_generation,
            )

    def _notify_progress_waiters(self) -> None:
        with self._progress_changed:
            self._progress_changed.notify_all()

    def _mark_progress(self, progress_at: float) -> None:
        # 先記錄時間再通知，避免 waiter 持有 condition lock 時延後可見進度。
        with self._progress_lock:
            self._progress_generation += 1
            self._progress_at = progress_at
        with self._progress_changed:
            self._progress_changed.notify_all()

    def _wait_with_progress(
        self,
        predicate: Callable[[], bool],
        *,
        stall_timeout_s: float,
        absolute_deadline: float | None,
        wait_state: _ProgressWait | None = None,
    ) -> bool:
        """直到條件成立；只有停滯或抵達絕對期限才回傳 False。"""
        if wait_state is None:
            wait_state = self._progress_wait_state()
        if stall_timeout_s == 0:
            return predicate()
        with self._progress_changed:
            while True:
                with self._progress_lock:
                    generation = self._progress_generation
                    progress_at = self._progress_at
                if generation != wait_state.generation:
                    if progress_at - wait_state.last_progress_at > stall_timeout_s:
                        return False
                    wait_state.last_progress_at = progress_at
                    wait_state.generation = generation

                now = time.monotonic()
                if now - wait_state.last_progress_at >= stall_timeout_s:
                    return False
                if absolute_deadline is not None and now >= absolute_deadline:
                    return False
                if predicate():
                    return True

                stall_deadline = wait_state.last_progress_at + stall_timeout_s
                deadline = stall_deadline
                if absolute_deadline is not None:
                    deadline = min(deadline, absolute_deadline)
                remaining = deadline - now

                # 定期重查 lock、queue、Event 等非 condition 狀態；worker 寫入時則立即通知。
                self._progress_changed.wait(min(remaining, 0.05))

    def _acquire_with_progress(
        self,
        lock: Any,
        *,
        stall_timeout_s: float,
        absolute_deadline: float | None,
        wait_state: _ProgressWait | None = None,
    ) -> bool:
        return self._wait_with_progress(
            lambda: lock.acquire(blocking=False),
            stall_timeout_s=stall_timeout_s,
            absolute_deadline=absolute_deadline,
            wait_state=wait_state,
        )

    def _acquire_pending_slot(self) -> bool:
        return self._wait_with_progress(
            lambda: self._pending_slots.acquire(blocking=False),
            stall_timeout_s=self.block_timeout_s,
            absolute_deadline=None,
        )

    def _require_running_locked(self) -> None:
        if self._state != "running":
            raise EvidenceFailure(
                f"Evidence writer state is {self._state}; expected running",
                code=self._failure or "HARNESS_EVIDENCE_WRITE_FAILED",
            )
        if self._failure is not None:
            raise EvidenceFailure(
                f"Evidence writer failed: {self._failure}",
                code=self._failure,
                detail=self._failure_detail,
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
                    self._set_failure_locked(code, detail="write_error")
                    self._state = "closed"
                raise EvidenceFailure(
                    f"無法建立證據檔 {self.path}: {exc}",
                    code=code,
                    detail="write_error",
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
                    self._set_failure_locked(
                        self._classify_error(exc),
                        worker_failed=True,
                        detail="write_error",
                    )
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
            progress_at = time.monotonic()
            chunk = payload[written : written + n]
            with self._lock:
                self._hasher.update(chunk)
                self._bytes_written += n
            written += n
            # 短寫每次成功寫出位元組都算進度，避免大筆資料被誤判為停滯。
            self._mark_progress(progress_at)
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
            self._notify_progress_waiters()
            if item is None:
                self._queue.task_done()
                self._notify_progress_waiters()
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
                        self._set_failure_locked(
                            code,
                            worker_failed=True,
                            detail="write_error",
                        )
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
                    self._notify_progress_waiters()
                continue
            if item.consumes_slot:
                # 有界上限計算尚未開始寫出的 queue backlog；目前寫入項目不占佇列容量。
                self._pending_slots.release()
                self._notify_progress_waiters()
            try:
                with self._lock:
                    skip = self._worker_failed
                if skip:
                    continue
                self._write_complete(item.payload)
            except Exception as exc:
                code = self._classify_error(exc)
                with self._lock:
                        self._set_failure_locked(
                            code,
                            worker_failed=True,
                            detail="write_error",
                        )
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
            self._set_failure_locked(code, detail="backpressure_stall")
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
                self._set_failure_locked(
                    "HARNESS_EVIDENCE_WRITE_FAILED", detail="write_error"
                )
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
            if not self._acquire_pending_slot():
                self._enqueue_overflow_marker_locked(
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                )
                raise EvidenceFailure(
                    "Queue full",
                    code="HARNESS_EVIDENCE_OVERFLOW",
                    detail="backpressure_stall",
                )
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
            if not self._acquire_pending_slot():
                self._enqueue_overflow_marker_locked(
                    port_id=port_id,
                    endpoint_generation=endpoint_generation,
                    dut_generation=dut_generation,
                )
                raise EvidenceFailure(
                    "Queue full",
                    code="HARNESS_EVIDENCE_OVERFLOW",
                    detail="backpressure_stall",
                )
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
        wait_state = self._progress_wait_state()
        absolute_deadline = time.monotonic() + _ABSOLUTE_WAIT_CAP_S
        if not self._acquire_with_progress(
            self._flush_lock,
            stall_timeout_s=timeout_s,
            absolute_deadline=absolute_deadline,
            wait_state=wait_state,
        ):
            with self._lock:
                self._set_failure_locked(
                    "HARNESS_EVIDENCE_WRITE_FAILED",
                    worker_failed=True,
                    detail="flush_stall",
                )
            self._raise_failure()
        try:
            if not self._acquire_with_progress(
                self._emit_lock,
                stall_timeout_s=timeout_s,
                absolute_deadline=absolute_deadline,
                wait_state=wait_state,
            ):
                with self._lock:
                    self._set_failure_locked(
                        "HARNESS_EVIDENCE_WRITE_FAILED",
                        worker_failed=True,
                        detail="flush_stall",
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
                def enqueue_flush_request() -> bool:
                    try:
                        self._queue.put_nowait(request)
                    except queue.Full:
                        return False
                    return True

                if not self._wait_with_progress(
                    enqueue_flush_request,
                    stall_timeout_s=timeout_s,
                    absolute_deadline=absolute_deadline,
                    wait_state=wait_state,
                ):
                    with self._lock:
                        self._set_failure_locked(
                            "HARNESS_EVIDENCE_WRITE_FAILED",
                            worker_failed=True,
                            detail="flush_stall",
                        )
                    self._raise_failure()
            finally:
                self._emit_lock.release()

            if not self._wait_with_progress(
                request.done.is_set,
                stall_timeout_s=timeout_s,
                absolute_deadline=absolute_deadline,
                wait_state=wait_state,
            ):
                with self._lock:
                    self._set_failure_locked(
                        "HARNESS_EVIDENCE_WRITE_FAILED",
                        worker_failed=True,
                        detail="flush_stall",
                    )
                self._raise_failure()
            if request.error_code is not None:
                raise EvidenceFailure(
                    f"Evidence flush failed: {request.error_code}",
                    code=request.error_code,
                    detail=self.failure_detail,
                )
            with self._lock:
                failure = self._failure
            if failure is not None:
                raise EvidenceFailure(
                    f"Evidence writer failed: {failure}",
                    code=failure,
                    detail=self._failure_detail,
                )
        finally:
            self._flush_lock.release()
            self._notify_progress_waiters()

    def _summary_locked(self) -> dict[str, Any]:
        return {
            "records": self._records_written,
            "bytes": self._bytes_written,
            "sha256": self._hasher.hexdigest(),
        }

    def close(self) -> dict[str, Any]:
        wait_state = self._progress_wait_state()
        absolute_deadline = time.monotonic() + _ABSOLUTE_WAIT_CAP_S
        if not self._acquire_with_progress(
            self._emit_lock,
            stall_timeout_s=self.close_timeout_s,
            absolute_deadline=absolute_deadline,
            wait_state=wait_state,
        ):
            with self._lock:
                self._set_failure_locked(
                    "HARNESS_EVIDENCE_WRITE_FAILED",
                    worker_failed=True,
                    detail="close_stall",
                )
                self._state = "closing"
                return self._summary_locked()

        try:
            with self._lock:
                if self._state == "closed":
                    return self._summary_locked()
                if self._state == "new":
                    self._state = "closed"
                    return self._summary_locked()
                self._state = "closing"
                thread = self._thread

            def close_worker_finished() -> bool:
                if thread is None:
                    return True
                with self._lock:
                    sentinel_pending = not self._sentinel_enqueued
                if sentinel_pending:
                    try:
                        self._queue.put_nowait(None)
                    except queue.Full:
                        return False
                    with self._lock:
                        self._sentinel_enqueued = True
                return not thread.is_alive()

            if not self._wait_with_progress(
                close_worker_finished,
                stall_timeout_s=self.close_timeout_s,
                absolute_deadline=absolute_deadline,
                wait_state=wait_state,
            ):
                with self._lock:
                    self._set_failure_locked(
                        "HARNESS_EVIDENCE_WRITE_FAILED",
                        worker_failed=True,
                        detail="close_stall",
                    )
                return self._summary()

            if thread is not None:
                thread.join()

            with self._lock:
                fd = self._fd
            if fd is not None:
                try:
                    self._os_fsync(fd)
                except Exception as exc:
                    with self._lock:
                        self._set_failure_locked(
                            self._classify_error(exc), detail="write_error"
                        )
                try:
                    os.close(fd)
                except OSError as exc:
                    with self._lock:
                        self._set_failure_locked(
                            self._classify_error(exc), detail="write_error"
                        )
                with self._lock:
                    # close(2) failure leaves descriptor ownership uncertain; never retry it.
                    self._fd = None
            with self._lock:
                self._state = "closed"
                return self._summary_locked()
        finally:
            self._emit_lock.release()
            self._notify_progress_waiters()

    def _summary(self) -> dict[str, Any]:
        with self._lock:
            return self._summary_locked()
