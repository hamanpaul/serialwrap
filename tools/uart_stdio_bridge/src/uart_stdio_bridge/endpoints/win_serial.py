"""以 pySerial 開啟 Windows COM 對端的 adapter。"""

from __future__ import annotations

import importlib
import math
import sys
import threading
import time
from typing import Any, Callable

from .base import EndpointError, EndpointWriteUncertain


class WinSerialEndpoint:
    """透過 pySerial 開啟 COM-B，不推測 Windows peer 狀態或斷線能力。"""

    kind = "win-serial"

    def __init__(
        self,
        com: str,
        *,
        baud: int = 115200,
        transport_mode: str = "virtual_com0com",
        allow_peer_close: bool = False,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if transport_mode not in ("virtual_com0com", "physical_usb_uart"):
            raise ValueError(f"不支援的 Windows transport_mode：{transport_mode}")
        self.com = com
        self.baud = baud
        self.transport_mode = transport_mode
        self.allow_peer_close = bool(allow_peer_close)
        self._serial_factory = serial_factory
        self._serial_obj: Any | None = None
        self._serial_timeout_exception: type[BaseException] | None = None
        self.generation = 0
        self.last_fault: str | None = None
        self._lock = threading.RLock()
        self._closed = False

    def open(self) -> None:
        """延遲載入 pySerial 並以固定 8N1 參數開啟 COM。"""
        with self._lock:
            self._open_locked()

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """讀取已到達的 bytes；斷線或 timeout 時回傳空 bytes。"""
        with self._lock:
            self._ensure_io_allowed_locked()
            start_generation = self.generation
        if max_bytes <= 0:
            return b""

        timeout = self._normalized_timeout(timeout_s)
        deadline = time.monotonic() + timeout
        while True:
            wait_without_handle = False
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return b""
                serial_obj = self._serial_obj
                if serial_obj is None:
                    wait_without_handle = True
                else:
                    try:
                        if serial_obj.timeout != timeout:
                            serial_obj.timeout = timeout
                        return bytes(serial_obj.read(max_bytes))
                    except Exception as exc:
                        raise EndpointError(
                            f"HARNESS_ENDPOINT_IO_ERROR: 讀取 {self.com} 失敗：{exc}"
                        ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            if wait_without_handle:
                time.sleep(min(0.01, remaining))

    def write_some(self, data: bytes, timeout_s: float) -> int:
        """寫入一段 bytes，回報 pySerial 實際回報的接受長度。"""
        with self._lock:
            self._ensure_io_allowed_locked()
            start_generation = self.generation
        if not data:
            return 0

        timeout = self._normalized_timeout(timeout_s)
        # pySerial 3.5 的 Windows backend 在 write_timeout=0 時無法確認
        # overlapped write 的實際接受長度，因此禁止使用該非阻塞回報路徑。
        write_timeout = max(timeout, 0.001)
        deadline = time.monotonic() + timeout
        while True:
            wait_without_handle = False
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return 0
                serial_obj = self._serial_obj
                if serial_obj is None:
                    wait_without_handle = True
                else:
                    try:
                        if serial_obj.write_timeout != write_timeout:
                            serial_obj.write_timeout = write_timeout
                        count = int(serial_obj.write(data))
                    except Exception as exc:
                        is_timeout = self._serial_timeout_exception is not None and isinstance(
                            exc, self._serial_timeout_exception
                        )
                        # 注入 factory 時也支援由替身提供的 pySerial 同名逾時例外。
                        if is_timeout or exc.__class__.__name__ == "SerialTimeoutException":
                            raise EndpointWriteUncertain(len(data), "write timeout") from exc
                        raise EndpointError(
                            f"HARNESS_ENDPOINT_IO_ERROR: 寫入 {self.com} 失敗：{exc}"
                        ) from exc
                    if not 0 <= count <= len(data):
                        raise EndpointWriteUncertain(
                            len(data), f"unexpected write count {count}"
                        )
                    return count

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0
            if wait_without_handle:
                time.sleep(min(0.01, remaining))

    def peer_open(self) -> None:
        return None

    def capabilities(self) -> dict[str, bool]:
        return {"transport_disconnect": False, "by_id_link": False, "peer_state": False}

    def disconnect(self, *, remove_link: bool) -> None:
        with self._lock:
            self._ensure_not_closed_locked()
            if not self.allow_peer_close:
                raise EndpointError(
                    "HARNESS_CAPABILITY_UNAVAILABLE: Windows COM 不支援 transport_disconnect"
                )
            if self._serial_obj is not None:
                self.last_fault = "peer_close"
                self._release_serial_locked()

    def reconnect(self) -> int:
        with self._lock:
            self._ensure_not_closed_locked()
            if self._serial_obj is not None:
                if not self.allow_peer_close:
                    raise EndpointError(
                        "HARNESS_CAPABILITY_UNAVAILABLE: Windows COM 重開需要 allow_peer_close"
                    )
                self.last_fault = "peer_close"
                self._release_serial_locked()
            self._open_locked()
            return self.generation

    def link_remove(self) -> None:
        raise EndpointError("HARNESS_CAPABILITY_UNAVAILABLE: Windows COM 不支援 by-id link_remove")

    def link_add(self) -> None:
        raise EndpointError("HARNESS_CAPABILITY_UNAVAILABLE: Windows COM 不支援 by-id link_add")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._release_serial_locked()

    def describe(self) -> dict[str, Any]:
        with self._lock:
            description: dict[str, Any] = {
                "kind": self.kind,
                "transport_mode": self.transport_mode,
                "com": self.com,
                "baud": self.baud,
                "generation": self.generation,
                "allow_peer_close": self.allow_peer_close,
            }
            if self.last_fault is not None:
                description["last_fault"] = self.last_fault
            return description

    def _open_locked(self) -> None:
        self._ensure_not_closed_locked()
        if self._serial_obj is not None:
            return

        module: Any | None = None
        factory = self._serial_factory
        if factory is None:
            try:
                module = importlib.import_module("serial")
            except ImportError as exc:
                raise EndpointError(
                    "HARNESS_ENDPOINT_OPEN_FAILED: Windows 端點需要 pyserial"
                ) from exc
            factory = module.Serial
        else:
            module = sys.modules.get("serial")

        try:
            serial_obj = factory(
                port=self.com,
                baudrate=self.baud,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.05,
                write_timeout=None,
                exclusive=None,
            )
        except Exception as exc:
            raise EndpointError(f"HARNESS_ENDPOINT_OPEN_FAILED: 無法開啟 {self.com}：{exc}") from exc

        self._serial_obj = serial_obj
        timeout_exception = getattr(module, "SerialTimeoutException", None)
        if isinstance(timeout_exception, type):
            self._serial_timeout_exception = timeout_exception
        self.generation += 1

    def _release_serial_locked(self) -> None:
        serial_obj = self._serial_obj
        self._serial_obj = None
        if serial_obj is not None:
            try:
                serial_obj.close()
            except Exception as exc:
                raise EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: 關閉 {self.com} 失敗：{exc}") from exc

    def _ensure_not_closed_locked(self) -> None:
        if self._closed:
            raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: Windows COM 已關閉")

    def _ensure_io_allowed_locked(self) -> None:
        self._ensure_not_closed_locked()
        if self.generation == 0:
            raise EndpointError("HARNESS_ENDPOINT_IO_ERROR: Windows COM 尚未開啟")

    @staticmethod
    def _normalized_timeout(timeout_s: float) -> float:
        if math.isnan(timeout_s) or timeout_s <= 0:
            return 0.0
        return timeout_s


__all__ = ["WinSerialEndpoint"]
