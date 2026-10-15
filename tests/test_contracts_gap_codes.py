"""P0-T7：補齊缺少契約測試的錯誤碼（Python 基線特徵化測試）。

對應 ``docs/rust-refactor/plans/01-stage0-contracts.md`` §Task T7。本檔鎖定 **Python
現況**（特徵化），因此寫成後應直接 PASS；本 Task 的紅燈閘門是 T6 的
``python3 -m tools.contracts check --only C09``（``contract_tests`` 為空即 FAIL），
而非本檔的 pytest 紅燈。

涵蓋範圍（每個測試名稱皆對應計畫表格的一列）：

- ``TestGapMemory``：行程內即可驗證的 error_code（alias、file transfer、console、
  service、event、arbiter、CLI bench／tunnel／systemd 替身）。
- ``TestGapFramingTcp``：RPC framing 位元組（``serve_connection``）、TCP 回應格式與
  daemon spawn 失敗；會開 loopback socket／asyncio server，故標 ``process_rpc``。
- ``TestGapPosixLock``：POSIX singleton lock 的 ``SOCKET_UNAVAILABLE``（``posix_only``）。
- ``TestGapLastError``：session last_error 的產生點（login FSM、attach、clear、bind、
  recovery、reprobe、bridge-down）。

以 pytest marker 標註依 T14 規則：本檔混合平台與資源，**模組層不放 marker**，每個 class
自帶一個平台 marker 與資源 marker（marker 註冊由 T14 於 ``tests/conftest.py`` 完成前，
pytest 只會發出 ``PytestUnknownMarkWarning``，不影響執行）。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import errno
import hashlib
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from sw_core import cli
from sw_core.arbiter import CommandArbiter
from sw_core.client import _rpc_call_once
from sw_core.config import SessionProfile, UartProfile
from sw_core.device_watcher import DeviceInfo
from sw_core.file_transfer import pull_file, push_file
from sw_core.login_fsm import LOGIN_FSM_DETAIL_ERRORS, ensure_ready
from sw_core.service import SerialwrapService
from sw_core.service_ctl import service_action
from sw_core.session_manager import (
    SessionManager,
    SessionRuntime,
    _login_fsm_rx_tail_detail,
)
from sw_core.wal import WalWriter

_PROMPT = r"root@host:~# "
_PROMPT_REGEX = r"root@host:~#\s"
_BY_ID = "/dev/serial/by-id/gap-dev0"
_BY_ID_NEW = "/dev/serial/by-id/gap-dev1"


# ---------------------------------------------------------------------------
# 共用輔助
# ---------------------------------------------------------------------------


def _profile(by_id: str | None = _BY_ID, *, platform: str = "prpl", **overrides) -> SessionProfile:
    fields: dict = dict(
        profile_name="gap",
        com="COM0",
        act_no=1,
        alias="gap-alias",
        device_by_id=by_id,
        platform=platform,
        uart=UartProfile(),
    )
    fields.update(overrides)
    return SessionProfile(**fields)


def _shell_login_profile(**overrides) -> SessionProfile:
    fields: dict = dict(
        profile_name="gap-shell",
        com="COM0",
        act_no=1,
        alias="gap-shell+1",
        device_by_id=_BY_ID,
        platform="shell",
        prompt_regex=_PROMPT,
        login_regex=r"(?mi)^login:\s*$",
        password_regex=r"(?mi)^password:\s*$",
        ready_probe="echo __READY__${nonce}",
        uart=UartProfile(),
    )
    fields.update(overrides)
    return SessionProfile(**fields)


def _mgr(tmp_path: Path, profiles: list[SessionProfile]) -> SessionManager:
    return SessionManager(
        profiles,
        WalWriter(wal_dir=str(tmp_path)),
        on_ready=lambda _sid: None,
        on_detached=lambda _sid: None,
        state_path=str(tmp_path / "state.json"),
    )


def _last_json_line(text: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "預期 stdout 至少有一行 JSON"
    return json.loads(lines[-1])


def _record_last_error(monkeypatch: pytest.MonkeyPatch) -> list:
    """包裝 ``SessionRuntime.last_error`` 的 setter，記錄每一次寫入值（保留原行為）。"""
    writes: list = []
    prop = SessionRuntime.__dict__["last_error"]

    def _setter(self, value):
        writes.append(value)
        prop.fset(self, value)

    monkeypatch.setattr(SessionRuntime, "last_error", property(prop.fget, _setter))
    return writes


def _join_threads(prefix: str) -> None:
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(timeout=5)


class _ScriptedBridge:
    """login FSM／recovery 測試用 fake bridge：``wait_for_regex`` 依序回放，其餘查詢回固定值。

    ``waits`` 耗盡後一律回 False（逾時），避免測試因 fake 不足而掛住。
    """

    def __init__(self, waits: list[bool] | None = None, tail: str = "") -> None:
        self._waits = list(waits or [])
        self._tail = tail
        self.sent: list[str] = []
        self.device_path = None
        self.vtty_path = None
        # 明確關閉 RX 洪水分類路徑（_maybe_reclassify_flood 對無 rx_stats 的 bridge 直通）。
        self.rx_stats = None

    def clear_rx_buffer(self) -> None:
        pass

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        self.sent.append(cmd)

    def send_secret(self, secret: str) -> None:
        self.sent.append("<secret>")

    def wait_for_regex(self, pattern: str, timeout_s: float) -> bool:
        return self._waits.pop(0) if self._waits else False

    def rx_tail(self, max_chars: int = 4096) -> str:
        return self._tail

    def rx_text_from(self, from_offset: int) -> str:
        return self._tail[from_offset:]

    def rx_total_bytes(self) -> int:
        return 0

    def rx_idle_s(self) -> float:
        return 60.0


class _MiniFileBridge:
    """file_transfer 特徵化測試用 fake（照 tests/test_file_transfer.py 的 _FakeBridge 模式精簡）。

    decoder／encoder 探測以 target sentinel 自動回應；其餘命令依排隊回放，回應缺 sentinel
    時補上（模擬 target 實際執行結果）。未排隊的命令不回任何 RX（模擬 prompt 逾時）。
    """

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._rx = ""
        self._queue: list[str] = []

    def enqueue_rx(self, text: str) -> None:
        self._queue.append(text)

    def send_command(self, cmd: str, *, source: str, cmd_id: str | None = None) -> None:
        if "SW_XFER_DECODER_B64_" in cmd:
            self._rx += "SW_XFER_DECODER_B64_42\r\n" + _PROMPT
            return
        if "SW_XFER_ENCODER_B64_" in cmd:
            self._rx += "SW_XFER_ENCODER_B64_42\r\n" + _PROMPT
            return
        self.commands.append(cmd)
        if not self._queue:
            return
        response = self._queue.pop(0)
        if "SW_XFER_CHUNK_" in cmd and "SW_XFER_CHUNK_42" not in response:
            response = response.replace(_PROMPT, "SW_XFER_CHUNK_42\r\n" + _PROMPT, 1)
        elif "SW_XFER_MD5_" in cmd and "SW_XFER_MD5_42" not in response:
            response = response.replace(_PROMPT, "SW_XFER_MD5_42\r\n" + _PROMPT, 1)
        elif "SW_XFER_MV_" in cmd and "SW_XFER_MV_42" not in response:
            response = response.replace(_PROMPT, "SW_XFER_MV_42\r\n" + _PROMPT, 1)
        self._rx += response

    def rx_snapshot_len(self) -> int:
        return len(self._rx)

    def rx_text_from(self, from_offset: int) -> str:
        return self._rx[from_offset:]

    def wait_for_regex_from(self, pattern: str, from_offset: int, timeout_s: float) -> bool:
        return re.search(pattern, self._rx[from_offset:]) is not None


def _roundtrip(payload: bytes, handler) -> list[bytes]:
    from sw_core.rpc_posix import serve_connection

    async def main():
        server = await asyncio.start_server(
            lambda r, w: serve_connection(r, w, handler, set()), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                break
            lines.append(line)
        writer.close()
        server.close()
        await server.wait_closed()
        return lines

    return asyncio.run(main())


# ---------------------------------------------------------------------------
# 行程內可驗證的 error_code
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
class TestGapMemory:
    def test_alias_not_found_saves_state_first(self, tmp_path: Path) -> None:
        mgr = _mgr(tmp_path, [_profile()])

        assert mgr.unassign_alias("ghost") == {"ok": False, "error_code": "ALIAS_NOT_FOUND", "alias": "ghost"}
        # 即使失敗也先落 state.json（unassign_alias 於回傳前呼叫 _save_state）。
        assert (tmp_path / "state.json").exists()

    def test_base64_decode_failed_pull(self, tmp_path: Path) -> None:
        bridge = _MiniFileBridge()
        # sentinel 區段內容 "QQ" 為錯誤 padding，base64.b64decode 必定拋例外。
        bridge.enqueue_rx("===SW_XFER_BEGIN===\r\nQQ\r\n===SW_XFER_END===\r\n" + _PROMPT)
        local = tmp_path / "out.bin"

        resp = pull_file(bridge, "/remote/x.bin", str(local), timeout_s=5.0, prompt_regex=_PROMPT_REGEX)

        assert resp["ok"] is False
        assert resp["error_code"] == "BASE64_DECODE_FAILED"
        assert not local.exists()

    def test_console_not_found_detach_unknown_client(self, tmp_path: Path) -> None:
        mgr = _mgr(tmp_path, [_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "READY"
        bridge = mock.MagicMock()
        bridge.detach_console.return_value = False
        session.bridge = bridge

        resp = mgr.detach_console("COM0", "nope")

        assert resp["ok"] is False
        assert resp["error_code"] == "CONSOLE_NOT_FOUND"
        assert resp["client_id"] == "nope"

    def test_device_not_bound_attach(self, tmp_path: Path) -> None:
        mgr = _mgr(tmp_path, [_profile()])
        # 建構子會略過未綁定 device 的 profile（不建 session），故以與
        # tests/test_explicit_target_device_ownership.py 相同的手法，於既有 session 上
        # 替換 profile 為 device_by_id=None，模擬「session 存在但未綁定」。
        session = mgr.get_session("COM0")
        assert session is not None
        session.profile = dataclasses.replace(session.profile, device_by_id=None)

        r = mgr.attach_session("COM0")

        assert r["ok"] is False
        assert r["error_code"] == "DEVICE_NOT_BOUND"
        assert "session" in r

    def test_move_timeout_push(self, tmp_path: Path) -> None:
        data = b"hello"
        md5 = hashlib.md5(data).hexdigest()
        bridge = _MiniFileBridge()
        bridge.enqueue_rx(f"ok\r\n{_PROMPT}")                      # chunk 成功
        bridge.enqueue_rx(f"{md5}  /tmp/.sw_upload_x\r\n{_PROMPT}")  # md5 一致
        # 第三筆（mv）不排隊：prompt 不出現 → MOVE_TIMEOUT
        local = tmp_path / "src.bin"
        local.write_bytes(data)

        r = push_file(bridge, str(local), "/remote/dest.bin", chunk_size=4096,
                      timeout_s=5.0, prompt_regex=_PROMPT_REGEX)

        assert r["ok"] is False
        assert r["error_code"] == "MOVE_TIMEOUT"
        # MOVE_TIMEOUT 路徑不做清理：mv 之後不得有 rm 清理命令。
        mv_idx = next(i for i, c in enumerate(bridge.commands) if c.startswith("mv "))
        assert not any("rm -f" in c for c in bridge.commands[mv_idx + 1:])

    def test_not_ready_attach_state_result(self, tmp_path: Path) -> None:
        mgr = _mgr(tmp_path, [_profile()])
        s = mgr.get_session("COM0")
        assert s is not None
        s.state = "ATTACHED"
        s.last_error = None

        assert mgr._attach_state_result(s, s.to_public_dict())["error_code"] == "NOT_READY"

    def test_force_recover_timeout(self, tmp_path: Path) -> None:
        mgr = _mgr(tmp_path, [_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        # clear_session 為 no-op、sleep 為 no-op，session 永不 READY。
        with mock.patch.object(SessionManager, "clear_session", return_value={"ok": True}), \
                mock.patch("time.sleep"):
            r = mgr.recover_session("COM0", force=True)

        assert r["error_code"] == "FORCE_RECOVER_TIMEOUT"
        assert r["action"] == "FORCE_CLEAR_REATTACH"
        assert r["recovered"] is False

    def test_invalid_rule_schema_uses_error_key(self) -> None:
        svc = SerialwrapService([])
        # 缺 kind 欄位 → validate_rule_dict 拋 ValueError → service 層轉 INVALID_RULE_SCHEMA。
        bad_rule = {
            "schema_version": 1,
            "owner": "gap",
            "name": "missing-kind",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
        }

        r = svc.rpc("event.rule_set", bad_rule)

        assert r["ok"] is False
        assert r["error_code"] == "INVALID_RULE_SCHEMA"
        assert "error" in r
        assert "message" not in r

    def test_rule_not_found(self) -> None:
        svc = SerialwrapService([])

        r = svc.rpc("event.rule_get", {"rule_id": "gap.no-such-rule"})

        assert r["ok"] is False
        assert r["error_code"] == "RULE_NOT_FOUND"

    def test_unknown_event_cmd_unreachable(self) -> None:
        # argparse 對不在 choices 內的子命令以 exit 2 結束，UNKNOWN_EVENT_CMD 因此不可達。
        with pytest.raises(SystemExit) as exc_info:
            cli.build_parser().parse_args(["event", "bogus"])
        assert exc_info.value.code == 2

    def test_no_systemd_service_action(self) -> None:
        r = service_action("start", mode="on-demand")

        assert r["ok"] is False
        assert r["error_code"] == "NO_SYSTEMD"
        assert r["mode"] == "on-demand"
        assert r["action"] == "start"
        assert r["hint"]

    def test_command_failed_fallback(self) -> None:
        arb = CommandArbiter(send_cb=lambda *a, **kw: {"ok": False})
        arb.register_session("fake")
        try:
            submitted = arb.submit(session_id="fake", command="echo ok",
                                   source="agent:test", mode="line", timeout_s=1)
            cmd_id = submitted["cmd_id"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if arb.get(cmd_id)["command"]["done_at"]:
                    break
                time.sleep(0.01)
            record = arb.get(cmd_id)["command"]
            assert record["error_code"] == "COMMAND_FAILED"
            assert record["status"] == "error"
        finally:
            arb.unregister_session("fake")

    def test_invalid_bench_config_connect(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
        benches = tmp_path / "benches.yaml"
        # X 缺少必要欄位（remote_socket／local_port／ssh_opts／autossh）→ INVALID_BENCH_CONFIG。
        benches.write_text("benches:\n  X:\n    target: eit@eit-test.example\n", encoding="utf-8")
        monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches))

        rc = cli.main(["connect", "X"])

        assert rc == 1
        assert _last_json_line(capsys.readouterr().out)["error_code"] == "INVALID_BENCH_CONFIG"

    def test_unknown_bench_code_connect(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
        benches = tmp_path / "benches.yaml"
        benches.write_text(
            "benches:\n"
            "  eit-test:\n"
            "    target: eit@eit-test.example\n"
            "    remote_socket: /tmp/serialwrap/serialwrapd.sock\n"
            "    local_port: 7777\n"
            "    ssh_opts: []\n"
            "    autossh: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches))

        rc = cli.main(["connect", "ZZ"])

        out = _last_json_line(capsys.readouterr().out)
        assert rc == 1
        assert out["error_code"] == "UNKNOWN_BENCH_CODE"
        assert out["message"] == "unknown bench code: ZZ"

    def test_tunnel_status_error_malformed(self, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
        from sw_core import remote_tunnel

        # rt.status 回 ok 但缺 tunnels 清單 → _status_tunnels 拋 TUNNEL_STATUS_ERROR。
        # 註：`serialwrap remote status` 直接透傳 rt.status（不經 _status_tunnels）；
        # 可達路徑為 `serialwrap benches`（盤點勘誤，見計畫 T7 表格的說明）。
        monkeypatch.setattr(remote_tunnel, "status", lambda run_dir: {"ok": True})

        rc = cli.main(["benches"])

        assert rc == 1
        assert _last_json_line(capsys.readouterr().out)["error_code"] == "TUNNEL_STATUS_ERROR"


# ---------------------------------------------------------------------------
# framing／TCP／subprocess
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.process_rpc
class TestGapFramingTcp:
    def test_framing_exception_bytes(self) -> None:
        def handler(method, params):
            raise RuntimeError("kaboom")

        lines = _roundtrip(b'{"id":1,"method":"boom"}\n', handler)

        assert lines == [b'{"id":1,"ok":false,"error_code":"EXCEPTION","message":"kaboom"}\n']

    def test_framing_invalid_json_bytes(self) -> None:
        lines = _roundtrip(b"{bad\n", lambda method, params: {"ok": True})

        assert lines == [b'{"ok": false, "error_code": "INVALID_JSON"}\n']

    def test_framing_invalid_request_bytes(self) -> None:
        lines = _roundtrip(b"[1]\n", lambda method, params: {"ok": True})

        assert lines == [b'{"ok": false, "error_code": "INVALID_REQUEST"}\n']

    def test_framing_invalid_method_bytes(self) -> None:
        lines = _roundtrip(b'{"id":7}\n', lambda method, params: {"ok": True})

        assert lines == [b'{"id":7,"ok":false,"error_code":"INVALID_METHOD"}\n']

    def test_invalid_response_non_object(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _serve() -> None:
            conn, _ = srv.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(b"[1]\n")

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            resp = _rpc_call_once(f"tcp://127.0.0.1:{port}", "health.ping", {})
        finally:
            thread.join(timeout=5)
            srv.close()

        assert resp == {"ok": False, "error_code": "INVALID_RESPONSE"}

    def test_daemon_exited_on_spawn(self, tmp_path: Path,
                                    capsys: pytest.CaptureFixture[str]) -> None:
        args = argparse.Namespace(
            profile_dir=str(tmp_path),
            socket=str(tmp_path / "serialwrap.sock"),
            lock=str(tmp_path / "serialwrap.lock"),
            foreground=False,
        )
        proc = mock.Mock(pid=4321, returncode=3)
        proc.poll.return_value = 3  # daemon 在 health 就緒前即退出

        with mock.patch("sw_core.cli.should_auto_spawn", return_value=True), \
                mock.patch("sw_core.cli._probe_healthy_daemon", return_value=False), \
                mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None), \
                mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]), \
                mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])), \
                mock.patch("sw_core.cli.subprocess.Popen", return_value=proc), \
                mock.patch("sw_core.cli.time.sleep"):
            rc = cli._run_daemon_start(args)

        out = _last_json_line(capsys.readouterr().out)
        assert rc == 2
        assert out["error_code"] == "DAEMON_EXITED"
        assert out["pid"] == 4321
        assert out["returncode"] == 3


# ---------------------------------------------------------------------------
# POSIX singleton lock
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
@pytest.mark.memory_only
class TestGapPosixLock:
    def test_socket_unavailable_lock(self, tmp_path: Path) -> None:
        from sw_core.lock_posix import SingletonLock

        sock_path = tmp_path / "serialwrap.sock"
        sock_path.write_text("", encoding="utf-8")  # 路徑存在，但非可連線的 socket
        lock = SingletonLock(str(tmp_path / "serialwrap.lock"), str(sock_path))

        with mock.patch.object(socket.socket, "connect",
                               side_effect=PermissionError(errno.EACCES, "denied")), \
                pytest.raises(RuntimeError, match="^SOCKET_UNAVAILABLE$"):
            lock.acquire()


# ---------------------------------------------------------------------------
# session last_error 產生點
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
class TestGapLastError:
    def test_last_error_pass_env_required(self) -> None:
        # login_fsm.py:112：profile 設 user_env 但缺 pass_env，login prompt 與 password prompt 皆出現。
        profile = _shell_login_profile(user_env="SW_GAP_USER", pass_env=None)
        bridge = _ScriptedBridge(waits=[False, True, True])  # prompt 未就緒 → login → password

        with mock.patch.dict(os.environ, {"SW_GAP_USER": "haman"}):
            ok, err = ensure_ready(bridge, profile)

        assert ok is False
        assert err == "PASS_ENV_REQUIRED"
        # detail 規則：屬 LOGIN_FSM_DETAIL_ERRORS 的失敗碼，detail 為 rx tail（或 None）。
        assert "PASS_ENV_REQUIRED" in LOGIN_FSM_DETAIL_ERRORS
        detail = _login_fsm_rx_tail_detail(bridge)
        assert detail is None or isinstance(detail, str)

    def test_last_error_login_user_required(self) -> None:
        # login_fsm.py:219：login prompt 出現，但未設定帳號（pass_env 有值以進入登入流程）。
        profile = _shell_login_profile(user_env=None, pass_env="SW_GAP_PASS")
        bridge = _ScriptedBridge(waits=[False, True])

        with mock.patch.dict(os.environ, {"SW_GAP_PASS": "secret"}):
            ok, err = ensure_ready(bridge, profile)

        assert ok is False
        assert err == "LOGIN_USER_REQUIRED"
        assert "LOGIN_USER_REQUIRED" in LOGIN_FSM_DETAIL_ERRORS

    def test_last_error_console_unlock_prompt_timeout(self, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        # login_fsm.py:323：console unlock 送出 secret 後 prompt 永不出現。
        # 走 reboot recovery 路徑（ensure_ready 的 err 直接指派給 session.last_error），
        # idle 門檻設 0 以免等待真實靜默時間。
        profile = _shell_login_profile(console_unlock_env="SW_GAP_CONSOLE", console_unlock_idle_s=0.0)
        mgr = _mgr(tmp_path, [profile])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        session.bridge = _ScriptedBridge(waits=[False, False])
        writes = _record_last_error(monkeypatch)

        with mock.patch.dict(os.environ, {"SW_GAP_CONSOLE": "secret"}):
            mgr._spawn_reboot_recovery(session.session_id, timeout_s=0.5)
            _join_threads("serialwrap-reboot-")

        assert "CONSOLE_UNLOCK_PROMPT_TIMEOUT" in writes
        assert session.last_error == "CONSOLE_UNLOCK_PROMPT_TIMEOUT"
        assert mgr._is_reprobe_prompt_error("ATTACHED", "CONSOLE_UNLOCK_PROMPT_TIMEOUT") is True
        assert mgr._is_reprobe_prompt_error("DETACHED", "CONSOLE_UNLOCK_PROMPT_TIMEOUT") is True

    def test_last_error_cleared(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])

        r = mgr.clear_session("COM0")

        assert r["ok"] is True
        assert "CLEARED" in writes
        assert mgr.get_session("COM0").last_error == "CLEARED"

    def test_last_error_cleared_device_online(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        mgr._devices = {_BY_ID: DeviceInfo(by_id=_BY_ID, real_path="/dev/ttyUSB7")}

        with mock.patch.object(SessionManager, "_spawn_attach"):
            mgr.clear_session("COM0")

        assert "CLEARED" in writes
        assert mgr.get_session("COM0").last_error is None

    def test_last_error_rebound(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()

        r = mgr.bind_session("COM0", _BY_ID_NEW)

        assert r["ok"] is True
        assert "REBOUND" in writes
        assert mgr.get_session("COM0").last_error == "DEVICE_NOT_FOUND"

    def test_last_error_rebound_device_online(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        session.bridge = mock.MagicMock()
        mgr._devices = {_BY_ID_NEW: DeviceInfo(by_id=_BY_ID_NEW, real_path="/dev/ttyUSB6")}

        with mock.patch.object(SessionManager, "_spawn_attach"):
            mgr.bind_session("COM0", _BY_ID_NEW)

        assert "REBOUND" in writes
        assert mgr.get_session("COM0").last_error is None

    def test_last_error_device_removed_during_attach(self, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        # passthrough：不做 probe，attach 收尾時才檢查裝置是否仍在。
        mgr = _mgr(tmp_path, [_profile(platform="passthrough")])
        mgr._devices = {_BY_ID: DeviceInfo(by_id=_BY_ID, real_path="/dev/ttyUSB5")}

        with mock.patch("sw_core.session_manager.UARTBridge") as mock_bridge:
            # bridge.start() 進行中移除裝置。
            mock_bridge.return_value.start.side_effect = lambda: mgr._devices.pop(_BY_ID, None)
            mgr._attach_by_id(_BY_ID)

        session = mgr.get_session("COM0")
        assert "DEVICE_REMOVED_DURING_ATTACH" in writes
        assert session.last_error == "DEVICE_REMOVED_DURING_ATTACH"
        assert session.state == "DETACHED"

    def test_last_error_recovery_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # reboot recovery 中 ensure_ready 拋例外 → worker 不死，記 RECOVERY_ERROR。
        profile = _profile()
        mgr = _mgr(tmp_path, [profile])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        session.bridge = _ScriptedBridge()
        writes = _record_last_error(monkeypatch)

        with mock.patch("sw_core.session_manager.ensure_ready", side_effect=RuntimeError("boom")):
            mgr._spawn_reboot_recovery(session.session_id, timeout_s=0.5)
            _join_threads("serialwrap-reboot-")

        assert "RECOVERY_ERROR" in writes
        assert session.last_error == "RECOVERY_ERROR"

    def test_last_error_recovery_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)

        # (a) bridge 為 None、逾時收尾 → DETACHED + RECOVERY_TIMEOUT。
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        mgr_a = _mgr(dir_a, [_profile()])
        session_a = mgr_a.get_session("COM0")
        assert session_a is not None
        session_a.state = "DETACHED"
        session_a.bridge = None
        mgr_a._spawn_reboot_recovery(session_a.session_id, timeout_s=0.0)
        _join_threads("serialwrap-reboot-")
        assert "RECOVERY_TIMEOUT" in writes
        assert session_a.state == "DETACHED"
        assert session_a.last_error == "RECOVERY_TIMEOUT"

        # (b) bridge 存在且既有 last_error=PROMPT_TIMEOUT → 逾時收尾保留既有值（`or` 語意）。
        (tmp_path / "b").mkdir()
        mgr_b = _mgr(tmp_path / "b", [_profile()])
        session_b = mgr_b.get_session("COM0")
        assert session_b is not None
        session_b.state = "ATTACHED"
        session_b.bridge = _ScriptedBridge()
        session_b.last_error = "PROMPT_TIMEOUT"
        mgr_b._spawn_reboot_recovery(session_b.session_id, timeout_s=0.0)
        _join_threads("serialwrap-reboot-")
        assert session_b.state == "ATTACHED"
        assert session_b.last_error == "PROMPT_TIMEOUT"

    def test_last_error_reprobe_failed_prefix(self, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        mgr._devices = {_BY_ID: DeviceInfo(by_id=_BY_ID, real_path="/dev/ttyUSB4")}
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "ATTACHED"
        # 自動重探只接手 prompt 類錯誤（_is_reprobe_prompt_error），故先設定 PROMPT_TIMEOUT。
        session.last_error = "PROMPT_TIMEOUT"
        bridge = mock.MagicMock()
        session.bridge = bridge

        with mock.patch.object(mgr, "_probe_existing_bridge", side_effect=OSError("probe io")):
            mgr._spawn_reprobe_probe(session.session_id, bridge)
            mgr.join_reprobe_workers(timeout=5)

        assert "REPROBE_FAILED:OSError" in writes
        assert session.last_error == "REPROBE_FAILED:OSError"
        assert session.state == "ATTACHED"

    def test_last_error_bridge_down_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "READY"
        bridge = mock.MagicMock()
        session.bridge = bridge

        mgr._handle_bridge_down(session.session_id, bridge, "SERIAL_READ:EIO")

        assert "BRIDGE_DOWN:SERIAL_READ:EIO" in writes
        assert session.last_error == "BRIDGE_DOWN:SERIAL_READ:EIO"

    def test_last_error_bridge_down_device_online(self, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        writes = _record_last_error(monkeypatch)
        mgr = _mgr(tmp_path, [_profile()])
        mgr._devices = {_BY_ID: DeviceInfo(by_id=_BY_ID, real_path="/dev/ttyUSB3")}
        session = mgr.get_session("COM0")
        assert session is not None
        session.state = "READY"
        bridge = mock.MagicMock()
        session.bridge = bridge

        with mock.patch.object(SessionManager, "_spawn_attach"):
            mgr._handle_bridge_down(session.session_id, bridge, "SERIAL_READ:EIO")

        assert "BRIDGE_DOWN:SERIAL_READ:EIO" in writes
        assert session.last_error is None
