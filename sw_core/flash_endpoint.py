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
import tty
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
                 on_flash_open=None, grace=0.3, idle_list_interval=0.5,
                 client_cooldown=3.0):
        self._link_path = link_path
        self._registry = registry
        self._list_candidates = list_candidates       # () -> list[dict]
        self._on_flash_open = on_flash_open            # (master_fd, slave_fd, first_bytes) -> None
        self._grace = grace
        self._idle_list_interval = idle_list_interval
        self._client_cooldown = client_cooldown        # flasher 寫入後抑制 idle 清單的秒數（I1）
        self._master_fd = None
        self._slave_fd = None
        self._stop = threading.Event()
        self._thread = None
        self._flash_active = threading.Event()
        self._last_list = 0.0
        self._last_client_write = 0.0                  # 最後一次 client（flasher）寫入的 monotonic

    def start(self):
        """開啟 PTY、建立 symlink、啟動背景 loop 執行緒。"""
        self._master_fd, self._slave_fd = pty.openpty()
        # 關鍵：把 slave 設為 raw，否則 PTY line discipline 會做 CR/LF 轉換與輸入處理，
        # 汙染 flasher（開 /dev/ttyMCU）的 SBL 二進位協定。byte-transparency 仰賴這一步。
        tty.setraw(self._slave_fd)
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
                # 記錄 flasher 活動時間：在 cool-down 內抑制 idle 清單寫入，避免在
                # flasher 的 sync/retry 視窗把支援清單當成回應 bytes 灌進去（I1）。
                self._last_client_write = time.monotonic()
                if not self._flash_active.is_set():
                    self._flash_active.set()
                    try:
                        if self._on_flash_open is not None:
                            self._on_flash_open(self._master_fd, self._slave_fd, first)
                    except Exception:
                        # on_flash_open（偵測 / pump）的任何例外都不得殺死端點執行緒；
                        # 收斂後繼續服務後續 open（C1 加固）。
                        pass
                    finally:
                        self._flash_active.clear()
                continue
            # idle：定期寫支援清單（供 cat 只讀查詢）。
            # 只有在「近期沒有任何 client 寫入」時才寫，確保 no-match / flasher 連線期間
            # 端點保持沉默（spec：no-match SHALL NOT 寫入端點）（I1）。
            now = time.monotonic()
            if now - self._last_client_write < self._client_cooldown:
                continue
            if now - self._last_list >= self._idle_list_interval:
                self._last_list = now
                text = self._registry.render_support_list(
                    candidates=self._list_candidates())
                try:
                    os.write(self._master_fd, text.encode())
                except OSError:
                    pass


class FlashSink(Protocol):
    """抽象 sink 介面：接收來自 flasher（透過 endpoint master）的位元組並轉送至裝置。"""

    def flash_tx(self, payload: bytes) -> None:
        """將 payload 原樣發送至底層裝置（binary-safe，不做任何轉義）。"""
        ...


def pump_endpoint_to_sink(
    master_fd: int,
    sink: "FlashSink",
    stop_event: threading.Event,
    first_bytes: bytes = b"",
    chunk: int = 4096,
) -> None:
    """讀 endpoint master → sink.flash_tx，原樣轉送（含先前已讀的 first_bytes）。

    阻塞直到 stop_event 設定或 flasher 關閉端點（EOF）。

    Args:
        master_fd: PTY master fd（應設為 non-blocking）。
        sink: 實作 :class:`FlashSink` 的物件（通常為 UARTBridge）。
        stop_event: 設定後函式應盡快結束（最多等一個 select timeout）。
        first_bytes: 已由呼叫者預讀的首段位元組，需先轉送給 sink。
        chunk: 每次 os.read 的最大讀取位元組數。
    """
    # sink.flash_tx 可能在 bridge 中途掉線時拋 RuntimeError("serial not ready")
    # 或 OSError；必須在 pump 內收斂為「乾淨結束」，否則例外會上拋並殺死端點執行緒，
    # 使 /dev/ttyMCU 永久失效直到 daemon 重啟（C1）。
    try:
        if first_bytes:
            sink.flash_tx(first_bytes)
        while not stop_event.is_set():
            try:
                rlist, _, _ = select.select([master_fd], [], [], 0.2)
            except OSError:
                return
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, chunk)
                except (OSError, BlockingIOError):
                    return
                if not data:
                    return  # EOF：flasher 已關閉端點
                sink.flash_tx(data)
    except (OSError, RuntimeError):
        return  # bridge 中途掉線 / 裝置不可用 → 結束 pump，讓上層 exit_flashing 收尾


def make_rx_to_endpoint_writer(master_fd: int):
    """回傳一個把 device RX 原樣寫進 endpoint master 的 callback（bytes -> None）。

    callback 設計為 binary-safe，OSError（如 fd 已關閉）會靜默吞掉。

    Args:
        master_fd: PTY master fd。

    Returns:
        ``(data: bytes) -> None`` callback，供 SessionManager.add_rx_observer 使用。
    """
    def _writer(data: bytes) -> None:
        try:
            os.write(master_fd, data)
        except OSError:
            pass
    return _writer


def resolve_flash_target(selector: str, sessions: list[dict], *, force: bool) -> dict:
    """解析顯式 flash 目標並防呆。

    .. note::
        **v1 保留、尚未接線**：目前 flash 走「開端點 → 自動 sync-probe 認線」路徑，
        偵測階段本就排除 `command_capable` console，無顯式 target/`--force` CLI。
        本函式為日後「顯式指定 flash 目標」路徑（`--selector/--by-id/--force`）預留的
        防呆建構塊，已測試但尚未由任何 runtime 路徑呼叫。

    若目標是 command_capable console（很可能是 DUT），預設擋下，需 force 才覆寫，
    避免把韌體燒進 console 線。

    Args:
        selector: 使用者指定的目標識別字（``com`` 或 ``by_id``）。
        sessions: 目前已知 session 清單，每筆含 ``com``、``by_id``、``command_capable`` 欄位。
        force: 若為 ``True``，即使目標是 command_capable console 仍允許通過。

    Returns:
        成功時回傳 ``{"ok": True, "by_id": ..., "com": ...}``；
        失敗時回傳 ``{"ok": False, "error_code": ..., "selector": ..., ...}``。
    """
    match = next((s for s in sessions
                  if selector in (s.get("com"), s.get("by_id"))), None)
    if match is None:
        return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
    if match.get("command_capable") and not force:
        return {"ok": False, "error_code": "FLASH_TARGET_IS_CONSOLE",
                "selector": selector,
                "hint": "目標是 command_capable console（可能是 DUT）；確認無誤再加 --force"}
    return {"ok": True, "by_id": match["by_id"], "com": match.get("com")}
