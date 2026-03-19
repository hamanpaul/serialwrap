from __future__ import annotations

import os
import re
import shlex
import threading
from typing import Any

from .arbiter import CommandArbiter
from .config import SessionProfile
from .constants import DEVICE_BY_ID_DIR, DEVICE_BY_PATH_DIR
from .device_watcher import DeviceWatcher
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
    def __init__(self, profiles: list[SessionProfile], *, by_id_dir: str = DEVICE_BY_ID_DIR, by_path_dir: str = DEVICE_BY_PATH_DIR) -> None:
        self._wal = WalWriter()
        self._lock = threading.RLock()
        self._running = False
        self._started_at: str | None = None
        self._profile_count = len(profiles)

        self._arbiter = CommandArbiter(self._send_cb)
        self._sessions = SessionManager(
            profiles,
            self._wal,
            on_ready=self._on_ready,
            on_detached=self._on_detached,
            on_console_line=self._on_console_line,
        )
        self._watcher = DeviceWatcher(
            by_id_dir, self._on_device_change,
            extra_scan_dirs=[by_path_dir],
        )

    def _on_ready(self, session_id: str) -> None:
        self._arbiter.register_session(session_id)

    def _on_detached(self, session_id: str) -> None:
        self._arbiter.unregister_session(session_id)

    def _send_cb(self, session_id: str, command: str, source: str, cmd_id: str, timeout_s: float, mode: str) -> dict[str, Any]:
        return self._sessions.execute_command(session_id, command, source, cmd_id, timeout_s=timeout_s, mode=mode)

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

    def _on_device_change(self, _added, _removed) -> None:
        self._sessions.update_devices(self._watcher.devices)

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._started_at = now_iso()
        self._watcher.start()
        self._watcher.poll_once()
        self._sessions.update_devices(self._watcher.devices)
        self._sessions.bootstrap_attach()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
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
            return None, {"ok": False, "error_code": "SESSION_NOT_READY", "session": session}
        return str(session["session_id"]), None

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "health.ping":
            return {"ok": True, "pong": True}
        if method == "health.status":
            return self.health()

        if method == "device.list":
            return {"ok": True, "devices": self._sessions.list_devices()}

        if method == "session.list":
            return {"ok": True, "sessions": self._sessions.list_sessions()}

        if method == "session.get_state":
            selector = str(params.get("selector") or "")
            return self._sessions.get_session_state(selector)

        if method == "session.self_test":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.self_test(selector, timeout_s=timeout_s)

        if method == "session.recover":
            selector = str(params.get("selector") or params.get("session_id") or params.get("com") or params.get("alias") or "")
            timeout_s = float(params.get("timeout_s") or 2.0)
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.recover_session(selector, timeout_s=timeout_s)

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
            if not selector:
                return {"ok": False, "error_code": "INVALID_ARGS"}
            return self._sessions.interactive_open(selector, owner=owner, timeout_s=timeout_s, command=command)

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
            return self._sessions.get_background_result(cmd_id, from_chunk=from_chunk, limit=limit)

        if method == "command.cancel":
            cmd_id = str(params.get("cmd_id") or "")
            return self._arbiter.cancel(cmd_id)

        if method == "result.tail":
            cmd_id = str(params.get("cmd_id") or "")
            if cmd_id:
                from_chunk = int(params.get("from_chunk") or 0)
                limit = int(params.get("limit") or 200)
                return self._sessions.get_background_result(cmd_id, from_chunk=from_chunk, limit=limit)
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

        return {"ok": False, "error_code": "METHOD_NOT_FOUND", "method": method}
