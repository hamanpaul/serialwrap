"""DUT 檔案目錄控制通道（ControlFileChannel 與 DutControlClient）。

透過 control.ndjson 與 ack.ndjson 檔案進行雙向控制與回應確認，
不需網路 socket，可直接於容器 bind mount 目錄運作。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any
import uuid


class ControlFileChannel:
    """DUT 端控制檔通道：輪詢 control.ndjson 並將 ack 回應寫入 ack.ndjson。"""

    def __init__(self, control_dir: pathlib.Path) -> None:
        self.control_dir = pathlib.Path(control_dir)
        self._control_file = self.control_dir / "control.ndjson"
        self._ack_file = self.control_dir / "ack.ndjson"
        self._offset: int = (
            self._control_file.stat().st_size if self._control_file.exists() else 0
        )
        self._buf: bytes = b""

    def poll(self) -> list[dict[str, Any]]:
        """讀取 control.ndjson 自上次 offset 起的完整新行。忽略未寫完的半行。"""
        if not self._control_file.exists():
            return []
        try:
            st = self._control_file.stat()
            if st.st_size < self._offset:
                self._offset = 0
                self._buf = b""
            with open(self._control_file, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return []
        if not chunk:
            return []

        self._buf += chunk
        self._offset += len(chunk)

        messages: list[dict[str, Any]] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line.decode("utf-8"))
                    if isinstance(obj, dict):
                        messages.append(obj)
                except Exception:
                    pass
        return messages

    def ack(self, ack: dict[str, Any]) -> None:
        """附加 ack 物件至 ack.ndjson，並執行 flush 與 fsync。"""
        line = (
            json.dumps(ack, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.control_dir.mkdir(parents=True, exist_ok=True)
        with open(self._ack_file, "ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


class DutControlClient:
    """Bridge 端控制通道客戶端：向 control.ndjson 發送訊息並等待 ack.ndjson。"""

    def __init__(self, control_dir: pathlib.Path) -> None:
        self.control_dir = pathlib.Path(control_dir)
        self._control_file = self.control_dir / "control.ndjson"
        self._ack_file = self.control_dir / "ack.ndjson"
        self._client_id: str = uuid.uuid4().hex[:8]
        self._counter: int = 0
        self._lock = threading.Lock()
        self._ack_offset: int = (
            self._ack_file.stat().st_size if self._ack_file.exists() else 0
        )
        self._ack_buf: bytes = b""
        self._received_acks: dict[str, dict[str, Any]] = {}

    def _read_acks(self) -> None:
        if not self._ack_file.exists():
            return
        try:
            st = self._ack_file.stat()
            if st.st_size < self._ack_offset:
                self._ack_offset = 0
                self._ack_buf = b""
            with open(self._ack_file, "rb") as f:
                f.seek(self._ack_offset)
                chunk = f.read()
        except OSError:
            return
        if not chunk:
            return

        self._ack_buf += chunk
        self._ack_offset += len(chunk)
        while b"\n" in self._ack_buf:
            line, self._ack_buf = self._ack_buf.split(b"\n", 1)
            line = line.strip()
            if line:
                try:
                    ack_obj = json.loads(line.decode("utf-8"))
                    if isinstance(ack_obj, dict):
                        ack_id = ack_obj.get("id")
                        if ack_id is not None:
                            self._received_acks[str(ack_id)] = ack_obj
                except Exception:
                    pass

    def send(self, msg: dict[str, Any], *, timeout_s: float = 5.0) -> dict[str, Any]:
        """發送控制指令並等待對應 id 的 ack。若逾時則拋出 TimeoutError。"""
        with self._lock:
            payload = dict(msg)
            if not payload.get("id"):
                self._counter += 1
                req_id = f"dc-{self._client_id}-{self._counter}"
                payload["id"] = req_id
            else:
                req_id = str(payload["id"])

            line = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            self.control_dir.mkdir(parents=True, exist_ok=True)
            with open(self._control_file, "ab") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                self._read_acks()
                if req_id in self._received_acks:
                    return self._received_acks.pop(req_id)
                time.sleep(0.02)

            self._read_acks()
            if req_id in self._received_acks:
                return self._received_acks.pop(req_id)

            raise TimeoutError(f"逾時 {timeout_s}s 等待 DUT 控制回應 id={req_id}")
