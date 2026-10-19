"""Task P1-OC-T6: SC-35b passthrough 命令路徑：完成條件、無救援按鍵、file.* 拒絕。

依 02b-stage0-owner-confirmations.md T6 規格：
- passthrough 完成條件三層：
  (1) 有 completion_regex → #230 producer 完成；
  (2) 否則 profile prompt_regex 通過 valid_completion_regex → 既有 prompt 完成；
  (3) 否則（others-template .*）→ 等待迴圈超時後回 PROMPT_TIMEOUT、partial=True、recovery_action=NONE。
- passthrough 未命中完成條件時不送 \\x03／\\x04、不轉 ATTACHED、不 _force_recover，session 維持 READY。
- passthrough 下 _is_reboot_command 特例不套用。
- file.push／file.pull 對 passthrough（任何 state）回 PROFILE_NOT_COMMAND_CAPABLE＋hint＋session、零 TX。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import os
try:  # POSIX-only：原生 Windows 無 pty 模組；僅 POSIX PTY 測試（posix_only + posix_pty）使用
    import pty
except ImportError:  # pragma: no cover -- 原生 Windows
    pty = None
import select
import threading
try:  # POSIX-only：原生 Windows 無 tty 模組；僅 POSIX PTY 測試（posix_only + posix_pty）使用
    import tty
except ImportError:  # pragma: no cover -- 原生 Windows
    tty = None
from typing import Any, Callable, Generator

import pytest

from sw_core.config import SessionProfile, UartProfile
from sw_core.session_manager import SessionManager, SessionRuntime
from sw_core.service import SerialwrapService
from sw_core.uart_io import UARTBridge
from sw_core.wal import WalWriter

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]


class PtyTarget:
    """以 PTY 模擬 UART target，記錄收到的 TX bytes 並依規則回應。"""

    def __init__(
        self,
        *,
        on_write: Callable[[bytes], bytes | None] | None = None,
    ) -> None:
        if pty is None or tty is None:
            pytest.skip("pty/tty 模組僅在 POSIX 環境可用")
        self.master_fd, self.slave_fd = pty.openpty()
        tty.setraw(self.slave_fd)
        self.path = os.ttyname(self.slave_fd)
        self.tx: list[bytes] = []
        self._stop = threading.Event()
        self._on_write = on_write
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self.master_fd], [], [], 0.02)
            if not r:
                continue
            try:
                data = os.read(self.master_fd, 4096)
            except OSError:
                break
            if not data:
                break
            self.tx.append(data)
            if self._on_write is not None:
                reply = self._on_write(data)
                if reply:
                    try:
                        os.write(self.master_fd, reply)
                    except OSError:
                        break

    def write(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def _make_others_profile(com: str = "COM0") -> SessionProfile:
    """建立 others-template profile（passthrough、prompt_regex='.*'、空 ready_probe）。"""
    return SessionProfile(
        profile_name="others-template",
        com=com,
        act_no=1,
        alias=f"lab+{com}",
        device_by_id=f"/dev/serial/by-id/fake-{com}",
        platform="passthrough",
        uart=UartProfile(),
        prompt_regex=".*",
        ready_probe="",
    )


def _make_uboot_profile(com: str = "COM0") -> SessionProfile:
    """建立 uboot-template profile（passthrough、可用 prompt_regex、有 ready_probe）。"""
    return SessionProfile(
        profile_name="uboot-template",
        com=com,
        act_no=1,
        alias=f"lab+{com}",
        device_by_id=f"/dev/serial/by-id/fake-{com}",
        platform="passthrough",
        uart=UartProfile(),
        prompt_regex=r"(?mi)^(=>|u-boot>|cfe>) $",
        ready_probe="echo __READY__${nonce}",
    )


def _make_prpl_profile(com: str = "COM0") -> SessionProfile:
    """建立非 passthrough（prpl）profile，供回歸防護比對。"""
    return SessionProfile(
        profile_name="prpl",
        com=com,
        act_no=1,
        alias=f"lab+{com}",
        device_by_id=f"/dev/serial/by-id/fake-{com}",
        platform="prpl",
        uart=UartProfile(),
        prompt_regex=r"(?m)^root@.*# ",
        ready_probe="echo __READY__${nonce}",
    )


def _drive_passthrough_ready(mgr: SessionManager, session: SessionRuntime) -> None:
    """以 T5 helper _promote_quiet_passthrough_locked 驅動 passthrough 進入 READY。

    若 T5 尚未實作（TDD red stage 期間），fallback 到直接設定 state 為 READY，
    以確保測試失敗原因集中於 T6 待實作的命令完成與恢復行為，而非相依介面的 AttributeError。
    """
    session.state = "ATTACHED"
    if hasattr(session, "passthrough_quiet_since"):
        session.passthrough_quiet_since = 1000.0
    promote = getattr(mgr, "_promote_quiet_passthrough_locked", None)
    if callable(promote):
        with mgr._lock:
            promoted = promote(2000.0)
            if session.session_id not in promoted:
                session.state = "READY"
    else:
        with mgr._lock:
            session.state = "READY"


@contextmanager
def setup_device(
    tmp_path: Any,
    profile: SessionProfile,
    *,
    on_write: Callable[[bytes], bytes | None] | None = None,
) -> Generator[tuple[PtyTarget, UARTBridge, SessionManager, SessionRuntime, WalWriter], None, None]:
    """建立測試所需的 PtyTarget、UARTBridge、SessionManager 與 WalWriter。"""
    target = PtyTarget(on_write=on_write)
    wal = WalWriter(wal_dir=str(tmp_path / "wal"))
    bridge = UARTBridge(profile.com, target.path, profile.uart, wal)
    bridge.start()
    mgr = SessionManager(
        [profile],
        wal,
        on_ready=lambda _: None,
        on_detached=lambda _: None,
        state_path=str(tmp_path / "state.json"),
    )
    session = mgr.get_session(profile.com)
    assert session is not None
    session.bridge = bridge
    bridge._on_rx_data = lambda data, wal_seq=None: mgr._on_bridge_rx(session.session_id, data, wal_seq=wal_seq)
    try:
        yield target, bridge, mgr, session, wal
    finally:
        bridge.stop()
        target.close()
        if not wal.closed:
            wal.close()


def test_completion_regex_producer_completes(tmp_path: Any) -> None:
    """others-template READY；target 回 out\\n__RC__=0\\n__END__\\n、不回 prompt；
    completion_regex='(?m)^__RC__=[0-9]+\\r*\\n__END__\\r*$'。
    斷言：ok is True、partial is False、'out' in stdout 且 stdout 以完成標記結尾。
    """
    profile = _make_others_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"run_proc" in data:
            return b"out\r\n__RC__=0\r\n__END__\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)
        completion_regex = r"(?m)^__RC__=[0-9]+\r*\n__END__\r*$"
        result = mgr.execute_command(
            session.session_id,
            "run_proc",
            "agent:test",
            "cmd-t1",
            timeout_s=2.0,
            completion_regex=completion_regex,
        )
        assert result["ok"] is True
        assert result["partial"] is False
        assert "out" in result["stdout"]
        assert result["stdout"].strip().endswith("__END__")


@pytest.mark.parametrize("prompt_regex", [".*", "["], ids=["empty-match", "invalid-regex"])
def test_completion_regex_stops_stdout_at_marker_for_unusable_prompt(
    tmp_path: Any, prompt_regex: str
) -> None:
    """完成標記後同 chunk 的 prompt／log 不進 stdout，無效 prompt 也不拋錯。"""
    profile = replace(_make_others_profile(), prompt_regex=prompt_regex)
    response = (
        b"run_proc\r\nout\r\n__RC__=0\r\n__END__\r\n"
        b"# next prompt\r\nasync log line\r\n"
    )

    def on_write(data: bytes) -> bytes | None:
        return response if b"run_proc" in data else None

    with setup_device(tmp_path, profile, on_write=on_write) as (_target, _bridge, mgr, session, _wal):
        _drive_passthrough_ready(mgr, session)
        completion_regex = r"(?m)^__RC__=[0-9]+\r*\n__END__\r*$"
        result = mgr.execute_command(
            session.session_id,
            "run_proc",
            "agent:test",
            "cmd-completion-tail",
            timeout_s=2.0,
            completion_regex=completion_regex,
        )

    assert result["ok"] is True
    assert result["stdout"] == "run_proc\nout\n__RC__=0\n__END__"


def test_uboot_prompt_completes(tmp_path: Any) -> None:
    """uboot 變體 READY；target 回 version\\r\\nU-Boot 2023\\r\\n=> 。
    斷言：ok is True、partial is False、stdout 為既有 _extract_command_stdout 結果。
    """
    profile = _make_uboot_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"version" in data:
            return b"version\r\nU-Boot 2023\r\n=> "
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)
        result = mgr.execute_command(
            session.session_id,
            "version",
            "agent:test",
            "cmd-t2",
            timeout_s=2.0,
        )
        assert result["ok"] is True
        assert result["partial"] is False
        expected_stdout = mgr._extract_command_stdout("version\r\nU-Boot 2023\r\n=> ", "version", profile.prompt_regex)
        assert result["stdout"] == expected_stdout
        assert "U-Boot 2023" in result["stdout"]


def test_unusable_prompt_times_out_without_recovery_keys(tmp_path: Any) -> None:
    """others-template READY、timeout_s=1.0；target 回一行後靜默。
    斷言：回應 == {"ok": False, "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}；
    target 收到的 bytes 只有命令本身（無 b"\\x03"、b"\\x04"）；state 仍 READY；command.get capture chunks 含該行。
    """
    profile = _make_others_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"test_cmd" in data:
            return b"partial output line\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)
        result = mgr.execute_command(
            session.session_id,
            "test_cmd",
            "agent:test",
            "cmd-t3",
            timeout_s=1.0,
        )
        assert result == {
            "ok": False,
            "error_code": "PROMPT_TIMEOUT",
            "partial": True,
            "recovery_action": "NONE",
        }
        assert not any(b"\x03" in b or b"\x04" in b for b in target.tx)
        assert session.state == "READY"
        capture = mgr.get_background_result("cmd-t3")
        assert capture.get("ok") is True
        assert any("partial output line" in chunk for chunk in capture.get("chunks", []))


def test_completion_miss_no_demote(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """uboot 變體、target 不回 prompt、timeout_s=1.0。
    斷言：PROMPT_TIMEOUT、無 \\x03／\\x04、state READY、未呼叫 _force_recover（monkeypatch 擲例外）。
    """
    profile = _make_uboot_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"test_cmd" in data:
            return b"some output without prompt\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)

        def _mock_force_recover(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("passthrough session 逾時不得呼叫 _force_recover")

        monkeypatch.setattr(mgr, "_force_recover", _mock_force_recover)

        result = mgr.execute_command(
            session.session_id,
            "test_cmd",
            "agent:test",
            "cmd-t4",
            timeout_s=1.0,
        )
        assert result.get("error_code") == "PROMPT_TIMEOUT"
        assert result.get("partial") is True
        assert result.get("recovery_action") == "NONE"
        assert not any(b"\x03" in b or b"\x04" in b for b in target.tx)
        assert session.state == "READY"


def test_reboot_command_not_special_on_passthrough(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """others-template READY 送 reboot。
    斷言：未呼叫 _handle_reboot_command（monkeypatch 擲例外）；結果同第 3 層。
    """
    profile = _make_others_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"reboot" in data:
            return b"rebooting now...\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)

        def _mock_handle_reboot(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("passthrough session 送 reboot 不得呼叫 _handle_reboot_command")

        monkeypatch.setattr(mgr, "_handle_reboot_command", _mock_handle_reboot)

        result = mgr.execute_command(
            session.session_id,
            "reboot",
            "agent:test",
            "cmd-t5",
            timeout_s=1.0,
        )
        assert result == {
            "ok": False,
            "error_code": "PROMPT_TIMEOUT",
            "partial": True,
            "recovery_action": "NONE",
        }
        assert not any(b"\x03" in b or b"\x04" in b for b in target.tx)
        assert session.state == "READY"


def test_file_push_pull_passthrough_rejected(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """READY 與 ATTACHED passthrough 各送 file.push／file.pull。
    斷言：error_code == "PROFILE_NOT_COMMAND_CAPABLE"、含 hint 與 session、target 未收到任何 byte。
    """
    profile = _make_others_profile()
    target = PtyTarget()
    wal = WalWriter(wal_dir=str(tmp_path / "wal"))
    monkeypatch.setattr("sw_core.service.WalWriter", lambda: wal)

    dummy_src = tmp_path / "dummy_push.txt"
    dummy_src.write_text("test push data", encoding="utf-8")
    dummy_dst = tmp_path / "dummy_pull.txt"

    service = SerialwrapService(
        [profile],
        by_id_dir=str(tmp_path / "by-id"),
        by_path_dir=str(tmp_path / "by-path"),
        state_path=str(tmp_path / "state.json"),
    )
    bridge = UARTBridge(profile.com, target.path, profile.uart, wal)
    bridge.start()
    session = service._sessions.get_session(profile.com)
    assert session is not None
    session.bridge = bridge

    try:
        # 狀態 1: READY
        _drive_passthrough_ready(service._sessions, session)
        assert session.state == "READY"

        res_push_ready = service.rpc(
            "file.push",
            {"selector": profile.com, "local_path": str(dummy_src), "remote_path": "/tmp/remote_push"},
        )
        assert res_push_ready["ok"] is False
        assert res_push_ready["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE"
        assert "hint" in res_push_ready and res_push_ready["hint"]
        assert "session" in res_push_ready and isinstance(res_push_ready["session"], dict)

        res_pull_ready = service.rpc(
            "file.pull",
            {"selector": profile.com, "remote_path": "/tmp/remote_pull", "local_path": str(dummy_dst)},
        )
        assert res_pull_ready["ok"] is False
        assert res_pull_ready["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE"
        assert "hint" in res_pull_ready and res_pull_ready["hint"]
        assert "session" in res_pull_ready and isinstance(res_pull_ready["session"], dict)

        # 狀態 2: ATTACHED
        with service._sessions._lock:
            session.state = "ATTACHED"

        res_push_att = service.rpc(
            "file.push",
            {"selector": profile.com, "local_path": str(dummy_src), "remote_path": "/tmp/remote_push"},
        )
        assert res_push_att["ok"] is False
        assert res_push_att["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE"
        assert "hint" in res_push_att and res_push_att["hint"]
        assert "session" in res_push_att and isinstance(res_push_att["session"], dict)

        res_pull_att = service.rpc(
            "file.pull",
            {"selector": profile.com, "remote_path": "/tmp/remote_pull", "local_path": str(dummy_dst)},
        )
        assert res_pull_att["ok"] is False
        assert res_pull_att["error_code"] == "PROFILE_NOT_COMMAND_CAPABLE"
        assert "hint" in res_pull_att and res_pull_att["hint"]
        assert "session" in res_pull_att and isinstance(res_pull_att["session"], dict)

        # target 應未收到任何 byte（零 TX）
        assert target.tx == []
    finally:
        bridge.stop()
        target.close()
        if not wal.closed:
            wal.close()


def test_background_mode_same_completion_rules(tmp_path: Any) -> None:
    """mode="background" 重跑第 1、3 列。
    斷言：完成／逾時語意相同；第 3 列 capture 有內容。
    """
    profile = _make_others_profile()

    # 重跑第 1 列（producer 完成條件）
    def on_write_1(data: bytes) -> bytes | None:
        if b"run_proc" in data:
            return b"out\r\n__RC__=0\r\n__END__\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write_1) as (target1, bridge1, mgr1, session1, wal1):
        _drive_passthrough_ready(mgr1, session1)
        completion_regex = r"(?m)^__RC__=[0-9]+\r*\n__END__\r*$"
        res1 = mgr1.execute_command(
            session1.session_id,
            "run_proc",
            "agent:test",
            "cmd-bg1",
            timeout_s=2.0,
            mode="background",
            completion_regex=completion_regex,
        )
        assert res1["ok"] is True
        assert res1["partial"] is False
        assert "out" in res1["stdout"]
        assert res1["stdout"].strip().endswith("__END__")
        assert res1.get("background_capture_id") == "cmd-bg1"
        cap1 = mgr1.get_background_result("cmd-bg1")
        assert cap1.get("ok") is True
        assert any("out" in chunk for chunk in cap1.get("chunks", []))

    # 重跑第 3 列（不可用 prompt，靜默逾時）
    def on_write_3(data: bytes) -> bytes | None:
        if b"test_cmd" in data:
            return b"bg partial output line\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write_3) as (target3, bridge3, mgr3, session3, wal3):
        _drive_passthrough_ready(mgr3, session3)
        res3 = mgr3.execute_command(
            session3.session_id,
            "test_cmd",
            "agent:test",
            "cmd-bg3",
            timeout_s=1.0,
            mode="background",
        )
        assert res3 == {
            "ok": False,
            "error_code": "PROMPT_TIMEOUT",
            "partial": True,
            "recovery_action": "NONE",
        }
        assert not any(b"\x03" in b or b"\x04" in b for b in target3.tx)
        assert session3.state == "READY"
        cap3 = mgr3.get_background_result("cmd-bg3")
        assert cap3.get("ok") is True
        assert len(cap3.get("chunks", [])) > 0
        assert any("bg partial output line" in chunk for chunk in cap3.get("chunks", []))


def test_interactive_mode_unchanged(tmp_path: Any) -> None:
    """READY passthrough mode="interactive"。
    斷言：回 execution_mode == "interactive" 與 lease id（既有語意）。
    """
    profile = _make_others_profile()
    with setup_device(tmp_path, profile) as (target, bridge, mgr, session, wal):
        _drive_passthrough_ready(mgr, session)
        result = mgr.execute_command(
            session.session_id,
            "",
            "agent:test",
            "cmd-t8",
            mode="interactive",
        )
        assert result["ok"] is True
        assert result["execution_mode"] == "interactive"
        assert "interactive_session_id" in result and result["interactive_session_id"]
        assert result["status"] == "interactive"
        assert session.interactive_session_id == result["interactive_session_id"]


def test_non_passthrough_recovery_unchanged(tmp_path: Any) -> None:
    """prpl READY 命令逾時。
    斷言：仍送 \\x03、\\x04 並依既有規則處理（回歸防護）。
    """
    profile = _make_prpl_profile()

    def on_write(data: bytes) -> bytes | None:
        if b"hang_cmd" in data:
            return b"hanging output\r\n"
        return None

    with setup_device(tmp_path, profile, on_write=on_write) as (target, bridge, mgr, session, wal):
        session.state = "READY"
        result = mgr.execute_command(
            session.session_id,
            "hang_cmd",
            "agent:test",
            "cmd-t9",
            timeout_s=0.5,
        )
        assert result["partial"] is True
        assert any(b"\x03" in b for b in target.tx)
        assert any(b"\x04" in b for b in target.tx)
        assert session.state == "ATTACHED"
