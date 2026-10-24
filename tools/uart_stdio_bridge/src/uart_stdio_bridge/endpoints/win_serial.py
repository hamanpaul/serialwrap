"""以 pySerial 開啟 Windows COM 對端的 adapter。"""

from __future__ import annotations

import importlib
import math
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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

    期限與串行化（G9，sol ca0106f Medium）：
    - pySerial 的期限 setter 會重設整個 COM handle；開啟時將讀取期限固定為 50 ms，`read()` 以輪詢累計呼叫期限，
      不在讀取期間呼叫 setter，避免阻塞寫入時也停住 UART 排空。
    - `write_timeout` 變更由 `_timeout_section` 取得兩把串行鎖後設定，再持有寫入鎖直到該次 I/O 結束，
      因此不會沿用其他並行呼叫改寫後的期限。
    - 串行鎖的取得順序固定為「寫入串行鎖 → 讀取串行鎖」；任何路徑都不在持有串行鎖時取得 `_lock`。
    - 等待中的期限設定數（`_reconfig_waiters`）會使常態 I/O 讓出，避免讀取端緊密迴圈餓死寫入端的期限設定。
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
        # `_lock` 只保護狀態（handle、generation、closed 與 peer 取樣狀態），絕不在阻塞 I/O 期間持有。
        self._lock = threading.RLock()
        # 讀與寫各自串行化、彼此並行：寫入阻塞（對端不讀、緩衝區滿）時，讀取端仍須可前進，
        # 否則 bridge 無法持續排空接收端，會形成 ST-03(a) 所描述的雙向死鎖。
        # 同時持有兩把時的取得順序固定為寫入 → 讀取（見類別說明）。
        self._read_io_lock = threading.Lock()
        self._write_io_lock = threading.Lock()
        # 等待中（或進行中）的期限設定數；受 `_reconfig_guard` 保護。該鎖為葉節點，持有期間不取得其他鎖。
        self._reconfig_guard = threading.Lock()
        self._reconfig_waiters = 0
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
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return b""
                serial_obj = self._serial_obj
            # 無 handle，或有期限設定等待中（讓出給設定端）時，本輪不進行 I/O，落到下方等待。
            if serial_obj is not None and not self._reconfig_requested():
                data = self._read_once(serial_obj, start_generation, max_bytes, deadline)
                if data:
                    return data

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
            with self._lock:
                self._ensure_io_allowed_locked()
                if self.generation != start_generation:
                    return 0
                serial_obj = self._serial_obj
                # peer gate：peer 未開啟或仍在 settle 窗口時不寫入（不進入埠、不產生寫入紀錄、不改期限），等至期限回 0。
                writable = serial_obj is not None and self._peer_writable_locked(serial_obj)
            if writable and serial_obj is not None and not self._reconfig_requested():
                count = self._write_once(serial_obj, start_generation, data, write_timeout, deadline)
                if count is not None:
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

    def _read_once(
        self, serial_obj: Any, generation: int, max_bytes: int, deadline: float
    ) -> bytes | None:
        """以固定短讀取期限讀取一次；handle 已換掉時回 None（由呼叫端等待後重試）。"""
        if not self._acquire_until(self._read_io_lock, deadline):
            return None
        try:
            if not self._handle_is_current(serial_obj, generation):
                return None
            return self._read_locked(serial_obj, max_bytes)
        finally:
            self._read_io_lock.release()

    def _write_once(
        self, serial_obj: Any, generation: int, data: bytes, write_timeout: float, deadline: float
    ) -> int | None:
        """以 `write_timeout` 寫入一次；handle 已換掉時回 None（不寫入，故不產生不確定寫入）。"""
        # 常態路徑：期限已符合時，於寫入串行鎖內直接寫入（期限與 I/O 同一臨界區）。
        with self._write_io_lock:
            if not self._handle_is_current(serial_obj, generation):
                return None
            if serial_obj.write_timeout == write_timeout:
                return self._write_locked(serial_obj, data)
        # 期限不符：需於雙向靜止時設定，並於同一臨界區內寫入。
        with self._timeout_section(serial_obj, generation, write_timeout, deadline) as held:
            if not held:
                return None
            return self._write_locked(serial_obj, data)

    @contextmanager
    def _timeout_section(
        self, serial_obj: Any, generation: int, write_timeout: float, deadline: float
    ) -> Iterator[bool]:
        """雙向靜止時設定 pySerial 寫入期限，並保留寫入串行鎖至區塊結束。

        取得順序固定為寫入串行鎖 → 讀取串行鎖：設定期間兩把鎖皆持有，確保無任一方向 I/O 進行中；
        設定後釋放另一方向的鎖，只保留本方向的鎖執行 I/O，因此寫入阻塞時讀取端仍可前進。
        回傳 False 表示取得鎖後 handle 已換掉（斷線或重開），呼叫端不得 I/O。
        """
        if not self._acquire_until(self._write_io_lock, deadline):
            yield False
            return
        write_acquired = True
        read_acquired = False
        self._adjust_reconfig_waiters(+1)
        try:
            read_acquired = self._acquire_until(self._read_io_lock, deadline)
            held = read_acquired and self._handle_is_current(serial_obj, generation)
            if held:
                if serial_obj.write_timeout != write_timeout:
                    serial_obj.write_timeout = write_timeout
        except BaseException:
            if read_acquired:
                self._read_io_lock.release()
            self._write_io_lock.release()
            write_acquired = False
            raise
        finally:
            self._adjust_reconfig_waiters(-1)

        if not read_acquired or not held:
            if read_acquired:
                self._read_io_lock.release()
            if write_acquired:
                self._write_io_lock.release()
            yield False
            return
        # 設定完成後放開讀取方向，讓寫入 I/O 期間讀取仍可前進。
        self._read_io_lock.release()
        read_acquired = False
        try:
            yield True
        finally:
            if read_acquired:
                self._read_io_lock.release()
            if write_acquired:
                self._write_io_lock.release()

    @staticmethod
    def _acquire_until(lock: Any, deadline: float) -> bool:
        """依呼叫期限取得串行鎖；鎖等待不得超過該次 read/write 的 deadline。"""
        return lock.acquire(timeout=max(0.0, deadline - time.monotonic()))

    def _read_locked(self, serial_obj: Any, max_bytes: int) -> bytes:
        """於讀取串行鎖內執行一次 pySerial 讀取；呼叫端須已持有讀取串行鎖且 handle 已驗證。"""
        try:
            return bytes(serial_obj.read(max_bytes))
        except Exception as exc:
            raise EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: 讀取 {self.com} 失敗：{exc}") from exc

    def _write_locked(self, serial_obj: Any, data: bytes) -> int:
        """於寫入串行鎖內執行一次 pySerial 寫入；呼叫端須已持有寫入串行鎖且 handle 已驗證。"""
        try:
            count = int(serial_obj.write(data))
        except Exception as exc:
            is_timeout = self._serial_timeout_exception is not None and isinstance(
                exc, self._serial_timeout_exception
            )
            # 注入 factory 時也支援由替身提供的 pySerial 同名逾時例外。
            if is_timeout or exc.__class__.__name__ == "SerialTimeoutException":
                raise EndpointWriteUncertain(len(data), "write timeout") from exc
            raise EndpointError(f"HARNESS_ENDPOINT_IO_ERROR: 寫入 {self.com} 失敗：{exc}") from exc
        if not 0 <= count <= len(data):
            raise EndpointWriteUncertain(len(data), f"unexpected write count {count}")
        return count

    def _handle_is_current(self, serial_obj: Any, generation: int) -> bool:
        """取得串行鎖後重驗 handle：斷線或重開會換掉 `_serial_obj` 並遞增 generation。"""
        return self._serial_obj is serial_obj and self.generation == generation

    def _adjust_reconfig_waiters(self, delta: int) -> None:
        with self._reconfig_guard:
            self._reconfig_waiters += delta

    def _reconfig_requested(self) -> bool:
        with self._reconfig_guard:
            return self._reconfig_waiters > 0

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
        # 鎖序固定為 `_lock` → 寫入串行鎖 → 讀取串行鎖（與期限設定路徑一致）；
        # I/O 路徑不在持有串行鎖時取得 `_lock`，故不會死鎖。
        with self._write_io_lock, self._read_io_lock:
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
