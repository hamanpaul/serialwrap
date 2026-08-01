from __future__ import annotations

import collections
import dataclasses
import errno
import os
import re
import select
import socket
import threading
import time
import uuid
from typing import Any, Callable

from .config import UartProfile
from .constants import DEFERRED_INPUT_MAX_BYTES, RX_RATE_WINDOW_S
from .serial_port import SerialPort, open_serial_port
from .telnet_console import TELNET_GREETING, TelnetFilter, escape_iac
from .util import strip_ansi
from .wal import WalWriter

# 序列埠的 termios/fcntl 設定與 _BAUD_MAP 已收斂進 sw_core/serial_port.py 的 SerialPort
# 後端（#84 PORT-1）。本模組僅保留 human console PTY 相關的 POSIX 呼叫（lazy import），
# 使 `import sw_core.uart_io` 在 Windows 不再因 top-level import termios/fcntl 而失敗。

_STALE_CONSOLE_GRACE_S = 2.0

# RX 速率視窗筆數硬上限（#153 防呆）：異常高頻小 chunk 下 deque 也不無界成長；
# 時間剪枝為主，此上限僅兜底（deque maxlen 自動丟最舊）。
_RX_WINDOW_MAX_ENTRIES = 4096


def _pty_available() -> bool:
    """此平台是否支援 PTY（human console / flash endpoint 的基礎，POSIX-only）。

    抽成函式便於測試以 monkeypatch 模擬 Windows（無 os.openpty）路徑（#84）。
    """
    return hasattr(os, "openpty")


@dataclasses.dataclass
class ConsoleClient:
    client_id: str
    label: str
    master_fd: int
    slave_fd: int
    slave_path: str
    attached_at: float
    tx_buffer: bytearray = dataclasses.field(default_factory=bytearray)
    # Windows human console（#84 PORT-2）：以 TCP socket 取代 PTY；PTY 路徑此欄為 None、
    # master_fd/slave_fd 為真實 fd；socket 路徑 sock 為連線 socket、master_fd/slave_fd=-1、
    # slave_path 為 "host:port" 端點字串。console 的狀態機（line buffer / raw / suspend-resume /
    # fan-out）對兩者共用，I/O 原語由 _console_send 與 console loop 依 sock 分派。
    sock: Any = None
    # broker 內部哨兵 primary（start() 建、無外部 reader、作 snapshot.vtty 錨點）為 True，永不被 reaper 回收；
    # 經 attach_console / TCP accept 建立的真實 console 為 False。
    internal: bool = False
    # Telnet 相容層（#131）：TCP console client 於 accept 時掛 TelnetFilter（server 主動
    # 協商 + 入向 IAC 過濾 + 出向 IAC 逸出）；PTY（POSIX）路徑恆為 None、行為不變。
    telnet: TelnetFilter | None = None
    # socket 送出序列化（#131 review）：RX fan-out（reader thread）與協商回覆/echo
    # （console thread）對同一 socket 併發 send 會撕裂 IAC 逸出/協商序列；PTY 路徑不用。
    sock_send_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


@dataclasses.dataclass
class PreservedConsoles:
    clients: dict[str, ConsoleClient]
    primary_client_id: str | None

    def primary_vtty(self) -> str | None:
        if self.primary_client_id is None:
            return None
        client = self.clients.get(self.primary_client_id)
        return client.slave_path if client is not None else None

    def has_client(self, client_id: str) -> bool:
        return client_id in self.clients


def _close_console_client_fds(client: ConsoleClient) -> None:
    if client.sock is not None:
        # Windows TCP console（#84 PORT-2）。
        try:
            client.sock.close()
        except OSError:
            pass
        return
    for fd in (client.master_fd, client.slave_fd):
        try:
            os.close(fd)
        except OSError:
            pass


