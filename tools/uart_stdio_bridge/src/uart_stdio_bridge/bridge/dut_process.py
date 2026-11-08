"""以二進位 pipe 管理 DUT 子程序及其世代。"""

from __future__ import annotations

import errno
import os
import queue
import select
import signal
import subprocess
import threading
from collections.abc import Callable
from typing import Any


class DutStdinClosed(OSError):
    """DUT stdin 已關閉或子程序已結束。"""


class DutProcess:
    """以不經文字轉換的 stdin、stdout、stderr pipe 啟動 DUT。"""

    def __init__(
        self,
        argv_factory: Callable[[int], list[str]],
        *,
        mode: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        image: str | None = None,
        image_id: str | None = None,
    ) -> None:
        if mode not in {"local-process", "docker"}:
            raise ValueError(f"不支援的 DUT 模式：{mode}")
        self._argv_factory = argv_factory
        self.mode = mode
        self.env = None if env is None else dict(env)
        self.cwd = cwd
        self.image = image
        self.image_id = image_id
        self.faults: list[dict[str, Any]] = []
        self.generation = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._argv: list[str] | None = None
        self._stdin_closed = False
        self._lock = threading.RLock()
        self._stdout_queue: queue.Queue[bytes | None] | None = None
        self._stdout_eof = False
        self._stdout_remainder = b""
        self._stdin_requests: queue.Queue[tuple[bytes, queue.Queue[object]]] | None = None
        self._stdin_pending_data: bytes | None = None
        self._stdin_pending_result: queue.Queue[object] | None = None

    def spawn(self) -> None:
        """啟動新一代 DUT，所有三條標準串流皆維持 binary mode。"""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("DUT 子程序仍在執行")
            next_generation = self.generation + 1
            argv = self._argv_factory(next_generation)
            if not argv or not all(isinstance(arg, str) for arg in argv):
                raise ValueError("DUT argv 必須是非空字串清單")
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "bufsize": 0,
                "close_fds": True,
                "env": self.env,
                "cwd": self.cwd,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(argv, **kwargs)
            except OSError as exc:
                raise OSError(f"HARNESS_DUT_SPAWN_FAILED: {exc}") from exc
            self._proc = proc
            self._argv = list(argv)
            self._stdin_closed = False
            self._stdin_pending_data = None
            self._stdin_pending_result = None
            self.generation = next_generation
            if os.name == "nt":
                self._start_windows_pipe_workers(proc)
            else:
                assert proc.stdin is not None
                os.set_blocking(proc.stdin.fileno(), False)

    def _start_windows_pipe_workers(self, proc: subprocess.Popen[bytes]) -> None:
        self._stdout_queue = queue.Queue(maxsize=4)
        self._stdout_eof = False
        self._stdout_remainder = b""
        stdout_queue = self._stdout_queue
        stdout_thread = threading.Thread(
            target=self._windows_stdout_reader,
            args=(proc, stdout_queue),
            name=f"dut-stdout-{proc.pid}",
            daemon=True,
        )
        stdout_thread.start()

        self._stdin_requests = queue.Queue(maxsize=1)
        stdin_requests = self._stdin_requests
        stdin_thread = threading.Thread(
            target=self._windows_stdin_writer,
            args=(proc, stdin_requests),
            name=f"dut-stdin-{proc.pid}",
            daemon=True,
        )
        stdin_thread.start()

    @staticmethod
    def _windows_stdout_reader(
        proc: subprocess.Popen[bytes], output: queue.Queue[bytes | None]
    ) -> None:
        if proc.stdout is None:
            return
        try:
            while True:
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    break
                output.put(data)
        except OSError:
            pass
        finally:
            output.put(None)

    @staticmethod
    def _windows_stdin_writer(
        proc: subprocess.Popen[bytes],
        requests: queue.Queue[tuple[bytes, queue.Queue[object]]],
    ) -> None:
        while True:
            data, result = requests.get()
            try:
                if proc.stdin is None:
                    raise DutStdinClosed("DUT stdin 已關閉")
                result.put(os.write(proc.stdin.fileno(), data))
            except BaseException as exc:
                result.put(exc)

    def stdin_write_some(self, data: bytes) -> int:
        """對 DUT stdin 寫入一段 bytes，回報核心實際接受長度。"""
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or self._stdin_closed:
                raise DutStdinClosed("DUT stdin 已關閉")
            if proc.poll() is not None:
                raise DutStdinClosed("DUT 子程序已結束")
            fd = proc.stdin.fileno()
            if os.name == "nt":
                requests = self._stdin_requests
                if requests is None:
                    raise DutStdinClosed("DUT stdin writer 未啟動")
                if self._stdin_pending_result is not None:
                    if self._stdin_pending_data != data:
                        raise RuntimeError("DUT stdin 尚有未完成的寫入")
                    result_queue = self._stdin_pending_result
                else:
                    result_queue = queue.Queue(maxsize=1)
                    self._stdin_pending_data = bytes(data)
                    self._stdin_pending_result = result_queue
                    requests.put((bytes(data), result_queue))
                pending = self._stdin_pending_result
                pending_data = self._stdin_pending_data
            else:
                pending = None
                pending_data = None
        if os.name == "nt":
            assert pending is not None and pending_data is not None
            try:
                result = pending.get(timeout=0.05)
            except queue.Empty:
                return 0
            with self._lock:
                if self._stdin_pending_result is pending:
                    self._stdin_pending_result = None
                    self._stdin_pending_data = None
            if isinstance(result, BaseException):
                if isinstance(result, BrokenPipeError) or getattr(result, "errno", None) in {
                    errno.EPIPE,
                    errno.EBADF,
                }:
                    raise DutStdinClosed("DUT stdin 已關閉") from result
                raise result
            return int(result)
        try:
            _readable, writable, _exceptional = select.select([], [fd], [], 0.05)
            if not writable:
                return 0
            return os.write(fd, data)
        except (BrokenPipeError, OSError) as exc:
            if isinstance(exc, BrokenPipeError) or getattr(exc, "errno", None) in {
                errno.EPIPE,
                errno.EBADF,
            }:
                raise DutStdinClosed("DUT stdin 已關閉") from exc
            if getattr(exc, "errno", None) in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return 0
            raise

    def stdout_read(self, max_bytes: int, timeout_s: float | None = None) -> bytes | None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return b""
            if self._stdout_eof:
                return b""
            fd = proc.stdout.fileno()
            output = self._stdout_queue if os.name == "nt" else None
        if os.name == "nt":
            assert output is not None
            with self._lock:
                if self._stdout_remainder:
                    data = self._stdout_remainder[:max_bytes]
                    self._stdout_remainder = self._stdout_remainder[max_bytes:]
                    return data
            try:
                data = output.get(timeout=timeout_s)
            except queue.Empty:
                return None
            if data is None:
                with self._lock:
                    self._stdout_eof = True
                return b""
            if len(data) > max_bytes:
                with self._lock:
                    self._stdout_remainder = data[max_bytes:]
                data = data[:max_bytes]
            return data
        try:
            if timeout_s is not None:
                readable, _writable, _exceptional = select.select([fd], [], [], timeout_s)
                if not readable:
                    return None
            return os.read(fd, max_bytes)
        except OSError as exc:
            if exc.errno in {errno.EBADF, errno.EIO}:
                return b""
            raise

    def stderr_read(self, max_bytes: int) -> bytes:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stderr is None:
                return b""
            fd = proc.stderr.fileno()
        try:
            return os.read(fd, max_bytes)
        except OSError as exc:
            if exc.errno in {errno.EBADF, errno.EIO}:
                return b""
            raise

    def close_stdin(self) -> None:
        with self._lock:
            proc = self._proc
            if self._stdin_closed:
                return
            self._stdin_closed = True
            stream = None if proc is None else proc.stdin
            self._stdin_pending_result = None
            self._stdin_pending_data = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def terminate(self, grace_s: float = 2.0) -> int | None:
        """先送出溫和終止訊號，逾時後強制結束子程序。"""
        with self._lock:
            proc = self._proc
        if proc is None:
            return None
        if proc.poll() is not None:
            self.close_stdin()
            return proc.returncode
        self.close_stdin()
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except (OSError, ValueError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            return proc.wait(timeout=max(0.0, grace_s))
        except subprocess.TimeoutExpired:
            return self.kill()

    def record_fault(self, kind: str, **details: Any) -> None:
        with self._lock:
            self.faults.append({"kind": kind, **details})

    def kill(self) -> int | None:
        with self._lock:
            proc = self._proc
            argv = list(self._argv or [])
        if proc is None:
            return None
        if self.mode == "docker":
            container_name = None
            for idx, arg in enumerate(argv):
                if arg == "--name" and idx + 1 < len(argv):
                    container_name = argv[idx + 1]
                    break
            if container_name:
                try:
                    subprocess.run(
                        ["docker", "kill", container_name],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            return proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return proc.poll()

    def poll(self) -> int | None:
        with self._lock:
            proc = self._proc
        return None if proc is None else proc.poll()

    def respawn(self, grace_s: float = 2.0) -> int:
        self.terminate(grace_s)
        self.spawn()
        return self.generation

    def describe(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            res: dict[str, Any] = {
                "mode": self.mode,
                "argv": list(self._argv or []),
                "pid": None if proc is None else proc.pid,
                "generation": self.generation,
                "faults": list(self.faults),
            }
            if self.mode == "docker":
                res["image"] = self.image
                res["image_id"] = self.image_id
            return res

    def _close_pipes(self) -> None:
        with self._lock:
            proc = self._proc
            streams = () if proc is None else (proc.stdin, proc.stdout, proc.stderr)
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
