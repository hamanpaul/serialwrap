"""P7 Task 1：以 Python `SessionManager`＋固定時鐘／uuid／路徑驅動的 golden（quiet／gate／reprobe／self-test／recover／log）。

每列的 `setup` 為情境描述（Rust 測試支援以同一份描述建 `CoreHarness`），欄位：
  template／profile_over／profile_source、state、last_error、last_error_detail、device_present（預設 true）、real_path、
  attached_real_path、bridge（null 或 {tail, text, rx_bytes, snapshot, waits}）、recovering、released、holder_pids、
  quiet（{until_rel, pending, deadline_rel, attempts, failed}，相對 now 的秒數）、lease（{owner, last_human_input_rel}）、
  last_rx_rel、reprobe（{attempts, next_rel, exhausted}）、now（monotonic 秒，預設 1000.0）、call 參數。
時間一律為虛擬：`time.monotonic`／`time.sleep` 由 `VirtualClock` 取代，`now_iso` 固定，uuid 依序取 `UUID(int=n)`。
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid as uuid_mod
from typing import Any
from unittest import mock

from capture_p7_goldens import profile_json, rows_with_ids, session_profile, wire
from p7_fake_bridge import VirtualClock

FIXED_ISO = "2026-10-26T00:00:00.000000+00:00"
FIXED_STRFTIME = "261026-000000"
BASE_NOW = 1000.0


class GoldenBridge:
    """SessionManager 用 fake bridge（沿 tests/test_rx_flood.ManagerFakeBridge 手法）。"""

    def __init__(self, spec: dict[str, Any], clock: VirtualClock) -> None:
        self._clock = clock
        self.tail = spec.get("tail", "")
        self.text = spec.get("text", self.tail)
        self.rx_bytes = spec.get("rx_bytes", 0)
        self.snap = {"running": True, "serial_alive": True, "vtty_alive": True, "interactive_owner": None,
                     "last_human_input_at": None, "vtty": "/tmp/fake-vtty"}
        self.snap.update(spec.get("snapshot", {}))
        self._waits = list(spec.get("waits", []))
        self.sent: list[list[Any]] = []

    def list_consoles(self) -> list[dict]:
        return []

    def console_endpoint(self) -> str | None:
        return None

    def snapshot(self) -> dict:
        return dict(self.snap)

    def console_has_external_peer(self, _client_id: str) -> bool:
        return True

    def set_interactive_owner(self, owner: str | None) -> None:
        self.snap["interactive_owner"] = owner

    def _enumerate_all_held_paths(self):
        return None

    def reap_stale_consoles(self, *, held_slave_paths=None):
        return []

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self.tail[-max_chars:]

    def rx_text_from(self, _offset: int) -> str:
        return self.text

    def rx_stats(self) -> dict[str, Any]:
        return {"rx_bytes_last_10s": self.rx_bytes, "rx_rate_bps": self.rx_bytes // 10}

    def rx_total_bytes(self) -> int:
        return 0

    def rx_snapshot_len(self) -> int:
        return 0

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None, permit: Any = None) -> None:
        self.sent.append([source, cmd])

    def send_bytes(self, payload: bytes, *, source: str, cmd_id: str | None = None, log: bool = True, permit: Any = None) -> None:
        self.sent.append([source, payload.decode("latin-1")])

    def wait_for_regex_from(self, _pattern: str, _from_offset: int, timeout_s: float) -> bool:
        ok = self._waits.pop(0) if self._waits else False
        if not ok:
            self._clock.sleep(timeout_s)
        return ok

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        return self.wait_for_regex_from(pattern, 0, timeout_s)

    def suspend_interactive(self) -> None:
        self.sent.append(["<suspend>", ""])

    def resume_interactive(self) -> None:
        self.sent.append(["<resume>", ""])

    def invalidate_deferred(self, _reason: str) -> None:
        pass

    def stop(self) -> None:
        pass


@contextlib.contextmanager
def golden_env(now: float = BASE_NOW):
    import sw_core.session_manager as sm

    clock = VirtualClock()
    clock.now = now
    counter = iter(range(1, 10_000))
    real_strftime = sm.time.strftime
    with tempfile.TemporaryDirectory() as td, \
         mock.patch.object(sm, "STATE_PATH", os.path.join(td, "state.json")), \
         mock.patch.object(sm.time, "monotonic", clock.monotonic), \
         mock.patch.object(sm.time, "sleep", clock.sleep), \
         mock.patch.object(sm.time, "strftime", lambda fmt, *a: FIXED_STRFTIME if fmt == "%y%m%d-%H%M%S" else real_strftime(fmt, *a)), \
         mock.patch.object(sm, "now_iso", lambda: FIXED_ISO), \
         mock.patch.object(sm.uuid, "uuid4", lambda: uuid_mod.UUID(int=next(counter))):
        yield clock, td


def build(spec: dict[str, Any], clock: VirtualClock, td: str):
    import sw_core.session_manager as sm
    from sw_core.device_watcher import DeviceInfo
    from sw_core.wal import WalWriter

    over = dict(spec.get("profile_over", {}))
    if "log_dir" in spec:
        over["log_dir"] = spec["log_dir"].replace("<TMP>", td)
    profile = session_profile(spec.get("template", "prpl-template"), **over)
    mgr = sm.SessionManager([profile], WalWriter(wal_dir=os.path.join(td, "wal")), on_ready=lambda _sid: None,
                            on_detached=lambda _sid: None)
    session = mgr.get_session("COM0")
    assert session is not None
    real_path = spec.get("real_path", "/dev/ttyFAKE0")
    with mgr._lock:
        if spec.get("device_present", True):
            mgr._devices = {profile.device_by_id: DeviceInfo(by_id=profile.device_by_id, real_path=real_path)}
        else:
            mgr._devices = {}
        if spec.get("released_by_id"):
            mgr._released_by_ids.add(profile.device_by_id)
    session.profile_source = spec.get("profile_source", "yaml-target")
    session.state = spec.get("state", "READY")
    if "last_error" in spec:
        session.last_error = spec["last_error"]
    if "last_error_detail" in spec:
        session.last_error_detail = spec["last_error_detail"]
    bridge = None
    if spec.get("bridge") is not None:
        bridge = GoldenBridge(spec["bridge"], clock)
        session.bridge = bridge
        session.attached_real_path = spec.get("attached_real_path", real_path)
        session.vtty_path = "/tmp/fake-vtty"
    session.recovering = bool(spec.get("recovering", False))
    rel = spec.get("released")
    if rel:
        session.released_by = rel.get("by")
        session.released_at = rel.get("at")
        session.released_reason = rel.get("reason")
    q = spec.get("quiet")
    if q:
        now = clock.now
        session.boot_quiet_until = 0.0 if q.get("until_rel") is None else now + q["until_rel"]
        session.ready_reconfirm_pending = bool(q.get("pending", False))
        session.ready_reconfirm_deadline = 0.0 if q.get("deadline_rel") is None else now + q["deadline_rel"]
        session.ready_reconfirm_attempts = int(q.get("attempts", 0))
        session.ready_reconfirm_failed = bool(q.get("failed", False))
        session.boot_banner_tail = q.get("tail", "")
    if "last_rx_rel" in spec:
        session.last_rx_mono = 0.0 if spec["last_rx_rel"] is None else clock.now - spec["last_rx_rel"]
    rp = spec.get("reprobe")
    if rp:
        session.reprobe_attempts = rp.get("attempts", 0)
        session.next_reprobe_at = None if rp.get("next_rel") is None else clock.now + rp["next_rel"]
        session.reprobe_exhausted = bool(rp.get("exhausted", False))
    lease = spec.get("lease")
    if lease:
        lid = "lease-1"
        mgr._interactive[lid] = sm.InteractiveLease(interactive_id=lid, session_id=session.session_id, owner=lease["owner"],
                                                    created_at=FIXED_ISO, timeout_s=lease.get("timeout_s", 300.0),
                                                    last_activity_at=clock.now)
        session.interactive_session_id = lid
        if bridge is not None:
            bridge.snap["interactive_owner"] = lease["owner"]
            if lease.get("last_human_input_rel") is not None:
                bridge.snap["last_human_input_at"] = clock.now - lease["last_human_input_rel"]
    if spec.get("pending_drain"):
        session._pending_drain = object()  # type: ignore[assignment]
    if spec.get("capture_active"):
        mgr.log_start("COM0")
    return mgr, session, bridge


def quiet_json(session: Any) -> dict[str, Any]:
    return {"boot_quiet_until": session.boot_quiet_until, "boot_banner_tail": session.boot_banner_tail,
            "pending": session.ready_reconfirm_pending, "deadline": session.ready_reconfirm_deadline,
            "attempts": session.ready_reconfirm_attempts, "failed": session.ready_reconfirm_failed}


# ── quiet_rx ────────────────────────────────────────────────────────────────

def gen_quiet_rx() -> list[dict[str, Any]]:
    import sw_core.session_manager as sm

    long_banner = "x" * 300 + "U-Boot TPL 2024.04" + "y" * 40
    cases: list[dict[str, Any]] = [
        {"template": "prpl-template", "steps": [(0.0, "U-Boot 2024.04 (Jan 01)\r\n"), (1.0, "Hit any key to stop autoboot:  3"),
                                                (2.0, "\r\nStarting kernel\r\n"), (40.0, "\r\nroot@prplOS:~# ")]},
        {"template": "prpl-template", "steps": [(0.0, "U-Bo"), (0.1, "ot 2024.04\r\n"), (5.0, "login: ")]},
        {"template": "prpl-template", "steps": [(0.0, long_banner)]},
        {"template": "prpl-template", "steps": [(0.0, "U-Boot 2024.04\r\n"), (3.0, "=> "), (4.0, "\r\n=> ")]},
        {"template": "brcm-template", "steps": [(0.0, "CFE version 1.0\r\n"), (1.0, "CFE> "), (2.0, "\r\nboot\r\n"), (9.0, "\r\n# ")]},
        {"template": "prpl-template", "steps": [(0.0, "U-Boot\r\n"), (200.0, "root@prplOS:~# "), (301.0, "noise")]},
        {"template": "prpl-template", "steps": [(0.0, "U-Boot\r\n"), (100.0, "U-Boot again\r\n"), (250.0, "x"), (310.0, "y")]},
        {"template": "prpl-template", "steps": [(0.0, "plain text\r\n"), (1.0, "root@prplOS:~# ")]},
        {"template": "others-template", "steps": [(0.0, "U-Boot 2024.04\r\n")]},
        {"template": "smart4R-profile", "steps": [(0.0, "CFE version 2\r\n"), (2.0, "root@Speedport Smart 4 Typ A:~# ")]},
        {"template": "prpl-template", "steps": [(0.0, "U-Boot 2024.04\r\n"), (1.0, "\r\nlogin: "), (2.0, "Hit any key to stop autoboot:  1")]},
        {"template": "prpl-template", "profile_over": {"prompt_regex": "("}, "steps": [(0.0, "U-Boot\r\n"), (1.0, "root@prplOS:~# ")]},
        {"template": "prpl-template", "steps": [(0.0, "y" * 250 + "CFE ver"), (0.5, "sion 3\r\n")]},
        # #234 boot_done_regex（add-only，014 起）：quiet 期間命中即解除、pending 不清；受 bootloader 尾行例外約束；
        # 無效 regex 靜默略過；quiet 未生效時不作用；跨 chunk 以 rolling tail 拼接；passthrough 一律不作用。
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "Please press Enter to activate this console"},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (20.0, "\r\nPlease press Enter to activate this console.\r\n"),
                   (21.0, "U-Boot again\r\n")]},
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "activate this console"},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (5.0, "Please press Enter to activate this console.\r\n=> ")]},
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "("},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (5.0, "Please press Enter to activate this console.\r\n")]},
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "activate this console"},
         "steps": [(0.0, "plain\r\n"), (1.0, "Please press Enter to activate this console.\r\n")]},
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "activate this console"},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (5.0, "Please press Enter to acti"), (5.1, "vate this console.\r\n")]},
        {"template": "others-template", "profile_over": {"boot_done_regex": "activate this console"},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (1.0, "Please press Enter to activate this console.\r\n")]},
        {"template": "prpl-template", "profile_over": {"boot_done_regex": "(?m)^ready$"},
         "steps": [(0.0, "U-Boot 2024.04\r\n"), (3.0, "\r\nready\r\n"), (4.0, "\r\nready")]},
    ]
    rows = []
    for case in cases:
        with golden_env() as (clock, td):
            base = clock.now
            mgr, session, _ = build({"template": case["template"], "profile_over": case.get("profile_over", {}), "state": "READY"}, clock, td)
            steps_out = []
            for t, chunk in case["steps"]:
                clock.now = base + t
                with mgr._lock:
                    mgr._update_boot_quiet_locked(session, chunk)
                now = clock.now
                steps_out.append({
                    "t": t, "chunk": chunk,
                    "until_rel": None if session.boot_quiet_until <= 0.0 else round(session.boot_quiet_until - base, 6),
                    "tail": session.boot_banner_tail, "pending": session.ready_reconfirm_pending,
                    "deadline_rel": None if session.ready_reconfirm_deadline <= 0.0 else round(session.ready_reconfirm_deadline - base, 6),
                    "attempts": session.ready_reconfirm_attempts, "failed": session.ready_reconfirm_failed,
                    "quiet_remaining_s": (round(session.boot_quiet_until - now, 1)
                                          if session.boot_quiet_until > 0.0 and session.boot_quiet_until > now else None),
                    "reconfirm_remaining_s": session.ready_reconfirm_remaining_s(now),
                })
            rows.append({"profile": profile_json(session.profile), "base_mono": base, "steps": steps_out})
    _ = sm
    return rows_with_ids("quiet_rx", rows)


# ── gate_responses ──────────────────────────────────────────────────────────

def gen_gate_responses() -> list[dict[str, Any]]:
    from sw_core.service import SerialwrapService

    rows: list[dict[str, Any]] = []
    quiet_specs = [
        {"until_rel": 120.04, "pending": True, "deadline_rel": 280.0},
        {"until_rel": None, "pending": True, "deadline_rel": 100.0},
        {"until_rel": None, "pending": True, "deadline_rel": -1.0},
        {"until_rel": None, "pending": True, "deadline_rel": 100.0, "attempts": 5},
        {"until_rel": None, "pending": False, "failed": True},
        {"until_rel": None, "pending": False},
        {"until_rel": 0.04, "pending": True, "deadline_rel": 10.0},
        {"until_rel": -0.5, "pending": True, "deadline_rel": 10.0},
    ]
    # execute 層：_agent_gate_reject_locked
    for q in quiet_specs:
        with golden_env() as (clock, td):
            mgr, session, _ = build({"state": "READY", "quiet": q, "bridge": {}}, clock, td)
            before = quiet_json(session)
            with mgr._lock:
                resp = mgr._agent_gate_reject_locked(session)
            rows.append({"layer": "execute", "quiet": before, "com": "COM0", "now": clock.now,
                         "response": None if resp is None else wire(resp), "quiet_after": quiet_json(session)})
    # submit 層：service._resolve_session_id 的 gate 段（READY session；source／trusted_human 變體）
    for q in quiet_specs:
        for source, trusted in [("agent", False), ("human:c1", False), ("human:c1", True)]:
            with golden_env() as (clock, td):
                mgr, session, _ = build({"state": "READY", "quiet": q, "bridge": {}}, clock, td)
                svc = SerialwrapService.__new__(SerialwrapService)
                svc._sessions = mgr
                public = mgr.get_session_state("COM0")["session"]
                _sid, resp = svc._resolve_session_id("COM0", source=source, trusted_human=trusted)
                rows.append({"layer": "submit", "public_session": public, "source": source, "trusted_human": trusted,
                             "response": None if resp is None else wire(resp)})
    return rows_with_ids("gate_responses", rows)


# ── reprobe_decisions ───────────────────────────────────────────────────────

def _reprobe_row(spec: dict[str, Any], op: str, **extra: Any) -> dict[str, Any]:
    with golden_env() as (clock, td):
        mgr, session, bridge = build(spec, clock, td)
        now = clock.now
        before = {"attempts": session.reprobe_attempts, "next_reprobe_at": session.next_reprobe_at,
                  "exhausted": session.reprobe_exhausted, "quiet": quiet_json(session), "state": session.state,
                  "last_error": session.last_error}
        action = None
        with mgr._lock:
            if op == "prepare":
                job = mgr._prepare_reprobe_locked(session, now)
                action = None if job is None else job[0]
            elif op == "record":
                mgr._record_reprobe_attempt_locked(session, now)
        if op == "finish":
            result_state = extra["result_state"]
            mgr._finish_probe_reprobe(session.session_id, bridge, {"ok": True, "session": {"state": result_state}}, now)
        return {"op": op, "setup": spec, "before": before, "now": now, "action": action,
                "attempts": session.reprobe_attempts,
                "next_reprobe_at_rel": None if session.next_reprobe_at is None else round(session.next_reprobe_at - now, 6),
                "exhausted": session.reprobe_exhausted, "state": session.state, "pending": session.ready_reconfirm_pending,
                "failed": session.ready_reconfirm_failed, "ready_reconfirm_attempts": session.ready_reconfirm_attempts,
                "last_error": session.last_error, "last_error_detail": session.last_error_detail, **extra}


def gen_reprobe_decisions() -> list[dict[str, Any]]:
    att = {"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "bridge": {}, "last_rx_rel": 10.0}
    rows = [
        _reprobe_row(att, "prepare"),
        _reprobe_row(dict(att, last_rx_rel=1.0), "prepare"),
        _reprobe_row(dict(att, last_rx_rel=None), "prepare"),
        _reprobe_row(dict(att, reprobe={"attempts": 3, "next_rel": 5.0}), "prepare"),
        _reprobe_row(dict(att, reprobe={"attempts": 3, "next_rel": -1.0}), "prepare"),
        _reprobe_row(dict(att, reprobe={"attempts": 10}), "prepare"),
        _reprobe_row(dict(att, reprobe={"exhausted": True}), "prepare"),
        _reprobe_row(dict(att, last_error="CREDENTIALS_UNRESOLVED"), "prepare"),
        _reprobe_row(dict(att, last_error="LOGIN_REQUIRED"), "prepare"),
        _reprobe_row(dict(att, last_error="RX_FLOOD"), "prepare"),
        _reprobe_row(dict(att, last_error="TRANSPORT_STALL"), "prepare"),
        _reprobe_row(dict(att, last_error="SHELL_PROMPT_TIMEOUT"), "prepare"),
        _reprobe_row(dict(att, device_present=False), "prepare"),
        _reprobe_row(dict(att, released_by_id=True), "prepare"),
        _reprobe_row(dict(att, quiet={"until_rel": 50.0, "pending": True, "deadline_rel": 200.0}), "prepare"),
        _reprobe_row(dict(att, template="smart4R-profile", quiet={"until_rel": 50.0, "pending": True, "deadline_rel": 200.0}), "prepare"),
        _reprobe_row(dict(att, template="others-template"), "prepare"),
        _reprobe_row(dict(att, lease={"owner": "human:c1", "last_human_input_rel": 5.0}), "prepare"),
        _reprobe_row(dict(att, lease={"owner": "human:c1", "last_human_input_rel": 100.0}), "prepare"),
        _reprobe_row(dict(att, bridge=None), "prepare"),
        _reprobe_row({"state": "DETACHED", "last_error": "PRPL_PROMPT_TIMEOUT", "last_rx_rel": 10.0}, "prepare"),
        _reprobe_row({"state": "DETACHED", "last_error": "PROMPT_UNAVAILABLE", "last_rx_rel": 10.0}, "prepare"),
        _reprobe_row({"state": "READY", "bridge": {}, "last_rx_rel": 10.0, "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0}}, "prepare"),
        _reprobe_row({"state": "READY", "bridge": {}, "last_rx_rel": 10.0, "quiet": {"until_rel": None, "pending": True, "deadline_rel": -1.0}}, "prepare"),
        _reprobe_row({"state": "READY", "bridge": {}, "last_rx_rel": 10.0, "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0},
                      "reprobe": {"exhausted": True}}, "prepare"),
        _reprobe_row({"state": "READY", "bridge": {}, "last_rx_rel": 10.0}, "prepare"),
        _reprobe_row({"state": "FLASHING", "bridge": {}, "last_error": "PROMPT_UNAVAILABLE", "last_rx_rel": 10.0}, "prepare"),
    ]
    for attempts in range(0, 10):
        rows.append(_reprobe_row(dict(att, reprobe={"attempts": attempts}), "record"))
    rows.append(_reprobe_row(att, "finish", result_state="READY"))
    rows.append(_reprobe_row(att, "finish", result_state="ATTACHED"))
    rows.append(_reprobe_row(dict(att, bridge={"tail": "\r\n=> "}), "finish", result_state="ATTACHED"))
    rows.append(_reprobe_row(dict(att, bridge={"tail": "\r\n=> "}, template="brcm-template"), "finish", result_state="ATTACHED"))
    rows.append(_reprobe_row({"state": "READY", "bridge": {}, "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0, "attempts": 4}},
                             "finish", result_state="ATTACHED"))
    rows.append(_reprobe_row({"state": "READY", "bridge": {}, "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0, "attempts": 1}},
                             "finish", result_state="ATTACHED"))
    # #162 A6：pending 中確認 probe 失敗（session 已落 ATTACHED）→ ready_reconfirm_attempts 累加；達上限落 failed。
    rows.append(_reprobe_row({"state": "ATTACHED", "last_error": "READY_NONCE_TIMEOUT", "bridge": {},
                              "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0, "attempts": 2}},
                             "finish", result_state="ATTACHED"))
    rows.append(_reprobe_row({"state": "ATTACHED", "last_error": "READY_NONCE_TIMEOUT", "bridge": {},
                              "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0, "attempts": 4}},
                             "finish", result_state="ATTACHED"))
    return rows_with_ids("reprobe_decisions", rows)


# ── reprobe_valid ───────────────────────────────────────────────────────────

def gen_reprobe_valid() -> list[dict[str, Any]]:
    att = {"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "bridge": {}, "last_rx_rel": 10.0}
    ready = {"state": "READY", "bridge": {}, "last_rx_rel": 10.0, "quiet": {"until_rel": None, "pending": True, "deadline_rel": 100.0}}
    specs: list[tuple[dict[str, Any], bool]] = [
        (att, False), (att, True), (dict(att, released_by_id=True), False), (dict(att, device_present=False), False),
        (dict(att, state="DETACHED"), False), (dict(att, state="FLASHING"), False), (dict(att, template="uboot-template"), False),
        (dict(att, reprobe={"exhausted": True}), False), (dict(att, last_error="LOGIN_REQUIRED"), False),
        (dict(att, quiet={"until_rel": 50.0}), False), (dict(att, template="smart4R-profile", quiet={"until_rel": 50.0}), False),
        (dict(att, lease={"owner": "human:c1", "last_human_input_rel": 5.0}), False),
        (ready, False), (dict(ready, quiet={"until_rel": None, "pending": False}), False),
        (dict(ready, quiet={"until_rel": None, "pending": True, "deadline_rel": -1.0}), False),
        (dict(ready, bridge={"tail": "\r\n=> "}), False), (dict(ready, bridge={"tail": "\r\nU-Boot> "}, profile_over={"bootloader_prompts": []}), False),
        (dict(ready, quiet={"until_rel": 20.0, "pending": True, "deadline_rel": 100.0}), False),
    ]
    rows = []
    for spec, other_bridge in specs:
        with golden_env() as (clock, td):
            mgr, session, bridge = build(spec, clock, td)
            now = clock.now
            before = {"quiet": quiet_json(session), "reprobe": {"attempts": session.reprobe_attempts, "exhausted": session.reprobe_exhausted}}
            target = object() if other_bridge else bridge
            with mgr._lock:
                valid = mgr._reprobe_target_still_valid_locked(session, target, now)  # type: ignore[arg-type]
            rows.append({"setup": spec, "same_bridge": not other_bridge, "before": before, "now": now, "valid": valid,
                         "state": session.state, "pending": session.ready_reconfirm_pending, "failed": session.ready_reconfirm_failed,
                         "last_error": session.last_error})
    return rows_with_ids("reprobe_valid", rows)


# ── self_test_responses ─────────────────────────────────────────────────────

def gen_self_test_responses() -> list[dict[str, Any]]:
    ready = {"state": "READY", "bridge": {"waits": [True, True]}}
    attached = {"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "bridge": {}}
    specs: list[dict[str, Any]] = [
        dict(ready, call={"selector": "COM9"}),
        {"state": "RELEASED", "released": {"by": "flasher", "at": FIXED_ISO, "reason": "mcu"}, "holder_pids": []},
        {"state": "RELEASED", "released": {"by": "flasher", "at": FIXED_ISO, "reason": "mcu"}, "holder_pids": [4242, 4343]},
        {"state": "RELEASED", "released": {"by": None, "at": None, "reason": None}, "device_present": False},
        dict(attached, recovering=True),
        dict(ready, lease={"owner": "human:c1", "last_human_input_rel": 5.0}, call={"strict_human_lock": True}),
        dict(ready, device_present=False),
        dict(ready, attached_real_path="/dev/ttyOLD"),
        {"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "bridge": None},
        dict(ready, bridge={"snapshot": {"running": False}}),
        dict(ready, bridge={"snapshot": {"vtty_alive": False}}),
        dict(ready, template="uboot-template"),
        dict(attached, template="uboot-template", last_error=None, last_rx_rel=12.0),
        dict(attached, template="others-template", last_error=None, profile_source="fallback", bridge={"tail": "\r\nroot@prplOS:~# "}),
        dict(attached, last_error="LOGIN_REQUIRED"),
        dict(attached, last_error="REBOOTING"),
        dict(attached, bridge={"tail": "\r\nU-Boot 2024.04\r\n=> "}),
        dict(attached, profile_over={"bootloader_prompts": []}, bridge={"tail": "\r\n=> "}),
        dict(attached, last_error="BOOTLOADER_STUCK", last_error_detail="matched_prompt=^=> $"),
        dict(attached, last_error="RX_FLOOD", bridge={"rx_bytes": 1200}),
        dict(attached, bridge={"rx_bytes": 20000}),
        dict(attached, bridge={"rx_bytes": 19999}),
        dict(attached, bridge={"tail": "\r\n=> ", "rx_bytes": 25000}),
        dict(ready, quiet={"until_rel": 100.0, "pending": True, "deadline_rel": 250.0}),
        ready,
        dict(ready, bridge={"waits": [False]}),
        dict(ready, bridge={"waits": [False], "rx_bytes": 30000}),
        dict(ready, lease={"owner": "human:c1", "last_human_input_rel": 5.0}),
        dict(ready, lease={"owner": "agent:x", "last_human_input_rel": None}),
        dict(ready, quiet={"until_rel": None, "pending": True, "deadline_rel": 100.0}),
    ]
    rows = []
    for spec in specs:
        with golden_env() as (clock, td):
            import sw_core.session_manager as sm

            mgr, session, bridge = build(spec, clock, td)
            holder = {"pids": spec.get("holder_pids", []), "holder": None}
            call = dict(spec.get("call", {}))
            selector = call.pop("selector", "COM0")
            start = clock.now
            with mock.patch.object(sm.SessionManager, "_probe_external_holder", lambda _self, _p: holder):
                resp = mgr.self_test(selector, timeout_s=call.get("timeout_s", 2.0), strict_human_lock=call.get("strict_human_lock", False))
            rows.append({"setup": spec, "response": wire(resp), "sent": [] if bridge is None else bridge.sent,
                         "virtual_elapsed_s": round(clock.now - start, 6)})
    return rows_with_ids("self_test_responses", rows)


# ── recover_responses ───────────────────────────────────────────────────────

def gen_recover_responses() -> list[dict[str, Any]]:
    attached = {"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "bridge": {}}
    ready = {"state": "READY", "bridge": {}}
    specs: list[dict[str, Any]] = [
        dict(ready, call={"selector": "COM9"}),
        dict(attached, pending_drain=True),
        {"state": "RELEASED", "released": {"by": "flasher", "at": FIXED_ISO, "reason": "mcu"}},
        dict(ready, state="FLASHING"),
        {"state": "DETACHED", "last_error": "PRPL_PROMPT_TIMEOUT", "bridge": None},
        {"state": "DETACHED", "last_error": "PRPL_PROMPT_TIMEOUT", "bridge": None, "device_present": False},
        dict(attached, probe_result={"state": "READY", "last_error": None, "last_error_detail": None}),
        dict(attached, probe_result={"state": "ATTACHED", "last_error": "READY_NONCE_TIMEOUT", "last_error_detail": "tail text"}),
        dict(attached, probe_result={"state": "ATTACHED", "last_error": "PROMPT_UNAVAILABLE", "last_error_detail": None},
             bridge={"tail": "\r\n=> "}),
        dict(attached, probe_result={"state": "ATTACHED", "last_error": "BOOTLOADER_STUCK", "last_error_detail": "matched_prompt=^=> $"}),
        dict(attached, probe_result={"state": "ATTACHED", "last_error": "CREDENTIALS_UNRESOLVED", "last_error_detail": None}),
        dict(attached, template="uboot-template", last_error=None, probe_result={"state": "ATTACHED", "last_error": None, "last_error_detail": None},
             bridge={"tail": "\r\n=> "}),
        dict(attached, probe_result={"ok": False, "error_code": "STATE_CHANGED"}),
        dict(attached, call={"force": True}, force_ready_after=None),
        dict(attached, call={"force": True}, force_ready_after=3),
        dict(attached, state="DETACHED", bridge={}, call={"force": True}, force_ready_after=None),
        dict(attached, state="DETACHED", bridge={}),
        dict(ready, bridge={"waits": [True], "text": "out\r\nroot@prplOS:~# "}),
        dict(ready, bridge={"waits": [False, True], "text": "out\r\nroot@prplOS:~# "}),
        dict(ready, bridge={"waits": [False, False]}, force_ready_after=None),
        dict(ready, bridge={"waits": [False, False]}, force_ready_after=2),
        dict(ready, bridge={"waits": [False, False]}, call={"force": True}, force_ready_after=None),
        dict(ready, quiet={"until_rel": 100.0, "pending": True, "deadline_rel": 200.0}, bridge={"waits": []}, force_ready_after=None),
        dict(ready, template="uboot-template", bridge={"waits": [], "text": "partial"}),
    ]
    rows = []
    for spec in specs:
        with golden_env() as (clock, td):
            import sw_core.session_manager as sm

            mgr, session, bridge = build(spec, clock, td)
            call = dict(spec.get("call", {}))
            selector = call.pop("selector", "COM0")
            start = clock.now
            polls = {"n": 0}

            def fake_clear(_selector: str, *a: Any, **k: Any) -> dict[str, Any]:
                with mgr._lock:
                    session.bridge = None
                    session.state = "ATTACHING"
                return {"ok": True}

            real_get_state = mgr.get_session_state

            def fake_get_state(sel: str) -> dict[str, Any]:
                polls["n"] += 1
                after = spec.get("force_ready_after")
                if after is not None and polls["n"] >= after:
                    with mgr._lock:
                        session.state = "READY"
                return real_get_state(sel)

            def fake_probe(_session: Any, _bridge: Any) -> dict[str, Any]:
                pr = spec["probe_result"]
                if pr.get("ok") is False:
                    return {"ok": False, "error_code": pr["error_code"], "session": session.to_public_dict()}
                with mgr._lock:
                    session.state = pr["state"]
                    session.last_error = pr["last_error"]
                    session.last_error_detail = pr["last_error_detail"]
                return {"ok": True, "session": session.to_public_dict()}

            spawned: list[str] = []
            with mock.patch.object(mgr, "clear_session", fake_clear), \
                 mock.patch.object(mgr, "get_session_state", fake_get_state), \
                 mock.patch.object(mgr, "_probe_existing_bridge", fake_probe), \
                 mock.patch.object(mgr, "_spawn_attach", lambda by_id: spawned.append(by_id)), \
                 mock.patch.object(sm.SessionManager, "_send_bytes_with_permit",
                                   staticmethod(lambda b, payload, *, source, cmd_id, permit: b.send_bytes(payload, source=source, cmd_id=cmd_id))):
                resp = mgr.recover_session(selector, timeout_s=call.get("timeout_s", 2.0), force=call.get("force", False))
            rows.append({"setup": spec, "response": wire(resp), "sent": [] if bridge is None else bridge.sent,
                         "spawned_attach": spawned, "force_polls": polls["n"], "virtual_elapsed_s": round(clock.now - start, 6)})
    return rows_with_ids("recover_responses", rows)


# ── log_responses ───────────────────────────────────────────────────────────

def gen_log_responses() -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {"op": "start", "log_dir": "<TMP>/blog"},
        {"op": "start", "log_dir": "<TMP>/blog", "capture_active": True},
        {"op": "start", "log_dir": "<TMP>/blog", "call": {"selector": "COM9"}},
        {"op": "start", "log_dir": "/proc/serialwrap-golden-no-such-dir"},
        {"op": "start", "log_dir": "<TMP>/blog", "log_path_is_dir": True},
        {"op": "stop", "log_dir": "<TMP>/blog", "capture_active": True},
        {"op": "stop", "log_dir": "<TMP>/blog"},
        {"op": "stop", "log_dir": "<TMP>/blog", "call": {"selector": "COM9"}},
        {"op": "status", "log_dir": "<TMP>/blog", "capture_active": True},
        {"op": "status", "log_dir": "<TMP>/blog"},
        {"op": "status", "log_dir": "<TMP>/blog", "call": {"selector": "COM9"}},
    ]
    rows = []
    for spec in specs:
        with golden_env() as (clock, td):
            full = dict(spec, state="READY", bridge={})
            mgr, _session, _ = build(full, clock, td)
            selector = spec.get("call", {}).get("selector", "COM0")
            if spec.get("log_path_is_dir"):
                os.makedirs(os.path.join(td, "blog", "COM0_" + FIXED_STRFTIME + ".log"))
            fn = {"start": mgr.log_start, "stop": mgr.log_stop, "status": mgr.log_status}[spec["op"]]
            try:
                resp_s = wire(fn(selector)).replace(td, "/golden")
                raised = None
            except OSError as exc:      # Python 例外穿出 manager → framing EXCEPTION
                resp_s, raised = None, type(exc).__name__
            rows.append({"setup": spec, "response": resp_s, "raised": raised})
            for fp in list(mgr._capture_fps.values()):
                fp.close()
    _ = json
    return rows_with_ids("log_responses", rows)
