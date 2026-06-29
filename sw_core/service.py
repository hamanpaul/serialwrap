from __future__ import annotations

import os
import re
import shlex
import threading
import time
from typing import Any

from .arbiter import CommandArbiter
from .config import ProfileTemplate, SessionProfile
from .constants import DEVICE_BY_ID_DIR, DEVICE_BY_PATH_DIR, EVENTS_DIR, EVENTS_RUNTIME_DIR, EVENTS_LOG_PATH, TTYMCU_PATH
from .flash_endpoint import FlashEndpoint, detect_mcu_line, pump_endpoint_to_sink
from .mcu_patterns import McuPatternRegistry
from .device_watcher import DeviceWatcher
from .event_engine import EventEngine, EngineDeps
from .event_engine.line_buffer import LineBuffer
from .session_manager import SessionManager
from .util import now_iso
from .wal import WalWriter

_HUMAN_INTERACTIVE_COMMANDS = {
    "alsamixer",
    "btop",
    "htop",
    "less",
    "menuconfig",
    "more",
    "most",
    "nano",
    "nmtui",
    "screen",
    "tig",
    "tmux",
    "top",
    "vi",
    "view",
    "vim",
    "vimdiff",
    "watch",
}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _coerce_rpc_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off", "none", "null"}:
            return False
        return False
    return bool(value)


