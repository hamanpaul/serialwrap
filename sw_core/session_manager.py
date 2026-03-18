from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import shlex
import threading
import time
import uuid
from typing import Any, Callable

from .alias_registry import AliasRegistry
from .auth import resolve_session_auth
from .config import SessionProfile
from .constants import STATE_PATH
from .device_watcher import DeviceInfo
from .login_fsm import ensure_ready, probe_ready
from .uart_io import UARTBridge
from .util import clean_text, now_iso
from .wal import WalWriter


_ATTACHED_CONSOLE_LEASE_TIMEOUT_S = 86400.0


def _is_reboot_command(command: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False

    base = os.path.basename(tokens[0])
    if base == "reboot":
        return True
    if base == "systemctl" and len(tokens) >= 2 and tokens[1] == "reboot":
        return True
    if base != "shutdown":
        return False
    return any(token == "-r" or token == "--reboot" or token.startswith("-r") for token in tokens[1:])


@dataclasses.dataclass
class BackgroundCapture:
    cmd_id: str
    session_id: str
    from_seq: int
    quiet_window_s: float
    created_at: str
    chunks: list[str] = dataclasses.field(default_factory=list)
    last_seq: int = 0
    status: str = "active"
    last_activity_mono: float = dataclasses.field(default_factory=time.monotonic)

    def maybe_finalize(self) -> None:
        if self.status == "active" and time.monotonic() - self.last_activity_mono >= self.quiet_window_s:
            self.status = "done"


@dataclasses.dataclass
class InteractiveLease:
    interactive_id: str
    session_id: str
    owner: str
    created_at: str
    timeout_s: float
    last_activity_at: float = dataclasses.field(default_factory=time.monotonic)
    status: str = "active"

    def touch(self) -> None:
        self.last_activity_at = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.last_activity_at > self.timeout_s


@dataclasses.dataclass
class SessionRuntime:
    session_id: str
    profile: SessionProfile
    state: str = "DETACHED"
    last_error: str | None = None
    detached_at: str | None = None
    last_ready_at: str | None = None
    vtty_path: str | None = None
    bridge: UARTBridge | None = None
    attached_real_path: str | None = None
    bridge_generation: int = 0
    recovering: bool = False
    recovery_started_at: str | None = None
    pending_auto_login: bool = False
    interactive_session_id: str | None = None
    foreground_busy: bool = False
    background_cmd_ids: list[str] = dataclasses.field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        console_count = 0
        if self.bridge is not None:
            console_count = len(self.bridge.list_consoles())
        return {
            "session_id": self.session_id,
            "profile": self.profile.profile_name,
            "com": self.profile.com,
            "alias": self.profile.alias,
            "act_no": self.profile.act_no,
            "device_by_id": self.profile.device_by_id,
            "platform": self.profile.platform,
            "state": self.state,
            "last_error": self.last_error,
            "detached_at": self.detached_at,
            "last_ready_at": self.last_ready_at,
            "vtty": self.vtty_path,
            "attached_real_path": self.attached_real_path,
            "bridge_generation": self.bridge_generation,
            "recovering": self.recovering,
            "interactive_session_id": self.interactive_session_id,
            "console_count": console_count,
        }


class SessionManager:
    def __init__(
        self,
        profiles: list[SessionProfile],
        wal: WalWriter,
        *,
        on_ready: Callable[[str], None],
        on_detached: Callable[[str], None],
        on_console_line: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._wal = wal
        self._on_ready = on_ready
        self._on_detached = on_detached
        self._on_console_line = on_console_line
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRuntime] = {}
        self._aliases = AliasRegistry()
        self._devices: dict[str, DeviceInfo] = {}
        self._binding_overrides: dict[str, str] = {}
        self._attach_inflight: set[str] = set()
        self._background: dict[str, BackgroundCapture] = {}
        self._interactive: dict[str, InteractiveLease] = {}

        self._load_state()
        for p in profiles:
            sid = f"{p.profile_name}:{p.com}"
            device_by_id = self._binding_overrides.get(sid, p.device_by_id)
            if not device_by_id:
                continue
            profile = dataclasses.replace(p, device_by_id=device_by_id)
            if sid not in self._sessions:
                self._sessions[sid] = SessionRuntime(session_id=sid, profile=profile)
            self._aliases.set_for_session(sid, profile.alias)
        self._save_state()

    def _load_state(self) -> None:
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fp:
                obj = json.load(fp)
        except Exception:
            return
        rows = obj.get("aliases") if isinstance(obj, dict) else None
        if isinstance(rows, dict):
            self._aliases.load(rows)
        bindings = obj.get("bindings") if isinstance(obj, dict) else None
        if isinstance(bindings, dict):
            normalized: dict[str, str] = {}
            for sid, by_id in bindings.items():
                if isinstance(sid, str) and isinstance(by_id, str) and sid.strip() and by_id.strip():
                    normalized[sid.strip()] = by_id.strip()
            self._binding_overrides = normalized

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as fp:
            json.dump(
                {"aliases": self._aliases.dump(), "bindings": dict(self._binding_overrides)},
                fp,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            fp.write("\n")

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [v.to_public_dict() for _, v in sorted(self._sessions.items())]

    def get_session(self, selector: str) -> SessionRuntime | None:
        with self._lock:
            if selector in self._sessions:
                return self._sessions[selector]
            for session in self._sessions.values():
                if selector == session.profile.com or selector == session.profile.alias:
                    return session
            return None

    def list_aliases(self) -> list[dict[str, Any]]:
        return self._aliases.list_alias()

    def set_alias_for_session(self, session_id: str, alias: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "session_id": session_id}
            session.profile = dataclasses.replace(session.profile, alias=alias)
            self._aliases.set_for_session(session_id, alias)
            self._save_state()
            return {"ok": True, "session_id": session_id, "alias": alias}

    def assign_alias(self, by_id: str, alias: str, profile: str | None = None) -> dict[str, Any]:
        self._aliases.assign_by_id(by_id, alias, profile)
        self._save_state()
        return {"ok": True, "alias": alias, "device_by_id": by_id}

    def unassign_alias(self, alias: str) -> dict[str, Any]:
        ok = self._aliases.unassign(alias)
        self._save_state()
        if not ok:
            return {"ok": False, "error_code": "ALIAS_NOT_FOUND", "alias": alias}
        return {"ok": True, "alias": alias}

    def _detach_session_locked(self, session: SessionRuntime, *, reason: str) -> None:
        if session.bridge is not None:
            session.bridge.stop()
            session.bridge = None
        session.vtty_path = None
        session.attached_real_path = None
        session.state = "DETACHED"
        session.detached_at = now_iso()
        session.last_error = reason
        if session.interactive_session_id is not None:
            lease = self._interactive.pop(session.interactive_session_id, None)
            if lease is not None:
                lease.status = "closed"
        session.interactive_session_id = None
        session.foreground_busy = False
        for cmd_id in list(session.background_cmd_ids):
            capture = self._background.get(cmd_id)
            if capture is not None:
                capture.status = "done"
        session.background_cmd_ids.clear()
        self._on_detached(session.session_id)

    def clear_session(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            self._detach_session_locked(session, reason="CLEARED")
            by_id = session.profile.device_by_id
            has_device = bool(by_id and by_id in self._devices)
            if has_device:
                session.state = "ATTACHING"
                session.last_error = None
        self._save_state()
        if has_device and by_id is not None:
            self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def bind_session(self, selector: str, device_by_id: str) -> dict[str, Any]:
        device_by_id = device_by_id.strip()
        if not device_by_id:
            return {"ok": False, "error_code": "INVALID_ARGS"}

        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            for other in self._sessions.values():
                if other.session_id != session.session_id and other.profile.device_by_id == device_by_id:
                    return {
                        "ok": False,
                        "error_code": "DEVICE_ALREADY_BOUND",
                        "device_by_id": device_by_id,
                        "session_id": other.session_id,
                    }
            if session.bridge is not None:
                self._detach_session_locked(session, reason="REBOUND")
            session.profile = dataclasses.replace(session.profile, device_by_id=device_by_id)
            self._binding_overrides[session.session_id] = device_by_id
            self._save_state()
            has_device = device_by_id in self._devices
            if has_device:
                session.state = "ATTACHING"
                session.last_error = None

        if has_device:
            self._spawn_attach(device_by_id)
        else:
            with self._lock:
                session.last_error = "DEVICE_NOT_FOUND"
                session.state = "DETACHED"
        return {"ok": True, "session": session.to_public_dict()}

    def attach_session(self, selector: str) -> dict[str, Any]:
        bridge: UARTBridge | None = None
        should_probe = False
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            if not by_id:
                return {"ok": False, "error_code": "DEVICE_NOT_BOUND", "session": session.to_public_dict()}
            if session.bridge is not None:
                lease = self._refresh_interactive_locked(session)
                if lease is not None and lease.owner.startswith("human:"):
                    return {"ok": True, "session": session.to_public_dict()}
                if session.state == "ATTACHED":
                    bridge = session.bridge
                    should_probe = True
                else:
                    return {"ok": True, "session": session.to_public_dict()}
            if by_id not in self._devices:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                return {"ok": False, "error_code": "DEVICE_NOT_FOUND", "session": session.to_public_dict()}
            if not should_probe:
                session.state = "ATTACHING"
                session.last_error = None
        if should_probe and bridge is not None:
            ok, err = probe_ready(bridge, session.profile)
            notify_ready = False
            with self._lock:
                current = self._sessions.get(session.session_id)
                if current is None or current.bridge is not bridge:
                    return {"ok": False, "error_code": "SESSION_NOT_READY"}
                if ok:
                    current.state = "READY"
                    current.last_error = None
                    current.last_ready_at = now_iso()
                    current.recovering = False
                    current.recovery_started_at = None
                    current.pending_auto_login = False
                    notify_ready = True
                else:
                    current.state = "ATTACHED"
                    current.last_error = err
                result = current.to_public_dict()
            if notify_ready:
                self._on_ready(session.session_id)
            return {"ok": True, "session": result}
        self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"by_id": by_id, "real_path": dev.real_path} for by_id, dev in sorted(self._devices.items())]

    def update_devices(self, devices: dict[str, DeviceInfo]) -> None:
        with self._lock:
            prev = self._devices
            self._devices = dict(devices)

        changed = sorted(
            by_id for by_id in set(prev.keys()) & set(devices.keys())
            if prev[by_id].real_path != devices[by_id].real_path
        )
        removed = sorted(set(prev.keys()) - set(devices.keys()))
        added = sorted(set(devices.keys()) - set(prev.keys()))

        for by_id in [*removed, *changed]:
            self._detach_by_id(by_id, reason="DEVICE_REBOUND_REQUIRED" if by_id in changed else "DEVICE_REMOVED")
        for by_id in [*added, *changed]:
            self._spawn_attach(by_id)

    def bootstrap_attach(self) -> None:
        with self._lock:
            keys = list(self._devices.keys())
        for by_id in keys:
            self._spawn_attach(by_id)

    def _spawn_attach(self, by_id: str) -> None:
        with self._lock:
            if by_id in self._attach_inflight:
                return
            self._attach_inflight.add(by_id)

        def _run() -> None:
            try:
                self._attach_by_id(by_id)
            finally:
                with self._lock:
                    self._attach_inflight.discard(by_id)

        threading.Thread(target=_run, name=f"serialwrap-attach-{by_id}", daemon=True).start()

    def _detach_by_id(self, by_id: str, *, reason: str) -> None:
        with self._lock:
            targets = [s for s in self._sessions.values() if s.profile.device_by_id == by_id]
            for session in targets:
                self._detach_session_locked(session, reason=reason)

    def _on_bridge_console_line(self, session_id: str, client_id: str, line: str) -> None:
        if self._on_console_line is not None:
            self._on_console_line(session_id, client_id, line)

    def _on_bridge_rx(self, session_id: str, data: bytes) -> None:
        chunk = clean_text(data.decode("utf-8", errors="replace"))
        if not chunk:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.foreground_busy:
                return
            for cmd_id in list(session.background_cmd_ids):
                capture = self._background.get(cmd_id)
                if capture is None or capture.status != "active":
                    continue
                capture.chunks.append(chunk)
                capture.last_activity_mono = time.monotonic()
                capture.last_seq = self._wal.current_seq

    def _handle_bridge_down(self, session_id: str, bridge: UARTBridge, reason: str) -> None:
        by_id: str | None = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.bridge is not bridge:
                return
            by_id = session.profile.device_by_id
            self._detach_session_locked(session, reason=f"BRIDGE_DOWN:{reason}")
            if by_id and by_id in self._devices:
                session.state = "RECOVERING" if session.pending_auto_login else "ATTACHING"
                session.last_error = None
        if by_id and by_id in self._devices:
            self._spawn_attach(by_id)

    def _attach_by_id(self, by_id: str) -> None:
        save_needed = False
        require_login = False
        passthrough_only = False
        with self._lock:
            session = next((s for s in self._sessions.values() if s.profile.device_by_id == by_id), None)
            if session is None:
                candidates = sorted(
                    [s for s in self._sessions.values() if s.state == "DETACHED" and s.profile.device_by_id not in self._devices],
                    key=lambda row: row.profile.act_no,
                )
                if candidates:
                    session = candidates[0]
                    session.profile = dataclasses.replace(session.profile, device_by_id=by_id)
                    self._binding_overrides[session.session_id] = by_id
                    save_needed = True
            dev = self._devices.get(by_id)
            if session is not None:
                require_login = session.pending_auto_login
                passthrough_only = session.profile.platform == "passthrough"
        if save_needed:
            self._save_state()
        if session is None or dev is None or session.bridge is not None:
            return

        bridge = UARTBridge(
            session.profile.com,
            dev.real_path,
            session.profile.uart,
            self._wal,
            on_console_line=lambda client_id, line, sid=session.session_id: self._on_bridge_console_line(sid, client_id, line),
            on_rx_data=lambda data, sid=session.session_id: self._on_bridge_rx(sid, data),
            on_bridge_down=lambda reason, sid=session.session_id: self._handle_bridge_down(sid, bridge, reason),
        )
        session.state = "ATTACHING"
        session.last_error = None

        try:
            bridge.start()
            if passthrough_only:
                ok = False
                err = None
            elif require_login:
                auth = resolve_session_auth(session.profile)
                ok, err = ensure_ready(bridge, session.profile, auth=auth)
                if not ok:
                    bridge.stop()
                    with self._lock:
                        session.state = "DETACHED"
                        session.last_error = err
                        session.detached_at = now_iso()
                        session.bridge = None
                        session.vtty_path = None
                        session.attached_real_path = None
                    self._on_detached(session.session_id)
                    return
            else:
                ok, err = probe_ready(bridge, session.profile)

            notify_ready = False
            with self._lock:
                current = self._devices.get(by_id)
                if current is None or current.real_path != dev.real_path or session.state == "DETACHED":
                    bridge.stop()
                    session.state = "DETACHED"
                    session.last_error = "DEVICE_REMOVED_DURING_ATTACH"
                    session.detached_at = now_iso()
                    session.bridge = None
                    session.vtty_path = None
                    session.attached_real_path = None
                    return
                session.bridge = bridge
                session.vtty_path = bridge.vtty_path
                session.attached_real_path = dev.real_path
                session.bridge_generation += 1
                if ok:
                    session.state = "READY"
                    session.last_error = None
                    session.last_ready_at = now_iso()
                    session.recovering = False
                    session.recovery_started_at = None
                    session.pending_auto_login = False
                    notify_ready = True
                else:
                    session.state = "ATTACHED"
                    session.last_error = err
                    session.recovering = False
                    session.recovery_started_at = None
            if notify_ready:
                self._on_ready(session.session_id)
        except Exception as exc:
            try:
                bridge.stop()
            except Exception:
                pass
            with self._lock:
                session.state = "DETACHED"
                session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                session.detached_at = now_iso()
                session.bridge = None
                session.vtty_path = None
                session.attached_real_path = None
            self._on_detached(session.session_id)

    def _last_prompt_start(self, text: str, prompt_regex: str) -> int | None:
        regex = re.compile(prompt_regex)
        last: re.Match[str] | None = None
        for match in regex.finditer(text):
            last = match
        return last.start() if last is not None else None

    def _extract_command_stdout(self, text: str, command: str, prompt_regex: str) -> str:
        cleaned = clean_text(text)
        prompt_start = self._last_prompt_start(cleaned, prompt_regex)
        if prompt_start is not None:
            cleaned = cleaned[:prompt_start]
        lines = cleaned.lstrip("\n").splitlines()
        if command:
            command_stripped = command.strip()
            while lines and lines[0].strip().endswith(command_stripped):
                lines = lines[1:]
        return "\n".join(lines).strip("\n")

    def _open_interactive_locked(self, session: SessionRuntime, *, owner: str, timeout_s: float) -> InteractiveLease:
        interactive_id = uuid.uuid4().hex
        lease = InteractiveLease(
            interactive_id=interactive_id,
            session_id=session.session_id,
            owner=owner,
            created_at=now_iso(),
            timeout_s=timeout_s,
        )
        self._interactive[interactive_id] = lease
        session.interactive_session_id = interactive_id
        assert session.bridge is not None
        session.bridge.set_interactive_owner(owner)
        return lease

    def _close_interactive_locked(
        self,
        session: SessionRuntime,
        *,
        interactive_id: str | None = None,
        expected_owner: str | None = None,
    ) -> InteractiveLease | None:
        lease_id = interactive_id or session.interactive_session_id
        if lease_id is None:
            return None
        lease = self._interactive.get(lease_id)
        if lease is not None and expected_owner is not None and lease.owner != expected_owner:
            return None
        if lease is not None:
            lease.status = "closed"
            self._interactive.pop(lease_id, None)
        session.interactive_session_id = None
        if session.bridge is not None:
            session.bridge.set_interactive_owner(None)
        return lease

    def _refresh_interactive_locked(self, session: SessionRuntime) -> InteractiveLease | None:
        lease_id = session.interactive_session_id
        if lease_id is None:
            return None
        lease = self._interactive.get(lease_id)
        if lease is None:
            self._close_interactive_locked(session, interactive_id=lease_id)
            return None
        if session.bridge is None:
            self._close_interactive_locked(session, interactive_id=lease_id)
            return None
        if lease.owner.startswith("human:"):
            client_id = lease.owner.split(":", 1)[1]
            if not session.bridge.console_has_external_peer(client_id):
                session.bridge.detach_console(client_id)
                session.vtty_path = session.bridge.vtty_path
                self._close_interactive_locked(session, interactive_id=lease_id)
                return None
            snapshot = session.bridge.snapshot()
            if snapshot.get("interactive_owner") != lease.owner:
                self._close_interactive_locked(session, interactive_id=lease_id)
                return None
        return lease

    def _transition_to_attached(self, session: SessionRuntime, *, reason: str) -> None:
        notify_not_ready = False
        with self._lock:
            if session.state == "READY":
                notify_not_ready = True
            session.state = "ATTACHED"
            session.last_error = reason
            session.recovering = False
            session.recovery_started_at = None
            session.pending_auto_login = False
        if notify_not_ready:
            self._on_detached(session.session_id)

    def _wait_for_human_interactive_release(
        self,
        session_id: str,
        *,
        timeout_s: float,
    ) -> tuple[bool, str | None]:
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None or session.bridge is None or session.state != "READY":
                    return False, "SESSION_NOT_READY"
                if session.recovering:
                    return False, "SESSION_RECOVERING"
                lease = self._refresh_interactive_locked(session)
                if lease is None:
                    return True, None
                if not lease.owner.startswith("human:"):
                    return False, "SESSION_INTERACTIVE_BUSY"
            time.sleep(0.05)

        return False, "SESSION_INTERACTIVE_BUSY"

    def _spawn_reboot_recovery(self, session_id: str, timeout_s: float) -> None:
        def _run() -> None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is None:
                        return
                    bridge = session.bridge
                    by_id = session.profile.device_by_id
                if bridge is not None:
                    auth = resolve_session_auth(session.profile)
                    ok, err = ensure_ready(bridge, session.profile, auth=auth)
                    if ok:
                        with self._lock:
                            session = self._sessions.get(session_id)
                            if session is None or session.bridge is not bridge:
                                continue
                            session.state = "READY"
                            session.last_error = None
                            session.last_ready_at = now_iso()
                            session.recovering = False
                            session.recovery_started_at = None
                            session.pending_auto_login = False
                        self._on_ready(session_id)
                        return
                    with self._lock:
                        session = self._sessions.get(session_id)
                        if session is None or session.bridge is not bridge:
                            continue
                        session.last_error = err
                elif by_id and by_id in self._devices:
                    self._spawn_attach(by_id)
                time.sleep(1.0)
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None:
                    return
                session.recovering = False
                session.recovery_started_at = None
                session.pending_auto_login = False
                if session.bridge is None:
                    session.state = "DETACHED"
                    session.last_error = "RECOVERY_TIMEOUT"
                else:
                    session.state = "ATTACHED"
                    session.last_error = session.last_error or "RECOVERY_TIMEOUT"

        threading.Thread(target=_run, name=f"serialwrap-reboot-{session_id}", daemon=True).start()

    def _handle_reboot_command(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        *,
        command: str,
        source: str,
        cmd_id: str,
        timeout_s: float,
        execution_mode: str,
    ) -> dict[str, Any]:
        prompt_regex = session.profile.prompt_regex
        pre_offset = bridge.rx_snapshot_len()
        bridge.send_command(command, source=source, cmd_id=cmd_id)
        if bridge.wait_for_regex_from(prompt_regex, pre_offset, min(timeout_s, 2.0)):
            raw_text = bridge.rx_text_from(pre_offset)
            stdout = self._extract_command_stdout(raw_text, command, prompt_regex)
            return {
                "ok": True,
                "execution_mode": execution_mode,
                "stdout": stdout,
                "partial": False,
            }

        if source.startswith("human:"):
            with self._lock:
                lease = self._refresh_interactive_locked(session)
                if lease is None:
                    lease = self._open_interactive_locked(
                        session,
                        owner=source,
                        timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
                    )
                session.state = "ATTACHED"
                session.last_error = "REBOOTING"
                session.recovering = False
                session.recovery_started_at = None
                session.pending_auto_login = False
            self._on_detached(session.session_id)
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "status": "interactive",
                "stdout": "",
                "partial": True,
                "recovery_action": "PROMOTE_HUMAN_INTERACTIVE",
            }

        with self._lock:
            session.pending_auto_login = True
            session.recovering = True
            session.recovery_started_at = now_iso()
            session.state = "RECOVERING"
            session.last_error = None
        self._on_detached(session.session_id)
        self._spawn_reboot_recovery(session.session_id, session.profile.hard_timeout_s)
        return {
            "ok": True,
            "execution_mode": execution_mode,
            "stdout": "",
            "partial": True,
            "status": "recovering",
            "recovery_action": "EXPECT_REBOOT",
        }

    def execute_command(
        self,
        session_id: str,
        command: str,
        source: str,
        cmd_id: str,
        *,
        timeout_s: float = 10.0,
        mode: str = "line",
    ) -> dict[str, Any]:
        normalized_mode = {"fg": "line", "bg": "background"}.get(mode, mode)
        wait_for_human_interactive = False
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease = self._refresh_interactive_locked(session)
            if lease is not None and normalized_mode != "interactive":
                if not source.startswith("human:") and lease.owner.startswith("human:"):
                    wait_for_human_interactive = True
                else:
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex

        if wait_for_human_interactive:
            wait_ok, wait_error = self._wait_for_human_interactive_release(session_id, timeout_s=timeout_s)
            if not wait_ok:
                with self._lock:
                    current = self._sessions.get(session_id)
                    result = {"ok": False, "error_code": wait_error or "SESSION_INTERACTIVE_BUSY"}
                    if current is not None and current.interactive_session_id is not None:
                        result["interactive_session_id"] = current.interactive_session_id
                    return result
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None or session.bridge is None or session.state != "READY":
                    return {"ok": False, "error_code": "SESSION_NOT_READY"}
                if session.recovering:
                    return {"ok": False, "error_code": "SESSION_RECOVERING"}
                lease = self._refresh_interactive_locked(session)
                if lease is not None:
                    return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
                bridge = session.bridge
                prompt_regex = session.profile.prompt_regex

        if normalized_mode == "interactive":
            with self._lock:
                for bg_cmd_id in list(session.background_cmd_ids):
                    capture = self._background.get(bg_cmd_id)
                    if capture is not None:
                        capture.status = "done"
                lease = self._open_interactive_locked(session, owner=source, timeout_s=max(timeout_s, session.profile.hard_timeout_s))
            if command:
                bridge.send_command(command, source=source, cmd_id=cmd_id)
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "stdout": "",
                "status": "interactive",
            }

        session.foreground_busy = True
        if normalized_mode != "background":
            with self._lock:
                for bg_cmd_id in list(session.background_cmd_ids):
                    capture = self._background.get(bg_cmd_id)
                    if capture is not None:
                        capture.status = "done"
        if _is_reboot_command(command):
            try:
                return self._handle_reboot_command(
                    session,
                    bridge,
                    command=command,
                    source=source,
                    cmd_id=cmd_id,
                    timeout_s=timeout_s,
                    execution_mode=normalized_mode,
                )
            finally:
                session.foreground_busy = False
        pre_offset = bridge.rx_snapshot_len()
        try:
            bridge.send_command(command, source=source, cmd_id=cmd_id)
            if not bridge.wait_for_regex_from(prompt_regex, pre_offset, timeout_s):
                return self._recover_after_failure(session, bridge, cmd_id=cmd_id, timeout_s=timeout_s, source=source)
            raw_text = bridge.rx_text_from(pre_offset)
            stdout = self._extract_command_stdout(raw_text, command, prompt_regex)
            result: dict[str, Any] = {
                "ok": True,
                "execution_mode": normalized_mode,
                "stdout": stdout,
                "partial": False,
            }
            if normalized_mode == "background":
                capture = BackgroundCapture(
                    cmd_id=cmd_id,
                    session_id=session.session_id,
                    from_seq=self._wal.current_seq + 1,
                    quiet_window_s=session.profile.quiet_window_s,
                    created_at=now_iso(),
                    last_seq=self._wal.current_seq,
                )
                with self._lock:
                    self._background[cmd_id] = capture
                    session.background_cmd_ids.append(cmd_id)
                result["background_capture_id"] = cmd_id
            return result
        finally:
            session.foreground_busy = False

    def _recover_after_failure(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        *,
        cmd_id: str,
        timeout_s: float,
        source: str,
    ) -> dict[str, Any]:
        if source.startswith("human:"):
            with self._lock:
                lease = self._refresh_interactive_locked(session)
                if lease is None:
                    lease = self._open_interactive_locked(
                        session,
                        owner=source,
                        timeout_s=max(timeout_s, session.profile.hard_timeout_s),
                    )
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "status": "interactive",
                "stdout": "",
                "partial": True,
                "recovery_action": "PROMOTE_HUMAN_INTERACTIVE",
            }

        prompt_regex = session.profile.prompt_regex
        for action_name, payload in (("CTRL_C", b"\x03"), ("CTRL_D", b"\x04")):
            offset = bridge.rx_snapshot_len()
            bridge.send_bytes(payload, source="system:recover", cmd_id=None)
            if bridge.wait_for_regex_from(prompt_regex, offset, min(timeout_s, 2.0)):
                stdout = self._extract_command_stdout(bridge.rx_text_from(offset), "", prompt_regex)
                return {
                    "ok": False,
                    "error_code": "PROMPT_TIMEOUT_RECOVERED",
                    "stdout": stdout,
                    "partial": True,
                    "recovery_action": action_name,
                }

        self._transition_to_attached(session, reason="PROMPT_TIMEOUT")
        return {
            "ok": False,
            "error_code": "PROMPT_TIMEOUT",
            "partial": True,
            "recovery_action": "NONE",
        }

    def get_session_state(self, selector: str) -> dict[str, Any]:
        session = self.get_session(selector)
        if session is None:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
        return {"ok": True, "session": session.to_public_dict()}

    def attach_console(self, selector: str, *, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state not in {"READY", "ATTACHED"}:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            payload = session.bridge.attach_console(label=label)
            if session.vtty_path is None:
                session.vtty_path = payload["vtty"]
            if session.state == "ATTACHED" and self._refresh_interactive_locked(session) is None:
                lease = self._open_interactive_locked(
                    session,
                    owner=f"human:{payload['client_id']}",
                    timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
                )
                payload["interactive_session_id"] = lease.interactive_id
                payload["interactive_owner"] = True
            payload["session"] = session.to_public_dict()
            return {"ok": True, **payload}

    def detach_console(self, selector: str, client_id: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            human_owner = f"human:{client_id}"
            lease = self._refresh_interactive_locked(session)
            ok = session.bridge.detach_console(client_id)
            if lease is not None and lease.owner == human_owner:
                self._close_interactive_locked(session, interactive_id=lease.interactive_id, expected_owner=human_owner)
            session.vtty_path = session.bridge.vtty_path
            if not ok:
                return {"ok": False, "error_code": "CONSOLE_NOT_FOUND", "client_id": client_id}
            return {"ok": True, "client_id": client_id, "session": session.to_public_dict()}

    def list_consoles(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            return {"ok": True, "consoles": session.bridge.list_consoles(), "session": session.to_public_dict()}

    def interactive_open(self, selector: str, *, owner: str = "agent", timeout_s: float = 60.0, command: str = "") -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            if self._refresh_interactive_locked(session) is not None:
                return {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY", "interactive_session_id": session.interactive_session_id}
            lease = self._open_interactive_locked(session, owner=owner, timeout_s=timeout_s)
            bridge = session.bridge
        if command:
            bridge.send_command(command, source=owner, cmd_id=None)
        return {
            "ok": True,
            "interactive_id": lease.interactive_id,
            "session": session.to_public_dict(),
        }

    def _encode_interactive_payload(self, data: str, encoding: str) -> bytes:
        if encoding == "plain":
            return data.encode("utf-8", errors="replace")
        if encoding == "base64":
            return base64.b64decode(data.encode("ascii"))
        key = data.strip().lower()
        key_map = {
            "enter": b"\n",
            "tab": b"\t",
            "escape": b"\x1b",
            "ctrl-c": b"\x03",
            "ctrl-d": b"\x04",
            "up": b"\x1b[A",
            "down": b"\x1b[B",
            "right": b"\x1b[C",
            "left": b"\x1b[D",
        }
        if encoding == "key" and key in key_map:
            return key_map[key]
        raise ValueError("INVALID_INTERACTIVE_ENCODING")

    def interactive_send(self, interactive_id: str, *, data: str, encoding: str = "plain") -> dict[str, Any]:
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None or lease.status != "active":
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.expired():
                lease.status = "expired"
                session = self._sessions.get(lease.session_id)
                if session is not None:
                    self._close_interactive_locked(session, interactive_id=interactive_id)
                return {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
            payload = self._encode_interactive_payload(data, encoding)
            session.bridge.send_bytes(payload, source=lease.owner, cmd_id=None)
            lease.touch()
            return {"ok": True, "interactive_id": interactive_id, "bytes": len(payload)}

    def interactive_status(self, interactive_id: str, *, screen_chars: int = 2048) -> dict[str, Any]:
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
            lease.touch()
            return {
                "ok": True,
                "interactive_id": interactive_id,
                "owner": lease.owner,
                "status": lease.status,
                "screen": clean_text(session.bridge.rx_tail(screen_chars)),
                "session": session.to_public_dict(),
            }

    def interactive_close(self, interactive_id: str) -> dict[str, Any]:
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is not None:
                self._close_interactive_locked(session, interactive_id=interactive_id)
            else:
                self._interactive.pop(interactive_id, None)
            return {"ok": True, "interactive_id": interactive_id}

    def self_test(self, selector: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            device = self._devices.get(session.profile.device_by_id)
            attached_real_path = session.attached_real_path
            bridge = session.bridge
            if session.recovering:
                return {
                    "ok": True,
                    "classification": "SESSION_RECOVERING",
                    "session": session.to_public_dict(),
                    "recommended_action": "wait",
                }
            lease = self._refresh_interactive_locked(session)
            if lease is not None and lease.owner.startswith("human:"):
                return {
                    "ok": True,
                    "classification": "HUMAN_INTERACTIVE_ACTIVE",
                    "interactive_id": lease.interactive_id,
                    "interactive_owner": lease.owner,
                    "session": session.to_public_dict(),
                    "recommended_action": "wait_or_detach_console",
                }
            if device is None:
                return {
                    "ok": True,
                    "classification": "DEVICE_MISSING",
                    "session": session.to_public_dict(),
                    "recommended_action": "check_cable_or_bind",
                }
            if attached_real_path and attached_real_path != device.real_path:
                return {
                    "ok": True,
                    "classification": "DEVICE_REBOUND_REQUIRED",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "recommended_action": "reattach",
                }
            if bridge is None:
                return {
                    "ok": True,
                    "classification": "BRIDGE_DOWN",
                    "session": session.to_public_dict(),
                    "current_real_path": device.real_path,
                    "recommended_action": "attach",
                }
            snapshot = bridge.snapshot()
            if not snapshot.get("running") or not snapshot.get("serial_alive"):
                return {
                    "ok": True,
                    "classification": "BRIDGE_DOWN",
                    "session": session.to_public_dict(),
                    "current_real_path": device.real_path,
                    "recommended_action": "recover",
                }
            if not snapshot.get("vtty_alive"):
                return {
                    "ok": True,
                    "classification": "VTTY_STALE",
                    "session": session.to_public_dict(),
                    "attached_vtty": snapshot.get("vtty"),
                    "recommended_action": "console_attach",
                }
            if session.state == "ATTACHED":
                if session.profile.platform == "passthrough":
                    classification = "PASSTHROUGH"
                    recommended_action = "console_attach"
                elif session.last_error == "LOGIN_REQUIRED":
                    classification = "LOGIN_REQUIRED"
                    recommended_action = "console_attach"
                elif session.last_error == "REBOOTING":
                    classification = "REBOOTING"
                    recommended_action = "wait_or_console_attach"
                else:
                    classification = "ATTACHED_NOT_READY"
                    recommended_action = "console_attach"
                return {
                    "ok": True,
                    "classification": classification,
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "attached_vtty": snapshot.get("vtty"),
                    "bridge_generation": session.bridge_generation,
                    "recommended_action": recommended_action,
                }

            nonce = uuid.uuid4().hex[:8]
            probe = session.profile.ready_probe.replace("${nonce}", nonce)
            offset = bridge.rx_snapshot_len()
            bridge.send_command(probe, source="system:self_test", cmd_id=None)
            if not bridge.wait_for_regex_from(nonce, offset, timeout_s):
                return {
                    "ok": True,
                    "classification": "TARGET_UNRESPONSIVE",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": device.real_path,
                    "probe_ok": False,
                    "recommended_action": "recover",
                }
            bridge.wait_for_regex_from(session.profile.prompt_regex, offset, timeout_s)
            return {
                "ok": True,
                "classification": "OK",
                "session": session.to_public_dict(),
                "attached_real_path": attached_real_path,
                "current_real_path": device.real_path,
                "attached_vtty": snapshot.get("vtty"),
                "bridge_generation": session.bridge_generation,
                "probe_ok": True,
                "recommended_action": "none",
            }

    def recover_session(self, selector: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.bridge is None:
                by_id = session.profile.device_by_id
                if by_id and by_id in self._devices:
                    session.state = "ATTACHING"
                    session.last_error = None
                    self._spawn_attach(by_id)
                    return {"ok": True, "recovering": False, "action": "REATTACH", "session": session.to_public_dict()}
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            if session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            bridge = session.bridge
        return self._recover_after_failure(session, bridge, cmd_id="", timeout_s=timeout_s, source="system:recover")

    def get_background_result(self, cmd_id: str, *, from_chunk: int = 0, limit: int = 200) -> dict[str, Any]:
        with self._lock:
            capture = self._background.get(cmd_id)
            if capture is None:
                return {"ok": False, "error_code": "CMD_NOT_FOUND", "cmd_id": cmd_id}
            capture.maybe_finalize()
            chunks = capture.chunks[from_chunk : from_chunk + limit]
            next_chunk = from_chunk + len(chunks)
            return {
                "ok": True,
                "cmd_id": cmd_id,
                "status": capture.status,
                "from_seq": capture.from_seq,
                "last_seq": capture.last_seq,
                "from_chunk": from_chunk,
                "next_chunk": next_chunk,
                "chunks": chunks,
            }
