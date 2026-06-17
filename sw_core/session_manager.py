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
from .config import ProfileTemplate, SessionProfile
from .constants import (
    BOOTLOADER_RX_TAIL_BYTES,
    HUMAN_ACTIVE_WINDOW_S,
    LOG_DIR,
    MAX_RECOVERY_LEASE_S,
    STATE_PATH,
)
from .device_watcher import DeviceInfo
from .login_fsm import detect_template, ensure_ready, probe_ready
from .uart_io import PreservedConsoles, UARTBridge
from .util import clean_text, now_iso
from .wal import WalWriter


_ATTACHED_CONSOLE_LEASE_TIMEOUT_S = 86400.0


def _matches_any_bootloader_prompt(
    rx_tail: str,
    patterns: "list[str] | tuple[str, ...]",
) -> "str | None":
    """rx_tail 的最後一個非空／非純空白行是否符合 patterns 中任一 regex。

    回傳第一個命中的 pattern 字串；無命中、空 rx_tail、空 patterns 均返回 None。
    invalid regex 不拋出例外，直接略過該 pattern。
    """
    if not rx_tail or not patterns:
        return None
    # 從尾端找最後一個非空白行
    lines = rx_tail.splitlines()
    last_line: str | None = None
    for line in reversed(lines):
        if line.strip():
            last_line = line
            break
    if last_line is None:
        return None
    for pattern in patterns:
        try:
            if re.search(pattern, last_line) is not None:
                return pattern
        except re.error:
            # invalid regex → 略過
            continue
    return None


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
    error_code: str | None = None
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
    recovery_mode: bool = False
    # 內部 lifecycle flag：recovery lease 開啟前是否已 suspend 人類 console lease；不透出 RPC。
    suspended_human: bool = False

    def touch(self) -> None:
        self.last_activity_at = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.last_activity_at > self.timeout_s


@dataclasses.dataclass
class SessionCapture:
    capture_id: str
    session_id: str
    log_path: str
    started_at: str
    line_count: int = 0
    byte_count: int = 0
    status: str = "active"


@dataclasses.dataclass
class _PostCloseAction:
    """lock 外需要執行的 bridge 操作，由 _close_interactive_locked 回傳。

    呼叫者在釋放 _lock 後執行 execute()。
    """
    bridge: "UARTBridge | None" = None
    needs_resume: bool = False
    clear_owner_after_resume: bool = False

    def execute(self) -> None:
        if self.bridge is None or not self.needs_resume:
            return
        self.bridge.resume_interactive()
        if self.clear_owner_after_resume:
            self.bridge.set_interactive_owner(None)


@dataclasses.dataclass
class SessionRuntime:
    session_id: str
    profile: SessionProfile
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
    active_capture: SessionCapture | None = None
    retained_consoles: PreservedConsoles | None = None
    retained_human_owner: str | None = None
    retained_human_timeout_s: float | None = None
    fg_cmd_started_mono: float | None = None
    fg_cmd_expected_duration_s: float | None = None
    # Activity tracking (issue #34)
    last_state_change_at: str | None = None
    last_rx_at: str | None = None
    last_tx_at: str | None = None
    last_probe_at: str | None = None
    last_rx_mono: float = 0.0
    last_tx_mono: float = 0.0
    # device handoff（issue #54）
    released_by: str | None = None
    released_at: str | None = None
    released_reason: str | None = None
    # MCU 燒錄狀態（issue #55）：僅 runtime transient，不寫 _save_state / to_public_dict
    flash_prev_state: str | None = None
    # recovery lease stash（Phase B issue #44）
    _stashed_human_lease: InteractiveLease | None = dataclasses.field(default=None, repr=False)
    _state: str = dataclasses.field(default="DETACHED", init=False, repr=False)

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        prev = getattr(self, "_state", None)
        if prev is not None and prev != value:
            self.last_state_change_at = now_iso()
        self._state = value

    def compute_idle_ms(self) -> int | None:
        last = max(self.last_rx_mono, self.last_tx_mono)
        if last == 0.0:
            return None
        return int((time.monotonic() - last) * 1000)

    def classify_activity(self) -> str:
        if self._state not in ("READY", "ATTACHED"):
            return "offline"
        idle = self.compute_idle_ms()
        if idle is None:
            return "newly-attached"
        if idle < 5_000:
            return "active"
        if idle < 60_000:
            return "idle-healthy"
        return "quiet-suspicious"

    def to_public_dict(self) -> dict[str, Any]:
        console_count = 0
        if self.bridge is not None:
            console_count = len(self.bridge.list_consoles())
        elif self.retained_consoles is not None:
            console_count = len(self.retained_consoles.clients)
        vtty_path = self.vtty_path
        if vtty_path is None and self.retained_consoles is not None:
            vtty_path = self.retained_consoles.primary_vtty()
        outstanding = len(self.background_cmd_ids) + (1 if self.foreground_busy else 0)
        return {
            "session_id": self.session_id,
            "profile": self.profile.profile_name,
            "com": self.profile.com,
            "alias": self.profile.alias,
            "act_no": self.profile.act_no,
            "device_by_id": self.profile.device_by_id,
            "platform": self.profile.platform,
            "command_capable": self.profile.command_capable,
            "state": self.state,
            "last_error": self.last_error,
            "detached_at": self.detached_at,
            "last_ready_at": self.last_ready_at,
            "vtty": vtty_path,
            "attached_real_path": self.attached_real_path,
            "bridge_generation": self.bridge_generation,
            "recovering": self.recovering,
            "interactive_session_id": self.interactive_session_id,
            "foreground_busy": self.foreground_busy,
            "fg_cmd_expected_duration_s": self.fg_cmd_expected_duration_s,
            "console_count": console_count,
            "last_state_change_at": self.last_state_change_at,
            "last_rx_at": self.last_rx_at,
            "last_tx_at": self.last_tx_at,
            "last_probe_at": self.last_probe_at,
            "idle_for_ms": self.compute_idle_ms(),
            "outstanding_commands": outstanding,
            "activity_classification": self.classify_activity(),
            "released_by": self.released_by,
            "released_at": self.released_at,
            "released_reason": self.released_reason,
            "capture": {
                "capture_id": self.active_capture.capture_id,
                "log_path": self.active_capture.log_path,
                "status": self.active_capture.status,
                "line_count": self.active_capture.line_count,
                "byte_count": self.active_capture.byte_count,
            } if self.active_capture else None,
        }