class UARTBridge:
    """單一 UART 的 broker bridge：序列埠 RX/TX、human console 多工、命令寫入仲裁、WAL。

    並發/鎖序不變式（修改 console 或寫入路徑前必讀）：
    - 鎖序固定 `_write_lock ⊃ _state_lock`：**永遠先取 _write_lock 再取 _state_lock，
      絕不反向**（全檔僅 send_bytes / set_flash_mode 兩處巢狀，皆 write 在外）。任何「持
      _state_lock 後再取 _write_lock」都會與之形成 AB-BA 死鎖——維持單向是 #69 gate 原子性
      與雙執行緒安全的前提。需要在持鎖後送資料者（如 resume_interactive flush）一律先**釋放**
      _state_lock 再呼 send_bytes。
    - reader 執行緒模型：POSIX 為**單一** `_loop`，以 select() 同時多工序列埠 fd 與 human
      console PTY master fd。Windows（`_pty_available()` 為 False）為**兩條**執行緒——`_loop`
      做序列埠阻塞讀、`_console_loop` 以 socket select() 收 TCP console；兩者並發存取 `_clients`
      與 bridge 狀態，全靠上述單向鎖序避免死鎖（serial reader 只取 _state_lock 做 fan-out；
      console thread 經 send_bytes 取 _write_lock→_state_lock）。
    - `_serial`(SerialPort) 與 `_serial_fd`(POSIX 整數別名) 必須**在同一個 _state_lock 內成對
      設定/清除**：start() 設 `_serial=port; _serial_fd=port.fileno()`，stop() 同時清為 None。
      `_loop` 以 `_serial_fd is None` 區分 POSIX(select)／Windows(阻塞讀) 分支，故兩者若不同步
      會讓 POSIX bridge 誤入 Windows 路徑（見 tests/test_serial_port.py 的配對不變式回歸）。
    """

    def __init__(
        self,
        com: str,
        device_path: str,
        profile: UartProfile,
        wal: WalWriter,
        *,
        on_console_line: Callable[[str, str], None] | None = None,
        on_rx_data: Callable[[bytes], None] | None = None,
        on_bridge_down: Callable[[str], None] | None = None,
        preserved_consoles: PreservedConsoles | None = None,
    ) -> None:
        self.com = com
        self.device_path = device_path
        self.profile = profile
        self.wal = wal
        self._on_console_line = on_console_line
        self._on_rx_data = on_rx_data
        self._on_bridge_down = on_bridge_down

        self._serial: SerialPort | None = None
        # POSIX 上 _serial_fd 是 _serial.fileno() 的整數別名，讓 select/os.read/os.write 熱路徑與
        # 既有測試（直接戳 _serial_fd / monkeypatch _write_all）逐字不變；Windows 上恆為 None，
        # 由 _serial(port) 的 read/write 承擔 I/O（#84 PORT-1）。
        self._serial_fd: int | None = None
        self._primary_client_id: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._rx_lock = threading.Lock()
        self._rx_text = ""
        self._rx_max_chars = 131072
        # 絕對串流偏移記帳（#158）：_rx_trimmed 為 _rx_text[0] 對應的絕對偏移（已被視窗
        # 修剪或 clear 丟棄的累計字元數），單調不減。rx_snapshot_len() 回傳
        # _rx_trimmed + len(_rx_text)（絕對偏移），使緩衝飽和修剪後既有 offset 仍可
        # 正確切片，prompt 匹配不再因「長度恆等於視窗上限」而永遠落空。
        self._rx_trimmed = 0
        # RX 速率統計視窗（#153）：(mono_ts, raw_byte_len) 佇列，_rx_lock 保護。計的是
        # raw serial bytes（clean_text 之前，ANSI/雜訊照算——對洪水最誠實），供 rx_stats()
        # 回報最近 RX_RATE_WINDOW_S 秒的量；maxlen 為筆數防呆上限。
        self._rx_window: collections.deque[tuple[float, int]] = collections.deque(
            maxlen=_RX_WINDOW_MAX_ENTRIES
        )
        # raw RX 累計計數器（#150）：單調遞增、clear_rx_buffer() 不歸零；供 probe 前後
        # 取差判定「probe 全程零 RX（連 echo 都無）」的 transport stall 特徵。
        self._rx_total_bytes = 0
        self._preserved_consoles = preserved_consoles
        self._clients: dict[str, ConsoleClient] = {}
        self._interactive_owner: str | None = None
        self._agent_active: bool = False
        self._suspended_owner: str | None = None
        self._suspend_depth: int = 0  # suspend/resume 巢狀深度（#78 可重入）
        self._deferred_buffers: dict[str, bytearray] = {}
        # 最後一次「真實 human owner 鍵入」的 monotonic 時間（#53）。僅 human-OWNER
        # 直接 raw 送出分支會更新；deferred buffer、serial RX loop、agent 注入都不更新。
        self._last_human_input_at: float | None = None
        # flash 模式（#55）：FLASHING 期間封鎖所有 console→device 注入，避免汙染 SBL binary；
        # RX→console 仍照常（唯讀快照）。flash bridge 走 flash_tx，不受此旗標影響。
        self._flash_mode: bool = False
        # Windows human console（#84 PORT-2）：無 PTY 平台改以 127.0.0.1 TCP listener 讓
        # TeraTerm/PuTTY 連入；每條連線即一個 socket-backed ConsoleClient。Linux 上這三者恆為 None。
        self._console_listener: socket.socket | None = None
        self._console_thread: threading.Thread | None = None
        self._console_endpoint: str | None = None

    def set_flash_mode(self, enabled: bool) -> None:
        """進出 flash 模式；FLASHING 期間擋下 console 注入（C2）。

        取 `_write_lock` 再改旗標，與 `send_bytes` 的 flash gate 共用同一鎖序
        （_write_lock ⊃ _state_lock），確保「檢查 flash_mode → 寫入」與「切換 flash_mode」
        互斥：flash 開啟一旦贏得鎖，任何進行中的非 flash 寫入已完成、後續非 flash 寫入必被擋下
        （#69 Finding 2 round2）。
        """
        with self._write_lock:
            with self._state_lock:
                self._flash_mode = enabled

    @property
    def vtty_path(self) -> str | None:
        with self._state_lock:
            if self._primary_client_id is None:
                return None
            client = self._clients.get(self._primary_client_id)
            return client.slave_path if client is not None else None

    def _set_nonblock(self, fd: int) -> None:
        # 僅 human console PTY master fd 走此路徑（POSIX-only）；序列埠 nonblock 已移至
        # SerialPort POSIX 後端。lazy import 使本模組在 Windows 仍可載入（#84 PORT-1）。
        import fcntl

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _configure_pty_slave(self, fd: int) -> None:
        import termios  # PTY slave 設定為 POSIX-only（#84 PORT-1，lazy import）

        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def _create_console_client(self, label: str | None = None) -> ConsoleClient:
        if not _pty_available():
            # human console 走 PTY，屬 POSIX-only（#84 PORT-2 為後續工作）；Windows 上序列埠
            # RX/TX 仍可運作，只是不支援 human console attach。
            raise RuntimeError("human console（PTY）在此平台不支援；序列埠 RX/TX 仍可用")
        master_fd, slave_fd = os.openpty()
        self._set_nonblock(master_fd)
        self._configure_pty_slave(slave_fd)
        client_id = uuid.uuid4().hex[:12]
        return ConsoleClient(
            client_id=client_id,
            label=(label or client_id).strip() or client_id,
            master_fd=master_fd,
            slave_fd=slave_fd,
            slave_path=os.ttyname(slave_fd),
            attached_at=time.time(),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        port = open_serial_port(self.device_path, self.profile)
        port.open()
        port.configure(self.profile)

        with self._state_lock:
            preserved = self._preserved_consoles
            self._preserved_consoles = None
            if preserved is not None and preserved.clients:
                clients = dict(preserved.clients)
                primary_client_id = preserved.primary_client_id if preserved.primary_client_id in clients else None
                if primary_client_id is None:
                    next_client = next(iter(clients.values()), None)
                    primary_client_id = next_client.client_id if next_client is not None else None
            elif _pty_available():
                primary = self._create_console_client("primary")
                primary.internal = True  # 哨兵 primary：永不被 reaper 回收
                clients = {primary.client_id: primary}
                primary_client_id = primary.client_id
            else:
                # Windows 等無 PTY 平台：序列埠 RX/TX 仍運作，只是不建立 human console（#84）。
                clients = {}
                primary_client_id = None
            self._serial = port
            self._serial_fd = port.fileno()  # POSIX：真實整數 fd；Windows：None
            self._clients = clients
            self._primary_client_id = primary_client_id

        self._stop_event.clear()
        if not _pty_available():
            # Windows：無 PTY → 開 127.0.0.1 TCP listener 供 TeraTerm/PuTTY 連入做 human console（#84 PORT-2）。
            self._start_console_listener()
        self._thread = threading.Thread(target=self._loop, name=f"serialwrap-uart-{self.com}", daemon=True)
        self._thread.start()

    def stop(self, *, preserve_consoles: bool = False) -> PreservedConsoles | None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

        console_thread = self._console_thread
        if console_thread and console_thread.is_alive() and threading.current_thread() is not console_thread:
            console_thread.join(timeout=2.0)

        preserved: PreservedConsoles | None = None
        with self._state_lock:
            port = self._serial
            console_listener = self._console_listener
            if preserve_consoles and self._clients:
                preserved = PreservedConsoles(clients=dict(self._clients), primary_client_id=self._primary_client_id)
                clients_to_close: list[ConsoleClient] = []
            else:
                clients_to_close = list(self._clients.values())
            self._serial = None
            self._serial_fd = None
            self._clients = {}
            self._primary_client_id = None
            self._interactive_owner = None
            self._suspended_owner = None
            self._agent_active = False
            self._suspend_depth = 0
            self._deferred_buffers.clear()
            self._console_listener = None
            self._console_thread = None
            self._console_endpoint = None

        if port is not None:
            port.close()
        if console_listener is not None:
            # listener 關閉後 port 釋放；保留的 socket console（preserve_consoles）連線本身不關，交接新 bridge。
            try:
                console_listener.close()
            except OSError:
                pass

        for client in clients_to_close:
            self._close_console_client(client)
        return preserved

    def _close_console_client(self, client: ConsoleClient) -> None:
        _close_console_client_fds(client)

    # ───────── Windows human console（TCP 端點，#84 PORT-2）─────────
    def _start_console_listener(self) -> None:
        """建立 127.0.0.1 TCP listener 並啟動 console accept/pump thread。

        每條 TeraTerm/PuTTY 連線即一個 socket-backed ConsoleClient；首個連線自動取得 raw
        interactive ownership，agent 命令期間以既有 suspend/resume 簿記保持連線不中斷。
        """
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lst.bind(("127.0.0.1", 0))
        lst.listen(8)
        lst.setblocking(False)
        host, port = lst.getsockname()[:2]
        self._console_listener = lst
        self._console_endpoint = f"{host}:{port}"
        self._console_thread = threading.Thread(
            target=self._console_loop, name=f"serialwrap-console-{self.com}", daemon=True
        )
        self._console_thread.start()

    def _client_for_sock_locked(self, sock_obj: Any) -> ConsoleClient | None:
        for client in self._clients.values():
            if client.sock is sock_obj:
                return client
        return None

    def _accept_console_conn(self, conn: socket.socket, addr: Any) -> None:
        conn.setblocking(False)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client_id = uuid.uuid4().hex[:12]
        peer = f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) and len(addr) >= 2 else str(addr)
        client = ConsoleClient(
            client_id=client_id,
            label=peer,
            master_fd=-1,
            slave_fd=-1,
            slave_path=self._console_endpoint or peer,
            attached_at=time.time(),
            sock=conn,
            telnet=TelnetFilter(),
        )
        # Server 主動 telnet 協商（#131）：accept 即送 greeting（含合法 IAC，走不逸出的
        # _console_sock_send）。**先於註冊進 _clients**——註冊後 RX fan-out 即可能對此
        # socket 送資料，greeting 若晚送會與 RX bytes 交錯、撕裂協商序列。
        try:
            self._console_sock_send(client, TELNET_GREETING)
        except OSError:
            pass  # 對端秒斷線；由 console loop 的 recv b"" 收屍
        with self._state_lock:
            self._clients[client_id] = client
            if self._primary_client_id is None:
                self._primary_client_id = client_id
            # 首個 human console 自動取得 raw interactive ownership（對齊 Linux console-attach；
            # Windows 無 session_manager 代為授予，故於 bridge 層授予，使方向鍵/Tab 即時透傳，
            # 且 agent 命令時走 suspend/resume coexistence、連線不中斷）。
            # #134：suspend 期間（agent 命令執行中）_interactive_owner 被暫存為 None，
            # 不得誤判為「首個 console」即時授予——owner 路徑會繞過 deferred buffer
            # 直寫 UART、汙染 agent 命令輸出。改記到 _suspended_owner（原本為 None
            # 時），期間輸入走既有 deferred 分支累積，resume 時無縫接手 raw 並 flush；
            # suspend 前已有 owner 者維持第二 console 的 line-buffer 行為。
            if self._interactive_owner is None and self._suspend_depth == 0:
                self._interactive_owner = f"human:{client_id}"
            elif self._suspend_depth > 0 and self._suspended_owner is None:
                self._suspended_owner = f"human:{client_id}"

    def _console_loop(self) -> None:
        listener = self._console_listener
        if listener is None:
            return
        while not self._stop_event.is_set():
            with self._state_lock:
                socks = [c.sock for c in self._clients.values() if c.sock is not None]
            try:
                rlist, _, _ = select.select([listener, *socks], [], [], 0.2)
            except OSError:
                if self._stop_event.is_set():
                    break
                time.sleep(0.02)  # listener/socket 失效；短暫退避後重整 fd 集合續行
                continue
            for s in rlist:
                if s is listener:
                    try:
                        conn, addr = listener.accept()
                    except OSError:
                        continue
                    self._accept_console_conn(conn, addr)
                    continue
                with self._state_lock:
                    client = self._client_for_sock_locked(s)
                if client is None:
                    continue
                try:
                    data = s.recv(8192)
                except BlockingIOError:
                    continue
                except OSError:
                    self._drop_console_client(client.client_id)
                    continue
                if not data:  # 對端關閉（TeraTerm/PuTTY 斷線）
                    self._drop_console_client(client.client_id)
                    continue
                try:
                    self._handle_console_rx(client, data)
                except Exception:  # noqa: BLE001 — 單一 console handler 例外不得殺死 console thread（比照 #79）
                    import logging

                    logging.getLogger("serialwrap").warning(
                        "console RX handler 例外（com=%s），略過此塊、console 續行", self.com, exc_info=True
                    )
                    continue

    def console_endpoint(self) -> str | None:
        """Windows TCP console 的連線端點（"host:port"）；POSIX 為 None（#84 PORT-2）。"""
        with self._state_lock:
            return self._console_endpoint

    def _enumerate_all_held_paths(self) -> set[str] | None:
        """掃描 /proc，回傳所有外部 process 持有的 fd readlink 目標集合（排除自身 pid）。

        procfs 不可用時回傳 None（呼叫端據此採保守策略）。本函式不取任何鎖，純讀 /proc；
        呼叫端自行決定是否在 _state_lock 內外呼叫（reaper 走鎖外、_client_has_external_peer_locked
        既有行為走鎖內，均不變）。
        """
        held: set[str] = set()
        self_pid = os.getpid()
        try:
            pids = os.listdir("/proc")
        except OSError:
            return None
        for pid_text in pids:
            if not pid_text.isdigit() or int(pid_text) == self_pid:
                continue
            fd_dir = os.path.join("/proc", pid_text, "fd")
            try:
                fd_names = os.listdir(fd_dir)
            except OSError:
                continue
            for fd_name in fd_names:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                held.add(target)
        return held

    def _client_has_external_peer_locked(self, client: ConsoleClient) -> bool:
        if client.sock is not None:
            # Windows TCP console（#84 PORT-2）：連線斷開由 console loop 的 recv b"" 直接偵測並
            # drop，不靠 /proc 掃描；故視為「仍有 peer」直到被 drop（避免 stale-pruning 誤剪）。
            return True
        held = self._enumerate_all_held_paths()
        if held is None:
            # procfs 不可用：保守起見視為仍有 peer（避免誤剪）。
            return True
        return client.slave_path in held

    def _drop_console_client(self, client_id: str) -> None:
        with self._state_lock:
            client = self._clients.pop(client_id, None)
            if client is None:
                return
            if self._primary_client_id == client_id:
                next_client = next(iter(self._clients.values()), None)
                self._primary_client_id = next_client.client_id if next_client is not None else None
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
            if self._suspended_owner == f"human:{client_id}":
                # suspend 期間斷線的（未來）owner（#136 review）：讓位 _suspended_owner，
                # 否則 resume 會把 ownership 還原成已不存在的 client，之後任何新連線都
                # 拿不到 raw ownership。刻意**不**比照 detach_console 歸零 _agent_active/
                # _suspend_depth——suspend 簿記由 agent 命令路徑持有，命令仍在跑，歸零會
                # 讓後續新連線立即取得 raw、重演 #134 的直寫汙染；讓位後新連線走 #134
                # 的 elif 接手 _suspended_owner，於 resume 時取得 ownership。
                self._suspended_owner = None
            self._deferred_buffers.pop(client_id, None)
        self._close_console_client(client)

    def reap_stale_consoles(self, *, held_slave_paths: set[str] | None = None) -> list[ConsoleClient]:
        """週期主動回收無外部 reader 的孤兒 console（含死掉的非-internal primary）。

        lock-split：鎖內快照候選 → 鎖外掃 /proc（若 held_slave_paths 未給）→ 回鎖 pop → 鎖外 close fd。
        硬性跳過當前 _interactive_owner / _suspended_owner / internal 哨兵，避免破壞 #78 suspend 簿記
        與 Fix3 的 peer-loss grace（owner 拆除全權交 session-layer）。
        """
        now = time.time()
        with self._state_lock:
            owner_cid = self._interactive_owner.split(":", 1)[1] if (self._interactive_owner or "").startswith("human:") else None
            susp_cid = self._suspended_owner.split(":", 1)[1] if (self._suspended_owner or "").startswith("human:") else None
            protected = {cid for cid in (owner_cid, susp_cid) if cid}
            candidates = [
                (c.client_id, c.slave_path)
                for c in self._clients.values()
                if not c.internal
                and c.sock is None  # POSIX PTY only（TCP client 的斷線由 console loop recv b"" 偵測）
                and c.client_id not in protected
                and (now - c.attached_at) >= _STALE_CONSOLE_GRACE_S
            ]
        if not candidates:
            return []
        if held_slave_paths is None:
            held_slave_paths = self._scan_held_slave_paths()  # 鎖外 /proc 掃描
        reaped: list[ConsoleClient] = []
        with self._state_lock:
            # 回鎖後重查保護集合（鎖外掃描期間可能變成 owner/suspended）；持鎖期間不會再變，
            # 故只計算一次、避免每個候選重算。
            cur_owner = self._interactive_owner.split(":", 1)[1] if (self._interactive_owner or "").startswith("human:") else None
            cur_susp = self._suspended_owner.split(":", 1)[1] if (self._suspended_owner or "").startswith("human:") else None
            cur_protected = {x for x in (cur_owner, cur_susp) if x}
            for cid, slave_path in candidates:
                client = self._clients.get(cid)
                if client is None or client.internal:
                    continue
                if cid in cur_protected:
                    continue
                if slave_path in held_slave_paths:
                    continue
                removed = self._clients.pop(cid, None)
                if removed is None:
                    continue
                self._deferred_buffers.pop(cid, None)
                if self._primary_client_id == cid:
                    nxt = next(iter(self._clients.values()), None)
                    self._primary_client_id = nxt.client_id if nxt is not None else None
                reaped.append(removed)
        for client in reaped:
            self._close_console_client(client)  # 鎖外關 fd
        return reaped

    def _scan_held_slave_paths(self) -> set[str]:
        """鎖外掃描 /proc，回傳「被外部 process 持有的 slave_path 集合」（排除自身 pid）。

        procfs 不可用（`_enumerate_all_held_paths` 回 None）時，保守起見回傳所有現存 client 的
        slave_path（視為仍被持有、不誤剪）——此分支會**內部取得 `_state_lock`** 做快照。
        """
        held = self._enumerate_all_held_paths()
        if held is None:
            with self._state_lock:
                return {c.slave_path for c in self._clients.values()}
        return held

    def _prune_stale_consoles_locked(self, *, now: float | None = None) -> list[ConsoleClient]:
        cutoff = time.time() if now is None else now
        stale: list[ConsoleClient] = []
        for client_id, client in list(self._clients.items()):
            if client_id == self._primary_client_id:
                continue
            if cutoff - client.attached_at < _STALE_CONSOLE_GRACE_S:
                continue
            if self._client_has_external_peer_locked(client):
                continue
            removed = self._clients.pop(client_id, None)
            if removed is None:
                continue
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
            stale.append(removed)
        return stale

    def _write_console_best_effort(self, fd: int, payload: bytes) -> None:
        try:
            os.write(fd, payload)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise

    def _console_send(self, client: ConsoleClient, payload: bytes) -> None:
        """把 bytes 送到一個 console client（RX fan-out 或本地回顯），best-effort 非阻塞。

        PTY（POSIX）走 os.write（行為與原版逐字相同）；TCP socket（Windows #84 PORT-2）走
        非阻塞 send。例外（含 socket EWOULDBLOCK / 斷線）一律由呼叫端的 `except OSError` 吸收
        （socket 斷線另由 console loop 的 recv b"" 偵測並 drop）。
        """
        if client.sock is not None:
            # telnet client（#131）：先逸出 0xFF → IAC IAC 再送（協商 bytes 由
            # _console_sock_send 直送、不經此處，不會被二次逸出）。
            if client.telnet is not None:
                payload = escape_iac(payload)
            self._console_sock_send(client, payload)
        else:
            self._write_console_best_effort(client.master_fd, payload)

    def _console_sock_send(self, client: ConsoleClient, payload: bytes) -> None:
        """socket console 的原始送出原語（無 telnet 逸出）。

        non-blocking socket 的 send() 可能只送出部分 bytes：必須迴圈推進 offset，
        否則 RX fan-out / 本地回顯會在 partial send 時靜默截斷遺失尾端（#84 review）。
        緩衝滿（BlockingIOError）時停止、丟棄剩餘（best-effort，與 PTY 路徑語意一致；
        極端情況下可能切斷 IAC IAC 對，屬 best-effort 已知極限）；
        其他 OSError（斷線）上拋由呼叫端 except OSError 吸收（斷線另由 recv b"" drop）。

        以 per-client ``sock_send_lock`` 序列化（#131 review）：RX fan-out（reader
        thread，持 _state_lock）與協商回覆/greeting/echo（console thread，無
        _state_lock）對同一 socket 併發 send 會把 IAC 序列插進逸出對中間。鎖序為
        _state_lock → sock_send_lock 單向（持本鎖期間不再取其他鎖），無死鎖風險。
        """
        with client.sock_send_lock:
            view = memoryview(payload)
            sent = 0
            while sent < len(payload):
                try:
                    n = client.sock.send(view[sent:])
                except BlockingIOError:
                    break
                if n <= 0:
                    break
                sent += n

    def _append_rx_text(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        now = time.monotonic()
        with self._rx_lock:
            # raw bytes 記帳（#153 速率視窗＋#150 累計計數）：同一鎖內單點維護。
            self._rx_total_bytes += len(payload)
            self._rx_window.append((now, len(payload)))
            self._prune_rx_window_locked(now)
            self._rx_text += text
            overflow = len(self._rx_text) - self._rx_max_chars
            if overflow > 0:
                # 視窗修剪必須記帳（#158）：丟棄的字元數推進 _rx_trimmed，維持絕對偏移語意。
                self._rx_text = self._rx_text[overflow:]
                self._rx_trimmed += overflow

    def _prune_rx_window_locked(self, now: float) -> None:
        """剪除速率視窗中超過 RX_RATE_WINDOW_S 的舊項（#153）；須持 _rx_lock。"""
        window = self._rx_window
        while window and now - window[0][0] > RX_RATE_WINDOW_S:
            window.popleft()

    def _handle_serial_rx(self, data: bytes) -> None:
        self.wal.append(com=self.com, direction="RX", source="device", payload=data)
        self._append_rx_text(data)
        if self._on_rx_data is not None:
            self._on_rx_data(data)
        # 在持 _state_lock 下逐一寫入（#83 RACE-1）：消除「鎖外快照 fd → 釋鎖 → 另一執行緒
        # 在 _clients pop 後 os.close(master_fd) → 此處 os.write 寫到已關閉/被重用的 fd」之
        # use-after-close（資料誤送）窗口。所有關閉路徑都「先在鎖內把 client 移出 _clients、
        # 再於鎖外 os.close」，故持鎖迭代 _clients 永不含已被移除者。best-effort 非阻塞 write
        # 很快，持鎖風險低（human console 仍是 best-effort 視圖，不阻塞 RX loop）。
        with self._state_lock:
            for client in list(self._clients.values()):
                try:
                    self._console_send(client, data)
                except OSError:
                    continue

    def _drain_line_buffer(self, client: ConsoleClient) -> list[str]:
        lines: list[str] = []
        while True:
            nl_positions = [pos for pos in (client.tx_buffer.find(b"\n"), client.tx_buffer.find(b"\r")) if pos >= 0]
            if not nl_positions:
                break
            pos = min(nl_positions)
            raw = bytes(client.tx_buffer[:pos])
            del client.tx_buffer[: pos + 1]
            while bytes(client.tx_buffer[:1]) in {b"\n", b"\r"}:
                del client.tx_buffer[:1]
            lines.append(raw.decode("utf-8", errors="replace"))
        return lines

    def _pop_last_console_char(self, buf: bytearray) -> bool:
        if not buf:
            return False
        idx = len(buf) - 1
        while idx > 0 and (buf[idx] & 0xC0) == 0x80:
            idx -= 1
        del buf[idx:]
        return True

    def _consume_console_input(self, client: ConsoleClient, data: bytes) -> tuple[list[str], bytes]:
        lines: list[str] = []
        echo = bytearray()
        last_terminator: int | None = None

        for b in data:
            if b in (0x08, 0x7F):
                last_terminator = None
                if self._pop_last_console_char(client.tx_buffer):
                    echo.extend(b"\b \b")
                continue

            if b in (0x0A, 0x0D):
                if last_terminator is not None and last_terminator != b and not client.tx_buffer:
                    last_terminator = None
                    continue
                lines.append(client.tx_buffer.decode("utf-8", errors="replace"))
                client.tx_buffer.clear()
                # Commit the local line visually without adding an extra blank
                # line before the target shell echoes the submitted command.
                echo.extend(b"\r")
                last_terminator = b
                continue

            last_terminator = None
            client.tx_buffer.append(b)
            if b == 0x09 or 0x20 <= b <= 0x7E or b >= 0x80:
                echo.append(b)

        return lines, bytes(echo)

    def _handle_console_rx(self, client: ConsoleClient, data: bytes) -> None:
        if client.telnet is not None:
            # telnet 入向過濾（#131）：先於 flash gate 推進 parser 狀態——協商回覆屬
            # client 向、不經 UART，flash 期間照送；資料位元組仍受下方 gate／owner／
            # deferred 管制（deferred buffer 因此存的是已過濾 bytes）。
            data, reply = client.telnet.feed(data)
            if reply:
                try:
                    self._console_sock_send(client, reply)
                except OSError:
                    pass
            if not data:
                return
        with self._state_lock:
            if self._flash_mode:
                # FLASHING 期間：console 為唯讀快照，丟棄所有 console→device 輸入，
                # 避免人類鍵入 / agent 注入汙染 flasher 的 SBL binary 串流（C2）。
                return
            owner = self._interactive_owner
            agent_active = self._agent_active
            suspended = self._suspended_owner

        if owner == f"human:{client.client_id}":
            # 僅在真實 human owner 鍵入時記錄時間（#53），供 human_active 時間窗判定。
            with self._state_lock:
                self._last_human_input_at = time.monotonic()
            self.send_bytes(data, source=f"human:{client.client_id}", cmd_id=None)
            return

        if agent_active and suspended == f"human:{client.client_id}":
            with self._state_lock:
                buf = self._deferred_buffers.get(client.client_id)
                if buf is None:
                    buf = bytearray()
                    self._deferred_buffers[client.client_id] = buf
                buf.extend(data)
                # 上限保護（#81）：agent 命令期間 human 持續輸入不致無界成長 → OOM；
                # 超過上限丟最舊位元組（保留最近輸入），resume 時 flush 較新的內容。
                if len(buf) > DEFERRED_INPUT_MAX_BYTES:
                    del buf[: len(buf) - DEFERRED_INPUT_MAX_BYTES]
            return

        lines, echo = self._consume_console_input(client, data)
        if echo:
            try:
                self._console_send(client, echo)
            except OSError:
                pass
        if self._on_console_line is None:
            return
        for line in lines:
            self._on_console_line(client.client_id, line)

    def _loop(self) -> None:
        failure_reason: str | None = None
        while not self._stop_event.is_set():
            with self._state_lock:
                port = self._serial
                serial_fd = self._serial_fd
                clients_by_fd = {client.master_fd: client for client in self._clients.values()}

            if port is None:
                break

            if serial_fd is None:
                # ── Windows/pyserial：序列埠 handle 無法被 select() 多工，且該平台無 PTY console。
                #    改以阻塞讀取（read timeout 控制輪詢節奏與 stop 反應延遲 ≤timeout）（#84 PORT-1）。
                try:
                    data = port.read(8192)
                except Exception as exc:  # noqa: BLE001 — pyserial SerialException / OSError 皆視為斷線
                    failure_reason = f"SERIAL_READ:{type(exc).__name__}"
                    self._stop_event.set()
                    break
                if not data:
                    continue
                try:
                    self._handle_serial_rx(data)
                except Exception:  # noqa: BLE001 — 單一 RX handler 例外不得殺死 reader thread（#79 STA-1）
                    import logging
                    logging.getLogger("serialwrap").warning(
                        "RX handler 例外（com=%s），略過此塊、reader 續行", self.com, exc_info=True
                    )
                continue

            # ── POSIX：序列埠 fd 與 human console PTY master fd 統一以 select() 多工（行為與原版逐字一致）。
            read_fds = [serial_fd, *clients_by_fd.keys()]
            try:
                rlist, _, _ = select.select(read_fds, [], [], 0.2)
            except OSError as exc:
                failure_reason = f"SELECT:{exc.errno or type(exc).__name__}"
                break

            for fd in rlist:
                try:
                    data = os.read(fd, 8192)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if fd == serial_fd:
                        failure_reason = f"SERIAL_READ:{exc.errno or type(exc).__name__}"
                        self._stop_event.set()
                        break
                    client = clients_by_fd.get(fd)
                    if client is not None:
                        self._drop_console_client(client.client_id)
                    continue
                if not data:
                    if fd != serial_fd:
                        client = clients_by_fd.get(fd)
                        if client is not None:
                            self._drop_console_client(client.client_id)
                    continue
                try:
                    if fd == serial_fd:
                        self._handle_serial_rx(data)
                        continue
                    client = clients_by_fd.get(fd)
                    if client is None:
                        continue
                    self._handle_console_rx(client, data)
                except Exception:  # noqa: BLE001 — 單一 RX handler 例外不得殺死 reader thread（致該 UART RX 永久停擺、session 卻仍顯示 READY）（#79 STA-1）
                    import logging
                    logging.getLogger("serialwrap").warning(
                        "RX handler 例外（com=%s），略過此塊、reader 續行", self.com, exc_info=True
                    )
                    continue
        if failure_reason and self._on_bridge_down is not None:
            threading.Thread(target=self._on_bridge_down, args=(failure_reason,), daemon=True).start()

    def send_bytes(
        self,
        payload: bytes,
        *,
        source: str,
        cmd_id: str | None = None,
        log: bool = True,
        _allow_during_flash: bool = False,
    ) -> None:
        # 全程持有 _write_lock，使「檢查 flash_mode → 實際寫入」對 set_flash_mode 切換原子化
        # （#69 Finding 2 round2）：否則檢查與寫入之間若 flash 開啟，非 flash byte 仍會寫出汙染 SBL。
        with self._write_lock:
            with self._state_lock:
                if self._flash_mode and not _allow_during_flash:
                    # FLASHING 期間僅允許 flasher 自身寫入（內部能力 _allow_during_flash，僅 flash_tx 帶）；
                    # 丟棄其他所有來源（system probe / reconcile 自動重探 / self_test / agent / command 注入等），
                    # 防止競態下把 bytes 寫進燒錄中的 device、汙染 SBL binary 串流（C2，#69 Finding 1）。
                    # 注意：授權不綁使用者可控的 `source` 稽核字串（cmd submit 可帶任意 source），
                    # 否則 source="flash-..." 的命令會繞過此 gate（#69 Finding round3）。
                    return
                port = self._serial
                serial_fd = self._serial_fd
            if serial_fd is not None:
                # POSIX：維持原寫入路徑（self._write_all），與既有測試（戳 _serial_fd / monkeypatch
                # _write_all）逐字相容；flash gate 與 _write_lock 原子性（#69）皆不變。
                self._write_all(serial_fd, payload)
            elif port is not None:
                # Windows/pyserial：序列埠無整數 fd，改由 port 寫入（#84 PORT-1）。
                port.write(payload)
            else:
                raise RuntimeError("serial not ready")
        if log:
            self.wal.append(com=self.com, direction="TX", source=source, payload=payload, cmd_id=cmd_id)

    def _write_all(self, fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        sent = 0
        while sent < len(payload):
            try:
                n = os.write(fd, view[sent:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.01)
                    continue
                raise
            if n <= 0:
                break
            sent += n

    def flash_tx(self, payload: bytes) -> None:
        """flash 模式：endpoint→device 原樣送出，跳過行處理（_consume_console_input）。

        以內部能力 `_allow_during_flash=True` 寫入——這是 FLASHING 期間唯一被授權的寫入路徑，
        不依賴可被偽造的 `source` 字串（#69 Finding round3）。
        """
        self.send_bytes(payload, source="flash", cmd_id=None, _allow_during_flash=True)

    def mirror_termios_from(self, slave_fd: int, *, fallback_baud: int | None = None) -> None:
        """把 endpoint PTY slave 的 baud 鏡射到 real device。

        鏡射失敗時，若有提供 fallback_baud（命中 pattern 的 registry baud，#55 I3），
        則明確把 real device 設成該 baud；否則維持 configure() 的 profile baud。

        本路徑為 flash endpoint（PTY slave）專用，屬 POSIX-only（#55）；termios 改 lazy import
        使 uart_io 在無 termios 的平台仍可載入（#84）。非 POSIX 後端（fileno()=None）只走
        fallback_baud→set_baud。
        """
        with self._state_lock:
            port = self._serial
        if port is None:
            return
        serial_fd = port.fileno()
        if serial_fd is None:
            # 無可多工 fd（如 Windows pyserial）：只能套 fallback baud（flash 在此平台不支援）。
            if fallback_baud is not None:
                port.set_baud(fallback_baud)
            return
        import termios

        try:
            attrs = termios.tcgetattr(slave_fd)
            ispeed, ospeed = attrs[4], attrs[5]
            dst = termios.tcgetattr(serial_fd)
            dst[4], dst[5] = ispeed, ospeed
            termios.tcsetattr(serial_fd, termios.TCSANOW, dst)
        except OSError:
            if fallback_baud is not None:
                try:
                    port.set_baud(fallback_baud)
                except OSError:
                    pass  # 連 fallback 都失敗 → 維持 profile baud

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        payload = cmd.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.send_bytes(payload, source=source, cmd_id=cmd_id)

    def send_command_echo_paced(
        self,
        cmd: str,
        *,
        source: str,
        cmd_id: str | None = None,
        slice_size: int = 64,
        echo_timeout_s: float = 2.0,
    ) -> dict[str, Any]:
        """以 echo 回讀節流送出單行命令（#161）：逐 slice 送、板端 echo 確認才送下一段。

        無流控（flow_control: none）真機 console 上長命令行會被節流掉字；本原語把命令
        本文切成 ``slice_size`` 短段，每段送出後等 `_await_echo_progress()` 在 RX 中比對到
        該段 echo 才續送——echo 即天然的應用層流控。**全部 slice 確認後才送 ``\\n``**，
        故 echo 停滯時換行尚未送出＝命令未執行＝呼叫端可安全重試（配 `cancel_input_line()`
        恢復半行）。WAL 維持一命令一筆 TX（成功記全文＋換行；停滯記實際已送出的部分）。

        回傳 ``{"ok", "acked_chars", "sent_chars"}``；``ok=False`` 表示某段 echo 逾時停滯。
        """
        # 同時去 CR（Copilot review）：呼叫端傳入 CRLF 結尾的命令時，只 rstrip("\n")
        # 會把 "\r" 留在 body 尾端當成命令本文——該字元會被送出、板端多半直接當換行
        # 執行命令（早於本函式自己送的 "\n"），破壞「全段確認才送換行＝停滯時命令尚未
        # 執行＝可安全重試」的核心不變量；且 _await_echo_progress 的比對已把 RX 的
        # CR/LF 正規化掉，這個 "\r" 永遠 ack 不到，必然變成假性 stall。
        body = cmd.rstrip("\r\n")
        slice_size = max(1, slice_size)
        slices = [body[i : i + slice_size] for i in range(0, len(body), slice_size)]
        pre = self.rx_snapshot_len()
        sent_chars = 0
        acked_chars = 0
        sent_payload = b""
        for idx, piece in enumerate(slices):
            data = piece.encode("utf-8", errors="replace")
            self.send_bytes(data, source=source, cmd_id=cmd_id, log=False)
            sent_payload += data
            sent_chars += len(piece)
            acked_chars = self._await_echo_progress(slices[: idx + 1], pre, echo_timeout_s)
            if acked_chars < sent_chars:
                # echo 停滯：換行未送出、命令未執行；只記實際送出的 bytes（單筆稽核）。
                if sent_payload:
                    self.wal.append(
                        com=self.com, direction="TX", source=source,
                        payload=sent_payload, cmd_id=cmd_id,
                    )
                return {"ok": False, "acked_chars": acked_chars, "sent_chars": sent_chars}
        self.send_bytes(b"\n", source=source, cmd_id=cmd_id, log=False)
        self.wal.append(
            com=self.com, direction="TX", source=source,
            payload=sent_payload + b"\n", cmd_id=cmd_id,
        )
        return {"ok": True, "acked_chars": acked_chars, "sent_chars": sent_chars}

    def _await_echo_progress(
        self, expected_cumulative: list[str], from_offset: int, timeout_s: float
    ) -> int:
        """等待已送出的 slice 串列依序全數出現在 echo 中；回傳已確認的累計字元數。

        比對法：`rx_text_from(from_offset)` 經 `strip_ansi()`＋去 CR/LF 正規化後，
        以**移動起點** ``find`` 逐 slice 依序比對——slice 之間允許任意雜訊（printk、
        終端控制殘字），吸收板端在 echo 間插入的非同步輸出。先檢查再 sleep（echo 常
        已同步到達）、poll 0.01s。逾時回傳當下已比對到的累計字元數（部分進度）。
        """
        deadline = time.monotonic() + timeout_s
        while True:
            text = strip_ansi(self.rx_text_from(from_offset)).replace("\r", "").replace("\n", "")
            pos = 0
            acked = 0
            complete = True
            for piece in expected_cumulative:
                found = text.find(piece, pos)
                if found < 0:
                    complete = False
                    break
                pos = found + len(piece)
                acked += len(piece)
            if complete:
                return acked
            if time.monotonic() >= deadline:
                return acked
            time.sleep(0.01)

    def cancel_input_line(self, *, source: str) -> None:
        """取消板端輸入緩衝中的半行（#161）：送 Ctrl-U（\\x15）清行＋換行重取 prompt。

        供 echo 停滯後復原用——換行尚未送出、命令未執行，Ctrl-U 清掉已累積的半行輸入，
        換行讓 shell 重新給 prompt。不做 per-platform 分歧：不吃 Ctrl-U 的 CLI（如 bcm
        原生 CLI）後果僅是多一個無效字元＋一次換行（既有 prompt 重取），無破壞性。
        """
        self.send_bytes(b"\x15\n", source=source, cmd_id=None)

    def send_secret(self, secret: str) -> None:
        payload = secret.encode("utf-8", errors="replace")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        self.send_bytes(payload, source="system:secret", cmd_id=None, log=False)

    def clear_rx_buffer(self) -> None:
        with self._rx_lock:
            # 保持絕對偏移單調（#158）：clear 也視為丟棄，推進 _rx_trimmed。持舊 offset 的
            # 讀取會拿到「clear 後新到的資料」（語意正確），而非 aliasing 到不相干的舊位置。
            self._rx_trimmed += len(self._rx_text)
            self._rx_text = ""

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            with self._rx_lock:
                snapshot = self._rx_text
            if regex.search(snapshot):
                return True
            time.sleep(0.05)
        return False

    def rx_snapshot_len(self) -> int:
        """回傳 RX 串流的**絕對偏移**（#158）：已修剪/清除的累計量＋現存緩衝長度。

        單調不減；有 RX 進來就嚴格遞增，不受視窗修剪影響（飽和前後語意一致）。
        """
        with self._rx_lock:
            return self._rx_trimmed + len(self._rx_text)

    def rx_stats(self) -> dict[str, Any]:
        """最近 ``RX_RATE_WINDOW_S`` 秒的 raw RX bytes 統計（#153）。

        - ``rx_bytes_last_10s``：視窗內 raw serial bytes 總量（ANSI/雜訊照算）。
        - ``rx_rate_bps``：視窗平均速率（bytes/s，取整）。

        供 probe 失敗的 RX_FLOOD 反分類與 session 公開指標露出使用。
        """
        now = time.monotonic()
        with self._rx_lock:
            self._prune_rx_window_locked(now)
            total = sum(size for _, size in self._rx_window)
        return {
            "rx_bytes_last_10s": total,
            "rx_rate_bps": int(total / RX_RATE_WINDOW_S),
        }

    def rx_total_bytes(self) -> int:
        """累計 raw RX bytes（#150）：單調遞增，``clear_rx_buffer()`` 不歸零。

        probe 前後取差＝probe 全程實際收到的 raw bytes；差為 0（連 echo 都無）
        是 transport stall（USB/usbip read-endpoint 凍結）的關鍵特徵。
        """
        with self._rx_lock:
            return self._rx_total_bytes

    def rx_text_from(self, from_offset: int) -> str:
        """以絕對偏移取文字（#158）；頭段已被修剪時降級回傳現存全窗（不失敗、不回空）。"""
        with self._rx_lock:
            rel = from_offset - self._rx_trimmed
            return self._rx_text if rel <= 0 else self._rx_text[rel:]

    def rx_tail(self, max_chars: int = 4096) -> str:
        with self._rx_lock:
            return self._rx_text[-max_chars:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            with self._rx_lock:
                # 每次持鎖重算相對偏移（#158）：等待期間 _rx_trimmed 可能因視窗修剪推進。
                rel = max(0, from_offset - self._rx_trimmed)
                snapshot = self._rx_text[rel:]
            if regex.search(snapshot):
                return True
            time.sleep(0.05)
        return False

    def attach_console(self, *, label: str | None = None) -> dict[str, Any]:
        if not _pty_available():
            # Windows（#84 PORT-2）：human 直接連 TCP 端點，console client 於連線時自動建立；
            # 此處回傳端點供呼叫端轉達使用者。#131 起 listener 講 telnet（server 主動協商），
            # TeraTerm/PuTTY 以 Telnet 服務連 127.0.0.1:port。
            return {
                "client_id": None,
                "label": label,
                "vtty": self._console_endpoint,
                "endpoint": self._console_endpoint,
                "transport": "tcp",
                "protocol": "telnet",
            }
        client = self._create_console_client(label)
        stale: list[ConsoleClient] = []
        with self._state_lock:
            stale = self._prune_stale_consoles_locked(now=client.attached_at)
            self._clients[client.client_id] = client
            if self._primary_client_id is None:
                self._primary_client_id = client.client_id
        for row in stale:
            self._close_console_client(row)
        return {
            "client_id": client.client_id,
            "label": client.label,
            "vtty": client.slave_path,
        }

    def detach_console(self, client_id: str) -> bool:
        with self._state_lock:
            client = self._clients.pop(client_id, None)
            if client is None:
                return False
            if self._primary_client_id == client_id:
                next_client = next(iter(self._clients.values()), None)
                self._primary_client_id = next_client.client_id if next_client is not None else None
            if self._interactive_owner == f"human:{client_id}":
                self._interactive_owner = None
            if self._suspended_owner == f"human:{client_id}":
                # 被 suspend 的 human console 斷線：放棄整個 suspend 簿記（保護對象已消失），
                # 後續未配對的 resume 因 depth 歸 0 成 no-op（#78）。
                self._suspended_owner = None
                self._agent_active = False
                self._suspend_depth = 0
            self._deferred_buffers.pop(client_id, None)
        self._close_console_client(client)
        return True

    def list_consoles(self) -> list[dict[str, Any]]:
        stale: list[ConsoleClient] = []
        with self._state_lock:
            stale = self._prune_stale_consoles_locked()
            owner = self._interactive_owner
            rows = [
                {
                    "client_id": client.client_id,
                    "label": client.label,
                    "vtty": client.slave_path,
                    "interactive_owner": owner == f"human:{client.client_id}",
                }
                for client in sorted(self._clients.values(), key=lambda row: (row.label, row.client_id))
            ]
        for row in stale:
            self._close_console_client(row)
        return rows

    def console_has_external_peer(self, client_id: str) -> bool:
        with self._state_lock:
            client = self._clients.get(client_id)
            if client is None:
                return False
            return self._client_has_external_peer_locked(client)

    def has_console(self, client_id: str) -> bool:
        with self._state_lock:
            return client_id in self._clients

    def set_interactive_owner(self, owner: str | None) -> None:
        with self._state_lock:
            self._interactive_owner = owner

    def suspend_interactive(self) -> None:
        """暫時掛起 human interactive ownership，切換到 deferred 模式。

        Agent 執行命令前呼叫；human console input 會累積在 deferred buffer
        而不是直接 raw 送到 UART。

        可重入（#78）：多條 agent 路徑（execute_command／file_push/pull／self_test／
        interactive lease soft-preempt）可能重疊呼叫，以巢狀深度計數——只有最外層
        suspend 保存原 owner、最外層 resume 才還原。否則巢狀 suspend 會把已保存的
        ``_suspended_owner`` 覆寫成 None，resume 後 human 永久失去 raw ownership。
        """
        with self._state_lock:
            if self._suspend_depth == 0:
                self._suspended_owner = self._interactive_owner
                self._interactive_owner = None
                self._agent_active = True
            self._suspend_depth += 1

    def resume_interactive(self) -> None:
        """恢復 human interactive ownership 並 flush deferred buffer 到 UART。

        Agent 命令完成後呼叫；僅在巢狀深度歸 0（最外層）時還原 owner 並 flush
        deferred 期間累積的 human 輸入（#78）。不平衡的 resume（depth 已 0）為 no-op。
        """
        flush_data: list[tuple[str, bytes]] = []
        with self._state_lock:
            if self._suspend_depth == 0:
                return
            self._suspend_depth -= 1
            if self._suspend_depth > 0:
                return
            self._interactive_owner = self._suspended_owner
            self._agent_active = False
            self._suspended_owner = None
            for client_id, buf in self._deferred_buffers.items():
                if buf:
                    flush_data.append((f"human:{client_id}", bytes(buf)))
            self._deferred_buffers.clear()
        for source, data in flush_data:
            self.send_bytes(data, source=source, cmd_id=None)

    def snapshot(self) -> dict[str, Any]:
        consoles = self.list_consoles()
        with self._rx_lock:
            rx_dropped_chars = self._rx_trimmed  # 視窗修剪/clear 累計丟棄量（#158 鑑識用）
            rx_total_bytes = self._rx_total_bytes  # raw RX 累計（#150 鑑識用，單調遞增）
        with self._state_lock:
            serial_fd = self._serial_fd
            port = self._serial
            primary_client_id = self._primary_client_id
            primary = None
            if primary_client_id is not None:
                client = self._clients.get(primary_client_id)
                if client is not None:
                    primary = client.slave_path
            interactive_owner = self._interactive_owner
            last_human_input_at = self._last_human_input_at
            console_endpoint = self._console_endpoint
            console_listener_alive = self._console_listener is not None
            agent_active = self._agent_active
            suspended_owner = self._suspended_owner
            flash_mode = self._flash_mode
        serial_alive = False
        if serial_fd is not None:
            try:
                os.fstat(serial_fd)
                serial_alive = True
            except OSError:
                serial_alive = False
        elif port is not None:
            # Windows/pyserial：無整數 fd，改問 port 是否仍開啟（#84 PORT-1）。
            serial_alive = port.is_alive()
        if console_endpoint is not None:
            # Windows TCP console（#84 PORT-2）：primary 是 "host:port"（非檔案路徑），os.path.exists
            # 永遠 False 會讓 SessionManager 誤判 VTTY_STALE/SESSION_NOT_READY；改以 listener 是否
            # 仍在監聽判定 console 是否可達（#84 review）。
            vtty_alive = console_listener_alive
        else:
            vtty_alive = bool(primary and os.path.exists(primary))
        return {
            "com": self.com,
            "device_path": self.device_path,
            "vtty": primary,
            "serial_alive": serial_alive,
            "vtty_alive": vtty_alive,
            "interactive_owner": interactive_owner,
            "last_human_input_at": last_human_input_at,
            "consoles": consoles,
            "running": bool(self._thread and self._thread.is_alive()),
            "console_endpoint": console_endpoint,  # Windows TCP console 端點；POSIX 為 None（#84 PORT-2）
            # Task 6 決策欄位（Codex finding-2）：供 SessionManager 自癒邏輯在同一 snapshot 取得所有
            # 判斷條件，避免分次讀取造成的 TOCTOU（racing suspend_interactive/flash 轉換）。
            "agent_active": agent_active,
            "suspended_owner": suspended_owner,
            "flash_mode": flash_mode,
            "primary_client_id": primary_client_id,
            # RX 視窗累計丟棄字元數（#158）：>0 代表曾觸頂修剪或 clear，供日後鑑識同類
            # 「offset 跨界」問題（絕對偏移語意下不再致病，僅觀測）。
            "rx_dropped_chars": rx_dropped_chars,
            # raw RX 累計 bytes（#150）：單調遞增、clear 不歸零，供 transport stall 鑑識。
            "rx_total_bytes": rx_total_bytes,
        }

    def try_grant_interactive_if_idle(self, owner: str) -> bool:
        """原子 check-and-set：僅當 bridge 完全 idle（無 owner、無 suspended owner、非 agent_active、非 flash）才授予 interactive ownership。

        單次 _state_lock critical section，消除「讀到陳舊 idle 快照→期間 agent suspend/flash→仍誤授」
        的 TOCTOU。供 Task 7 self-heal 邏輯使用。
        """
        with self._state_lock:
            if (
                self._interactive_owner is None
                and self._suspended_owner is None
                and not self._agent_active
                and not self._flash_mode
            ):
                self._interactive_owner = owner
                return True
            return False
