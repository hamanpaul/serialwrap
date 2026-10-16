"""Stage 0 行為變更測試共用替身（P1 T01 建立；P1 T20 增補 submit_guard／block_in_guard／on_write）。

規則（00-master-plan Global Constraints）：本模組不得於頂層 import 本 Phase 才新增的名稱
（`TxResult`、`TxPermit`、`DrainReceipt` 等），一律於函式內延遲 import，使產品碼尚未實作時
本模組仍可被 collection。現有產品模組（UARTBridge、WalWriter、SessionManager）可頂層 import。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from typing import Any, Callable

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter


class ScriptedWrite:
    """取代 `os.write`：依序消費 actions。

    - int：回傳寫出的 bytes 數（部分寫入或全寫入）。
    - BaseException：raise（例如 `BlockingIOError(errno.EAGAIN, ...)`）。
    - callable(data) -> int：以 data 計算回傳值。

    每次呼叫都記入 `calls`；actions 耗盡後再被呼叫即 raise AssertionError（多出的 OS 寫入本身即失敗）。
    """

    def __init__(self, actions: list[Any]) -> None:
        self._actions = list(actions)
        self.calls: list[bytes] = []

    def __call__(self, fd: int, data: bytes | memoryview) -> int:
        raw = bytes(data)
        self.calls.append(raw)
        if not self._actions:
            raise AssertionError(f"ScriptedWrite 已耗盡：多出的 os.write({len(raw)} bytes)")
        action = self._actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return int(action(raw))
        return int(action)


class FakePort:
    """`SerialPort` 介面替身（fileno() 為 None，走 UARTBridge 的 port.write 路徑）。

    `write` 語意（T20 凍結）：
    - 無 `submit_guard`：先 `block.wait()`（若有 block），再記錄 `writes`，回傳 write_results 下一項或預設 completed。
    - 有 `submit_guard`：進入 guard（持 `_tx_fence_lock`）後，若 reason 非 None 回傳
      `TxResult("not_submitted", 0, n, reason)` 且不記錄；否則記錄 `writes`。
      `block_in_guard=True` 時在 guard 內 `block.wait()`（模擬 Windows pyserial 持鎖阻塞寫入）；
      否則於 guard 結束後才 `block.wait()`（模擬完成等待）。
    - write_results 項目為 BaseException 時 raise；其他值原樣回傳。
    - `on_write(data)`（測試專用）於寫入提交完成後呼叫，可用來模擬 target 回應（注入 RX）。
    - `cancel_pending_write()` 只計數 `cancel_write_calls`，不放行 block（模擬驅動不理會 CancelIoEx）。
    """

    def __init__(
        self,
        write_results: list[Any] | None = None,
        *,
        block: threading.Event | None = None,
        block_in_guard: bool = False,
        on_write: Callable[[bytes], None] | None = None,
    ) -> None:
        self._results: list[Any] = list(write_results or [])
        self._block = block
        self._block_in_guard = block_in_guard
        self._on_write = on_write
        self._mu = threading.Lock()
        self.writes: list[bytes] = []
        self.close_calls = 0
        self.cancel_write_calls = 0
        self.opened = False
        self.configured: Any = None
        self.baud: int | None = None

    # ---- SerialPort 介面 ----
    def open(self) -> None:
        self.opened = True

    def configure(self, profile: Any) -> None:
        self.configured = profile

    def read(self, max_bytes: int = 8192) -> bytes:
        return b""

    def is_alive(self) -> bool:
        return self.opened and self.close_calls == 0

    def fileno(self) -> int | None:
        return None

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def close(self) -> None:
        self.close_calls += 1

    def cancel_pending_write(self) -> None:
        self.cancel_write_calls += 1

    def write(
        self,
        payload: bytes,
        *,
        cancel: threading.Event | None = None,
        submit_guard: Callable[[], Any] | None = None,
    ) -> Any:
        from sw_core.serial_port import TxResult  # 延遲 import：TxResult 為 T01 新增

        data = bytes(payload)
        if submit_guard is None:
            if self._block is not None:
                self._block.wait()
            with self._mu:
                self.writes.append(data)
            result = self._next_result(data)
        else:
            with submit_guard() as reason:
                if reason is not None:
                    return TxResult("not_submitted", 0, len(data), reason)
                with self._mu:
                    self.writes.append(data)
                if self._block is not None and self._block_in_guard:
                    self._block.wait()
                result = self._next_result(data)
            if self._block is not None and not self._block_in_guard:
                self._block.wait()
        if self._on_write is not None:
            self._on_write(data)
        return result

    def _next_result(self, data: bytes) -> Any:
        from sw_core.serial_port import TxResult  # 延遲 import

        if not self._results:
            return TxResult("completed", len(data), len(data))
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_profile(
    name: str = "p",
    com: str = "COM0",
    by_id: str = "/dev/serial/by-id/X",
    *,
    platform: str = "prpl",
    **overrides: Any,
) -> SessionProfile:
    """建立測試用 SessionProfile（欄位同 tests/test_device_handoff.py::_Base._make_profile）。"""
    base = SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias="lab+1",
        device_by_id=by_id,
        platform=platform,
        uart=UartProfile(),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def make_bridge(
    tmp_path: Path,
    *,
    port: FakePort | None = None,
    com: str = "COM0",
    wal: WalWriter | None = None,
    fd: int | None = None,
    on_rx_data: Callable[[bytes], None] | None = None,
) -> tuple[UARTBridge, WalWriter]:
    """建立不呼叫 `start()` 的 UARTBridge。`port` 走 port.write 路徑；`fd` 走 `_write_all(fd, …)` 路徑。"""
    wal_obj = wal if wal is not None else WalWriter(wal_dir=str(tmp_path / "wal"))
    bridge = UARTBridge(
        com=com,
        device_path="fake",
        profile=UartProfile(),
        wal=wal_obj,
        on_rx_data=on_rx_data,
    )
    if port is not None:
        bridge._serial = port
        bridge._serial_fd = None
    if fd is not None:
        bridge._serial = None
        bridge._serial_fd = fd
    return bridge, wal_obj


def wal_records(wal: WalWriter) -> list[dict[str, Any]]:
    """讀回 raw.wal.ndjson 的全部記錄（檔案不存在時回空串列）。"""
    path = Path(wal.wal_path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def make_manager(
    tmp_path: Path,
    profiles: list[SessionProfile],
    *,
    wal: WalWriter | None = None,
) -> SessionManager:
    """建立 SessionManager；state.json 注入 tmp，不改模組層 STATE_PATH。"""
    wal_obj = wal if wal is not None else WalWriter(wal_dir=str(tmp_path / "wal"))
    return SessionManager(
        profiles,
        wal_obj,
        on_ready=lambda _s: None,
        on_detached=lambda _s: None,
        state_path=str(tmp_path / "state.json"),
    )