def _human_console_mode(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return "line"

    idx = 0
    while idx < len(tokens):
        token = tokens[idx].strip()
        if not token:
            idx += 1
            continue
        if token == "--":
            idx += 1
            continue
        if _ENV_ASSIGNMENT_RE.match(token):
            idx += 1
            continue

        base = os.path.basename(token)
        if base == "sudo":
            idx += 1
            while idx < len(tokens):
                opt = tokens[idx]
                if opt == "--":
                    idx += 1
                    break
                if opt in {"-u", "-g", "-h", "-p", "-C", "-T", "-r", "-t"}:
                    idx += 2
                    continue
                if opt.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if base == "env":
            idx += 1
            while idx < len(tokens):
                opt = tokens[idx]
                if opt == "--":
                    idx += 1
                    break
                if opt.startswith("-") or _ENV_ASSIGNMENT_RE.match(opt):
                    idx += 1
                    continue
                break
            continue
        if base in {"command", "builtin", "exec"}:
            idx += 1
            continue

        return "interactive" if base in _HUMAN_INTERACTIVE_COMMANDS else "line"
    return "line"


class _BridgeProbe:
    """ProbeTransport 實作：透過已啟動的 UARTBridge 送 sync 並等待 ACK。

    與 detect_mcu_line 搭配；_flash_rx_buffers 由 _flash_rx_observer 即時填充。

    關鍵（真機修正）：probe 送的是 **flasher 自己的 sync bytes**（`sync_bytes`），而非另外注入
    一組 sync。否則獨立 probe 會「吃掉」MCU 對 sync 的 ACK，導致 flasher 隨後自己的 sync 不再被
    回應（double-sync）。命中時把 MCU 回來的 bytes 存進 `acks[by_id]`，供 bridge 啟動時回放給 flasher。
    """

    def __init__(self, svc: "SerialwrapService", by_id_to_com: dict[str, str],
                 sync_bytes: bytes = b"") -> None:
        self._svc = svc
        self._by_id_to_com = by_id_to_com
        self._sync_bytes = sync_bytes
        self.acks: dict[str, bytes] = {}      # by_id -> 命中時擷取到的 MCU 回應（含 ACK），供回放
        # RACE-2（#83）：命中候選的 flash_mode gate 不在 probe 結束時解除，改保持「持有」直到偵測完成、
        # 由 _on_flash_open 交接給 enter_flashing 或統一釋放——避免命中線在 detect 仍續探其他候選
        # （ambiguity scan）期間被解 gate、與 human console 形成雙寫入者。com -> bridge。
        self.held: dict[str, Any] = {}

    def probe(self, by_id: str, probe_bytes: bytes, expect: bytes, timeout_ms: int) -> bool:
        """以 flasher 的 sync（無則退回 pattern probe）試探並等待 ACK（最多 timeout_ms 毫秒）。"""
        com = self._by_id_to_com.get(by_id)
        if com is None:
            return False
        sess = self._svc._sessions.get_session(com)
        if sess is None or sess.bridge is None:
            return False
        bridge = sess.bridge
        with self._svc._flash_lock:
            self._svc._flash_rx_buffers[com] = bytearray()
        # RACE-2（#83）：probe 在正式 enter_flashing 之前進行，此時候選 bridge 的 flash_mode 仍為 False；
        # 若其上有 human raw console 正鍵入，其 send_bytes 會與 probe 的 sync bytes 在同一 UART 交錯
        # （兩個邏輯寫入者 → 污染 target 輸入行 / 干擾 ACK 判讀）。probe 期間開 flash_mode gate 取得該
        # bridge 的寫入仲裁：console→device 寫入（human 鍵入／注入）被 drop，而 probe 的 flash_tx 帶
        # _allow_during_flash 不受影響、device→buffer 的 ACK 擷取（_handle_serial_rx 不看 flash_mode）亦
        # 不受影響（候選必為非 FLASHING——_flash_candidates 只收 READY/ATTACHED）。
        bridge.set_flash_mode(True)
        matched = False
        try:
            try:
                bridge.flash_tx(self._sync_bytes or probe_bytes)
            except Exception:
                return False
            deadline = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < deadline:
                with self._svc._flash_lock:
                    buf = bytes(self._svc._flash_rx_buffers.get(com, b""))
                if expect in buf:
                    self.acks[by_id] = buf      # 記下 MCU 回應，bridge 啟動時回放給 flasher
                    matched = True
                    self.held[com] = bridge     # 命中：保持 gate，交由 _on_flash_open 解/交接
                    return True
                time.sleep(0.02)
            return False
        finally:
            # 未命中才在此解 gate；命中者保持 gate（held）直到偵測完成、由 _on_flash_open 交接
            # enter_flashing 或統一釋放——杜絕命中線在後續候選 probe 期間被解 gate 的雙寫入者窗口。
            if not matched:
                bridge.set_flash_mode(False)

    def release_held(self, *, adopt: str | None = None) -> None:
        """釋放 probe 命中時持有的所有 flash gate；``adopt`` 指定的 com 交接給 flash 生命週期（不解、移出）。"""
        for com in list(self.held):
            bridge = self.held.pop(com)
            if com == adopt:
                continue  # 交接：gate 維持 True，後續由 enter_flashing/exit_flashing 管理
            try:
                bridge.set_flash_mode(False)
            except Exception:
                pass


class SerialwrapService:
    def __init__(
        self,
        profiles: list[SessionProfile],
        *,
        templates: list[ProfileTemplate] | None = None,
        max_sessions: int = 16,
        by_id_dir: str = DEVICE_BY_ID_DIR,
        by_path_dir: str = DEVICE_BY_PATH_DIR,
    ) -> None:
        self._wal = WalWriter()
        self._lock = threading.RLock()
        self._running = False
        self._started_at: str | None = None
        self._profile_count = len(profiles)

        self._arbiter = CommandArbiter(self._send_cb)
        self._sessions = SessionManager(
            profiles,
            self._wal,
            templates=templates,
            max_sessions=max_sessions,
            on_ready=self._on_ready,
            on_detached=self._on_detached,
            on_console_line=self._on_console_line,
        )
        self._engine = EventEngine(EngineDeps(
            events_dir=EVENTS_DIR,
            runtime_dir=EVENTS_RUNTIME_DIR,
            log_path=EVENTS_LOG_PATH,
            bridge=self._sessions,
        ))
        self._engine_line_buffers: dict[str, LineBuffer] = {}
        self._engine_buffers_lock = threading.Lock()
        self._watcher = DeviceWatcher(
            by_id_dir, self._on_device_change,
            extra_scan_dirs=[by_path_dir],
            on_tick=self._sessions.reconcile_readiness,
        )
        self._mcu_registry = McuPatternRegistry.load(None)
        # flash 雙向接線狀態（probe buffer、active com、master fd）
        self._flash_lock = threading.Lock()
        self._flash_rx_buffers: dict[str, bytearray] = {}   # com -> 最近 RX（probe 用）
        self._flash_active_com: str | None = None
        self._flash_master_fd: int | None = None
        self._flash_last_detect: dict | None = None     # 最近一次偵測結果（供 mcu status）（I2）
        self._sessions.add_rx_observer(self._flash_rx_observer)
        self._flash_endpoint = FlashEndpoint(
            link_path=TTYMCU_PATH,
            on_flash_open=self._on_flash_open,
        )

    def _on_ready(self, session_id: str) -> None:
        self._arbiter.register_session(session_id)

    def _on_detached(self, session_id: str) -> None:
        self._arbiter.unregister_session(session_id)

    def _engine_rx_observer(self, com: str, data: bytes, wal_seq: int) -> None:
        with self._engine_buffers_lock:
            buf = self._engine_line_buffers.get(com)
            if buf is None:
                buf = LineBuffer()
                self._engine_line_buffers[com] = buf
        for line in buf.feed(data):
            self._engine.feed_line(com, line, wal_seq)

    def _send_cb(self, session_id: str, command: str, source: str, cmd_id: str, timeout_s: float, mode: str, expected_duration_s: float | None = None) -> dict[str, Any]:
        return self._sessions.execute_command(session_id, command, source, cmd_id, timeout_s=timeout_s, mode=mode, expected_duration_s=expected_duration_s)

    def _on_console_line(self, session_id: str, client_id: str, line: str) -> None:
        mode = _human_console_mode(line)
        self._arbiter.submit(
            session_id=session_id,
            command=line,
            source=f"human:{client_id}",
            mode=mode,
            timeout_s=30.0,
            priority=100,
        )

    def _bg_fallback_from_arbiter(
        self, cmd_id: str, from_chunk: int = 0,
    ) -> dict[str, Any]:
        """BackgroundCapture 尚未建立時，以 arbiter 狀態合成 result_tail 回應。

        當 background 命令已被 arbiter 接受但 worker 尚未執行完畢（或
        執行失敗導致 BackgroundCapture 從未建立），回傳帶有 arbiter
        狀態的空 chunks 回應，避免 caller 收到 CMD_NOT_FOUND。
        """
        arb_result = self._arbiter.get(cmd_id)
        if not arb_result.get("ok"):
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
        cmd_rec = arb_result["command"]
        if cmd_rec.get("execution_mode") != "background":
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
        status = cmd_rec["status"]
        # done 狀態應有 BackgroundCapture；若缺失代表內部異常，不遮蓋
        if status == "done":
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
        # canceled 若已開始執行，capture 可能稍後建立，不能提前宣告終止
        if status == "canceled" and cmd_rec.get("started_at") is not None:
            return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
        return {
            "ok": True,
            "cmd_id": cmd_id,
            "status": status,
            "error_code": cmd_rec.get("error_code"),
            "from_seq": 0,  # pre-capture sentinel，非真實 WAL 邊界
            "last_seq": 0,
            "from_chunk": from_chunk,
            "next_chunk": from_chunk,
            "chunks": [],
        }

    def _on_device_change(self, _added, _removed) -> None:
        self._sessions.update_devices(self._watcher.devices)

    def _flash_rx_observer(self, com: str, data: bytes, wal_seq: int) -> None:
        """RX observer：累積 probe buffer；active flash 期間把 RX bytes 寫入 endpoint master。

        此 callback 由 SessionManager 在 RX 執行緒呼叫，需 thread-safe。
        binary-safe：不做任何文字處理，原樣轉送。
        """
        with self._flash_lock:
            buf = self._flash_rx_buffers.get(com)
            if buf is not None:
                buf.extend(data)
                # 限制 buffer 大小，避免長時間累積佔用記憶體
                if len(buf) > 4096:
                    del buf[:-4096]
            active = self._flash_active_com == com
            master_fd = self._flash_master_fd
        if active and master_fd is not None:
            try:
                os.write(master_fd, data)   # device RX → endpoint（flasher 讀）
            except OSError:
                pass

    def _on_flash_open(self, master_fd: int, slave_fd: int, first_bytes: bytes) -> None:
        """FlashEndpoint 偵測到 flasher 寫入時的回呼。

        流程：
          1. 以 flasher 自己的 sync bytes（first_bytes）為 probe + detect_mcu_line 偵測目標 MCU 線。
          2. 命中 → 把 MCU 的 ACK 回放給 flasher（它在等自己 sync 的回應）→ enter_flashing →
             同步 termios → 雙向 pump（**不**重送 first_bytes，已當 probe 送出）直到 flasher 結束。
          3. pump 結束（flasher 關閉端點或 stop_event）→ exit_flashing，清除狀態。
          4. 未命中 → 靜默返回（保留 flasher 自身 retry 機會）。

        真機修正：偵測用 flasher 的 sync（非另注入一組），命中後回放 ACK；否則獨立 probe 會吃掉
        MCU 的 sync ACK，使 flasher 隨後自己的 sync 收不到回應而永遠 timeout。
        """
        # RACE-2 final（#83）：**原子**收集候選並同時標記 flash-critical（snapshot+mark 同鎖），使
        # destructive op（clear/release/bind/recover）於 probe 窗口（enter_flashing 之前、state 仍
        # READY/ATTACHED）也回 FLASHING_BUSY，不得 detach/rebind bridge 中斷 probe；亦杜絕 snapshot 與
        # mark 之間的 TOCTOU。命中者於 enter_flashing 後改由 FLASHING state 守護；outer finally 統一解標。
        candidates = self._sessions.collect_flash_candidates_and_mark()
        probe_coms = [c["com"] for c in candidates if c.get("com")]
        by_id_to_com = {c["by_id"]: c["com"] for c in candidates if c.get("by_id")}
        transport = _BridgeProbe(self, by_id_to_com, sync_bytes=first_bytes)
        # detect_mcu_line 會掃完所有候選做 ambiguity scan，故命中候選在後續 probe 期間必須保持 gate
        # （probe 命中時不解、記入 transport.held）。outer finally 釋放任何仍持有但未交接的 gate
        # （早退 no-match/ambiguous、sess 失效、例外路徑）；命中且有效則交接 enter_flashing。
        try:
            result = detect_mcu_line(candidates, self._mcu_registry, transport)
            com = by_id_to_com.get(result.by_id) if result.by_id else None
            # 記錄最近一次偵測結果，供 `mcu status` 呈現（含 ambiguous 命中清單）（I2）。
            with self._flash_lock:
                self._flash_last_detect = {
                    "status": result.status,
                    "com": com,
                    "family": result.family,
                    "hits": [{"by_id": h, "com": by_id_to_com.get(h)} for h in result.hits],
                }
            if result.status != "matched":
                return  # 沒命中 / 多義：保持沉默，讓 flasher 自身 retry/timeout（狀態見 mcu status）
            sess = self._sessions.get_session(com) if com else None
            if sess is None or sess.bridge is None:
                return
            # 命中 pattern 的 registry baud，供 termios 鏡射失敗時 fallback（I3）。
            try:
                fallback_baud = self._mcu_registry.get(result.family).baud
            except (KeyError, AttributeError):
                fallback_baud = None
            ack = transport.acks.get(result.by_id, b"")   # 偵測時 MCU 回的 ACK，回放給 flasher
            self._sessions.enter_flashing(com)
            transport.release_held(adopt=com)   # 命中線 gate 交接 flash 生命週期；其餘 held 一併釋放
            # 命中線已進 FLASHING（由 state 守護），偵測也已結束——立即解除**全部**候選的 flash-critical
            # 標記，避免在後續（可能數分鐘的）pump 期間誤擋無關候選 COM 的 destructive op。
            for _com in probe_coms:
                self._sessions.unmark_flash_critical(_com)
            stop = threading.Event()
            with self._flash_lock:
                self._flash_active_com = com
                self._flash_master_fd = master_fd
            # daemon 同時持有 PTY master+slave fd（持 slave 是為了避免閒置時 master 一直 EOF 空轉），
            # 所以 flasher 關閉端點時 master 收不到 EOF。改以 holder-probe 偵測 flasher 斷線：
            # 一旦曾偵測到外部持有（flasher 開著），之後降到 0 即視為結束、收掉 pump。
            try:
                slave_path = os.ttyname(slave_fd)
            except OSError:
                slave_path = None
            if slave_path is not None:
                threading.Thread(target=self._watch_flasher_disconnect,
                                 args=(slave_path, stop), daemon=True).start()
            try:
                sess.bridge.mirror_termios_from(slave_fd, fallback_baud=fallback_baud)
                if ack:
                    try:
                        os.write(master_fd, ack)   # 讓 flasher 看到自己 sync 的回應
                    except OSError:
                        pass
                # 注意：first_bytes 已在 probe 階段送給 MCU，這裡不可重送（會變成多餘的 sync）。
                pump_endpoint_to_sink(master_fd, sess.bridge, stop, first_bytes=b"")
            finally:
                with self._flash_lock:
                    self._flash_active_com = None
                    self._flash_master_fd = None
                    self._flash_rx_buffers.pop(com, None)   # 清掉本次 probe/flash 的 RX buffer（M1）
                self._sessions.exit_flashing(com)
        finally:
            transport.release_held()   # 釋放任何仍持有但未交接的 flash gate（早退/ambiguous/例外）
            for _com in probe_coms:    # 解除 flash-critical 標記（命中者此時已由 FLASHING state 守護）
                self._sessions.unmark_flash_critical(_com)

    def _watch_flasher_disconnect(self, slave_path: str, stop: "threading.Event",
                                  poll_s: float = 0.5, max_s: float = 1800.0) -> None:
        """輪詢 flash 端點 PTY 的外部持有者；flasher 斷線（持有歸零）即設 stop 結束 pump。"""
        armed = False
        deadline = time.monotonic() + max_s
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                pids = self._sessions._probe_external_holder(slave_path).get("pids", [])
            except Exception:
                pids = []
            if pids:
                armed = True
            elif armed:
                break       # 曾連上、現在斷了 → flasher 結束
            time.sleep(poll_s)
        stop.set()

    def _flash_candidates(self) -> list[dict]:
        """flash 偵測候選：已 attached 且非 command_capable console 的 session，附上 real_path。"""
        devices = {d["by_id"]: d["real_path"] for d in self._sessions.list_devices()}
        out: list[dict] = []
        for s in self._sessions.list_sessions():
            if s.get("command_capable"):
                continue
            if s.get("state") not in ("READY", "ATTACHED"):
                continue
            out.append({
                "com": s.get("com"),
                "by_id": s.get("device_by_id"),
                "real_path": devices.get(s.get("device_by_id")),
                "command_capable": False,
            })
        return out

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = now_iso()
        self._engine.start()
        self._sessions.add_rx_observer(self._engine_rx_observer)
        self._watcher.start()
        self._watcher.poll_once()
        # #100：在 spawn attach threads 之前，先對「當下在線的 dynamic 裝置」依 by-id
        # 排序一次配好 COM rank（兩條 startup 入口 update_devices / bootstrap_attach 都會
        # 觸發並發 attach；先預配才能消除「誰先搶到 lock 誰拿 COM0」的 race）。
        self._sessions.prepare_dynamic_rank(list(self._watcher.devices.keys()))
        self._sessions.update_devices(self._watcher.devices)
        self._sessions.bootstrap_attach()
        self._flash_endpoint.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._engine.stop()
        self._watcher.stop()
        for row in self._sessions.list_sessions():
            sid = row["session_id"]
            self._arbiter.unregister_session(sid)
        try:
            self._flash_endpoint.stop()
        except OSError:
            pass

    def health(self) -> dict[str, Any]:
        with self._lock:
            sessions = self._sessions.list_sessions()
            devices = self._sessions.list_devices()
            warnings: list[str] = []
            if self._profile_count == 0:
                warnings.append("no_profiles_loaded")
            if not devices:
                warnings.append("no_devices_found")
            result: dict[str, Any] = {
                "ok": True,
                "pid": os.getpid(),
                "running": self._running,
                "started_at": self._started_at,
                "sessions": len(sessions),
                "devices": len(devices),
                "commands": len(self._arbiter.snapshot()),
                "wal_path": self._wal.wal_path,
                "mirror_path": self._wal.mirror_path,
            }
            if warnings:
                result["warnings"] = warnings
            return result

    def _resolve_session_id(self, selector: str) -> tuple[str | None, dict[str, Any] | None]:
        state = self._sessions.get_session_state(selector)
        if not state.get("ok"):
            return None, state
        session = state["session"]
        if session.get("state") != "READY":
            # ATTACHED 且非 command-capable（無 ready_probe，僅支援 console）的 profile
            # 永遠不會進 READY，回語意明確的錯誤碼而非 SESSION_NOT_READY。
            if session.get("state") == "ATTACHED" and not session.get("command_capable", True):
                return None, {
                    "ok": False,
                    "error_code": "PROFILE_NOT_COMMAND_CAPABLE",
                    "hint": "此 profile 僅支援 console；要下命令請設定 ready_probe 或改用具 prompt 的 profile。",
                    "session": session,
                }
            return None, {"ok": False, "error_code": "SESSION_NOT_READY", "session": session}
        return str(session["session_id"]), None

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "health.ping":
            return {"ok": True, "pong": True}
        if method == "health.status":
            return self.health()

        if method == "mcu.patterns":
            return {"ok": True, "patterns": [
                {"family": p.family, "probe": p.probe.hex(" "),
                 "expect": p.expect.hex(" "), "baud": p.baud}
                for p in self._mcu_registry.all()]}
        if method == "mcu.status":
            with self._flash_lock:
                last_detect = dict(self._flash_last_detect) if self._flash_last_detect else None
                active_com = self._flash_active_com
            return {"ok": True,
                    "candidates": self._flash_candidates(),
                    "flashing": self._flash_endpoint.is_flashing(),
                    "flashing_com": active_com,
                    "last_detect": last_detect}

        if method == "device.list":
            return {"ok": True, "devices": self._sessions.list_devices()}

        if method == "device.release":
            selector = str(params.get("selector") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            source = str(params.get("source") or "cli")
            reason = params.get("reason")
            return self._sessions.release_device(selector, source=source, reason=reason)

        if method == "device.attach":
            selector = str(params.get("selector") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.attach_device(selector, force=bool(params.get("force")))

        if method == "session.list":
            return {"ok": True, "sessions": self._sessions.list_sessions()}

        if method == "session.get_state":
            selector = str(params.get("selector") or "")
            return self._sessions.get_session_state(selector)

        if method == "session.activity":
            selector = str(
                params.get("selector")
                or params.get("session_id")
                or params.get("com")
                or params.get("alias")
                or ""
            )
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.get_session_state(selector)

        if method == "session.self_test":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            strict_human_lock = _coerce_rpc_bool(params.get("strict_human_lock"))
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.self_test(
                selector,
                timeout_s=timeout_s,
                strict_human_lock=strict_human_lock,
            )

        if method == "session.recover":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            force = bool(params.get("force"))
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.recover_session(selector, timeout_s=timeout_s, force=force)

        if method == "session.clear":
            selector = str(
                params.get("selector")
                or params.get("session_id")
                or params.get("com")
                or params.get("alias")
                or ""
            )
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.clear_session(selector)

        if method == "session.bind":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            device_by_id = str(params.get("device_by_id") or params.get("by_id") or "")
            if not selector or not device_by_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.bind_session(selector, device_by_id)

        if method == "session.pin":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            profile_name = str(params.get("profile") or params.get("profile_name") or "")
            if not selector or not profile_name:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.pin_session(selector, profile_name)

        if method == "session.unpin":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.unpin_session(selector)

        if method == "session.attach":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.attach_session(selector)

        if method == "session.console_attach":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            label = params.get("label")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.attach_console(selector, label=str(label) if label else None)

        if method == "session.console_detach":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            client_id = str(params.get("client_id") or "")
            if not selector or not client_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.detach_console(selector, client_id)

        if method == "session.console_list":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.list_consoles(selector)

        if method == "session.interactive_open":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            owner = str(params.get("owner") or "agent")
            timeout_s = float(params.get("timeout_s") or 60.0)
            command = str(params.get("command") or "")
            allow_attached = bool(params.get("allow_attached", False))
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_open(selector, owner=owner, timeout_s=timeout_s, command=command, allow_attached=allow_attached)

        if method == "session.interactive_send":
            interactive_id = str(params.get("interactive_id") or "")
            data = str(params.get("data") or "")
            encoding = str(params.get("encoding") or "plain")
            if not interactive_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_send(interactive_id, data=data, encoding=encoding)

        if method == "session.interactive_status":
            interactive_id = str(params.get("interactive_id") or "")
            screen_chars = int(params.get("screen_chars") or 2048)
            if not interactive_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_status(interactive_id, screen_chars=screen_chars)

        if method == "session.interactive_close":
            interactive_id = str(params.get("interactive_id") or "")
            if not interactive_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_close(interactive_id)

        if method == "alias.list":
            return {"ok": True, "aliases": self._sessions.list_aliases()}

        if method == "alias.set":
            session_id = str(params.get("session_id") or "")
            alias = str(params.get("alias") or "")
            return self._sessions.set_alias_for_session(session_id, alias)

        if method == "alias.assign":
            by_id = str(params.get("by_id") or "")
            alias = str(params.get("alias") or "")
            profile = params.get("profile")
            if not by_id or not alias:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.assign_alias(by_id, alias, str(profile) if profile else None)

        if method == "alias.unassign":
            alias = str(params.get("alias") or "")
            return self._sessions.unassign_alias(alias)

        if method == "command.submit":
            selector = str(params.get("selector") or params.get("com") or params.get("alias") or "")
            cmd = str(params.get("cmd") or params.get("command") or "")
            source = str(params.get("source") or "agent")
            mode = str(params.get("mode") or "line")
            timeout_s = float(params.get("timeout_s") or 10.0)
            priority = int(params.get("priority") or 10)
            raw_ed = params.get("expected_duration_s")
            expected_duration_s: float | None = float(raw_ed) if raw_ed is not None else None
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            session_id, err = self._resolve_session_id(selector)
            if err is not None:
                return err
            assert session_id is not None
            return self._arbiter.submit(
                session_id=session_id,
                command=cmd,
                source=source,
                mode=mode,
                timeout_s=timeout_s,
                priority=priority,
                expected_duration_s=expected_duration_s,
            )

        if method == "command.get":
            cmd_id = str(params.get("cmd_id") or "")
            return self._arbiter.get(cmd_id)

        if method == "command.result_tail":
            cmd_id = str(params.get("cmd_id") or "")
            from_chunk = int(params.get("from_chunk") or 0)
            limit = int(params.get("limit") or 200)
            if not cmd_id:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            result = self._sessions.get_background_result(cmd_id, from_chunk=from_chunk, limit=limit)
            if not result.get("ok") and result.get("error_code") == "CMD_NOT_FOUND":
                result = self._bg_fallback_from_arbiter(cmd_id, from_chunk)
            return result

        if method == "command.cancel":
            cmd_id = str(params.get("cmd_id") or "")
            return self._arbiter.cancel(cmd_id)

        if method == "result.tail":
            cmd_id = str(params.get("cmd_id") or "")
            if cmd_id:
                from_chunk = int(params.get("from_chunk") or 0)
                limit = int(params.get("limit") or 200)
                result = self._sessions.get_background_result(cmd_id, from_chunk=from_chunk, limit=limit)
                if not result.get("ok") and result.get("error_code") == "CMD_NOT_FOUND":
                    result = self._bg_fallback_from_arbiter(cmd_id, from_chunk)
                return result
            # Deprecated legacy path: fall back to raw WAL tail by selector.

        if method in {"result.tail", "log.tail_raw"}:
            com = params.get("com")
            selector = str(com or params.get("selector") or "")
            from_seq = int(params.get("from_seq") or 0)
            limit = int(params.get("limit") or 200)
            target_com: str | None = None
            if selector:
                state = self._sessions.get_session_state(selector)
                if not state.get("ok"):
                    return state
                target_com = str(state["session"]["com"])
            rows = self._wal.tail_raw(from_seq=from_seq, com=target_com, limit=limit)
            return {"ok": True, "records": rows}

        if method == "log.tail_text":
            com = params.get("com")
            selector = str(com or params.get("selector") or "")
            from_seq = int(params.get("from_seq") or 0)
            limit = int(params.get("limit") or 200)
            target_com: str | None = None
            if selector:
                state = self._sessions.get_session_state(selector)
                if not state.get("ok"):
                    return state
                target_com = str(state["session"]["com"])
            lines = self._wal.tail_text(from_seq=from_seq, com=target_com, limit=limit)
            return {"ok": True, "lines": lines}

        if method == "wal.range":
            from_seq = int(params.get("from_seq") or 0)
            to_seq = int(params.get("to_seq") or 0)
            limit = int(params.get("limit") or 1000)
            rows = self._wal.tail_raw(from_seq=from_seq, com=None, limit=limit)
            if to_seq > 0:
                rows = [r for r in rows if int(r.get("seq", 0)) <= to_seq]
            return {"ok": True, "records": rows}

        if method == "wal.reset":
            return self._wal.reset()

        if method == "wal.current_seq":
            return {"ok": True, "seq": self._wal.current_seq}

        if method == "session.log_start":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.log_start(selector)

        if method == "session.log_stop":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.log_stop(selector)

        if method == "session.log_status":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.log_status(selector)

        if method == "file.push":
            selector = str(params.get("selector") or "")
            local_path = str(params.get("local_path") or "")
            remote_path = str(params.get("remote_path") or "")
            chunk_size = int(params.get("chunk_size") or 2048)
            source = str(params.get("source") or "agent")
            if not selector or not local_path or not remote_path:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            session_id, err = self._resolve_session_id(selector)
            if err is not None:
                return err
            return self._sessions.file_push(
                selector,
                local_path=local_path,
                remote_path=remote_path,
                chunk_size=chunk_size,
                source=source,
            )

        if method == "file.pull":
            selector = str(params.get("selector") or "")
            remote_path = str(params.get("remote_path") or "")
            local_path = params.get("local_path")
            source = str(params.get("source") or "agent")
            if not selector or not remote_path:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            session_id, err = self._resolve_session_id(selector)
            if err is not None:
                return err
            return self._sessions.file_pull(
                selector,
                remote_path=remote_path,
                local_path=str(local_path) if local_path else None,
                source=source,
            )

        if method == "event.rule_set":
            try:
                rule = self._engine.rule_set(params or {})
                return {"ok": True, **dict(rule.raw)}
            except ValueError as e:
                return {"ok": False, "error_code": "INVALID_RULE_SCHEMA", "error": str(e)}
        if method == "event.rule_delete":
            deleted = self._engine.rule_delete(str(params.get("rule_id") or ""))
            return {"ok": True, "deleted": deleted}
        if method == "event.rule_list":
            return {"ok": True, "rules": self._engine.rule_list(
                selector=params.get("selector"),
                owner=params.get("owner"),
            )}
        if method == "event.rule_get":
            result = self._engine.rule_get(str(params.get("rule_id") or ""))
            if result is None:
                return {"ok": False, "error_code": "RULE_NOT_FOUND"}
            return {"ok": True, **result}
        if method == "event.com_enable":
            return {"ok": True, **self._engine.com_enable(str(params.get("selector") or ""))}
        if method == "event.com_disable":
            return {"ok": True, **self._engine.com_disable(str(params.get("selector") or ""))}
        if method == "event.com_status":
            return {"ok": True, **self._engine.com_status(params.get("selector"))}
        if method == "event.reset":
            cleared = self._engine.reset(
                rule_id=params.get("rule_id"),
                selector=params.get("selector"),
            )
            return {"ok": True, "cleared": cleared}
        if method == "event.reload":
            return {"ok": True, **self._engine.reload()}
        if method == "event.tail":
            return {"ok": True, "entries": self._engine.tail(
                rule_id=params.get("rule_id"),
                selector=params.get("selector"),
                since_ts=params.get("since_ts"),
                n=params.get("n"),
            )}

        return {"ok": False, "error_code": "METHOD_NOT_FOUND", "method": method}