class SessionManager:
    def __init__(
        self,
        profiles: list[SessionProfile],
        wal: WalWriter,
        *,
        templates: list[ProfileTemplate] | None = None,
        max_sessions: int = 16,
        on_ready: Callable[[str], None],
        on_detached: Callable[[str], None],
        on_console_line: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._wal = wal
        self._on_ready = on_ready
        self._on_detached = on_detached
        self._on_console_line = on_console_line
        self._lock = threading.RLock()
        self._rx_observers: list[Callable[[str, bytes, int], None]] = []
        self._sessions: dict[str, SessionRuntime] = {}
        self._aliases = AliasRegistry()
        self._devices: dict[str, DeviceInfo] = {}
        self._binding_overrides: dict[str, str] = {}
        self._attach_inflight: set[str] = set()
        self._released_by_ids: set[str] = set()
        self._loaded_released: dict[str, dict[str, str | None]] = {}
        self._background: dict[str, BackgroundCapture] = {}
        self._interactive: dict[str, InteractiveLease] = {}
        self._capture_fps: dict[str, Any] = {}  # capture_id → open file object
        self._templates: list[ProfileTemplate] = list(templates) if templates else []
        self._max_sessions = max_sessions

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
        for sid, meta in self._loaded_released.items():
            s = self._sessions.get(sid)
            if s is not None:
                s.state = "RELEASED"
                s.released_by = meta.get("released_by")
                s.released_at = meta.get("released_at")
                s.released_reason = meta.get("reason")
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
        released = obj.get("released") if isinstance(obj, dict) else None
        if isinstance(released, dict):
            loaded: dict[str, dict[str, str | None]] = {}
            for sid, meta in released.items():
                if not isinstance(sid, str) or not isinstance(meta, dict):
                    continue
                by_id = meta.get("by_id")
                loaded[sid] = {
                    "by_id": by_id,
                    "released_by": meta.get("released_by"),
                    "released_at": meta.get("released_at"),
                    "reason": meta.get("reason"),
                }
                if isinstance(by_id, str) and by_id:
                    self._released_by_ids.add(by_id)
            self._loaded_released = loaded

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        released: dict[str, dict[str, str | None]] = {}
        for sid, s in self._sessions.items():
            if s.state == "RELEASED":
                released[sid] = {
                    "by_id": s.profile.device_by_id,
                    "released_by": s.released_by,
                    "released_at": s.released_at,
                    "reason": s.released_reason,
                }
        with open(STATE_PATH, "w", encoding="utf-8") as fp:
            json.dump(
                {"aliases": self._aliases.dump(), "bindings": dict(self._binding_overrides), "released": released},
                fp,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            fp.write("\n")

    def _next_dynamic_com(self) -> str:
        """分配下一個可用的 COM 編號（須在 self._lock 內呼叫）。"""
        used = {s.profile.com for s in self._sessions.values()}
        for i in range(self._max_sessions):
            com = f"COM{i}"
            if com not in used:
                return com
        return f"COM{len(self._sessions)}"

    def _session_from_template(
        self,
        tpl: ProfileTemplate,
        device_by_id: str,
    ) -> SessionRuntime:
        """從 template 建立新的動態 session（須在 self._lock 內呼叫）。"""
        com = self._next_dynamic_com()
        act_no = len(self._sessions) + 1
        alias = f"{tpl.profile_name}+{act_no}"
        profile = SessionProfile(
            profile_name=tpl.profile_name,
            com=com,
            act_no=act_no,
            alias=alias,
            device_by_id=device_by_id,
            platform=tpl.platform,
            prompt_regex=tpl.prompt_regex,
            login_regex=tpl.login_regex,
            password_regex=tpl.password_regex,
            post_login_cmd=tpl.post_login_cmd,
            ready_probe=tpl.ready_probe,
            username=tpl.username,
            user_env=tpl.user_env,
            pass_env=tpl.pass_env,
            env_file=tpl.env_file,
            timeout_s=tpl.timeout_s,
            quiet_window_s=tpl.quiet_window_s,
            hard_timeout_s=tpl.hard_timeout_s,
            log_dir=tpl.log_dir,
            bootloader_prompts=tpl.bootloader_prompts,
            uart=tpl.uart,
        )
        sid = f"{profile.profile_name}:{com}"
        session = SessionRuntime(session_id=sid, profile=profile)
        self._sessions[sid] = session
        self._aliases.set_for_session(sid, alias)
        return session

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

    def active_cmd_id_for(self, com: str) -> str | None:
        with self._lock:
            session = next(
                (s for s in self._sessions.values() if s.profile.com == com), None
            )
            if session is None:
                return None
            return "foreground" if session.foreground_busy else None

    def profile_for(self, com: str) -> str | None:
        with self._lock:
            session = next(
                (s for s in self._sessions.values() if s.profile.com == com), None
            )
            if session is None:
                return None
            return session.profile.profile_name

    def known_coms(self) -> list[str]:
        with self._lock:
            return sorted(s.profile.com for s in self._sessions.values())

    def add_rx_observer(self, observer: Callable[[str, bytes, int], None]) -> None:
        with self._lock:
            self._rx_observers.append(observer)

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

    def _store_retained_consoles_locked(
        self,
        session: SessionRuntime,
        preserved: PreservedConsoles | None,
        *,
        human_owner: str | None = None,
        human_timeout_s: float | None = None,
    ) -> None:
        if not isinstance(preserved, PreservedConsoles) or not preserved.clients:
            session.retained_consoles = None
            session.retained_human_owner = None
            session.retained_human_timeout_s = None
            session.vtty_path = None
            return
        session.retained_consoles = preserved
        session.vtty_path = preserved.primary_vtty()
        session.retained_human_owner = None
        session.retained_human_timeout_s = None
        if human_owner is None or not human_owner.startswith("human:"):
            return
        client_id = human_owner.split(":", 1)[1]
        if client_id and preserved.has_client(client_id):
            session.retained_human_owner = human_owner
            session.retained_human_timeout_s = human_timeout_s

    def _restore_retained_human_console_locked(self, session: SessionRuntime) -> None:
        owner = session.retained_human_owner
        timeout_s = session.retained_human_timeout_s
        session.retained_human_owner = None
        session.retained_human_timeout_s = None
        if owner is None or session.bridge is None or not owner.startswith("human:"):
            return
        client_id = owner.split(":", 1)[1]
        if not client_id or not session.bridge.has_console(client_id):
            return
        lease, _ = self._refresh_interactive_locked(session)
        if lease is None:
            self._open_interactive_locked(
                session,
                owner=owner,
                timeout_s=timeout_s or max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
            )

    def _detach_session_locked(self, session: SessionRuntime, *, reason: str, drop_consoles: bool = False) -> None:
        preserved = session.retained_consoles
        retained_human_owner = session.retained_human_owner
        retained_human_timeout_s = session.retained_human_timeout_s
        if session.interactive_session_id is not None:
            lease = self._interactive.get(session.interactive_session_id)
            if lease is not None and lease.owner.startswith("human:"):
                retained_human_owner = lease.owner
                retained_human_timeout_s = lease.timeout_s
        if session.bridge is not None:
            preserved = session.bridge.stop(preserve_consoles=not drop_consoles)
            session.bridge = None
        if drop_consoles:
            preserved = None
            retained_human_owner = None
            retained_human_timeout_s = None
            session.retained_consoles = None
        self._store_retained_consoles_locked(
            session,
            preserved,
            human_owner=retained_human_owner,
            human_timeout_s=retained_human_timeout_s,
        )
        session.attached_real_path = None
        session.state = "DETACHED"
        session.detached_at = now_iso()
        session.last_error = reason
        if session.interactive_session_id is not None:
            lease = self._interactive.pop(session.interactive_session_id, None)
            if lease is not None:
                lease.status = "closed"
        session.interactive_session_id = None
        session._stashed_human_lease = None  # 清除 recovery lease stash，避免跨 bridge 殘留
        session.foreground_busy = False
        self._stop_capture_locked(session)
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
            if session.state == "RELEASED" or session.profile.device_by_id in self._released_by_ids:
                return {"ok": True, "released": True, "session": session.to_public_dict()}
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

    def release_device(self, selector: str, *, source: str = "cli", reason: str | None = None) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state == "RELEASED":
                return {"ok": True, "already_released": True, "session": session.to_public_dict()}
            by_id = session.profile.device_by_id
            closed_consoles = len(session.bridge.list_consoles()) if session.bridge is not None else 0
            aborted_cmd = session.foreground_busy
            self._detach_session_locked(session, reason="RELEASED", drop_consoles=True)
            session.state = "RELEASED"
            session.released_by = source
            session.released_at = now_iso()
            session.released_reason = reason
            if by_id:
                self._released_by_ids.add(by_id)
            public = session.to_public_dict()
        self._save_state()
        return {"ok": True, "session": public, "closed_consoles": closed_consoles, "aborted_cmd": aborted_cmd}

    def enter_flashing(self, selector: str) -> dict:
        """進入 FLASHING：只標狀態 + 擋命令，**不** detach bridge（daemon 仍是 real device 唯一 reader）。"""
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state == "FLASHING":
                return {"ok": True, "already_flashing": True, "session": session.to_public_dict()}
            session.flash_prev_state = session.state   # 記住以便結束後恢復
            session.state = "FLASHING"
            public = session.to_public_dict()
        return {"ok": True, "session": public}

    def exit_flashing(self, selector: str) -> dict:
        """結束 FLASHING：恢復先前狀態（bridge 全程未關，無需 re-attach）。"""
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.state != "FLASHING":
                return {"ok": True, "not_flashing": True, "session": session.to_public_dict()}
            session.state = session.flash_prev_state or ("READY" if session.bridge is not None else "DETACHED")
            session.flash_prev_state = None
            public = session.to_public_dict()
        return {"ok": True, "session": public}

    def _probe_external_holder(self, real_path: str, *, _proc_root: str = "/proc") -> dict[str, Any]:
        """唯讀偵測 real_path 是否被其他 process 持有；讀 _proc_root/*/fd，不開 tty、不做 I/O。"""
        my_pid = os.getpid()
        try:
            target = os.path.realpath(real_path)
        except OSError:
            target = real_path
        try:
            target_rdev = os.stat(real_path).st_rdev
        except OSError:
            target_rdev = 0
        holders: set[int] = set()
        try:
            entries = os.listdir(_proc_root)
        except OSError:
            return {"pids": [], "holder": None}
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            fd_dir = os.path.join(_proc_root, entry, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                fd_path = os.path.join(fd_dir, fd)
                try:
                    link = os.readlink(fd_path)
                except OSError:
                    continue
                matched = link == target or link == real_path
                # 即使外部以 by-id / 其他 symlink 開啟，也以 device number(st_rdev)
                # 比對同一個 char device，避免漏判導致 attach 誤判可收回、重回 two-reader race。
                if not matched and target_rdev:
                    try:
                        if os.stat(fd_path).st_rdev == target_rdev:
                            matched = True
                    except OSError:
                        pass
                if matched:
                    holders.add(pid)
                    break
        ordered = sorted(holders)
        return {"pids": ordered, "holder": (ordered[0] if ordered else None)}

    def attach_device(self, selector: str, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            # 冪等：session 已 attached（有 bridge）且非 RELEASED → 直接回覆，不要改 state。
            # 否則會被設成 ATTACHING，但 _attach_by_id 因 bridge 已存在而早退，卡在 ATTACHING。
            if session.bridge is not None and session.state != "RELEASED" and by_id not in self._released_by_ids:
                return {"ok": True, "already_attached": True, "session": session.to_public_dict()}
            if not by_id or by_id not in self._devices:
                return {"ok": False, "error_code": "DEVICE_NOT_PRESENT", "selector": selector, "device_by_id": by_id}
            real_path = self._devices[by_id].real_path
        if not force:
            holder = self._probe_external_holder(real_path)
            if holder["pids"]:
                return {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": holder["pids"], "selector": selector}
        with self._lock:
            self._released_by_ids.discard(by_id)
            session.released_by = None
            session.released_at = None
            session.released_reason = None
            session.state = "ATTACHING"
            session.last_error = None
            public = session.to_public_dict()
        self._save_state()
        self._spawn_attach(by_id)
        return {"ok": True, "session": public}

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
            # 冪等：已綁定同 device 且 bridge 存在且 READY/ATTACHED → 不 detach
            if (
                session.bridge is not None
                and session.profile.device_by_id == device_by_id
                and session.state in ("READY", "ATTACHED")
            ):
                return {"ok": True, "already_bound": True, "session": session.to_public_dict()}
            if session.bridge is not None:
                self._detach_session_locked(session, reason="REBOUND")
            # I1：對 RELEASED session 重綁新 by_id 屬明確覆寫（離開 RELEASED）——
            # 須把舊 by_id 移出 _released_by_ids、清 provenance，並把 state 移出 RELEASED，
            # 否則下方 _save_state 會以新 by_id、released_by=None 寫入半殘 released entry，
            # 導致 daemon 重啟後 session 被復活成 RELEASED、永遠無法 attach。
            if session.state == "RELEASED":
                old_by_id = session.profile.device_by_id
                if old_by_id:
                    self._released_by_ids.discard(old_by_id)
                session.released_by = None
                session.released_at = None
                session.released_reason = None
                session.state = "DETACHED"
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
        post = _PostCloseAction()
        result: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            by_id = session.profile.device_by_id
            if not by_id:
                return {"ok": False, "error_code": "DEVICE_NOT_BOUND", "session": session.to_public_dict()}
            # C2：RELEASED 早退——比照 clear_session，不改 state、不 spawn、不動集合，
            # 避免卡死 ATTACHING 與下一次 _save_state 把 released map 寫空。
            if session.state == "RELEASED" or by_id in self._released_by_ids:
                return {
                    "ok": True,
                    "released": True,
                    "recommended_action": "device_attach",
                    "session": session.to_public_dict(),
                }
            if session.bridge is not None:
                lease, post = self._refresh_interactive_locked(session)
                if lease is not None and lease.owner.startswith("human:"):
                    result = {"ok": True, "session": session.to_public_dict()}
                elif session.state == "ATTACHED":
                    bridge = session.bridge
                    should_probe = True
                else:
                    result = {"ok": True, "session": session.to_public_dict()}
            if result is None and by_id not in self._devices:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                result = {"ok": False, "error_code": "DEVICE_NOT_FOUND", "session": session.to_public_dict()}
            if result is None and not should_probe:
                session.state = "ATTACHING"
                session.last_error = None
        post.execute()
        if result is not None:
            return result
        if should_probe and bridge is not None:
            return self._probe_existing_bridge(session, bridge)
        self._spawn_attach(by_id)
        return {"ok": True, "session": session.to_public_dict()}

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"by_id": by_id, "real_path": dev.real_path} for by_id, dev in sorted(self._devices.items())]

    def _mark_missing_devices_locked(self) -> None:
        missing_at = now_iso()
        for session in self._sessions.values():
            by_id = session.profile.device_by_id
            if not by_id or session.bridge is not None:
                continue
            if by_id in self._devices:
                continue
            session.state = "DETACHED"
            if session.last_error is None:
                session.last_error = "DEVICE_NOT_FOUND"
            if session.detached_at is None:
                session.detached_at = missing_at

    def update_devices(self, devices: dict[str, DeviceInfo]) -> None:
        with self._lock:
            prev = self._devices
            self._devices = dict(devices)
            self._mark_missing_devices_locked()

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
            if by_id in self._released_by_ids:
                return
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
            targets = [
                s for s in self._sessions.values()
                if s.profile.device_by_id == by_id and s.state != "RELEASED"
            ]
            for session in targets:
                self._detach_session_locked(session, reason=reason)

    def _mark_session_rx(self, session: SessionRuntime) -> None:
        """Update session's last_rx_at / last_rx_mono. Cheap; safe outside lock."""
        session.last_rx_at = now_iso()
        session.last_rx_mono = time.monotonic()

    def _mark_session_tx(self, session: SessionRuntime) -> None:
        """Update session's last_tx_at / last_tx_mono. Cheap; safe outside lock."""
        session.last_tx_at = now_iso()
        session.last_tx_mono = time.monotonic()

    def _on_bridge_console_line(self, session_id: str, client_id: str, line: str) -> None:
        if self._on_console_line is not None:
            self._on_console_line(session_id, client_id, line)

    def _on_bridge_rx(self, session_id: str, data: bytes) -> None:
        # Notify RX observers before foreground_busy gate (observers receive ALL rx).
        # IMPORTANT: Observer MUST NOT block on I/O or cross-thread wait, and MUST NOT
        # call SessionManager methods under _lock (deadlock risk). Exceptions are silently swallowed.
        if self._rx_observers:
            with self._lock:
                session = self._sessions.get(session_id)
                com = session.profile.com if session is not None else session_id
                wal_seq = self._wal.current_seq
                observers = list(self._rx_observers)
            for obs in observers:
                try:
                    obs(com, data, wal_seq)
                except Exception:
                    pass
        chunk = clean_text(data.decode("utf-8", errors="replace"))
        if not chunk:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            self._mark_session_rx(session)
            if session.foreground_busy:
                return
            # agent log capture
            cap = session.active_capture
            if cap is not None and cap.status == "active":
                fp = self._capture_fps.get(cap.capture_id)
                if fp is not None:
                    try:
                        fp.write(chunk)
                        fp.flush()
                        cap.byte_count += len(chunk.encode("utf-8"))
                        cap.line_count += chunk.count("\n")
                    except Exception:
                        pass
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

    def _probe_existing_bridge(self, session: SessionRuntime, bridge: UARTBridge) -> dict[str, Any]:
        # 非 command-capable（無 ready_probe，含 passthrough）維持現狀不升 READY。
        if not session.profile.command_capable:
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            return {"ok": True, "session": current.to_public_dict()}

        if session.profile.login_regex:
            auth = resolve_session_auth(session.profile)
            if auth.username and auth.password:
                ok, err = ensure_ready(bridge, session.profile, auth=auth)
            else:
                ok, err = probe_ready(bridge, session.profile)
        else:
            ok, err = probe_ready(bridge, session.profile)

        notify_ready = False
        with self._lock:
            current = self._sessions.get(session.session_id)
            if current is None or current.bridge is not bridge:
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            current.recovering = False
            current.recovery_started_at = None
            current.pending_auto_login = False
            if ok:
                current.state = "READY"
                current.last_error = None
                current.last_ready_at = now_iso()
                notify_ready = True
            else:
                current.state = "ATTACHED"
                current.last_error = err
            result = current.to_public_dict()
        if notify_ready:
            self._on_ready(session.session_id)
        return {"ok": True, "session": result}

    def _attach_by_id(self, by_id: str) -> None:
        save_needed = False
        dynamic_created = False
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
            # 若仍無匹配且有 templates → 嘗試動態偵測
            if session is None and self._templates:
                if len(self._sessions) >= self._max_sessions:
                    import logging
                    logging.getLogger("serialwrap").warning(
                        "已達 max_sessions=%d，忽略裝置 %s", self._max_sessions, by_id,
                    )
                    return
                dynamic_created = True
        if save_needed:
            self._save_state()

        # --- 動態偵測路徑 ---
        if dynamic_created:
            self._attach_by_id_dynamic(by_id)
            return

        with self._lock:
            if session is None:
                return
            dev = self._devices.get(by_id)
            if dev is None:
                session.state = "DETACHED"
                session.last_error = "DEVICE_NOT_FOUND"
                session.detached_at = now_iso()
                return
            if session.bridge is not None or session.profile.device_by_id != by_id:
                return
            # C1 早退：release 早於 attach 開 FD 時，根本不啟動（常見情形不開 FD）。
            if session.state == "RELEASED" or by_id in self._released_by_ids:
                return
            session.state = "ATTACHING"
            session.last_error = None
            gen_before = session.bridge_generation
            session_id = session.session_id
            profile = session.profile
            require_login = session.pending_auto_login or bool(profile.login_regex)
            # 以 ready_probe 判定可否下命令；非 command-capable（含無 ready_probe 的
            # passthrough）僅停在 ATTACHED 不 probe，有 ready_probe 的 passthrough
            # 也要能走 probe 進 READY。
            command_capable = profile.command_capable
            real_path = dev.real_path
            preserved_consoles = session.retained_consoles if isinstance(session.retained_consoles, PreservedConsoles) else None

        bridge = UARTBridge(
            profile.com,
            real_path,
            profile.uart,
            self._wal,
            on_console_line=lambda client_id, line, sid=session_id: self._on_bridge_console_line(sid, client_id, line),
            on_rx_data=lambda data, sid=session_id: self._on_bridge_rx(sid, data),
            on_bridge_down=lambda reason, sid=session_id: self._handle_bridge_down(sid, bridge, reason),
            preserved_consoles=preserved_consoles,
        )

        try:
            bridge.start()
            if not command_capable:
                ok = False
                err = None
            elif require_login:
                auth = resolve_session_auth(profile)
                if auth.username and auth.password:
                    ok, err = ensure_ready(bridge, profile, auth=auth)
                    if not ok:
                        preserved = bridge.stop(preserve_consoles=True)
                        with self._lock:
                            self._store_retained_consoles_locked(
                                session,
                                preserved,
                                human_owner=session.retained_human_owner,
                                human_timeout_s=session.retained_human_timeout_s,
                            )
                            session.state = "DETACHED"
                            session.last_error = err
                            session.detached_at = now_iso()
                            session.bridge = None
                            session.attached_real_path = None
                        self._on_detached(session.session_id)
                        return
                else:
                    # 無帳密時退回 probe，讓 human 手動登入
                    ok, err = probe_ready(bridge, profile)
            else:
                ok, err = probe_ready(bridge, profile)

            notify_ready = False
            with self._lock:
                # C1 backstop：release 落在 attach 飛行窗口（bridge.start()+probe 耗時）內時，
                # 關掉剛開的 FD（clean slate），但**保留 RELEASED**——不可打回 DETACHED。
                if session.state == "RELEASED" or by_id in self._released_by_ids:
                    bridge.stop(preserve_consoles=False)
                    session.bridge = None
                    session.attached_real_path = None
                    return
                current = self._devices.get(by_id)
                if current is None or current.real_path != real_path or session.state == "DETACHED" or session.bridge_generation != gen_before:
                    preserved = bridge.stop(preserve_consoles=True)
                    self._store_retained_consoles_locked(
                        session,
                        preserved,
                        human_owner=session.retained_human_owner,
                        human_timeout_s=session.retained_human_timeout_s,
                    )
                    session.state = "DETACHED"
                    session.last_error = "DEVICE_REMOVED_DURING_ATTACH"
                    session.detached_at = now_iso()
                    session.bridge = None
                    session.attached_real_path = None
                    return
                session.bridge = bridge
                session.vtty_path = bridge.vtty_path
                session.attached_real_path = real_path
                session.bridge_generation += 1
                session.retained_consoles = None
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
                self._restore_retained_human_console_locked(session)
            if notify_ready:
                self._on_ready(session.session_id)
        except Exception as exc:
            try:
                preserved = bridge.stop(preserve_consoles=True)
            except Exception:
                preserved = None
            with self._lock:
                self._store_retained_consoles_locked(
                    session,
                    preserved,
                    human_owner=session.retained_human_owner,
                    human_timeout_s=session.retained_human_timeout_s,
                )
                session.state = "DETACHED"
                session.last_error = f"ATTACH_FAILED:{type(exc).__name__}"
                session.detached_at = now_iso()
                session.bridge = None
                session.attached_real_path = None
            self._on_detached(session.session_id)

    def _default_passthrough_template(self) -> ProfileTemplate | None:
        """auto-detect 失敗時的通用 passthrough fallback。

        優先選非 command-capable 的 passthrough（如 others-template，純 console），
        避免 uboot-template 這類具 ready_probe 的特定 passthrough 被誤當通用 fallback；
        若沒有非 command-capable 的 passthrough，才退而用任一 passthrough（向後相容）。
        """
        generic = next(
            (t for t in self._templates
             if t.platform == "passthrough" and not t.command_capable),
            None,
        )
        if generic is not None:
            return generic
        return next((t for t in self._templates if t.platform == "passthrough"), None)

    def _attach_by_id_dynamic(self, by_id: str) -> None:
        """動態偵測 template 並建立新 session。"""
        from .config import UartProfile

        with self._lock:
            dev = self._devices.get(by_id)
            if dev is None:
                return
            real_path = dev.real_path

        # 先用預設 UART 參數開 bridge 做 probe
        default_uart = UartProfile()
        probe_bridge = UARTBridge(
            "PROBE",
            real_path,
            default_uart,
            self._wal,
        )
        detected: ProfileTemplate | None = None
        try:
            probe_bridge.start()
            detected = detect_template(probe_bridge, self._templates)
        except Exception:
            pass
        finally:
            try:
                probe_bridge.stop()
            except Exception:
                pass

        # 找 passthrough fallback（通用 fallback 須為非 command-capable 的 passthrough，
        # 避免 uboot-template 這類 command-capable 的特定 passthrough 搶走通用 fallback）
        passthrough = self._default_passthrough_template()
        tpl = detected or passthrough
        if tpl is None:
            return

        with self._lock:
            # 確認裝置仍在線且 session 數仍未超限
            if by_id not in self._devices or len(self._sessions) >= self._max_sessions:
                return
            session = self._session_from_template(tpl, by_id)

        # 用正確 uart 參數重新開 bridge
        profile = session.profile
        with self._lock:
            dev = self._devices.get(by_id)
            if dev is None:
                return
            # C1 早退（dynamic 版）：release 早於 attach 開 FD 時不啟動。
            if session.state == "RELEASED" or by_id in self._released_by_ids:
                return
            real_path = dev.real_path
            session.state = "ATTACHING"
            session.last_error = None
            gen_before = session.bridge_generation
            session_id = session.session_id
            require_login = session.pending_auto_login or bool(profile.login_regex)
            # 與 _attach_by_id 一致，以 ready_probe 判定可否下命令。
            command_capable = profile.command_capable

        bridge = UARTBridge(
            profile.com,
            real_path,
            profile.uart,
            self._wal,
            on_console_line=lambda client_id, line, sid=session_id: self._on_bridge_console_line(sid, client_id, line),
            on_rx_data=lambda data, sid=session_id: self._on_bridge_rx(sid, data),
            on_bridge_down=lambda reason, sid=session_id: self._handle_bridge_down(sid, bridge, reason),
        )

        try:
            bridge.start()
            if not command_capable:
                ok, err = False, None
            elif require_login:
                auth = resolve_session_auth(profile)
                if auth.username and auth.password:
                    ok, err = ensure_ready(bridge, profile, auth=auth)
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
                    ok, err = probe_ready(bridge, profile)
            else:
                ok, err = probe_ready(bridge, profile)

            notify_ready = False
            with self._lock:
                # C1 backstop（dynamic 版）：release 落在飛行窗口內 → 關 FD、保留 RELEASED。
                if session.state == "RELEASED" or by_id in self._released_by_ids:
                    bridge.stop(preserve_consoles=False)
                    session.bridge = None
                    session.vtty_path = None
                    session.attached_real_path = None
                    return
                current = self._devices.get(by_id)
                if current is None or current.real_path != real_path or session.state == "DETACHED" or session.bridge_generation != gen_before:
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
                session.attached_real_path = real_path
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

    def _open_interactive_locked(
        self,
        session: SessionRuntime,
        *,
        owner: str,
        timeout_s: float,
        recovery_mode: bool = False,
        suspended_human: bool = False,
    ) -> InteractiveLease:
        interactive_id = uuid.uuid4().hex
        lease = InteractiveLease(
            interactive_id=interactive_id,
            session_id=session.session_id,
            owner=owner,
            created_at=now_iso(),
            timeout_s=timeout_s,
            recovery_mode=recovery_mode,
            suspended_human=suspended_human,
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
    ) -> tuple[InteractiveLease | None, _PostCloseAction]:
        """Close interactive lease。回傳 (closed_lease, post_close_action)。

        呼叫者必須在 _lock 外呼叫 post_close_action.execute()。
        """
        post = _PostCloseAction()
        lease_id = interactive_id or session.interactive_session_id
        if lease_id is None:
            return None, post
        lease = self._interactive.get(lease_id)
        if lease is not None and expected_owner is not None and lease.owner != expected_owner:
            return None, post
        if lease is not None:
            lease.status = "closed"
            self._interactive.pop(lease_id, None)

        # 帶 suspended_human 的 lease（bootloader recovery 或 #53 soft preempt）關閉時 →
        # 還原 stash 的 human lease 或丟棄；不限 recovery_mode，soft preempt（recovery_mode=False）亦適用。
        if lease is not None and lease.suspended_human:
            stash = session._stashed_human_lease
            session._stashed_human_lease = None
            bridge = session.bridge
            post.bridge = bridge
            post.needs_resume = True

            restored = False
            if stash is not None and not stash.expired():
                client_id = stash.owner.split(":", 1)[1] if ":" in stash.owner else ""
                if bridge is not None and bridge.console_has_external_peer(client_id):
                    # 還原 human lease 狀態（lock 內 state mutation）
                    self._interactive[stash.interactive_id] = stash
                    session.interactive_session_id = stash.interactive_id
                    bridge.set_interactive_owner(stash.owner)
                    restored = True

            if not restored:
                # 丟棄 stash：清除 session 狀態，resume 後清除 ghost bridge owner
                session.interactive_session_id = None
                if bridge is not None:
                    bridge.set_interactive_owner(None)
                post.clear_owner_after_resume = True

            return lease, post

        # 一般 close
        session.interactive_session_id = None
        if session.bridge is not None:
            session.bridge.set_interactive_owner(None)
        return lease, post

    def _refresh_interactive_locked(
        self, session: SessionRuntime
    ) -> tuple[InteractiveLease | None, _PostCloseAction]:
        post = _PostCloseAction()
        lease_id = session.interactive_session_id
        if lease_id is None:
            return None, post
        lease = self._interactive.get(lease_id)
        if lease is None:
            _, post = self._close_interactive_locked(session, interactive_id=lease_id)
            return None, post
        if session.bridge is None:
            _, post = self._close_interactive_locked(session, interactive_id=lease_id)
            return None, post
        if lease.owner.startswith("human:"):
            client_id = lease.owner.split(":", 1)[1]
            if not session.bridge.console_has_external_peer(client_id):
                session.bridge.detach_console(client_id)
                session.vtty_path = session.bridge.vtty_path
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
            snapshot = session.bridge.snapshot()
            if snapshot.get("interactive_owner") != lease.owner:
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                return None, post
        else:
            if lease.expired():
                _, post = self._close_interactive_locked(session, interactive_id=lease_id)
                restored_id = session.interactive_session_id
                restored = self._interactive.get(restored_id) if restored_id is not None else None
                return restored, post
        return lease, post

    def _lease_context(
        self, lease: InteractiveLease | None, *, bridge: UARTBridge | None = None
    ) -> dict[str, Any]:
        interactive_owner = lease.owner if lease is not None else None
        human_attached = bool(interactive_owner and interactive_owner.startswith("human:"))
        # human_active：human_attached 且最近一次真實鍵入仍在時間窗內（#53）。
        # 讓「人類已 attach 但長時間 idle」的 lease 不再被當成正在使用，避免誤擋
        # agent 行為；無 bridge / 從未鍵入 / 逾時皆為 False。
        human_active = False
        if human_attached and bridge is not None:
            last = bridge.snapshot().get("last_human_input_at")
            # last 在 production 僅為 None 或 float；以 isinstance 防呆，避免遇到
            # 非數值（如未設定 return_value 的 mock）時在 '<=' 比較炸掉。
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                if (time.monotonic() - last) <= HUMAN_ACTIVE_WINDOW_S:
                    human_active = True
        return {
            "interactive_owner": interactive_owner,
            "human_attached": human_attached,
            "human_active": human_active,
            "recovery_mode": bool(lease is not None and lease.recovery_mode),
        }

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
            post = _PostCloseAction()
            outcome: tuple[bool, str | None] | None = None
            with self._lock:
                session = self._sessions.get(session_id)
                if session is None or session.bridge is None or session.state != "READY":
                    return False, "SESSION_NOT_READY"
                if session.recovering:
                    return False, "SESSION_RECOVERING"
                lease, post = self._refresh_interactive_locked(session)
                if lease is None:
                    outcome = (True, None)
                elif not lease.owner.startswith("human:"):
                    outcome = (False, "SESSION_INTERACTIVE_BUSY")
            post.execute()
            if outcome is not None:
                return outcome
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

    def _set_terminal_capture_locked(
        self,
        session: SessionRuntime,
        *,
        cmd_id: str,
        chunks: list[str] | None = None,
        error_code: str | None = None,
    ) -> None:
        if not cmd_id:
            return
        capture = self._background.get(cmd_id)
        if capture is None:
            capture = BackgroundCapture(
                cmd_id=cmd_id,
                session_id=session.session_id,
                from_seq=self._wal.current_seq + 1,
                quiet_window_s=session.profile.quiet_window_s,
                created_at=now_iso(),
                last_seq=self._wal.current_seq,
            )
            self._background[cmd_id] = capture
        if chunks:
            capture.chunks.extend(chunk for chunk in chunks if chunk)
        capture.last_seq = self._wal.current_seq
        capture.last_activity_mono = time.monotonic()
        capture.status = "error" if error_code else "done"
        capture.error_code = error_code

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
        self._mark_session_tx(session)
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
            post = _PostCloseAction()
            with self._lock:
                lease, post = self._refresh_interactive_locked(session)
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
            post.execute()
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
        expected_duration_s: float | None = None,
    ) -> dict[str, Any]:
        normalized_mode = {"fg": "line", "bg": "background"}.get(mode, mode)
        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.state == "FLASHING":
                return {"ok": False, "error_code": "FLASHING_BUSY", "selector": session.profile.com}
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None and normalized_mode != "interactive":
                if not source.startswith("human:") and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {
                        "ok": False,
                        "error_code": "SESSION_INTERACTIVE_BUSY",
                        "interactive_session_id": session.interactive_session_id,
                    }
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex

        post.execute()
        if busy_result is not None:
            return busy_result
        if suspend_human_interactive:
            bridge.suspend_interactive()

        try:
            return self._execute_command_inner(
                session, bridge, command, source, cmd_id,
                timeout_s=timeout_s, normalized_mode=normalized_mode,
                prompt_regex=prompt_regex,
                expected_duration_s=expected_duration_s,
            )
        finally:
            if suspend_human_interactive:
                bridge.resume_interactive()

    def _execute_command_inner(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        command: str,
        source: str,
        cmd_id: str,
        *,
        timeout_s: float,
        normalized_mode: str,
        prompt_regex: str,
        expected_duration_s: float | None = None,
    ) -> dict[str, Any]:
        if normalized_mode == "interactive":
            with self._lock:
                for bg_cmd_id in list(session.background_cmd_ids):
                    capture = self._background.get(bg_cmd_id)
                    if capture is not None:
                        capture.status = "done"
                lease = self._open_interactive_locked(session, owner=source, timeout_s=max(timeout_s, session.profile.hard_timeout_s))
            if command:
                self._mark_session_tx(session)
                bridge.send_command(command, source=source, cmd_id=cmd_id)
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "stdout": "",
                "status": "interactive",
            }

        with self._lock:
            session.foreground_busy = True
            session.fg_cmd_started_mono = time.monotonic()
            session.fg_cmd_expected_duration_s = expected_duration_s
            if normalized_mode != "background":
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
                with self._lock:
                    session.foreground_busy = False
                    session.fg_cmd_started_mono = None
                    session.fg_cmd_expected_duration_s = None
        pre_offset = bridge.rx_snapshot_len()
        try:
            self._mark_session_tx(session)
            bridge.send_command(command, source=source, cmd_id=cmd_id)

            # — heartbeat / keepalive 迴圈 —
            effective_timeout = timeout_s
            if expected_duration_s is not None:
                effective_timeout = max(timeout_s, expected_duration_s)
            silence_limit = min(timeout_s, 30.0)
            deadline = time.monotonic() + effective_timeout
            matched = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_chunk = min(silence_limit, remaining)
                pre_rx = bridge.rx_snapshot_len()
                if bridge.wait_for_regex_from(prompt_regex, pre_offset, wait_chunk):
                    matched = True
                    break
                if bridge.rx_snapshot_len() == pre_rx:
                    # 真正靜默，不再等待
                    break
                # 有 RX 活動，繼續等待

            if not matched:
                return self._recover_after_failure(
                    session,
                    bridge,
                    cmd_id=cmd_id,
                    timeout_s=timeout_s,
                    source=source,
                    command=command,
                    prompt_regex=prompt_regex,
                    pre_offset=pre_offset,
                )
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
            with self._lock:
                session.foreground_busy = False
                session.fg_cmd_started_mono = None
                session.fg_cmd_expected_duration_s = None

    def _recover_after_failure(
        self,
        session: SessionRuntime,
        bridge: UARTBridge,
        *,
        cmd_id: str,
        timeout_s: float,
        source: str,
        command: str,
        prompt_regex: str,
        pre_offset: int,
    ) -> dict[str, Any]:
        if source.startswith("human:"):
            post = _PostCloseAction()
            with self._lock:
                lease, post = self._refresh_interactive_locked(session)
                if lease is None:
                    lease = self._open_interactive_locked(
                        session,
                        owner=source,
                        timeout_s=max(timeout_s, session.profile.hard_timeout_s),
                    )
            post.execute()
            return {
                "ok": True,
                "execution_mode": "interactive",
                "interactive_session_id": lease.interactive_id,
                "status": "interactive",
                "stdout": "",
                "partial": True,
                "recovery_action": "PROMOTE_HUMAN_INTERACTIVE",
            }

        for action_name, payload in (("CTRL_C", b"\x03"), ("CTRL_D", b"\x04")):
            offset = bridge.rx_snapshot_len()
            self._mark_session_tx(session)
            bridge.send_bytes(payload, source="system:recover", cmd_id=None)
            if bridge.wait_for_regex_from(prompt_regex, offset, min(timeout_s, 2.0)):
                stdout = self._extract_command_stdout(bridge.rx_text_from(pre_offset), command, prompt_regex)
                with self._lock:
                    self._set_terminal_capture_locked(
                        session,
                        cmd_id=cmd_id,
                        chunks=[stdout] if stdout else None,
                        error_code="PROMPT_TIMEOUT_RECOVERED",
                    )
                return {
                    "ok": True,
                    "error_code": "PROMPT_TIMEOUT_RECOVERED",
                    "stdout": stdout,
                    "partial": True,
                    "recovery_action": action_name,
                }

        partial_stdout = clean_text(bridge.rx_text_from(pre_offset))
        with self._lock:
            self._set_terminal_capture_locked(
                session,
                cmd_id=cmd_id,
                chunks=[partial_stdout] if partial_stdout else None,
                error_code="PROMPT_TIMEOUT",
            )
        self._transition_to_attached(session, reason="PROMPT_TIMEOUT")

        # Auto-fallback: try force clear + reattach when both CTRL_C/D fail
        if source == "system:recover":
            force_result = self._force_recover(session)
            if force_result.get("recovered"):
                return {
                    "ok": True,
                    "error_code": "PROMPT_TIMEOUT_FORCE_RECOVERED",
                    "partial": True,
                    "recovery_action": "FORCE_CLEAR_REATTACH",
                    "session": force_result.get("session", {}),
                }

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
        post = _PostCloseAction()
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state not in {"READY", "ATTACHED"}:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            payload = session.bridge.attach_console(label=label)
            if session.vtty_path is None:
                session.vtty_path = payload["vtty"]
            lease, post = self._refresh_interactive_locked(session)
            if session.state in {"ATTACHED", "READY"} and lease is None:
                lease = self._open_interactive_locked(
                    session,
                    owner=f"human:{payload['client_id']}",
                    timeout_s=max(session.profile.hard_timeout_s, _ATTACHED_CONSOLE_LEASE_TIMEOUT_S),
                )
                payload["interactive_session_id"] = lease.interactive_id
                payload["interactive_owner"] = True
            payload["session"] = session.to_public_dict()
            result = {"ok": True, **payload}
        post.execute()
        return result

    def detach_console(self, selector: str, client_id: str) -> dict[str, Any]:
        post = _PostCloseAction()
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            human_owner = f"human:{client_id}"
            lease, post = self._refresh_interactive_locked(session)
            ok = session.bridge.detach_console(client_id)
            if lease is not None and lease.owner == human_owner:
                # Human lease close：一般 human lease 的 suspended_human=False，故
                # _close_interactive_locked 走一般 close 分支、post.needs_resume 必為 False
                # （no-op post），安全丟棄。（還原分支現以 suspended_human 判定，非 recovery_mode。）
                _, _ = self._close_interactive_locked(
                    session, interactive_id=lease.interactive_id, expected_owner=human_owner
                )
            session.vtty_path = session.bridge.vtty_path
            if not ok:
                result = {"ok": False, "error_code": "CONSOLE_NOT_FOUND", "client_id": client_id}
            else:
                result = {"ok": True, "client_id": client_id, "session": session.to_public_dict()}
        post.execute()
        return result

    def list_consoles(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}
            return {"ok": True, "consoles": session.bridge.list_consoles(), "session": session.to_public_dict()}

    def interactive_open(
        self,
        selector: str,
        *,
        owner: str = "agent",
        timeout_s: float = 60.0,
        command: str = "",
        allow_attached: bool = False,
    ) -> dict[str, Any]:
        while True:
            retry_after_post = False
            post = _PostCloseAction()
            with self._lock:
                session = self.get_session(selector)
                if session is None or session.bridge is None:
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                if session.state == "READY":
                    # READY 一律走既有路徑；allow_attached 在 READY 下不改變語意。
                    existing, post = self._refresh_interactive_locked(session)
                    if post.needs_resume:
                        retry_after_post = True
                    elif existing is not None:
                        # #53 soft preempt：human lease 閒置（human_active=False）時，
                        # agent 可暫停（降級）human lease 取得控制權；human console 不中斷，
                        # 其鍵入進 deferred buffer，agent close 後還原並回放。
                        # human 仍 active 或既有為 agent lease → 維持 BUSY。
                        # 註：從未鍵入（last_human_input_at=None）視為 idle、可被 preempt——
                        # soft preempt 非破壞性（降級+回放），故此處理為刻意行為。
                        human_active = self._lease_context(
                            existing, bridge=session.bridge
                        )["human_active"]
                        if (
                            existing.owner.startswith("human:")
                            and not human_active
                            and not owner.startswith("human:")
                        ):
                            self._interactive.pop(existing.interactive_id, None)
                            session.interactive_session_id = None
                            session._stashed_human_lease = existing
                            session.bridge.suspend_interactive()
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=timeout_s,
                                suspended_human=True,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": False,
                                "soft_preempted": True,
                            }
                        else:
                            return {
                                "ok": False,
                                "error_code": "SESSION_INTERACTIVE_BUSY",
                                "interactive_session_id": session.interactive_session_id,
                            }
                    else:
                        lease = self._open_interactive_locked(session, owner=owner, timeout_s=timeout_s)
                        bridge = session.bridge
                        result: dict[str, Any] = {
                            "ok": True,
                            "interactive_id": lease.interactive_id,
                            "session": session.to_public_dict(),
                            "recovery_mode": False,
                        }

                elif not allow_attached:
                    # 原有行為：未 opt-in 時只允許 READY。
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                elif session.state == "ATTACHED":
                    # bootloader recovery path
                    snap = session.bridge.snapshot()
                    if not snap.get("running") or not snap.get("serial_alive") or not snap.get("vtty_alive"):
                        return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

                    rx_tail_raw = session.bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)
                    rx_tail_clean = clean_text(rx_tail_raw)
                    matched = _matches_any_bootloader_prompt(rx_tail_clean, session.profile.bootloader_prompts)
                    if matched is None:
                        return {
                            "ok": False,
                            "error_code": "SESSION_NOT_READY",
                            "selector": selector,
                            "error_detail": "NOT_BOOTLOADER",
                        }

                    # 清除 expired lease（避免 BUSY 誤判）；若 close 需要 resume，
                    # 先在 lock 外完成 post-close，再重新評估是否要 stash human。
                    existing, post = self._refresh_interactive_locked(session)
                    if post.needs_resume:
                        retry_after_post = True
                    else:
                        clamped_timeout = min(timeout_s, MAX_RECOVERY_LEASE_S)

                        if existing is None:
                            # 無現有 lease：直接開啟 recovery lease
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=clamped_timeout,
                                recovery_mode=True, suspended_human=False,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": True,
                            }

                        elif existing.owner.startswith("human:"):
                            # 有 human lease：stash and open recovery
                            human_id = existing.interactive_id
                            self._interactive.pop(human_id, None)
                            session.interactive_session_id = None
                            session._stashed_human_lease = existing
                            session.bridge.suspend_interactive()
                            lease = self._open_interactive_locked(
                                session, owner=owner, timeout_s=clamped_timeout,
                                recovery_mode=True, suspended_human=True,
                            )
                            bridge = session.bridge
                            result = {
                                "ok": True,
                                "interactive_id": lease.interactive_id,
                                "session": session.to_public_dict(),
                                "recovery_mode": True,
                            }

                        else:
                            # 有其他 agent lease：BUSY
                            return {
                                "ok": False,
                                "error_code": "SESSION_INTERACTIVE_BUSY",
                                "interactive_session_id": session.interactive_session_id,
                            }

                else:
                    return {"ok": False, "error_code": "SESSION_NOT_READY", "selector": selector}

            post.execute()
            if retry_after_post:
                continue
            break

        if command:
            self._mark_session_tx(session)
            bridge.send_command(command, source=owner, cmd_id=None)
        return result

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
        post = _PostCloseAction()
        result: dict[str, Any]
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None or lease.status != "active":
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            if lease.expired():
                lease.status = "expired"
                session = self._sessions.get(lease.session_id)
                if session is not None:
                    _, post = self._close_interactive_locked(session, interactive_id=interactive_id)
                result = {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            else:
                session = self._sessions.get(lease.session_id)
                if session is None or session.bridge is None:
                    result = {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
                else:
                    payload = self._encode_interactive_payload(data, encoding)
                    self._mark_session_tx(session)
                    session.bridge.send_bytes(payload, source=lease.owner, cmd_id=None)
                    lease.touch()
                    result = {"ok": True, "interactive_id": interactive_id, "bytes": len(payload)}
        post.execute()
        return result

    def interactive_status(self, interactive_id: str, *, screen_chars: int = 2048) -> dict[str, Any]:
        post = _PostCloseAction()
        result: dict[str, Any]
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is None or session.bridge is None:
                return {"ok": False, "error_code": "SESSION_NOT_READY", "interactive_id": interactive_id}
            # 先檢查 expired 再 touch
            if lease.expired():
                lease.status = "expired"
                _, post = self._close_interactive_locked(session, interactive_id=interactive_id)
                result = {"ok": False, "error_code": "INTERACTIVE_EXPIRED", "interactive_id": interactive_id}
            else:
                lease.touch()
                result = {
                    "ok": True,
                    "interactive_id": interactive_id,
                    "owner": lease.owner,
                    "status": lease.status,
                    "recovery_mode": lease.recovery_mode,
                    "screen": clean_text(session.bridge.rx_tail(screen_chars)),
                    "session": session.to_public_dict(),
                }
        post.execute()
        return result

    def interactive_close(self, interactive_id: str) -> dict[str, Any]:
        post = _PostCloseAction()
        with self._lock:
            lease = self._interactive.get(interactive_id)
            if lease is None:
                return {"ok": False, "error_code": "INTERACTIVE_NOT_FOUND", "interactive_id": interactive_id}
            session = self._sessions.get(lease.session_id)
            if session is not None:
                _, post = self._close_interactive_locked(session, interactive_id=interactive_id)
            else:
                self._interactive.pop(interactive_id, None)
        post.execute()
        return {"ok": True, "interactive_id": interactive_id}

    def self_test(self, selector: str, *, timeout_s: float = 2.0, strict_human_lock: bool = False) -> dict[str, Any]:
        """對外入口：在所有分支的最外層 result dict 注入 command_capable。

        #51 sub-task A：呼叫端不必鑽進巢狀 "session" dict 即可判斷該 session
        是否可下命令。SESSION_NOT_FOUND 等查無 session 的分支一律以 False 表示。
        """
        result = self._self_test_impl(
            selector, timeout_s=timeout_s, strict_human_lock=strict_human_lock
        )
        if "command_capable" not in result:
            # 直接取用 impl 在 lock 內快照進 result["session"] 的值，避免再開一次
            # lock 重撈 session（消除多餘鎖與 TOCTOU 窗口）。查無 session 的分支
            # （如 SESSION_NOT_FOUND）沒有 "session" key → 一律 False。
            nested = result.get("session")
            result["command_capable"] = (
                bool(nested.get("command_capable", False)) if nested is not None else False
            )
        return result

    def _self_test_impl(self, selector: str, *, timeout_s: float = 2.0, strict_human_lock: bool = False) -> dict[str, Any]:
        suspend_human_interactive = False
        post = _PostCloseAction()
        result: dict[str, Any] | None = None
        # I2：RELEASED 分支須在 lock 內擷取、出 lock 後再掃 /proc（_probe_external_holder
        # 會掃整個 /proc，持 lock 期間會阻塞所有 RPC）。
        released_capture: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {
                    "ok": False,
                    "error_code": "SESSION_NOT_FOUND",
                    "selector": selector,
                    **self._lease_context(None),
                }
            if session.state == "RELEASED":
                device = self._devices.get(session.profile.device_by_id)
                released_capture = {
                    "real_path": device.real_path if device is not None else None,
                    "session": session.to_public_dict(),
                    "released_by": session.released_by,
                    "released_at": session.released_at,
                    "reason": session.released_reason,
                    "lease_context": self._lease_context(None),
                }
            if released_capture is None:
                lease = self._interactive.get(session.interactive_session_id) if session.interactive_session_id is not None else None
                if session.recovering:
                    return {
                        "ok": True,
                        "classification": "SESSION_RECOVERING",
                        "session": session.to_public_dict(),
                        "recommended_action": "wait",
                        **self._lease_context(lease, bridge=session.bridge),
                    }
                lease, post = self._refresh_interactive_locked(session)
                lease_context = self._lease_context(lease, bridge=session.bridge)
                device = self._devices.get(session.profile.device_by_id)
                attached_real_path = session.attached_real_path
                bridge = session.bridge
                if strict_human_lock and lease is not None and lease.owner.startswith("human:"):
                    result = {
                        "ok": True,
                        "classification": "HUMAN_INTERACTIVE_ACTIVE",
                        "interactive_id": lease.interactive_id,
                        "session": session.to_public_dict(),
                        "recommended_action": "wait_or_detach_console",
                        **lease_context,
                    }
                elif device is None:
                    result = {
                        "ok": True,
                        "classification": "DEVICE_MISSING",
                        "session": session.to_public_dict(),
                        "recommended_action": "check_cable_or_bind",
                        **lease_context,
                    }
                elif attached_real_path and attached_real_path != device.real_path:
                    result = {
                        "ok": True,
                        "classification": "DEVICE_REBOUND_REQUIRED",
                        "session": session.to_public_dict(),
                        "attached_real_path": attached_real_path,
                        "current_real_path": device.real_path,
                        "recommended_action": "reattach",
                        **lease_context,
                    }
                elif bridge is None:
                    result = {
                        "ok": True,
                        "classification": "BRIDGE_DOWN",
                        "session": session.to_public_dict(),
                        "current_real_path": device.real_path,
                        "recommended_action": "attach",
                        **lease_context,
                    }
                else:
                    snapshot = bridge.snapshot()
                    if not snapshot.get("running") or not snapshot.get("serial_alive"):
                        result = {
                            "ok": True,
                            "classification": "BRIDGE_DOWN",
                            "session": session.to_public_dict(),
                            "current_real_path": device.real_path,
                            "recommended_action": "recover",
                            **lease_context,
                        }
                    elif not snapshot.get("vtty_alive"):
                        result = {
                            "ok": True,
                            "classification": "VTTY_STALE",
                            "session": session.to_public_dict(),
                            "attached_vtty": snapshot.get("vtty"),
                            "recommended_action": "console_attach",
                            **lease_context,
                        }
                    elif session.state == "ATTACHED":
                        if session.profile.platform == "passthrough":
                            classification = "PASSTHROUGH"
                            recommended_action = "console_attach"
                            extra: dict[str, Any] = {}
                        elif session.last_error == "LOGIN_REQUIRED":
                            classification = "LOGIN_REQUIRED"
                            recommended_action = "console_attach"
                            extra = {}
                        elif session.last_error == "REBOOTING":
                            classification = "REBOOTING"
                            recommended_action = "wait_or_console_attach"
                            extra = {}
                        else:
                            # BOOTLOADER detection：讀取 RX tail 並比對 bootloader prompts
                            rx_tail_raw = bridge.rx_tail(BOOTLOADER_RX_TAIL_BYTES)
                            rx_tail_evidence = clean_text(rx_tail_raw)
                            matched = _matches_any_bootloader_prompt(
                                rx_tail_evidence, session.profile.bootloader_prompts
                            )
                            if matched is not None:
                                classification = "BOOTLOADER"
                                recommended_action = "recover_interactive"
                                extra = {"matched_prompt": matched, "rx_tail": rx_tail_evidence}
                            else:
                                classification = "ATTACHED_NOT_READY"
                                recommended_action = "console_attach"
                                extra = {}
                        result = {
                            "ok": True,
                            "classification": classification,
                            "session": session.to_public_dict(),
                            "attached_real_path": attached_real_path,
                            "current_real_path": device.real_path,
                            "attached_vtty": snapshot.get("vtty"),
                            "bridge_generation": session.bridge_generation,
                            "recommended_action": recommended_action,
                            **extra,
                            **lease_context,
                        }
                    else:
                        nonce = uuid.uuid4().hex[:8]
                        probe = session.profile.ready_probe.replace("${nonce}", nonce)
                        session.last_probe_at = now_iso()
                        prompt_regex = session.profile.prompt_regex
                        bridge_generation = session.bridge_generation
                        attached_vtty = snapshot.get("vtty")
                        current_real_path = device.real_path
                        suspend_human_interactive = lease is not None and lease.owner.startswith("human:")

        # I2：RELEASED 早退分支——出 lock 後才掃 /proc，避免阻塞所有 RPC。
        if released_capture is not None:
            real_path = released_capture["real_path"]
            holder = self._probe_external_holder(real_path) if real_path else {"pids": [], "holder": None}
            reclaimable = not holder["pids"]
            return {
                "ok": True,
                "classification": "RELEASED",
                "session": released_capture["session"],
                "released_by": released_capture["released_by"],
                "released_at": released_capture["released_at"],
                "reason": released_capture["reason"],
                "external_holder": holder["pids"] if holder["pids"] else "none",
                "reclaimable": reclaimable,
                "recommended_action": "device_attach" if reclaimable else "wait_external_flash",
                **released_capture["lease_context"],
            }

        post.execute()
        if result is not None:
            return result
        if suspend_human_interactive:
            bridge.suspend_interactive()
        try:
            offset = bridge.rx_snapshot_len()
            self._mark_session_tx(session)
            bridge.send_command(probe, source="system:self_test", cmd_id=None)
            if not bridge.wait_for_regex_from(nonce, offset, timeout_s):
                return {
                    "ok": True,
                    "classification": "TARGET_UNRESPONSIVE",
                    "session": session.to_public_dict(),
                    "attached_real_path": attached_real_path,
                    "current_real_path": current_real_path,
                    "probe_ok": False,
                    "recommended_action": "recover",
                    **lease_context,
                }
            bridge.wait_for_regex_from(prompt_regex, offset, timeout_s)
            return {
                "ok": True,
                "classification": "OK",
                "session": session.to_public_dict(),
                "attached_real_path": attached_real_path,
                "current_real_path": current_real_path,
                "attached_vtty": attached_vtty,
                "bridge_generation": bridge_generation,
                "probe_ok": True,
                "recommended_action": "none",
                **lease_context,
            }
        finally:
            if suspend_human_interactive:
                bridge.resume_interactive()

    def recover_session(self, selector: str, *, timeout_s: float = 2.0, force: bool = False) -> dict[str, Any]:
        reprobe = False
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            # C2：RELEASED 早退——比照 clear_session，不改 state、不 spawn、不動集合。
            if session.state == "RELEASED" or session.profile.device_by_id in self._released_by_ids:
                return {
                    "ok": True,
                    "released": True,
                    "recommended_action": "device_attach",
                    "session": session.to_public_dict(),
                }
            if session.bridge is None:
                by_id = session.profile.device_by_id
                if by_id and by_id in self._devices:
                    session.state = "ATTACHING"
                    session.last_error = None
                    self._spawn_attach(by_id)
                    return {"ok": True, "recovering": False, "action": "REATTACH", "session": session.to_public_dict()}
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            if session.state == "ATTACHED":
                if force:
                    return self._force_recover(session)
                bridge = session.bridge
                reprobe = True
            elif session.state != "READY":
                if force:
                    return self._force_recover(session)
                return {"ok": False, "error_code": "SESSION_NOT_READY", "session": session.to_public_dict()}
            else:
                bridge = session.bridge
        if reprobe:
            result = self._probe_existing_bridge(session, bridge)
            if not result.get("ok"):
                if force:
                    return self._force_recover(session)
                return result
            recovered = result["session"]["state"] == "READY"
            payload: dict[str, Any] = {
                "ok": True,
                "recovering": False,
                "action": "REPROBE",
                "recovered": recovered,
                "session": result["session"],
            }
            if not recovered:
                payload["error_code"] = result["session"].get("last_error") or "SESSION_NOT_READY"
                if force:
                    return self._force_recover(session)
            return payload
        return self._recover_after_failure(
            session,
            bridge,
            cmd_id="",
            timeout_s=timeout_s,
            source="system:recover",
            command="",
            prompt_regex=session.profile.prompt_regex,
            pre_offset=bridge.rx_snapshot_len(),
        )

    def _force_recover(self, session: SessionRuntime) -> dict[str, Any]:
        """Force recovery via clear + reattach + wait-ready."""
        selector = session.session_id
        self.clear_session(selector)
        import time
        for _ in range(10):
            time.sleep(1.0)
            state = self.get_session_state(selector)
            if state.get("ok") and state.get("session", {}).get("state") == "READY":
                return {
                    "ok": True,
                    "recovering": False,
                    "action": "FORCE_CLEAR_REATTACH",
                    "recovered": True,
                    "session": state["session"],
                }
        state = self.get_session_state(selector)
        return {
            "ok": False,
            "error_code": "FORCE_RECOVER_TIMEOUT",
            "action": "FORCE_CLEAR_REATTACH",
            "recovered": False,
            "session": state.get("session", {}),
        }

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
                "error_code": capture.error_code,
                "from_seq": capture.from_seq,
                "last_seq": capture.last_seq,
                "from_chunk": from_chunk,
                "next_chunk": next_chunk,
                "chunks": chunks,
            }

    # ------------------------------------------------------------------
    # Agent log capture: start / stop / status
    # ------------------------------------------------------------------

    def _resolve_log_dir(self, session: SessionRuntime) -> str:
        return session.profile.log_dir or LOG_DIR

    def _stop_capture_locked(self, session: SessionRuntime) -> None:
        cap = session.active_capture
        if cap is None:
            return
        cap.status = "stopped"
        fp = self._capture_fps.pop(cap.capture_id, None)
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass
        session.active_capture = None

    def log_start(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            if session.active_capture is not None and session.active_capture.status == "active":
                cap = session.active_capture
                return {
                    "ok": True,
                    "already_active": True,
                    "capture_id": cap.capture_id,
                    "log_path": cap.log_path,
                    "session": session.to_public_dict(),
                }
            log_dir = self._resolve_log_dir(session)
            os.makedirs(log_dir, exist_ok=True)
            ts = time.strftime("%y%m%d-%H%M%S")
            filename = f"{session.profile.com}_{ts}.log"
            log_path = os.path.join(log_dir, filename)
            capture_id = str(uuid.uuid4())
            try:
                fp = open(log_path, "a", encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error_code": "LOG_OPEN_FAILED", "detail": str(exc)}
            cap = SessionCapture(
                capture_id=capture_id,
                session_id=session.session_id,
                log_path=log_path,
                started_at=now_iso(),
            )
            session.active_capture = cap
            self._capture_fps[capture_id] = fp
            return {
                "ok": True,
                "capture_id": capture_id,
                "log_path": log_path,
                "session": session.to_public_dict(),
            }

    def log_stop(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            cap = session.active_capture
            if cap is None:
                return {"ok": False, "error_code": "NO_ACTIVE_CAPTURE", "session": session.to_public_dict()}
            result = {
                "ok": True,
                "capture_id": cap.capture_id,
                "log_path": cap.log_path,
                "line_count": cap.line_count,
                "byte_count": cap.byte_count,
                "started_at": cap.started_at,
            }
            self._stop_capture_locked(session)
            return result

    def log_status(self, selector: str) -> dict[str, Any]:
        with self._lock:
            session = self.get_session(selector)
            if session is None:
                return {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": selector}
            cap = session.active_capture
            if cap is None:
                return {"ok": True, "active": False, "session": session.to_public_dict()}
            return {
                "ok": True,
                "active": cap.status == "active",
                "capture_id": cap.capture_id,
                "log_path": cap.log_path,
                "line_count": cap.line_count,
                "byte_count": cap.byte_count,
                "started_at": cap.started_at,
                "session": session.to_public_dict(),
            }

    # ── 檔案傳輸 ──────────────────────────────────────────────

    def file_push(
        self,
        selector: str,
        *,
        local_path: str,
        remote_path: str,
        chunk_size: int = 2048,
        source: str = "agent",
    ) -> dict[str, Any]:
        """將 host 端檔案推送到 target。"""
        from .file_transfer import push_file

        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None:
                if not source.startswith("human:") and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
            if busy_result is None:
                session.foreground_busy = True

        post.execute()
        if busy_result is not None:
            return busy_result
        if suspend_human_interactive:
            bridge.suspend_interactive()
        try:
            return push_file(
                bridge,
                local_path,
                remote_path,
                chunk_size=chunk_size,
                timeout_s=10.0,
                prompt_regex=prompt_regex,
                source=source,
            )
        finally:
            with self._lock:
                session.foreground_busy = False
            if suspend_human_interactive:
                bridge.resume_interactive()

    def file_pull(
        self,
        selector: str,
        *,
        remote_path: str,
        local_path: str | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        """從 target 拉取檔案到 host。"""
        from .file_transfer import pull_file

        suspend_human_interactive = False
        post = _PostCloseAction()
        busy_result: dict[str, Any] | None = None
        with self._lock:
            session = self.get_session(selector)
            if session is None or session.bridge is None or session.state != "READY":
                return {"ok": False, "error_code": "SESSION_NOT_READY"}
            if session.recovering:
                return {"ok": False, "error_code": "SESSION_RECOVERING"}
            lease, post = self._refresh_interactive_locked(session)
            if lease is not None:
                if not source.startswith("human:") and lease.owner.startswith("human:"):
                    suspend_human_interactive = True
                else:
                    busy_result = {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}
            bridge = session.bridge
            prompt_regex = session.profile.prompt_regex
            if busy_result is None:
                session.foreground_busy = True

        post.execute()
        if busy_result is not None:
            return busy_result
        if suspend_human_interactive:
            bridge.suspend_interactive()
        try:
            return pull_file(
                bridge,
                remote_path,
                local_path,
                timeout_s=30.0,
                prompt_regex=prompt_regex,
                source=source,
            )
        finally:
            with self._lock:
                session.foreground_busy = False
            if suspend_human_interactive:
                bridge.resume_interactive()
