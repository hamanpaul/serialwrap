"""以 pySerial 開啟 Windows COM 對端的 adapter。"""

from __future__ import annotations

import importlib
import math
import sys
import threading
import time
from typing import Any, Callable

from .base import EndpointError, EndpointWriteUncertain

# pySerial 的 Windows `open()` 先以 SetCommState 拉起 DTR（對端即見 DSR），之後才以 PurgeComm 清空
# 兩向緩衝；DSR 轉高到 PurgeComm 之間的極短窗口若寫入，bytes 會被清掉。peer 首次被觀測到開啟後，
# 延後此時間才允許寫入，以跨過該窗口。0.2 s 為未經量測的選值，待 G3 原生實測（PENDING-OD-S1X-WIN-BANNER）校準。
_PEER_OPEN_SETTLE_S = 0.2


class WinSerialEndpoint:
    """透過 pySerial 開啟 COM-B（bridge 端）。

    peer 開啟狀態以 COM-B 的 DSR 取樣：com0com 將對端（COM-A）開啟後拉起的 DTR 映射到本端 DSR。
    `peer_gate=True` 時，peer 未開啟（或剛開啟、仍在 settle 窗口）不寫入，寫入串流長度為 0，
    對應 POSIX PTY 的 peer gate。`capabilities()` 僅於 com0com 且 `peer_gate=True` 時宣告 `peer_state`
    （DSR 取樣已於 COM20:COM21 原生實測）；`transport_disconnect` 恆為 False，不由本取樣推論。
    """

    kind = "win-serial"

    def __init__(
        self,
        com: str,
        *,
        baud: int = 115200,
        transport_mode: str = "virtual_com0com",
        allow_peer_close: bool = False,
        peer_gate: bool = False,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if transport_mode not in ("virtual_com0com", "physical_usb_uart"):
            raise ValueError(f"不支援的 Windows transport_mode：{transport_mode}")
        if bool(peer_gate) and transport_mode != "virtual_com0com":
            # DSR 映射僅於 com0com（COM-A DTR → COM-B DSR）實測；3 線實體 adapter 的 DSR 可能未接，
            # 啟用 gate 會使 DSR 恆低而永不寫入，故拒絕而非靜默失效。
            raise ValueError("peer_gate 僅適用 transport_mode=virtual_com0com（DSR 映射僅於 com0com 實測）")
        self.com = com
        self.baud = baud
        self.transport_mode = transport_mode
        self.allow_peer_close = bool(allow_peer_close)
        self.peer_gate = bool(peer_gate)
        self._serial_factory = serial_factory
        self._serial_obj: Any | None = None
        self._serial_timeout_exception: type[BaseException] | None = None
        # DSR 首次被觀測為高的時間點（monotonic）；DSR 為低時清為 None。受 `_lock` 保護。
        self._peer_on_since: float | None = None
        self.generation = 0
        self.last_fault: str | None = None
        # `_lock` 只保護狀態（handle、generation、closed 與 timeout 設定），絕不在阻塞 I/O 期間持有。
        self._lock = threading.RLock()
        # 讀與寫各自串行化、彼此並行：寫入阻塞（對端不讀、緩衝區滿）時，讀取端仍須可前進，
        # 否則 bridge 無法持續排空接收端，會形成 ST-03(a) 所描述的雙向死鎖。
        self._read_io_lock = threading.Lock()
        self._write_io_lock = threading.Lock()
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
                elif serial_obj.timeout != timeout:
                    serial_obj.timeout = timeout
            if not wait_without_handle:
                # 阻塞讀取不持有狀態鎖（僅持讀取串行鎖），寫入端可同時前進。
                with self._read_io_lock:
                    # 取得串行鎖前 handle 可能已被控制面換掉（斷線或重開）：於串行鎖內重驗，不讀已關閉的 handle。
                    # 未通過則落到下方等待，由迴圈頂端依最新狀態回空或等待（與無 handle 同語意）。
                    if self._serial_obj is serial_obj and self.generation == start_generation:
                        try:
                            return bytes(serial_obj.read(max_bytes))
                        except Exception as exc:
                            raise EndpointError(
                                f"HARNESS_ENDPOINT_IO_ERROR: 讀取 {self.com} 失敗：{exc}"
                            ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
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
            must_wait = False
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return 0
                serial_obj = self._serial_obj
                if serial_obj is None:
                    must_wait = True
                else:
                    if serial_obj.write_timeout != write_timeout:
                        serial_obj.write_timeout = write_timeout
                    # peer gate：peer 未開啟或仍在 settle 窗口時不寫入（不進入埠、不產生寫入紀錄），等至期限回 0。
                    if not self._peer_writable_locked(serial_obj):
                        must_wait = True
            if not must_wait:
                # 阻塞寫入不持有狀態鎖（僅持寫入串行鎖），讀取端不受阻塞。
                with self._write_io_lock:
                    # 同讀取端：取得串行鎖前 handle 可能已被換掉，於串行鎖內重驗；未通過則不寫入（不產生不確定寫入）。
                    if self._serial_obj is serial_obj and self.generation == start_generation:
                        try:
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
            time.sleep(min(0.01, remaining))

    def peer_open(self) -> bool | None:
        """以 COM-B 的 DSR 取樣對端是否開啟；未啟用 peer gate、無 handle 或讀取失敗時回 None（不可得知）。

        未啟用 peer gate 時不取樣，維持與既有行為一致：若回 False，pumps 會將寫入逾時縮為 50 ms，
        導致對端關閉時寫入逾時並 fail-stop（HARNESS_WRITE_UNCERTAIN）。
        """
        if not self.peer_gate:
            return None
        with self._lock:
            serial_obj = self._serial_obj
            if serial_obj is None:
                return None
            return self._peer_dsr_locked(serial_obj)

    def capabilities(self) -> dict[str, bool]:
        # peer_state 僅於 com0com 且 peer gate 啟用時宣告：peer 關閉後 DSR 於 1 s 內拉低，寫入回 0，
        # 已於 COM20:COM21 原生實測（b-log/rust-refactor/g3-peer-gate/261023-061416-2646662）。
        # transport_disconnect 恆為 False：com0com 無不需管理員權限的斷線原語，peer 關閉僅標示為 peer_close。
        peer_state = self.peer_gate and self.transport_mode == "virtual_com0com"
        return {"transport_disconnect": False, "by_id_link": False, "peer_state": peer_state}

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
                "peer_gate": self.peer_gate,
                "peer_open_settle_s": _PEER_OPEN_SETTLE_S,
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
        self._peer_on_since = None
        timeout_exception = getattr(module, "SerialTimeoutException", None)
        if isinstance(timeout_exception, type):
            self._serial_timeout_exception = timeout_exception
        self.generation += 1

    def _peer_dsr_locked(self, serial_obj: Any) -> bool | None:
        """讀取本端 DSR（pySerial 的 `dsr` 以 GetCommModemStatus 實作）並維護 settle 起點；讀不到回 None。"""
        try:
            dsr_on = bool(serial_obj.dsr)
        except Exception:
            return None
        if not dsr_on:
            self._peer_on_since = None
        elif self._peer_on_since is None:
            self._peer_on_since = time.monotonic()
        return dsr_on

    def _peer_writable_locked(self, serial_obj: Any) -> bool:
        """peer gate 判定：關閉時恆允許；開啟時需 DSR 為高且已過 settle 窗口。"""
        if not self.peer_gate:
            return True
        dsr_on = self._peer_dsr_locked(serial_obj)
        if not dsr_on or self._peer_on_since is None:
            return False
        return time.monotonic() - self._peer_on_since >= _PEER_OPEN_SETTLE_S

    def _release_serial_locked(self) -> None:
        # 控制面（關閉、重開）必須等待進行中的讀取與寫入結束才換掉 handle：阻塞中的 I/O 不被半途關閉。
        # 鎖序固定為 `_lock` → 讀取串行鎖 → 寫入串行鎖；I/O 路徑不在持有串行鎖時取得 `_lock`，故不會死鎖。
        with self._read_io_lock, self._write_io_lock:
            serial_obj = self._serial_obj
            self._serial_obj = None
            self._peer_on_since = None
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
