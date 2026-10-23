"""組裝外部 bridge、維護方向 worker 與執行故障控制動作。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from uart_stdio_bridge import TOOL_NAME, __version__
from uart_stdio_bridge.endpoints.base import EndpointWriteUncertain

from .dut_process import DutStdinClosed
from . import pumps


@dataclass(frozen=True)
class BridgeConfig:
    """bridge 執行參數與每方向緩衝上限。"""

    run_id: str
    case_id: str
    port_id: str
    evidence_dir: pathlib.Path
    read_chunk: int = 4096
    max_inflight_bytes: int = 1_048_576
    write_timeout_s: float = 5.0
    barrier_quiet_ms: int = 500
    stderr_cap_bytes: int = 268_435_456
    allow_dut_exit: bool = False
    com_driver: dict[str, Any] | None = None
    com_driver_install_method: str | None = None
    test_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.case_id or not self.port_id:
            raise ValueError("run_id、case_id 與 port_id 不得為空")
        if self.read_chunk <= 0 or self.max_inflight_bytes <= 0:
            raise ValueError("read_chunk 與 max_inflight_bytes 必須大於 0")
        if self.write_timeout_s < 0 or self.barrier_quiet_ms < 0:
            raise ValueError("timeout 與 quiet 時間不得為負數")
        if self.stderr_cap_bytes < 0:
            raise ValueError("stderr_cap_bytes 不得為負數")


class Bridge:
    """獨立維護 TX、RX、stderr 與控制通道，不解析 UART 文字內容。"""

    dut_stdin_closed_type = DutStdinClosed
    endpoint_write_uncertain_type = EndpointWriteUncertain

    def __init__(
        self,
        cfg: BridgeConfig,
        endpoint: Any,
        dut: Any,
        writer: Any,
        dut_control: Any | None,
    ) -> None:
        self.cfg = cfg
        self.endpoint = endpoint
        self.dut = dut
        self.writer = writer
        self.dut_control = dut_control
        self.tx_q = self._new_byte_queue(cfg.max_inflight_bytes)
        self.rx_q = self._new_byte_queue(cfg.max_inflight_bytes)
        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._action_lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._done_event = threading.Event()
        self._reader_pause = {"TX": threading.Event(), "RX": threading.Event()}
        self._writer_pause = {"TX": threading.Event(), "RX": threading.Event()}
        self._pump_states: dict[str, str] = {}
        self._pump_busy: dict[str, bool] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._last_activity_ns: dict[str, int] = {}
        self._pump_inflight_bytes: dict[str, int] = {}
        self._reader_io_busy = {"TX": False, "RX": False}
        self._wire_seq = 0
        self._control_seq = 0
        self._dut_generation_value = 0
        self._failure: str | None = None
        self._failure_detail: str | None = None
        self._started = False
        self._finished = False
        self._closing = False
        self._return_code = 0
        self._dut_generation_base = 0
        self._dut_ack_generation = 1
        self._stdout_eof_generations: set[int] = set()
        self._intentional_dut_exits: set[int] = set()
        self._tx_restart_discard_generation: int | None = None
        self._transport_disconnected = False
        self._peer_state_lock = threading.Lock()
        self._last_peer_state: bool | None = None
        self._quiesce_action_id: str | None = None
        self._stderr_total = 0
        self._stderr_written = 0
        self._stderr_truncated = False
        self._stderr_file: Any | None = None
        self._control_file: Any | None = None
        self._started_at = self._wall_ts()
        self._closed_at: str | None = None
        self._pending_at_close: dict[str, int] | None = None

    @staticmethod
    def _new_byte_queue(max_bytes: int) -> Any:
        from .bytequeue import ByteQueue

        return ByteQueue(max_bytes=max_bytes)

    @staticmethod
    def _wall_ts() -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def failure(self) -> str | None:
        with self._state_lock:
            return self._failure

    def start(self) -> None:
        """啟動證據 writer、端點、DUT 與各自獨立的 worker。"""
        with self._state_lock:
            if self._started:
                return
            self._started = True
            self._last_activity_ns = {"start": time.monotonic_ns()}
        try:
            from uart_stdio_bridge.evidence.manifest import ensure_private_dir

            ensure_private_dir(self.cfg.evidence_dir)
            self._open_private_file("control.ndjson", "ab", attr="_control_file")
            self._open_private_file("dut.stderr.log", "wb", attr="_stderr_file")
            self.writer.start()
            self.endpoint.open()
            self.dut.spawn()
            self._wait_dut_control_ready()
            self._dut_generation_value = max(1, int(getattr(self.dut, "generation", 1)))
            self._set_dut_generation_mapping(self._dut_generation_value)
            self._record_meta({"event": "bridge_start"})
            self._start_stream_workers()
            self._start_worker("dut_watch", pumps.dut_watch)
            self._start_worker("peer_watch", pumps.peer_watch)
        except Exception as exc:
            code = self._exception_code(exc, "HARNESS_DUT_SPAWN_FAILED")
            self._fail(code, exc)
            self._finish()
            raise

    def _wait_dut_control_ready(self, timeout_s: float = 15.0) -> None:
        """等待 DUT 控制通道就緒（發送 ready_probe 直至收到 ack 或逾時）。"""
        if self.dut_control is None:
            return
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if hasattr(self.dut, "poll") and self.dut.poll() is not None:
                raise OSError("HARNESS_DUT_SPAWN_FAILED: DUT exited prematurely during ready probe")
            try:
                self.dut_control.send(
                    {"action": "ready_probe"},
                    timeout_s=min(0.5, max(0.05, deadline - time.monotonic())),
                )
                return
            except (TimeoutError, OSError):
                time.sleep(0.02)
                continue
        raise OSError("HARNESS_DUT_SPAWN_FAILED: timeout waiting for DUT control readiness")

    def _open_private_file(self, name: str, mode: str, *, attr: str) -> None:
        path = self.cfg.evidence_dir / name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if "a" in mode:
            flags |= os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, mode, buffering=0)
        setattr(self, attr, stream)

    def _start_stream_workers(self) -> None:
        self._start_worker("tx_reader", pumps.tx_reader)
        self._start_worker("tx_writer", pumps.tx_writer)
        self._start_worker("rx_reader", pumps.rx_reader)
        self._start_worker("rx_writer", pumps.rx_writer)
        self._start_worker("stderr_drain", pumps.stderr_drain)

    def _start_worker(self, name: str, target: Any) -> None:
        with self._state_lock:
            current = self._threads.get(name)
            if current is not None and current.is_alive():
                return
            thread = threading.Thread(
                target=target,
                args=(self,),
                name=name.replace("_", "-"),
                daemon=True,
            )
            self._threads[name] = thread
            self._pump_states[name] = "starting"
            self._pump_busy[name] = False
            self._pump_inflight_bytes[name] = 0
        thread.start()

    def _pump_checkpoint(self, name: str, direction: str, side: str) -> bool:
        pause = self._reader_pause[direction] if side == "reader" else self._writer_pause[direction]
        with self._state_changed:
            while pause.is_set() and not self._stop_event.is_set():
                self._pump_states[name] = "paused"
                self._pump_busy[name] = False
                self._state_changed.notify_all()
                self._state_changed.wait(0.05)
            if self._stop_event.is_set():
                return True
            self._pump_states[name] = "running"
            self._state_changed.notify_all()
            return False

    def _pump_busy_now(self, name: str, busy: bool) -> None:
        with self._state_changed:
            self._pump_busy[name] = busy
            self._state_changed.notify_all()

    def _pump_inflight_now(self, name: str, byte_count: int) -> None:
        with self._state_changed:
            self._pump_inflight_bytes[name] = max(0, int(byte_count))
            self._state_changed.notify_all()

    def _reader_io_now(self, direction: str, busy: bool) -> None:
        with self._state_changed:
            self._reader_io_busy[direction] = busy
            self._state_changed.notify_all()

    def _pump_stopped(self, name: str) -> None:
        with self._state_changed:
            self._pump_states[name] = "stopped"
            self._pump_busy[name] = False
            self._state_changed.notify_all()

    def _pause_reader(self, direction: str, timeout_s: float = 2.0) -> None:
        direction = direction.upper()
        if direction not in self._reader_pause:
            raise ValueError("dir 必須是 TX 或 RX")
        name = "tx_reader" if direction == "TX" else "rx_reader"
        event = self._reader_pause[direction]
        event.set()
        deadline = time.monotonic() + timeout_s
        with self._state_changed:
            self._state_changed.notify_all()
            thread = self._threads.get(name)
            while thread is not None and thread.is_alive():
                if self._pump_states.get(name) == "paused":
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    event.clear()
                    self._state_changed.notify_all()
                    raise TimeoutError(f"{direction} reader 未能暫停")
                self._state_changed.wait(min(remaining, 0.05))

    def _resume_reader(self, direction: str) -> None:
        direction = direction.upper()
        if direction not in self._reader_pause:
            raise ValueError("dir 必須是 TX 或 RX")
        self._reader_pause[direction].clear()
        with self._state_changed:
            self._state_changed.notify_all()

    def _pause_writer(self, direction: str, timeout_s: float = 2.0) -> None:
        direction = direction.upper()
        name = "tx_writer" if direction == "TX" else "rx_writer"
        event = self._writer_pause[direction]
        event.set()
        deadline = time.monotonic() + timeout_s
        with self._state_changed:
            self._state_changed.notify_all()
            thread = self._threads.get(name)
            while thread is not None and thread.is_alive():
                if self._pump_states.get(name) == "paused":
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    event.clear()
                    self._state_changed.notify_all()
                    raise TimeoutError(f"{direction} writer 未能暫停")
                self._state_changed.wait(min(remaining, 0.05))

    def _resume_writer(self, direction: str) -> None:
        self._writer_pause[direction.upper()].clear()
        with self._state_changed:
            self._state_changed.notify_all()

    def _dut_generation(self) -> int:
        with self._state_lock:
            return self._dut_generation_value or int(getattr(self.dut, "generation", 0))

    def _process_generation(self) -> int:
        with self._state_lock:
            return int(getattr(self.dut, "generation", 0))

    def _should_discard_tx_remainder(self, process_generation: int) -> bool:
        with self._state_lock:
            if self._tx_restart_discard_generation != process_generation:
                return False
            self._tx_restart_discard_generation = None
            return True

    def _endpoint_generation(self) -> int:
        with self._state_lock:
            return int(getattr(self.endpoint, "generation", 0))

    def _is_transport_disconnected(self) -> bool:
        with self._state_lock:
            return self._transport_disconnected

    def _is_intentional_dut_exit(self, generation: int) -> bool:
        with self._state_lock:
            return generation in self._intentional_dut_exits

    def _set_dut_generation_mapping(self, bridge_generation: int) -> None:
        # DUT 控制 ack 的 generation 會在每個新程序從 1 開始；bridge 世代跨程序單調遞增。
        with self._state_changed:
            self._dut_generation_base = max(0, int(bridge_generation) - 1)
            self._dut_ack_generation = 1
            self._state_changed.notify_all()

    def _wait_for_dut_generation_change(
        self, generation: int, timeout_s: float | None = None
    ) -> bool:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._state_changed:
            while not self._stop_event.is_set() and self._dut_generation() == generation:
                if deadline is None:
                    self._state_changed.wait(0.1)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._state_changed.wait(min(remaining, 0.05))
            return not self._stop_event.is_set()

    def _record_stdout_eof(self, generation: int) -> None:
        with self._state_lock:
            if self._closing or self._stop_event.is_set():
                return
            if generation in self._stdout_eof_generations:
                return
            self._stdout_eof_generations.add(generation)
        self._record_meta({"event": "dut_stdout_eof"})

    def _note_peer_state(self, current: bool) -> None:
        """peer 狀態與上次記錄不同時寫 META peer_state（peer-watch 與 rx-writer 共用，避免重複）。"""
        with self._peer_state_lock:
            if current == self._last_peer_state:
                return
            self._last_peer_state = current
            self._record_meta({"event": "peer_state", "open": current})

    def _note_activity(self, observation: str) -> None:
        with self._state_changed:
            self._last_activity_ns[observation] = time.monotonic_ns()
            self._state_changed.notify_all()

    def _offset(self, observation: str) -> int:
        offsets = self.writer.offsets(self.cfg.port_id, self._endpoint_generation())
        return int(offsets.get(observation, 0))

    def _record_data(self, observation: str, data: bytes) -> bool:
        try:
            seq = self.writer.emit_data(
                port_id=self.cfg.port_id,
                endpoint_generation=self._endpoint_generation(),
                dut_generation=self._dut_generation(),
                observation=observation,
                payload=data,
            )
        except Exception as exc:
            self._writer_failed(exc)
            return False
        with self._state_lock:
            self._wire_seq = max(self._wire_seq, int(seq))
        self._check_writer_failure()
        return self.failure is None

    def _record_meta(self, meta: dict[str, Any], *, loss_flag: bool = False) -> int | None:
        try:
            seq = self.writer.emit_meta(
                port_id=self.cfg.port_id,
                endpoint_generation=self._endpoint_generation(),
                dut_generation=self._dut_generation(),
                meta=meta,
                loss_flag=loss_flag,
            )
        except Exception as exc:
            self._writer_failed(exc)
            return None
        with self._state_lock:
            self._wire_seq = max(self._wire_seq, int(seq))
        self._check_writer_failure()
        return int(seq)

    def _check_writer_failure(self) -> None:
        code = getattr(self.writer, "failure", None)
        if code:
            self._fail(str(code), "證據 writer 已失效")

    def _writer_failed(self, exc: BaseException) -> None:
        code = self._exception_code(exc, "HARNESS_EVIDENCE_WRITE_FAILED")
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            code = "HARNESS_DISK_FULL"
        writer_code = getattr(self.writer, "failure", None)
        if writer_code:
            code = str(writer_code)
        self._fail(code, exc)

    @staticmethod
    def _exception_code(exc: BaseException, fallback: str) -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.startswith("HARNESS_"):
            return code
        for candidate in (
            "HARNESS_DISK_FULL",
            "HARNESS_WRITE_UNCERTAIN",
            "HARNESS_ENDPOINT_IO_ERROR",
            "HARNESS_ENDPOINT_OPEN_FAILED",
            "HARNESS_DUT_SPAWN_FAILED",
        ):
            if candidate in str(exc):
                return candidate
        return fallback

    def _fail(
        self,
        code: str,
        detail: BaseException | str | None = None,
        *,
        force: bool = False,
        signal_done: bool = True,
    ) -> None:
        with self._state_changed:
            evidence_failure = code in {
                "HARNESS_DISK_FULL",
                "HARNESS_EVIDENCE_WRITE_FAILED",
                "HARNESS_EVIDENCE_OVERFLOW",
                "HARNESS_WRITE_UNCERTAIN",
                "HARNESS_ENDPOINT_IO_ERROR",
                "HARNESS_DUT_EXITED",
            }
            if self._closing and not force and not evidence_failure:
                return
            if self._failure is None:
                self._failure = code
                self._failure_detail = None if detail is None else str(detail)
                self._return_code = 3
            self._stop_event.set()
            self._reader_pause["TX"].clear()
            self._reader_pause["RX"].clear()
            self._writer_pause["TX"].clear()
            self._writer_pause["RX"].clear()
            self._state_changed.notify_all()
        self.tx_q.close()
        self.rx_q.close()
        if signal_done:
            self._done_event.set()

    def _write_stderr(self, data: bytes) -> None:
        stream = self._stderr_file
        if stream is None:
            self._fail("HARNESS_EVIDENCE_WRITE_FAILED", "stderr 證據檔未開啟")
            return
        self._stderr_total += len(data)
        remaining = max(0, self.cfg.stderr_cap_bytes - self._stderr_written)
        portion = data[:remaining]
        if len(portion) < len(data):
            self._stderr_truncated = True
        if portion:
            try:
                stream.write(portion)
                self._stderr_written += len(portion)
            except OSError as exc:
                code = "HARNESS_DISK_FULL" if exc.errno == errno.ENOSPC else "HARNESS_EVIDENCE_WRITE_FAILED"
                self._fail(code, exc)

    def _control_phase(
        self,
        *,
        action_id: str,
        action: str,
        phase: str,
        params: dict[str, Any],
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        stream = self._control_file
        if stream is None:
            self._fail("HARNESS_EVIDENCE_WRITE_FAILED", "control.ndjson 未開啟")
            return
        from uart_stdio_bridge.evidence.clock import clock_id

        with self._control_lock:
            self._control_seq += 1
            rec = {
                "schema_version": "control.v1",
                "run_id": self.cfg.run_id,
                "case_id": self.cfg.case_id,
                "seq": self._control_seq,
                "mono_ts_ns": time.monotonic_ns(),
                "wall_ts": self._wall_ts(),
                "clock_id": clock_id(),
                "action_id": action_id,
                "action": action,
                "phase": phase,
                "params": params,
                "result": result,
                "error": error,
                "endpoint_generation": self._endpoint_generation(),
                "dut_generation": self._dut_generation(),
                "wire_seq": self._wire_seq,
            }
            payload = (
                json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as exc:
                code = "HARNESS_DISK_FULL" if exc.errno == errno.ENOSPC else "HARNESS_EVIDENCE_WRITE_FAILED"
                self._fail(code, exc)

    def _barrier(self, params: dict[str, Any], action_id: str) -> dict[str, Any]:
        mode = params.get("mode", "passive")
        if mode not in {"passive", "quiesce"}:
            return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
        quiet_ms = int(params.get("quiet_ms", self.cfg.barrier_quiet_ms))
        timeout_ms = int(params.get("timeout_ms", 5000))
        if quiet_ms < 0 or timeout_ms < 0:
            return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
        deadline = time.monotonic() + timeout_ms / 1000
        resumed_after_timeout = False
        if mode == "quiesce":
            try:
                self._pause_reader("RX", timeout_s=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                self._resume_reader("RX")
                self._flush_writer()
                offsets = self.writer.offsets(self.cfg.port_id, self._endpoint_generation())
                return {
                    "settled": False,
                    "mode": mode,
                    "offsets": offsets,
                    "endpoint_generation": self._endpoint_generation(),
                    "dut_generation": self._dut_generation(),
                    "wire_seq": self._wire_seq,
                }
            self._quiesce_action_id = action_id
            self._record_meta(
                {"event": "quiesce", "action_id": action_id, "phase": "begin", "settled": False}
            )

        settled = False
        quiet_ns = quiet_ms * 1_000_000
        while time.monotonic() <= deadline and not self._stop_event.is_set():
            pending_empty = self.tx_q.pending_bytes() == 0 and self.rx_q.pending_bytes() == 0
            busy = any(self._pump_busy.values())
            with self._state_lock:
                last_activity = max(self._last_activity_ns.values(), default=0)
            quiet = time.monotonic_ns() - last_activity >= quiet_ns
            if pending_empty and not busy and quiet:
                settled = True
                break
            time.sleep(0.01)

        if not settled and mode == "quiesce":
            self._resume_reader("RX")
            self._quiesce_action_id = None
            resumed_after_timeout = True
            self._record_meta(
                {"event": "quiesce", "action_id": action_id, "phase": "end", "settled": False}
            )

        self._flush_writer()
        offsets = self.writer.offsets(self.cfg.port_id, self._endpoint_generation())
        if mode == "quiesce" and settled:
            # RX 保持暫停；呼叫端在取得 WAL 邊界後明確 resume。
            self._quiesce_action_id = action_id
        elif resumed_after_timeout:
            self._quiesce_action_id = None
        return {
            "settled": settled,
            "mode": mode,
            "offsets": offsets,
            "endpoint_generation": self._endpoint_generation(),
            "dut_generation": self._dut_generation(),
            "wire_seq": self._wire_seq,
        }

    def _flush_writer(self) -> None:
        try:
            self.writer.flush()
        except Exception as exc:
            self._writer_failed(exc)

    def _checkpoint(self, action_id: str) -> dict[str, Any]:
        self._flush_writer()
        if self._control_file is not None:
            with self._control_lock:
                self._control_file.flush()
                os.fsync(self._control_file.fileno())
        wire_path = self.cfg.evidence_dir / "wire.ndjson"
        control_path = self.cfg.evidence_dir / "control.ndjson"
        wire_data = wire_path.read_bytes()
        wire_end = wire_data.rfind(b"\n") + 1
        wire_data = wire_data[:wire_end]
        wire_seq = 0
        if wire_data:
            last_record = json.loads(wire_data.rsplit(b"\n", 2)[-2].decode("utf-8"))
            wire_seq = int(last_record["seq"])
        with self._control_lock:
            control_data = control_path.read_bytes()
        control_end = control_data.rfind(b"\n") + 1
        control_data = control_data[:control_end]
        checkpoint_id = uuid.uuid4().hex
        checkpoint_dir = self.cfg.evidence_dir / "checkpoints"
        from uart_stdio_bridge.evidence.manifest import ensure_private_dir, write_json_atomic

        ensure_private_dir(checkpoint_dir)
        checkpoint_path = checkpoint_dir / f"{checkpoint_id}.json"
        checkpoint = {
            "schema_version": "checkpoint.v1",
            "checkpoint_id": checkpoint_id,
            "run_id": self.cfg.run_id,
            "case_id": self.cfg.case_id,
            "wire_len": len(wire_data),
            "wire_sha256": hashlib.sha256(wire_data).hexdigest(),
            "wire_seq": wire_seq,
            "control_len": len(control_data),
            "control_sha256": hashlib.sha256(control_data).hexdigest(),
        }
        write_json_atomic(checkpoint_path, checkpoint)
        self._record_meta(
            {"event": "checkpoint", "checkpoint_id": checkpoint_id, "wire_len": len(wire_data)}
        )
        return {
            "checkpoint_id": checkpoint_id,
            "path": str(checkpoint_path),
            "wire_len": len(wire_data),
            "wire_sha256": checkpoint["wire_sha256"],
            "wire_seq": wire_seq,
            "control_len": len(control_data),
            "control_sha256": checkpoint["control_sha256"],
        }

    def _handle_action(self, action: str, params: dict[str, Any], action_id: str) -> dict[str, Any]:
        if action == "status":
            # 對外 status 只在端點宣告 peer_state 能力時揭露 peer_open（POSIX PTY；win-serial 僅 com0com 且 peer gate 啟用時）；其餘為 null。
            # 內部 peer gate、shutdown 與 rx_writer 仍直接取樣，不受此對外欄位影響。
            try:
                peer_open = self.endpoint.peer_open() if self._capability("peer_state") else None
            except OSError:
                peer_open = None
            with self._state_lock:
                tx_pending = self.tx_q.pending_bytes() + self._pump_inflight_bytes.get("tx_writer", 0)
                rx_pending = self.rx_q.pending_bytes() + self._pump_inflight_bytes.get("rx_writer", 0)
                tx_states = (self._pump_states.get("tx_reader"), self._pump_states.get("tx_writer"))
                rx_states = (self._pump_states.get("rx_reader"), self._pump_states.get("rx_writer"))
            state_for = lambda states: "stopped" if all(s in {None, "stopped"} for s in states) else (
                "paused" if "paused" in states else "running"
            )
            return {
                "endpoint_generation": self._endpoint_generation(),
                "dut_generation": self._dut_generation(),
                "failure": self.failure,
                "peer_open": peer_open,
                "pending": {"tx": tx_pending, "rx": rx_pending},
                "pumps": {"tx": state_for(tx_states), "rx": state_for(rx_states)},
                "wire_seq": self._wire_seq,
            }
        if action == "barrier":
            return self._barrier(params, action_id)
        if action == "checkpoint":
            try:
                return self._checkpoint(action_id)
            except OSError as exc:
                code = "HARNESS_DISK_FULL" if exc.errno == errno.ENOSPC else "HARNESS_EVIDENCE_WRITE_FAILED"
                return {"ok": False, "error": code}
        if action == "mark":
            label = params.get("label")
            op_id = params.get("op_id")
            if not isinstance(label, str) or not isinstance(op_id, str):
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            seq = self._record_meta({"event": "mark", "label": label, "op_id": op_id})
            return {"wire_seq": self._wire_seq if seq is None else seq}
        if action in {"pause_pump", "resume_pump"}:
            direction = str(params.get("dir", "")).upper()
            if direction not in {"TX", "RX"}:
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            if action == "pause_pump":
                try:
                    self._pause_reader(direction)
                except TimeoutError:
                    return {"ok": False, "error": "HARNESS_WINDOW_UNSETTLED"}
                state = "paused"
            else:
                self._resume_reader(direction)
                state = "running"
                if direction == "RX" and self._quiesce_action_id is not None:
                    qid = self._quiesce_action_id
                    self._record_meta(
                        {"event": "quiesce", "action_id": qid, "phase": "end", "settled": True}
                    )
                    self._quiesce_action_id = None
            return {"dir": direction, "state": state}
        if action in {"by_id_link_remove", "by_id_link_add"}:
            if not self._capability("by_id_link"):
                return {"ok": False, "error": "HARNESS_CAPABILITY_UNAVAILABLE"}
            if action.endswith("remove"):
                self.endpoint.link_remove()
            else:
                self.endpoint.link_add()
            return {}
        if action == "transport_disconnect":
            if not self._capability("transport_disconnect"):
                return {"ok": False, "error": "HARNESS_CAPABILITY_UNAVAILABLE"}
            if self._is_transport_disconnected():
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            remove_link = bool(params.get("remove_link", True))
            with self._state_lock:
                self._transport_disconnected = True
            try:
                self.endpoint.disconnect(remove_link=remove_link)
            except OSError as exc:
                with self._state_lock:
                    self._transport_disconnected = False
                return {"ok": False, "error": self._exception_code(exc, "HARNESS_ENDPOINT_IO_ERROR")}
            return {"endpoint_generation": self._endpoint_generation()}
        if action == "transport_reconnect":
            if not self._capability("transport_disconnect"):
                return {"ok": False, "error": "HARNESS_CAPABILITY_UNAVAILABLE"}
            if not self._is_transport_disconnected():
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            old_generation = self._endpoint_generation()
            reader_was_paused = self._reader_pause["RX"].is_set()
            try:
                self._pause_reader("RX")
            except TimeoutError:
                return {"ok": False, "error": "HARNESS_WINDOW_UNSETTLED"}
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.rx_q.pending_bytes() == 0 and not self._pump_busy.get("rx_writer", False):
                    break
                time.sleep(0.01)
            else:
                if not reader_was_paused:
                    self._resume_reader("RX")
                return {"ok": False, "error": "HARNESS_WINDOW_UNSETTLED"}
            if reader_was_paused:
                discarded_stdout = 0
            else:
                try:
                    discarded_stdout = self._drain_disconnected_stdout(timeout_s=5.0)
                except Exception as exc:
                    self._resume_reader("RX")
                    return {"ok": False, "error": self._exception_code(exc, "HARNESS_DUT_EXITED")}
            if discarded_stdout is None:
                if not reader_was_paused:
                    self._resume_reader("RX")
                return {"ok": False, "error": "HARNESS_WINDOW_UNSETTLED"}
            discarded = self.rx_q.discard_all() + discarded_stdout
            if discarded:
                self._record_meta(
                    {
                        "event": "fault",
                        "action_id": action_id,
                        "action": "transport_disconnect",
                        "discarded_len": discarded,
                    },
                    loss_flag=False,
                )
            try:
                new_generation = int(self.endpoint.reconnect())
            except OSError as exc:
                if not reader_was_paused:
                    self._resume_reader("RX")
                return {"ok": False, "error": self._exception_code(exc, "HARNESS_ENDPOINT_OPEN_FAILED")}
            with self._state_lock:
                self._transport_disconnected = False
                self._state_changed.notify_all()
            self._record_meta(
                {
                    "event": "endpoint_generation",
                    "from": old_generation,
                    "to": new_generation,
                    "reason": "reconnect",
                }
            )
            if not reader_was_paused:
                self._resume_reader("RX")
            return {"endpoint_generation": new_generation}
        if action == "dut_restart":
            return self._dut_restart(params, action_id)
        if action == "dut_kill":
            generation = self._process_generation()
            with self._state_lock:
                self._intentional_dut_exits.add(generation)
            if hasattr(self.dut, "record_fault"):
                kind = (
                    "docker_transport_fault"
                    if getattr(self.dut, "mode", None) == "docker"
                    else "process_transport_fault"
                )
                self.dut.record_fault(kind)
            returncode = self.dut.kill()
            self._record_meta(
                {"event": "fault", "action_id": action_id, "action": "dut_kill"}
            )
            return {"returncode": returncode}
        if action == "dut_spawn":
            if self.dut.poll() is None:
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            try:
                self.dut.spawn()
                self._wait_dut_control_ready()
            except Exception as exc:
                return {"ok": False, "error": self._exception_code(exc, "HARNESS_DUT_SPAWN_FAILED")}
            old_generation = self._dut_generation()
            with self._state_changed:
                self._dut_generation_value += 1
                new_generation = self._dut_generation_value
                self._set_dut_generation_mapping(new_generation)
                self._state_changed.notify_all()
            self._record_meta(
                {
                    "event": "dut_generation",
                    "from": old_generation,
                    "to": new_generation,
                    "reason": "restart",
                }
            )
            self._start_worker("rx_reader", pumps.rx_reader)
            self._start_worker("stderr_drain", pumps.stderr_drain)
            return {"dut_generation": self._dut_generation()}
        if action == "dut_control":
            if self.dut_control is None:
                return {"ok": False, "error": "HARNESS_CAPABILITY_UNAVAILABLE"}
            msg = params.get("msg")
            if not isinstance(msg, dict):
                return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
            try:
                ack = self.dut_control.send(msg, timeout_s=float(params.get("timeout_s", 5.0)))
            except Exception as exc:
                return {"ok": False, "error": self._exception_code(exc, "HARNESS_CONTROL_PROTOCOL_ERROR")}
            ack_generation = ack.get("generation")
            if isinstance(ack_generation, int) and ack_generation > self._dut_ack_generation:
                old_generation = self._dut_generation()
                new_generation = self._dut_generation_base + ack_generation
                self._dut_ack_generation = ack_generation
            else:
                new_generation = self._dut_generation()
            if new_generation > self._dut_generation():
                with self._state_changed:
                    self._dut_generation_value = new_generation
                    self._state_changed.notify_all()
                self._record_meta(
                    {
                        "event": "dut_generation",
                        "from": old_generation,
                        "to": new_generation,
                        "reason": "logical_reboot",
                    }
                )
            return ack
        if action == "shutdown":
            return self._shutdown_action()
        return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}

    def _capability(self, name: str) -> bool:
        try:
            return bool(self.endpoint.capabilities().get(name, False))
        except Exception:
            return False

    def _drain_paused_endpoint(self, *, timeout_s: float = 0.05) -> int:
        discarded = 0
        while not self._stop_event.is_set():
            if self._is_transport_disconnected():
                return discarded
            try:
                data = self.endpoint.read(self.cfg.read_chunk, timeout_s)
            except OSError as exc:
                if self._is_transport_disconnected():
                    return discarded
                raise exc
            if not data:
                return discarded
            self._note_activity("serial_read")
            if not self._record_data("serial_read", data):
                return discarded
            discarded += len(data)
        return discarded

    def _drain_disconnected_stdout(self, *, timeout_s: float) -> int | None:
        """排空斷線期間已到達 DUT stdout 的資料並記為刻意丟棄。"""
        deadline = time.monotonic() + timeout_s
        quiet_s = max(0.1, self.cfg.barrier_quiet_ms / 1000)
        quiet_since: float | None = None
        discarded = 0
        generation = self._dut_generation()
        while time.monotonic() < deadline:
            try:
                data = self.dut.stdout_read(self.cfg.read_chunk, timeout_s=0.05)
            except TypeError:
                data = self.dut.stdout_read(self.cfg.read_chunk)
            if data is None:
                quiet_since = time.monotonic() if quiet_since is None else quiet_since
                if time.monotonic() - quiet_since >= quiet_s:
                    return discarded
                continue
            if not data:
                self._record_stdout_eof(generation)
                return discarded
            quiet_since = None
            self._note_activity("dut_stdout_read")
            if not self._record_data("dut_stdout_read", data):
                return discarded
            discarded += len(data)
        return None

    def _dut_restart(self, params: dict[str, Any], action_id: str) -> dict[str, Any]:
        pending = params.get("pending", "discard")
        if pending not in {"discard", "deliver"}:
            return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
        grace_s = float(params.get("grace_s", 2.0))
        if grace_s < 0:
            return {"ok": False, "error": "HARNESS_CONTROL_PROTOCOL_ERROR"}
        tx_was_reader_paused = self._reader_pause["TX"].is_set()
        tx_was_writer_paused = self._writer_pause["TX"].is_set()
        generation = self._dut_generation()
        process_generation = self._process_generation()
        with self._state_lock:
            self._intentional_dut_exits.add(process_generation)
        drained_len = 0
        discarded = 0
        try:
            self._pause_reader("TX")
            self._pause_writer("TX")
            if pending == "discard":
                drained_len = self._drain_paused_endpoint()
            # deliver 不主動讀端點；kernel PTY 內尚未讀取的 bytes 會在 TX reader 恢復後依序送往新 DUT。
            inflight = self._pump_inflight_bytes.get("tx_writer", 0)
            if pending == "discard":
                discarded = self.tx_q.discard_all() + drained_len + inflight
            process_generation = self.dut.respawn(grace_s=grace_s)
            self._wait_dut_control_ready()
            if hasattr(self.dut, "record_fault"):
                self.dut.record_fault("dut_reboot")
            with self._state_changed:
                self._dut_generation_value = generation + 1
                new_generation = self._dut_generation_value
                self._set_dut_generation_mapping(new_generation)
                if pending == "discard" and inflight:
                    self._tx_restart_discard_generation = process_generation
                self._state_changed.notify_all()
            self._record_meta(
                {
                    "event": "dut_generation",
                    "from": generation,
                    "to": new_generation,
                    "reason": "restart",
                }
            )
            if pending == "discard":
                self._record_meta(
                    {
                        "event": "fault",
                        "action_id": action_id,
                        "action": "dut_restart",
                        "discarded_len": discarded,
                    },
                    loss_flag=False,
                )
        except Exception as exc:
            try:
                if self.dut.poll() is None:
                    self.dut.kill()
            except Exception:
                pass
            return {"ok": False, "error": self._exception_code(exc, "HARNESS_DUT_SPAWN_FAILED")}
        finally:
            if not tx_was_writer_paused:
                self._resume_writer("TX")
            if not tx_was_reader_paused:
                self._resume_reader("TX")
        self._start_worker("rx_reader", pumps.rx_reader)
        self._start_worker("stderr_drain", pumps.stderr_drain)
        return {"dut_generation": self._dut_generation(), "discarded_len": discarded}

    def _shutdown_action(self) -> dict[str, Any]:
        with self._state_lock:
            self._closing = True
        try:
            peer_open = self.endpoint.peer_open()
        except OSError:
            peer_open = None
        if peer_open is False:
            self.rx_q.discard_all()
        deadline = time.monotonic() + 5.0
        drained = False
        while time.monotonic() < deadline:
            if (
                self.tx_q.pending_bytes() == 0
                and self.rx_q.pending_bytes() == 0
                and not any(self._pump_inflight_bytes.values())
                and not any(self._pump_busy.values())
                and not any(self._reader_io_busy.values())
            ):
                drained = True
                break
            time.sleep(0.01)
        if not drained:
            pending = {
                "tx": self.tx_q.pending_bytes() + self._pump_inflight_bytes.get("tx_writer", 0),
                "rx": self.rx_q.pending_bytes() + self._pump_inflight_bytes.get("rx_writer", 0),
            }
            self._pending_at_close = pending
            self._record_meta(
                {
                    "event": "fault",
                    "action_id": uuid.uuid4().hex,
                    "action": "shutdown",
                    "discarded_len": pending["tx"] + pending["rx"],
                },
                loss_flag=True,
            )
            # shutdown 是呼叫端明確要求的結束，不屬 fail-stop（計畫 Task 5：shutdown → wait() 回 0）；
            # 排空逾時未送達的 bytes 已以 loss_flag:true 的 fault META 與 manifest pending_at_close 誠實記錄，
            # 比較器據此判證據不足。HARNESS_WINDOW_UNSETTLED 屬比較器 correlation 原因碼，不作 bridge 失敗碼。
        self._flush_writer()
        self._record_meta({"event": "bridge_stop"})
        self._flush_writer()
        with self._state_lock:
            self._return_code = 3 if self._failure else 0
        # 讓 ControlServer 先將 shutdown 回應送回呼叫端。
        timer = threading.Timer(0.1, self._done_event.set)
        timer.daemon = True
        timer.start()
        return {"evidence": self._evidence_summary()}

    def _evidence_summary(self) -> dict[str, Any]:
        path = self.cfg.evidence_dir / "wire.ndjson"
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        return {
            "records": sum(1 for line in data.splitlines() if line),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def handle_action(
        self, action: str, params: dict[str, Any], action_id: str
    ) -> dict[str, Any]:
        """記錄控制 action 的 requested/completed/failed，並回傳其結果。"""
        if not isinstance(params, dict):
            params = {}
        action_id = action_id or uuid.uuid4().hex
        if action in {"status", "pause_pump", "resume_pump"}:
            return self._handle_action_recorded(action, params, action_id)
        with self._action_lock:
            return self._handle_action_recorded(action, params, action_id)

    def _handle_action_recorded(
        self, action: str, params: dict[str, Any], action_id: str
    ) -> dict[str, Any]:
        self._control_phase(
            action_id=action_id,
            action=action,
            phase="requested",
            params=params,
            result=None,
            error=None,
        )
        try:
            result = self._handle_action(action, params, action_id)
        except Exception as exc:
            result = {
                "ok": False,
                "error": self._exception_code(exc, "HARNESS_CONTROL_PROTOCOL_ERROR"),
            }
        failed = result.get("ok") is False
        self._control_phase(
            action_id=action_id,
            action=action,
            phase="failed" if failed else "completed",
            params=params,
            result=None if failed else result,
            error=str(result.get("error")) if failed else None,
        )
        return result

    def wait(self) -> int:
        """等待 shutdown 或 fail-stop，結束所有 worker 並寫入最終 manifest。"""
        self._done_event.wait()
        self._finish()
        return self._return_code

    def _finish(self) -> None:
        with self._finish_lock:
            if self._finished:
                return
            self._finished = True
            with self._state_lock:
                self._closing = True
        self._stop_event.set()
        self.tx_q.close()
        self.rx_q.close()
        with self._state_changed:
            for pause in (*self._reader_pause.values(), *self._writer_pause.values()):
                pause.clear()
            self._state_changed.notify_all()
        try:
            self.endpoint.close()
        except Exception:
            pass
        try:
            self.dut.terminate(grace_s=0.5)
        except Exception:
            try:
                self.dut.kill()
            except Exception:
                pass
        current = threading.current_thread()
        for thread in list(self._threads.values()):
            if thread is not current:
                thread.join(timeout=2.0)
        close_pipes = getattr(self.dut, "_close_pipes", None)
        if callable(close_pipes):
            close_pipes()
        for attr in ("_control_file", "_stderr_file"):
            stream = getattr(self, attr)
            if stream is not None:
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except OSError:
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        try:
            summary = self.writer.close()
            if not isinstance(summary, dict):
                summary = self._evidence_summary()
        except Exception as exc:
            summary = self._evidence_summary()
            self._writer_failed(exc)
        # close() 的最後一次 fsync/close 也可能首次失敗；不得把它寫成乾淨關閉。
        self._check_writer_failure()
        self._closed_at = self._wall_ts()
        try:
            self._write_manifest(summary)
        except Exception as exc:
            self._fail(self._exception_code(exc, "HARNESS_EVIDENCE_WRITE_FAILED"), exc)
        with self._state_lock:
            if self._failure:
                self._return_code = 3
        if self._failure:
            import sys

            sys.stderr.write(
                json.dumps(
                    {"code": self._failure, "event": "BRIDGE_FAILED"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            sys.stderr.flush()

    def _write_manifest(self, summary: dict[str, Any]) -> None:
        wire_path = self.cfg.evidence_dir / "wire.ndjson"
        try:
            wire_data = wire_path.read_bytes()
        except OSError:
            wire_data = b""
        wire_info = {
            "path": "wire.ndjson",
            "records": int(summary.get("records", self._wire_seq)),
            "bytes": len(wire_data),
            "sha256": hashlib.sha256(wire_data).hexdigest(),
        }
        from uart_stdio_bridge.evidence.manifest import (
            bridge_manifest,
            platform_info,
            write_json_atomic,
        )
        from uart_stdio_bridge.identity import tool_tree_sha256

        transport = self.endpoint.describe()
        manifest = bridge_manifest(
            run_id=self.cfg.run_id,
            case_id=self.cfg.case_id,
            tool={"name": TOOL_NAME, "version": __version__, "tree_sha256": tool_tree_sha256()},
            transport=transport,
            dut=self.dut.describe(),
            evidence={"wire": wire_info},
            platform_info=platform_info(
                endpoint=transport.get("kind"),
                com_driver=self.cfg.com_driver,
                com_driver_install_method=self.cfg.com_driver_install_method,
            ),
            started_at=self._started_at,
            closed_at=self._closed_at,
            closed_cleanly=self._failure is None,
            failure=self._failure,
        )
        manifest.update(
            {
                "dut_stderr_bytes": self._stderr_total,
                "dut_stderr_written_bytes": self._stderr_written,
                "dut_stderr_truncated": self._stderr_truncated,
                "test_overrides": dict(self.cfg.test_overrides),
            }
        )
        if self._pending_at_close is not None:
            manifest["pending_at_close"] = dict(self._pending_at_close)
        path = self.cfg.evidence_dir / "bridge-manifest.json"
        try:
            write_json_atomic(path, manifest)
        except OSError:
            # manifest 寫入失敗不能將原先成功的執行標成乾淨關閉。
            with self._state_lock:
                if self._failure is None:
                    self._failure = "HARNESS_EVIDENCE_WRITE_FAILED"
                    self._return_code = 3

__all__ = ["BridgeConfig", "Bridge"]
