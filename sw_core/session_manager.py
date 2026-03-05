from __future__ import annotations

import dataclasses
import json
import os
import threading
from typing import Any, Callable

from .alias_registry import AliasRegistry
from .config import SessionProfile
from .constants import STATE_PATH
from .device_watcher import DeviceInfo
from .login_fsm import ensure_ready
from .uart_io import UARTBridge
from .util import now_iso
from .wal import WalWriter


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

    def to_public_dict(self) -> dict[str, Any]:
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
        }


class SessionManager:
    def __init__(
        self,
        profiles: list[SessionProfile],
        wal: WalWriter,
        *,
        on_ready: Callable[[str], None],
        on_detached: Callable[[str], None],
    ) -> None:
        self._wal = wal
        self._on_ready = on_ready
        self._on_detached = on_detached
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRuntime] = {}
        self._aliases = AliasRegistry()
        self._devices: dict[str, DeviceInfo] = {}
        self._binding_overrides: dict[str, str] = {}
        self._attach_inflight: set[str] = set()

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
                if not isinstance(sid, str):
                    continue
                if not isinstance(by_id, str):
                    continue
                sid = sid.strip()
                by_id = by_id.strip()
                if not sid or not by_id:
                    continue
                normalized[sid] = by_id
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

    def clear_session(self, selector: str) -> dict[str, Any]:
        """Clear runtime state but keep session registration/binding."""
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}

            if session.bridge is not None:
                session.bridge.stop()
                session.bridge = None
                self._on_detached(session.session_id)

            session.vtty_path = None
            session.detached_at = now_iso()

            by_id = session.profile.device_by_id
            has_device = bool(by_id and by_id in self._devices)
            if has_device:
                session.state = "ATTACHING"
                session.last_error = None
            else:
                session.state = "DETACHED"
                session.last_error = "CLEARED"

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
                if other.session_id == session.session_id:
                    continue
                if other.profile.device_by_id == device_by_id:
                    return {
                        "ok": False,
                        "error_code": "DEVICE_ALREADY_BOUND",
                        "device_by_id": device_by_id,
                        "session_id": other.session_id,
                    }

            if session.bridge is not None:
                session.bridge.stop()
                session.bridge = None
                session.vtty_path = None
                session.state = "DETACHED"
                session.last_error = "REBOUND"
                session.detached_at = now_iso()
                self._on_detached(session.session_id)

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
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}

            by_id = session.profile.device_by_id
            if not by_id:
                return {"ok": False, "error_code": "DEVICE_NOT_BOUND", "session": session.to_public_dict()}

            if session.bridge is not None:
                session.bridge.stop()
                session.bridge = None
                session.vtty_path = None
                session.state = "DETACHED"
                session.last_error = "MANUAL_ATTACH"
                session.detached_at = now_iso()
                self._on_detached(session.session_id)

            if by_id not in self._devices:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                return {"ok": False, "error_code": "DEVICE_NOT_FOUND", "session": session.to_public_dict()}

            session.state = "ATTACHING"
            session.last_error = None

        self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for by_id, dev in sorted(self._devices.items()):
                out.append({"by_id": by_id, "real_path": dev.real_path})
            return out

    def update_devices(self, devices: dict[str, DeviceInfo]) -> None:
        with self._lock:
            prev = self._devices
            self._devices = dict(devices)

        removed = sorted(set(prev.keys()) - set(devices.keys()))
        added = sorted(set(devices.keys()) - set(prev.keys()))

        for by_id in removed:
            self._detach_by_id(by_id)
        for by_id in added:
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

        th = threading.Thread(target=_run, name=f"serialwrap-attach-{by_id}", daemon=True)
        th.start()

    def _detach_by_id(self, by_id: str) -> None:
        with self._lock:
            targets = [s for s in self._sessions.values() if s.profile.device_by_id == by_id]
        for session in targets:
            if session.bridge is not None:
                session.bridge.stop()
                session.bridge = None
            session.vtty_path = None
            session.state = "DETACHED"
            session.detached_at = now_iso()
            session.last_error = "DEVICE_REMOVED"
            self._on_detached(session.session_id)

    def _attach_by_id(self, by_id: str) -> None:
        save_needed = False
        with self._lock:
            session = next((s for s in self._sessions.values() if s.profile.device_by_id == by_id), None)
            # 無精確匹配 → 嘗試自動綁定到 act_no 最小、device_by_id 無效的 DETACHED session
            if session is None:
                candidates = sorted(
                    [
                        s for s in self._sessions.values()
                        if s.state == "DETACHED" and s.profile.device_by_id not in self._devices
                    ],
                    key=lambda s: s.profile.act_no,
                )
                if candidates:
                    session = candidates[0]
                    session.profile = dataclasses.replace(session.profile, device_by_id=by_id)
                    self._binding_overrides[session.session_id] = by_id
                    save_needed = True
            dev = self._devices.get(by_id)
        if save_needed:
            self._save_state()
        if session is None or dev is None:
            return
        if session.bridge is not None:
            return

        bridge = UARTBridge(session.profile.com, dev.real_path, session.profile.uart, self._wal)
        session.state = "ATTACHING"
        session.last_error = None

        try:
            bridge.start()
            ok, err = ensure_ready(bridge, session.profile)
            if not ok:
                bridge.stop()
                session.state = "DETACHED"
                session.last_error = err
                session.detached_at = now_iso()
                session.bridge = None
                session.vtty_path = None
                self._on_detached(session.session_id)
                return

            # 持鎖重新驗證裝置是否仍存在，避免與 _detach_by_id 競態
            with self._lock:
                if by_id not in self._devices or session.state == "DETACHED":
                    bridge.stop()
                    session.state = "DETACHED"
                    session.last_error = "DEVICE_REMOVED_DURING_ATTACH"
                    session.detached_at = now_iso()
                    session.bridge = None
                    session.vtty_path = None
                    return

                session.bridge = bridge
                session.vtty_path = bridge.vtty_path
                session.state = "READY"
                session.last_error = None
                session.last_ready_at = now_iso()
            self._on_ready(session.session_id)
        except Exception as exc:
            try:
                bridge.stop()
            except Exception:
                pass
            session.state = "DETACHED"
            session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
            session.detached_at = now_iso()
            session.bridge = None
            session.vtty_path = None
            self._on_detached(session.session_id)

    def send_command(self, session_id: str, command: str, source: str, cmd_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.bridge is None or session.state != "READY":
                raise RuntimeError("SESSION_NOT_READY")
            bridge = session.bridge
        bridge.send_command(command, source=source, cmd_id=cmd_id)

    def execute_command(self, session_id: str, command: str, source: str, cmd_id: str, *, timeout_s: float = 10.0) -> None:
        """送出命令並等待 target prompt 回應，期間暫緩 minicom TX（結束後排程送出）。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.bridge is None or session.state != "READY":
                raise RuntimeError("SESSION_NOT_READY")
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
        bridge.begin_agent_cmd()
        pre_offset = bridge.rx_snapshot_len()
        try:
            bridge.send_command(command, source=source, cmd_id=cmd_id)
            if not bridge.wait_for_regex_from(prompt_regex, pre_offset, timeout_s):
                raise RuntimeError("PROMPT_TIMEOUT")
        except Exception:
            self._recover_prompt_after_failure(bridge, prompt_regex)
            raise
        finally:
            bridge.end_agent_cmd()

    @staticmethod
    def _recover_prompt_after_failure(bridge: UARTBridge, prompt_regex: str) -> None:
        """Best-effort recovery to avoid leaving shell at continuation prompt."""
        try:
            offset = bridge.rx_snapshot_len()
            bridge.send_command("\x03", source="agent:recover", cmd_id=None)
            bridge.wait_for_regex_from(prompt_regex, offset, timeout_s=2.0)
        except Exception:
            pass

    def get_session_state(self, selector: str) -> dict[str, Any]:
        session = self.get_session(selector)
        if session is None:
            return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
        return {"ok": True, "session": session.to_public_dict()}
