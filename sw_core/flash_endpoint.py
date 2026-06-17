"""sw_core/flash_endpoint.py — MCU flash 端點（sync-probe 偵測器 + PTY 端點）。

提供 ``detect_mcu_line`` 偵測器與 ``FlashEndpoint`` 常駐 PTY 端點。
"""
from __future__ import annotations
import dataclasses
import os
import pty
import select
import threading
import time
from typing import Protocol

from .mcu_patterns import McuPatternRegistry


class ProbeTransport(Protocol):
    """抽象傳輸介面：對指定 by_id 送出 probe 並回傳是否收到期望 ACK。"""

    def probe(self, by_id: str, probe_bytes: bytes, expect: bytes, timeout_ms: int) -> bool:
        """送出非破壞性 probe 並等待 ACK。

        Args:
            by_id: 硬體裝置識別碼（USB by-id 路徑）。
            probe_bytes: 傳送至 MCU 的 sync 位元組。
            expect: 期望收到的 ACK 前綴位元組。
            timeout_ms: 等待逾時（毫秒）。

        Returns:
            True 表示收到符合 expect 前綴的回應；否則 False。
        """
        ...


@dataclasses.dataclass
class DetectResult:
    """sync-probe 偵測結果。

    Attributes:
        status: 偵測狀態，為以下三者之一：

            - ``"matched"``：唯一命中，``by_id`` 與 ``family`` 已填入。
            - ``"ambiguous"``：多個候選均命中，無法自動決定；``hits`` 列出全部。
            - ``"none"``：無任何候選命中。
        by_id: 命中的裝置 by-id（僅 status == "matched" 時有值）。
        family: 命中的 MCU 家族（僅 status == "matched" 時有值）。
        hits: 所有命中的 by_id 清單（status == "ambiguous" 時使用）。
    """

    status: str               # "matched" | "ambiguous" | "none"
    by_id: str | None = None
    family: str | None = None
    hits: list[str] = dataclasses.field(default_factory=list)


def detect_mcu_line(
    candidates: list[dict],
    registry: McuPatternRegistry,
    transport: ProbeTransport,
) -> DetectResult:
    """排除 command_capable console；逐候選逐 pattern 送非破壞 probe。

    演算法：
        1. 過濾掉 ``command_capable=True`` 的候選（這些是 console UART，不應被 probe）。
        2. 對每個剩餘候選，依序嘗試 registry 中所有 pattern；首個命中即 break（不重複計）。
        3. 命中 0 → ``none``；命中 1 → ``matched``；命中 >1 → ``ambiguous``（不自動挑選）。

    Args:
        candidates: COM port 候選清單，每筆含 ``by_id``、``command_capable`` 等欄位。
        registry: 已初始化的 :class:`McuPatternRegistry`。
        transport: 實作 :class:`ProbeTransport` 協定的傳輸物件。

    Returns:
        :class:`DetectResult` 描述偵測結果。
    """
    # 步驟 1：排除 command_capable console（例如已被 serialwrap broker 佔用的 terminal）
    eligible = [c for c in candidates if not c.get("command_capable")]

    hits: list[tuple[str, str]] = []  # (by_id, family)
    for c in eligible:
        by_id = c["by_id"]
        # 步驟 2：逐 pattern 嘗試，首個命中即停止（同一候選不重複計入）
        for p in registry.all():
            if transport.probe(by_id, p.probe, p.expect, p.timeout_ms):
                hits.append((by_id, p.family))
                break

    # 步驟 3：依命中數回傳結果
    if not hits:
        return DetectResult(status="none")
    if len(hits) > 1:
        return DetectResult(status="ambiguous", hits=[h[0] for h in hits])
    by_id, family = hits[0]
    return DetectResult(status="matched", by_id=by_id, family=family)


class FlashEndpoint:
    """常駐 PTY flash 端點：slave 以穩定 symlink 命名（如 /dev/ttyMCU），daemon 持 master。

    開啟分流：client 先寫 bytes（flasher 的 sync）→ master 變可讀 → 走 flash 路徑；
    只讀不寫（cat）→ daemon 定期把支援清單寫進 master 供其讀取（不依賴 EOF）。
    """

    def __init__(self, *, link_path, registry, list_candidates,
                 on_flash_open=None, grace=0.3, idle_list_interval=0.5):
        self._link_path = link_path
        self._registry = registry
        self._list_candidates = list_candidates       # () -> list[dict]
        self._on_flash_open = on_flash_open            # (master_fd, slave_fd, first_bytes) -> None
        self._grace = grace
        self._idle_list_interval = idle_list_interval
        self._master_fd = None
        self._slave_fd = None
        self._stop = threading.Event()
        self._thread = None
        self._flash_active = threading.Event()
        self._last_list = 0.0

    def start(self):
        """開啟 PTY、建立 symlink、啟動背景 loop 執行緒。"""
        self._master_fd, self._slave_fd = pty.openpty()
        os.set_blocking(self._master_fd, False)
        slave_name = os.ttyname(self._slave_fd)
        os.makedirs(os.path.dirname(self._link_path), exist_ok=True)
        try:
            os.remove(self._link_path)
        except FileNotFoundError:
            pass
        os.symlink(slave_name, self._link_path)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="serialwrap-ttyMCU")
        self._thread.start()

    def stop(self):
        """停止背景執行緒、關閉 fd、移除 symlink。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        for fd in (self._master_fd, self._slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master_fd = self._slave_fd = None
        try:
            os.remove(self._link_path)
        except OSError:
            pass

    def is_flashing(self) -> bool:
        """回傳目前是否有 flash 操作進行中。"""
        return self._flash_active.is_set()

    def _loop(self):
        """背景主迴圈：偵測 client 寫入（flasher）或定期發送支援清單（cat）。"""
        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select([self._master_fd], [], [], self._grace)
            except OSError:
                return
            if self._stop.is_set():
                return
            if rlist:
                # client 寫入 = flasher。讀出首段交給 flash 路徑（供其轉送）。
                try:
                    first = os.read(self._master_fd, 4096)
                except (OSError, BlockingIOError):
                    first = b""
                if not self._flash_active.is_set():
                    self._flash_active.set()
                    try:
                        if self._on_flash_open is not None:
                            self._on_flash_open(self._master_fd, self._slave_fd, first)
                    finally:
                        self._flash_active.clear()
                continue
            # idle：定期寫支援清單（供 cat 讀取）
            now = time.monotonic()
            if now - self._last_list >= self._idle_list_interval:
                self._last_list = now
                text = self._registry.render_support_list(
                    candidates=self._list_candidates())
                try:
                    os.write(self._master_fd, text.encode())
                except OSError:
                    pass
