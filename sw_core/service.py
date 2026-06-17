from __future__ import annotations

import os
import re
import shlex
import threading
from typing import Any

from .arbiter import CommandArbiter
from .config import ProfileTemplate, SessionProfile
from .constants import DEVICE_BY_ID_DIR, DEVICE_BY_PATH_DIR, EVENTS_DIR, EVENTS_RUNTIME_DIR, EVENTS_LOG_PATH
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
        self._sessions.update_devices(self._watcher.devices)
        self._sessions.bootstrap_attach()

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
