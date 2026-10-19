"""管理外部 uart-stdio-bridge 程序與它的控制／證據介面。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

from .daemon_harness import ROOT_DIR, StepError

BRIDGE_SRC = ROOT_DIR / "tools" / "uart_stdio_bridge" / "src"
_PATH_CLASS = type(pathlib.Path("."))
_ALLOWED_RUN_ARGS = {"--max-inflight-bytes", "--read-chunk", "--write-timeout-s", "--allow-dut-exit"}


class BridgeSession:
    """啟動、操作、settle 與停止單一外部 bridge run。"""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        port_id: str,
        endpoint_spec: dict[str, Any],
        evidence_dir: pathlib.Path,
        target_json: dict[str, Any],
        plan_json: dict[str, Any] | None,
        dut_mode: str,
        env: dict[str, str],
        quiesce: bool = False,
        extra_args: Sequence[str] = (),
        dut_image: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self.port_id = port_id
        self.endpoint_spec = dict(endpoint_spec)
        self.evidence_dir = evidence_dir if isinstance(evidence_dir, pathlib.PurePath) else _PATH_CLASS(evidence_dir)
        self.target_json = dict(target_json)
        self.plan_json = None if plan_json is None else dict(plan_json)
        self.dut_mode = dut_mode
        self.env = dict(env)
        self.quiesce = quiesce
        self.extra_args = tuple(extra_args)
        self.dut_image = dut_image
        self.rpc_endpoint: str | None = None
        self.rpc_env: dict[str, str] | None = None
        self.process: subprocess.Popen[str] | None = None
        self.control_info: pathlib.Path = self.evidence_dir / "control.json"
        self._control_client: Any | None = None
        self._ready_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._log_stream: Any | None = None
        self._input_paths: list[pathlib.Path] = []
        self._started = False
        self._quiet_ms = 500
        self.last_checkpoint: pathlib.Path | None = None

    def command(self) -> list[str]:
        """組裝與驗證 bridge run 子命令 argv。"""
        args = [
            sys.executable, "-m", "uart_stdio_bridge", "run",
            "--run-id", self.run_id,
            "--case-id", self.case_id,
            "--port-id", self.port_id,
            "--evidence-dir", str(self.evidence_dir),
        ]
        kind = self.endpoint_spec.get("kind")
        if kind == "posix-pty":
            link = self.endpoint_spec.get("by_id_link")
            if not link:
                raise ValueError("posix-pty endpoint_spec 缺少 by_id_link")
            args.extend(["--endpoint", "posix-pty", "--by-id-link", str(link)])
        elif kind == "win-serial":
            com = self.endpoint_spec.get("com")
            if not com:
                raise ValueError("win-serial endpoint_spec 缺少 com")
            args.extend([
                "--endpoint", "win-serial", "--com", str(com),
                "--transport-mode", str(self.endpoint_spec.get("transport_mode", "virtual_com0com")),
            ])
        else:
            raise ValueError(f"不支援的 bridge endpoint：{kind!r}")
        args.extend(["--dut-mode", self.dut_mode])
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.evidence_dir / ".func-test-target.json"
        target_path.write_text(json.dumps(self.target_json, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self._input_paths.append(target_path)
        args.extend(["--dut-target-json", str(target_path)])
        if self.plan_json is not None:
            plan_path = self.evidence_dir / ".func-test-plan.json"
            plan_path.write_text(json.dumps(self.plan_json, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            self._input_paths.append(plan_path)
            args.extend(["--dut-plan-json", str(plan_path)])
        if self.dut_mode == "docker":
            image = self.dut_image or (self.plan_json or {}).get("image")
            if not image:
                raise StepError("docker DUT 模式必須提供 --dut-image 或 dut_plan.image")
            args.extend(["--dut-image", str(image)])
        args.extend(self._validated_extra_args())
        return args

    def _validated_extra_args(self) -> list[str]:
        result: list[str] = []
        index = 0
        while index < len(self.extra_args):
            option = self.extra_args[index]
            if option not in _ALLOWED_RUN_ARGS:
                raise StepError(f"bridge.run_args 不允許參數：{option}")
            result.append(option)
            if option == "--allow-dut-exit":
                index += 1
                continue
            if index + 1 >= len(self.extra_args):
                raise StepError(f"bridge.run_args 缺少 {option} 的值")
            result.append(str(self.extra_args[index + 1]))
            index += 2
        return result

    def start(self, timeout_s: float = 15.0) -> None:
        """啟動子程序並等待 BRIDGE_READY 控制端點。"""
        if self._started:
            return
        command = self.command()
        env = dict(self.env)
        src = str(BRIDGE_SRC)
        env["PYTHONPATH"] = os.pathsep.join(
            [src, *[part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part and part != src]]
        )
        log_path = self.evidence_dir / "bridge.log"
        self._log_stream = log_path.open("ab", buffering=0)
        self.process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._read_output, name=f"bridge-ready-{self.run_id}", daemon=True)
        self._reader_thread.start()
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None and self._ready_queue.empty():
                    raise StepError(f"bridge 在 BRIDGE_READY 前結束：{self._tail_log()}")
                try:
                    item = self._ready_queue.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if isinstance(item, BaseException):
                    raise StepError(f"bridge 無法輸出 READY：{item}")
                if item.get("event") == "BRIDGE_READY":
                    control_info = item.get("control_info")
                    if not isinstance(control_info, str):
                        raise StepError("bridge BRIDGE_READY 缺少 control_info")
                    self.control_info = _PATH_CLASS(control_info)
                    from uart_stdio_bridge.bridge.control import ControlClient

                    self._control_client = ControlClient(self.control_info, timeout_s=30.0)
                    self._started = True
                    for path in self._input_paths:
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    self._input_paths.clear()
                    return
            raise TimeoutError(f"bridge 未在 {timeout_s}s 內回報 BRIDGE_READY")
        except Exception:
            self.stop()
            raise

    def _read_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if self._log_stream is not None:
                    self._log_stream.write(line.encode("utf-8", errors="replace"))
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("event") == "BRIDGE_READY":
                    self._ready_queue.put(obj)
        except BaseException as exc:
            self._ready_queue.put(exc)

    def _tail_log(self) -> str:
        try:
            return (self.evidence_dir / "bridge.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            return "bridge.log 不可讀"

    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._control_client is None:
            raise StepError("bridge control client 尚未就緒")
        return self._control_client.call(action, params)

    def _wal_current_seq(self) -> int:
        if not self.rpc_endpoint:
            raise StepError("bridge settle 缺少 daemon RPC endpoint")
        from .cli_client import cli_run

        response = cli_run(
            ["wal", "current-seq"],
            socket_path=self.rpc_endpoint,
            env=self.rpc_env or self.env,
            timeout=3.0,
        )
        if response.get("ok") is not True or type(response.get("seq")) is not int:
            raise StepError(f"無法查詢 WAL current-seq：{response}")
        return int(response["seq"])

    @staticmethod
    def _wire_result(result: dict[str, Any]) -> dict[str, Any]:
        offsets = result.get("offsets")
        if not isinstance(offsets, dict):
            offsets = {}
        return {
            "offsets": {key: int(offsets.get(key, 0)) for key in (
                "serial_read", "dut_stdin_write", "dut_stdout_read", "serial_write"
            )},
            "endpoint_generation": int(result.get("endpoint_generation", 1)),
            "dut_generation": int(result.get("dut_generation", 1)),
            "wire_seq": int(result.get("wire_seq", 0)),
        }

    def snapshot(self, *, settle: bool) -> dict[str, Any]:
        """取 WAL／wire 邊界；必要時重試到 offsets 與 WAL seq 穩定。"""
        if not settle:
            wire_raw = self.call("barrier", {"mode": "passive", "quiet_ms": 0, "timeout_ms": 500})
            seq = self._wal_current_seq()
            wire = self._wire_result(wire_raw)
            return {"settled": wire_raw.get("settled") is True, "wal_seq": seq, "wire": wire}

        if self.quiesce:
            first: dict[str, Any] = {}
            try:
                first = self.call("barrier", {"mode": "quiesce", "quiet_ms": self._quiet_ms, "timeout_ms": 5000})
                if first.get("settled") is not True:
                    return {"settled": False, "wal_seq": self._wal_current_seq(), "wire": self._wire_result(first)}
                for _ in range(10):
                    seq1 = self._wal_current_seq()
                    time.sleep(self._quiet_ms / 1000)
                    seq2 = self._wal_current_seq()
                    if seq1 != seq2:
                        continue
                    second = self.call("barrier", {
                        "mode": "passive", "quiet_ms": self._quiet_ms, "timeout_ms": 5000
                    })
                    wire1 = self._wire_result(first)
                    wire2 = self._wire_result(second)
                    if second.get("settled") is True and wire1["offsets"] == wire2["offsets"]:
                        return {"settled": True, "wal_seq": seq2, "wire": wire2}
                return {"settled": False, "wal_seq": self._wal_current_seq(), "wire": self._wire_result(first)}
            finally:
                try:
                    self.call("resume_pump", {"dir": "RX"})
                except Exception:
                    pass

        for _ in range(10):
            first = self.call("barrier", {
                "mode": "passive", "quiet_ms": self._quiet_ms, "timeout_ms": 5000
            })
            seq1 = self._wal_current_seq()
            second = self.call("barrier", {
                "mode": "passive", "quiet_ms": self._quiet_ms, "timeout_ms": 5000
            })
            seq2 = self._wal_current_seq()
            wire1 = self._wire_result(first)
            wire2 = self._wire_result(second)
            if (
                first.get("settled") is True
                and second.get("settled") is True
                and seq1 == seq2
                and wire1["offsets"] == wire2["offsets"]
            ):
                return {"settled": True, "wal_seq": seq2, "wire": wire2}
        return {"settled": False, "wal_seq": self._wal_current_seq(), "wire": wire2}

    def checkpoint(self) -> pathlib.Path:
        """要求 bridge 落下 wire/control 一致前綴 checkpoint。"""
        result = self.call("checkpoint")
        path_value = result.get("path")
        if not isinstance(path_value, str):
            raise StepError(f"bridge checkpoint 未回傳路徑：{result}")
        self.last_checkpoint = _PATH_CLASS(path_value)
        return self.last_checkpoint

    def wire_tail(self) -> "WireTail":
        return WireTail(self.evidence_dir / "wire.ndjson")

    def stop(self) -> dict[str, Any]:
        """送出 shutdown，確保 bridge process tree 結束並回傳摘要。"""
        process = self.process
        if process is not None:
            if process.poll() is None and self._control_client is not None:
                if self.quiesce:
                    try:
                        self.call("dut_control", {
                            "msg": {"action": "pause_responding", "duration_ms": 30000}
                        })
                    except Exception:
                        pass
                    try:
                        self.call("barrier", {
                            "mode": "passive",
                            "quiet_ms": self._quiet_ms,
                            "timeout_ms": 5000,
                        })
                    except Exception:
                        pass
                    try:
                        self.call("barrier", {
                            "mode": "quiesce",
                            "quiet_ms": self._quiet_ms,
                            "timeout_ms": 5000,
                        })
                    except Exception:
                        pass
                try:
                    self.call("shutdown")
                except Exception:
                    pass
            if process.poll() is None:
                try:
                    process.wait(timeout=15.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3.0)
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=1.0)
            self.process = None
        if self._control_client is not None:
            self._control_client.close()
            self._control_client = None
        if self._log_stream is not None:
            self._log_stream.flush()
            self._log_stream.close()
            self._log_stream = None
        self._started = False
        wire_path = self.evidence_dir / "wire.ndjson"
        digest = hashlib.sha256(wire_path.read_bytes()).hexdigest() if wire_path.is_file() else None
        manifest_path = self.evidence_dir / "bridge-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("bridge manifest root is not an object")
            closed_cleanly = manifest.get("closed_cleanly") is True
            failure = manifest.get("failure")
        except (OSError, ValueError, TypeError):
            closed_cleanly = False
            failure = "BRIDGE_MANIFEST_UNAVAILABLE"
        return {
            "exit_code": process.returncode if process is not None else None,
            "evidence_digest": digest,
            "closed_cleanly": closed_cleanly,
            "failure": failure,
        }


class WireTail:
    """可跨 NDJSON chunk 邊界搜尋指定 observation 的原始 byte stream。"""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path if isinstance(path, pathlib.PurePath) else pathlib.Path(path)

    def find(self, marker: bytes, *, observation: str, timeout_s: float) -> int | None:
        if not marker:
            return 0
        deadline = time.monotonic() + timeout_s
        while True:
            streams: dict[tuple[str, int], dict[str, Any]] = {}
            if self.path.is_file():
                for line in self.path.read_bytes().splitlines():
                    try:
                        record = json.loads(line)
                        if record.get("observation") != observation or not record.get("payload_b64"):
                            continue
                        port_id = record.get("port_id")
                        generation = record.get("endpoint_generation")
                        offset = record.get("stream_offset")
                        if not isinstance(port_id, str) or type(generation) is not int or type(offset) is not int:
                            continue
                        payload = base64.b64decode(record["payload_b64"], validate=True)
                        key = (port_id, generation)
                        state = streams.get(key)
                        if state is None or offset != state["next_offset"]:
                            state = {"start_offset": offset, "next_offset": offset, "data": bytearray()}
                            streams[key] = state
                        state["data"].extend(payload)
                        state["next_offset"] = offset + len(payload)
                        index = bytes(state["data"]).find(marker)
                        if index >= 0:
                            return state["start_offset"] + index
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


__all__ = ["BridgeSession", "WireTail"]
