# tests/test_stage0_uart_exclusive.py
"""Task P1-T10「BC-04 Per-UART 獨占、holder 診斷、Windows ACCESS_DENIED 映射、SC-05 同步收回、DF-04」失敗測試。

涵蓋：
1. BC-04：POSIX 對已開啟 tty fd 取非阻塞 flock 並設 TIOCEXCL；失敗不啟動收送；holder 診斷（pids、holder_unknown）；
   Windows pyserial dwShareMode=0 開檔失敗的 ERROR_ACCESS_DENIED 映射為 DEVICE_STILL_HELD（holder_unknown:true）。
2. SC-05：device.attach 同步重新取得獨占，失敗回 DEVICE_STILL_HELD 或 DEVICE_NOT_PRESENT，成功才 ATTACHING。
3. DF-04：動態 session 的 RELEASED 跨任意次重啟保留；裝置離線 tick 不得把 RELEASED 降為 DETACHED。

同檔混合平台（P0 T14 規則 2）：不放模組層平台 marker 與資源 marker。
- TestPosixExclusive：posix_only ＋ posix_pty（需要時函式層加 process_rpc）
- TestManagerAttach：core_portable ＋ memory_only
- TestWindowsNativeCom：windows_native ＋ windows_com
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest.mock as mock
from pathlib import Path

import pytest

from sw_core.config import SessionProfile, UartProfile
from sw_core import serial_port as sp


def _sessions_from_cli(stdout: str) -> list[dict]:
    """解析 `serialwrap session list` 輸出：RPC 回應為 {"ok":true,"sessions":[...]}，取出 sessions 串列。"""
    if not stdout.strip():
        return []
    obj = json.loads(stdout)
    if isinstance(obj, dict):
        return list(obj.get("sessions") or [])
    return list(obj)


@pytest.fixture
def pty_path():
    m, s = os.openpty()
    path = os.ttyname(s)
    os.close(s)
    yield path
    os.close(m)


# ──────────────────────────────────────────────────────────────────────────────
# POSIX 獨占測試
# ──────────────────────────────────────────────────────────────────────────────


class TestPosixExclusive:
    pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

    def test_second_open_of_same_tty_is_held(self, pty_path):
        a = sp._PosixSerialPort(pty_path, UartProfile())
        a.open()
        try:
            b = sp._PosixSerialPort(pty_path, UartProfile())
            with pytest.raises(sp.DeviceHeldError) as ei:
                b.open()
            assert ei.value.holder.holder_unknown is True  # 持有者是自身 pid，被排除 → 查不到
        finally:
            a.close()

    @pytest.mark.process_rpc
    def test_external_flock_holder_reported_with_pid(self, pty_path):
        code = (
            "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); "
            "fcntl.flock(fd, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, pty_path], stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ok"
            with pytest.raises(sp.DeviceHeldError) as ei:
                sp._PosixSerialPort(pty_path, UartProfile()).open()
            assert child.pid in ei.value.holder.pids and ei.value.holder.holder_unknown is False
        finally:
            child.kill()
            child.wait()
        p = sp._PosixSerialPort(pty_path, UartProfile())
        p.open()
        p.close()  # 程序結束後可取得

    def test_tiocexcl_set_on_opened_fd(self, pty_path, monkeypatch):
        calls = []
        real = fcntl.ioctl
        monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, *a: (calls.append(req), real(fd, req, *a))[1])
        p = sp._PosixSerialPort(pty_path, UartProfile())
        p.open()
        p.close()
        assert termios.TIOCEXCL in calls

    def test_close_clears_tiocexcl_reopen_with_master_open(self, pty_path, monkeypatch):
        # R0-F3：fixture 全程持有 PTY master；close 若只解 flock、關 fd 而未 TIOCNXCL，第一次 plain open 即 EBUSY
        if os.geteuid() == 0:
            pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
        import errno
        events = []
        real_ioctl, real_flock = fcntl.ioctl, fcntl.flock
        monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, *a: (events.append(("ioctl", req)), real_ioctl(fd, req, *a))[1])
        monkeypatch.setattr(fcntl, "flock", lambda fd, op: (events.append(("flock", op)), real_flock(fd, op))[1])
        for _ in range(3):  # PROBE → 正式 bridge → recover 的 open／close 循環
            events.clear()
            p = sp._PosixSerialPort(pty_path, UartProfile())
            p.open()
            p.close()
            tail = [e for e in events if e in (("ioctl", termios.TIOCNXCL), ("flock", fcntl.LOCK_UN))]
            assert tail == [("ioctl", termios.TIOCNXCL), ("flock", fcntl.LOCK_UN)]  # 先解除獨占，再解鎖、關 fd
            try:
                fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)  # 外部工具角度：一般 open 必須成功
            except OSError as e:
                pytest.fail(f"plain open after close must not fail, errno={errno.errorcode.get(e.errno)}")
            os.close(fd)

    def test_post_lock_reject_clears_excl(self, pty_path, monkeypatch):
        # R0-F3：TIOCEXCL 成功後被鎖後重掃拒絕，失敗路徑也必須 TIOCNXCL 才 close（成功路徑中 probe_holders 只在重掃呼叫）
        if os.geteuid() == 0:
            pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
        monkeypatch.setattr(sp, "probe_holders", lambda *a, **k: sp.HolderReport((4242,), False, "ok"))
        with pytest.raises(sp.DeviceHeldError) as ei:
            sp._PosixSerialPort(pty_path, UartProfile()).open()
        assert ei.value.holder.pids == (4242,)
        monkeypatch.undo()
        fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)  # 不得 EBUSY
        os.close(fd)

    def test_symlink_to_same_device_conflicts(self, pty_path, tmp_path):
        link = tmp_path / "by-id-link"
        link.symlink_to(pty_path)
        a = sp._PosixSerialPort(pty_path, UartProfile())
        a.open()
        try:
            with pytest.raises(sp.DeviceHeldError):
                sp._PosixSerialPort(str(link), UartProfile()).open()
        finally:
            a.close()

    @pytest.mark.process_rpc
    def test_fd_not_inherited_by_child(self, pty_path):
        a = sp._PosixSerialPort(pty_path, UartProfile())
        a.open()
        try:
            out = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; print(any(os.path.realpath(f'/proc/self/fd/{f}')==sys.argv[1] "
                    "for f in os.listdir('/proc/self/fd')))",
                    os.path.realpath(pty_path),
                ],
                capture_output=True,
                text=True,
                close_fds=False,
            )
            assert out.stdout.strip() == "False"
        finally:
            a.close()

    @pytest.mark.process_rpc
    def test_external_open_without_flock_is_rejected(self, pty_path):
        code = (
            "import os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); "
            "print('ok', flush=True); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, pty_path], stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ok"
            port = sp._PosixSerialPort(pty_path, UartProfile())
            with pytest.raises(sp.DeviceHeldError) as ei:
                port.open()
            assert child.pid in ei.value.holder.pids
            assert port.fileno() is None
        finally:
            child.kill()
            child.wait()

    @pytest.mark.process_rpc
    def test_attach_device_force_rejects_plain_external_holder(self, pty_path, tmp_path):
        from sw_core.session_manager import SessionManager
        from sw_core.device_watcher import DeviceInfo
        from sw_core.wal import WalWriter
        import sw_core.session_manager as sm_mod

        state_file = tmp_path / "state.json"
        old_state = sm_mod.STATE_PATH
        sm_mod.STATE_PATH = str(state_file)
        try:
            profile = SessionProfile(
                profile_name="p",
                com="COM0",
                act_no=1,
                alias="lab+1",
                device_by_id=pty_path,
                platform="prpl",
                uart=UartProfile(),
            )
            wal = WalWriter(wal_dir=str(tmp_path / "wal"))
            mgr = SessionManager([profile], wal, on_ready=lambda _s: None, on_detached=lambda _s: None)
            session = mgr.get_session("COM0")
            assert session is not None
            with mgr._lock:
                mgr._devices = {pty_path: DeviceInfo(by_id=pty_path, real_path=pty_path)}
                mgr._released_by_ids.add(pty_path)
                session.state = "RELEASED"
            mgr._save_state()
            state_bytes = state_file.read_bytes()
            state_mtime = state_file.stat().st_mtime_ns

            code = (
                "import os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); "
                "print('ok', flush=True); time.sleep(30)"
            )
            child = subprocess.Popen([sys.executable, "-c", code, pty_path], stdout=subprocess.PIPE, text=True)
            try:
                assert child.stdout.readline().strip() == "ok"
                with mock.patch.object(mgr, "_spawn_attach") as spawn_mock:
                    resp = mgr.attach_device("COM0", force=True)
                assert resp == {
                    "ok": False,
                    "error_code": "DEVICE_STILL_HELD",
                    "pids": [child.pid],
                    "holder_unknown": False,
                    "selector": "COM0",
                }
                assert session.state == "RELEASED"
                assert state_file.read_bytes() == state_bytes
                assert state_file.stat().st_mtime_ns == state_mtime
                assert not getattr(mgr, "_preopened_ports", {}).get(pty_path)
                spawn_mock.assert_not_called()
            finally:
                child.kill()
                child.wait()

            resp2 = mgr.attach_device("COM0", force=True)
            assert resp2["ok"] is True
            assert session.state == "ATTACHING"
        finally:
            sm_mod.STATE_PATH = old_state

    @pytest.mark.process_rpc
    def test_auto_attach_plain_external_holder_zero_tx(self, pty_path, tmp_path):
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        link = by_id_dir / "uart0"
        os.symlink(pty_path, link)

        code = (
            "import os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); "
            "print('ok', flush=True); time.sleep(30)"
        )
        child = subprocess.Popen([sys.executable, "-c", code, pty_path], stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ok"
            root = tmp_path / "daemon"
            root.mkdir(parents=True, exist_ok=True)
            prof_dir = root / "profiles"
            prof_dir.mkdir(parents=True, exist_ok=True)
            prof = (
                "profiles:\n  p:\n    platform: prpl\n    prompt_regex: '(?m)^root@.*# '\n"
                "    ready_probe: 'echo ok'\n    uart:\n      baud: 115200\n"
                f"targets:\n  - act_no: 1\n    com: COM0\n    profile: p\n    device_by_id: {link}\n"
            )
            (prof_dir / "target.yaml").write_text(prof, encoding="utf-8")
            env = os.environ.copy()
            env["SERIALWRAP_STATE_DIR"] = str(root / "state")
            env["SERIALWRAP_RUN_DIR"] = str(root / "run")
            env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
            env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")
            env["SERIALWRAP_WAL_DIR"] = str(root / "wal")
            env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")
            env["SERIALWRAP_EVENTS_DIR"] = str(root / "events.d")
            sock = str(root / "run" / "serialwrapd.sock")
            dproc = subprocess.Popen(
                [sys.executable, "-m", "sw_core.daemon", "--profile-dir", str(prof_dir), "--socket", sock],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5.0 and not os.path.exists(sock):
                    time.sleep(0.05)
                cli_res = subprocess.run(
                    [sys.executable, "-m", "sw_core.cli", "--socket", sock, "session", "list"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                sessions = _sessions_from_cli(cli_res.stdout)
                com0 = next((s for s in sessions if s.get("com") == "COM0"), {})
                assert com0.get("state") == "DETACHED"
                assert com0.get("last_error") == "ATTACH_FAILED:DeviceHeldError"

                att_res = subprocess.run(
                    [sys.executable, "-m", "sw_core.cli", "--socket", sock, "device", "attach", "--selector", "COM0", "--force"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                att_json = json.loads(att_res.stdout)
                assert att_json.get("error_code") == "DEVICE_STILL_HELD"
                assert att_json.get("pids") == [child.pid]
                assert att_json.get("holder_unknown") is False
            finally:
                dproc.kill()
                dproc.wait()
        finally:
            child.kill()
            child.wait()

    def test_post_lock_scan_partial_permission_with_pids_rejects(self, pty_path, monkeypatch):
        monkeypatch.setattr(sp, "probe_holders", lambda *a, **k: sp.HolderReport((123,), False, "permission"))
        tc_calls = []
        monkeypatch.setattr(termios, "tcsetattr", lambda *a, **k: tc_calls.append(a))
        port = sp._PosixSerialPort(pty_path, UartProfile())
        with pytest.raises(sp.DeviceHeldError) as ei:
            port.open()
        assert ei.value.holder.pids == (123,)
        assert port.fileno() is None
        assert tc_calls == []

    @pytest.mark.process_rpc
    def test_two_daemons_compete_for_same_tty(self, pty_path, tmp_path):
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        link = by_id_dir / "uart0"
        os.symlink(pty_path, link)

        def make_daemon(d_id):
            root = tmp_path / f"daemon{d_id}"
            root.mkdir(parents=True, exist_ok=True)
            prof_dir = root / "profiles"
            prof_dir.mkdir(parents=True, exist_ok=True)
            prof = (
                "profiles:\n  p:\n    platform: passthrough\n    uart:\n      baud: 115200\n"
                f"targets:\n  - act_no: 1\n    com: COM0\n    profile: p\n    device_by_id: {link}\n"
            )
            (prof_dir / "target.yaml").write_text(prof, encoding="utf-8")
            env = os.environ.copy()
            env["SERIALWRAP_STATE_DIR"] = str(root / "state")
            env["SERIALWRAP_RUN_DIR"] = str(root / "run")
            env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
            env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")
            env["SERIALWRAP_WAL_DIR"] = str(root / "wal")
            env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")
            env["SERIALWRAP_EVENTS_DIR"] = str(root / "events.d")
            sock = str(root / "run" / "serialwrapd.sock")
            proc = subprocess.Popen(
                [sys.executable, "-m", "sw_core.daemon", "--profile-dir", str(prof_dir), "--socket", sock],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return proc, sock, env

        d1, s1, e1 = make_daemon(1)
        d2, s2, e2 = make_daemon(2)
        try:
            for s in (s1, s2):
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5.0 and not os.path.exists(s):
                    time.sleep(0.05)
            time.sleep(1.0)
            c1 = subprocess.run([sys.executable, "-m", "sw_core.cli", "--socket", s1, "session", "list"], env=e1, capture_output=True, text=True)
            c2 = subprocess.run([sys.executable, "-m", "sw_core.cli", "--socket", s2, "session", "list"], env=e2, capture_output=True, text=True)
            s1_list = _sessions_from_cli(c1.stdout)
            s2_list = _sessions_from_cli(c2.stdout)
            st1 = s1_list[0].get("state") if s1_list else None
            st2 = s2_list[0].get("state") if s2_list else None
            states = {st1, st2}
            assert ("ATTACHED" in states or "READY" in states)
            assert "DETACHED" in states
            loser = s1_list[0] if st1 == "DETACHED" else s2_list[0]
            assert loser.get("last_error") == "ATTACH_FAILED:DeviceHeldError"
        finally:
            d1.kill()
            d2.kill()
            d1.wait()
            d2.wait()

    @pytest.mark.process_rpc
    def test_force_spawn_does_not_bypass_uart_lock(self, pty_path, tmp_path):
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        link = by_id_dir / "uart0"
        os.symlink(pty_path, link)

        def make_daemon(d_id, extra_args=None):
            root = tmp_path / f"d_{d_id}"
            root.mkdir(parents=True, exist_ok=True)
            prof_dir = root / "profiles"
            prof_dir.mkdir(parents=True, exist_ok=True)
            prof = (
                "profiles:\n  p:\n    platform: passthrough\n    uart:\n      baud: 115200\n"
                f"targets:\n  - act_no: 1\n    com: COM0\n    profile: p\n    device_by_id: {link}\n"
            )
            (prof_dir / "target.yaml").write_text(prof, encoding="utf-8")
            env = os.environ.copy()
            env["SERIALWRAP_STATE_DIR"] = str(root / "state")
            env["SERIALWRAP_RUN_DIR"] = str(root / "run")
            env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
            env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")
            env["SERIALWRAP_WAL_DIR"] = str(root / "wal")
            env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")
            env["SERIALWRAP_EVENTS_DIR"] = str(root / "events.d")
            sock = str(root / "run" / "serialwrapd.sock")
            cmd = [sys.executable, "-m", "sw_core.daemon", "--profile-dir", str(prof_dir), "--socket", sock]
            if extra_args:
                cmd.extend(extra_args)
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return proc, sock, env

        d1, s1, e1 = make_daemon(1)
        d2, s2, e2 = make_daemon(2, ["--force-spawn"])
        try:
            for s in (s1, s2):
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5.0 and not os.path.exists(s):
                    time.sleep(0.05)
            time.sleep(1.0)
            c1 = subprocess.run([sys.executable, "-m", "sw_core.cli", "--socket", s1, "session", "list"], env=e1, capture_output=True, text=True)
            c2 = subprocess.run([sys.executable, "-m", "sw_core.cli", "--socket", s2, "session", "list"], env=e2, capture_output=True, text=True)
            s1_list = _sessions_from_cli(c1.stdout)
            s2_list = _sessions_from_cli(c2.stdout)
            st1 = s1_list[0].get("state") if s1_list else None
            st2 = s2_list[0].get("state") if s2_list else None
            states = {st1, st2}
            assert ("ATTACHED" in states or "READY" in states)
            assert "DETACHED" in states
            loser = s1_list[0] if st1 == "DETACHED" else s2_list[0]
            assert loser.get("last_error") == "ATTACH_FAILED:DeviceHeldError"
        finally:
            d1.kill()
            d2.kill()
            d1.wait()
            d2.wait()

    @pytest.mark.process_rpc
    def test_release_then_external_plain_open_succeeds(self, pty_path, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        link = by_id_dir / "uart0"
        os.symlink(pty_path, link)

        root = tmp_path / "daemon"
        root.mkdir(parents=True, exist_ok=True)
        prof_dir = root / "profiles"
        prof_dir.mkdir(parents=True, exist_ok=True)
        prof = (
            "profiles:\n  p:\n    platform: passthrough\n    uart:\n      baud: 115200\n"
            f"targets:\n  - act_no: 1\n    com: COM0\n    profile: p\n    device_by_id: {link}\n"
        )
        (prof_dir / "target.yaml").write_text(prof, encoding="utf-8")
        env = os.environ.copy()
        env["SERIALWRAP_STATE_DIR"] = str(root / "state")
        env["SERIALWRAP_RUN_DIR"] = str(root / "run")
        env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
        env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")
        env["SERIALWRAP_WAL_DIR"] = str(root / "wal")
        env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")
        env["SERIALWRAP_EVENTS_DIR"] = str(root / "events.d")
        sock = str(root / "run" / "serialwrapd.sock")
        dproc = subprocess.Popen(
            [sys.executable, "-m", "sw_core.daemon", "--profile-dir", str(prof_dir), "--socket", sock],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5.0 and not os.path.exists(sock):
                time.sleep(0.05)
            time.sleep(1.0)
            rel = subprocess.run(
                [sys.executable, "-m", "sw_core.cli", "--socket", sock, "device", "release", "--selector", "COM0"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            rel_json = json.loads(rel.stdout)
            assert rel_json.get("ok") is True

            code = "import os,sys; fd=os.open(sys.argv[1], os.O_RDWR|os.O_NOCTTY); os.write(fd, b'X'); os.close(fd)"
            sub = subprocess.run([sys.executable, "-c", code, pty_path], capture_output=True, text=True)
            assert sub.returncode == 0, f"plain open failed: {sub.stderr}"

            att = subprocess.run(
                [sys.executable, "-m", "sw_core.cli", "--socket", sock, "device", "attach", "--selector", "COM0"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            att_json = json.loads(att.stdout)
            assert att_json.get("ok") is True
        finally:
            dproc.kill()
            dproc.wait()

    @pytest.mark.process_rpc
    def test_dynamic_probe_then_bridge_reopen_native(self, pty_path, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
        by_id_dir = tmp_path / "by-id"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        link = by_id_dir / "uart0"
        os.symlink(pty_path, link)

        root = tmp_path / "daemon"
        root.mkdir(parents=True, exist_ok=True)
        prof_dir = root / "profiles"
        prof_dir.mkdir(parents=True, exist_ok=True)
        prof = (
            "profiles:\n  prpl:\n    platform: prpl\n    prompt_regex: '(?m)^root@prplOS:.*# '\n"
            "    ready_probe: 'echo ok'\n    uart:\n      baud: 115200\n"
        )
        (prof_dir / "template.yaml").write_text(prof, encoding="utf-8")
        env = os.environ.copy()
        env["SERIALWRAP_STATE_DIR"] = str(root / "state")
        env["SERIALWRAP_RUN_DIR"] = str(root / "run")
        env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
        env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")
        env["SERIALWRAP_WAL_DIR"] = str(root / "wal")
        env["SERIALWRAP_CONFIG_DIR"] = str(root / "config")
        env["SERIALWRAP_EVENTS_DIR"] = str(root / "events.d")
        sock = str(root / "run" / "serialwrapd.sock")
        dproc = subprocess.Popen(
            [sys.executable, "-m", "sw_core.daemon", "--profile-dir", str(prof_dir), "--socket", sock],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5.0 and not os.path.exists(sock):
                time.sleep(0.05)
            deadline = time.monotonic() + 20.0
            detected = False
            while time.monotonic() < deadline:
                c = subprocess.run([sys.executable, "-m", "sw_core.cli", "--socket", sock, "session", "list"], env=env, capture_output=True, text=True)
                s_list = _sessions_from_cli(c.stdout)
                if s_list:
                    s0 = s_list[0]
                    assert s0.get("last_error") != "ATTACH_FAILED:DeviceHeldError"
                    if s0.get("profile_source") == "detected" and s0.get("state") == "READY":
                        detected = True
                        break
                time.sleep(0.2)
            assert detected is True
        finally:
            dproc.kill()
            dproc.wait()


# ──────────────────────────────────────────────────────────────────────────────
# Manager 層及替身測試
# ──────────────────────────────────────────────────────────────────────────────


def _make_profile(name="p", com="COM0", by_id="/dev/serial/by-id/X", platform="prpl"):
    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias="lab+1",
        device_by_id=by_id,
        platform=platform,
        uart=UartProfile(),
    )


def _make_manager(tmp_path, profiles):
    from sw_core.session_manager import SessionManager
    import sw_core.session_manager as sm_mod
    from sw_core.wal import WalWriter

    sm_mod.STATE_PATH = str(tmp_path / "state.json")
    wal = WalWriter(wal_dir=str(tmp_path / "wal"))
    return SessionManager(profiles, wal, on_ready=lambda _s: None, on_detached=lambda _s: None)


class TestManagerAttach:
    pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

    def test_holder_scan_permission_failure_is_unknown(self, tmp_path):
        r = sp.probe_holders("/dev/null", proc_root=str(tmp_path / "missing"))
        assert r == sp.HolderReport((), True, "permission")

    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)", ("held", 5)),
            ("could not open port 'COM3': OSError(22, 'The semaphore timeout period has expired.', None, 121)", ("other", 121)),
            ("could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)", ("not_found", 2)),
            ("garbage", ("other", None)),
        ],
    )
    def test_classify_pyserial_open_error(self, msg, expected):
        assert sp.classify_pyserial_open_error(msg) == expected

    def test_pyserial_access_denied_raises_device_held_unknown(self, monkeypatch):
        import serial

        class FakeSerial:
            def __init__(self, *a, **k):
                pass

            def open(self):
                raise serial.SerialException("could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)")

        monkeypatch.setattr(sp, "serial", mock.MagicMock(Serial=FakeSerial, SerialException=serial.SerialException), raising=False)
        port = sp._PySerialPort("COM3", UartProfile())
        with pytest.raises(sp.DeviceHeldError) as ei:
            port.open()
        assert ei.value.holder == sp.HolderReport((), True, "unsupported")

    def test_attach_device_success_is_sync_and_attaching(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        fake_port = mock.MagicMock()
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        with mock.patch.object(mgr, "_spawn_attach") as spawn_mock:
            resp = mgr.attach_device("COM0")
        assert resp["ok"] is True
        assert session.state == "ATTACHING"
        assert by_id in getattr(mgr, "_preopened_ports", {})
        spawn_mock.assert_called_once_with(by_id)

    def test_attach_device_held_keeps_released(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo
        import sw_core.session_manager as sm_mod

        state_file = tmp_path / "state.json"
        sm_mod.STATE_PATH = str(state_file)
        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"
        mgr._save_state()
        state_bytes = state_file.read_bytes()
        state_mtime = state_file.stat().st_mtime_ns

        fake_port = mock.MagicMock()
        fake_port.open.side_effect = sp.DeviceHeldError(
            by_id, holder=sp.HolderReport((4321,), False, "ok")
        )
        fake_port.writes = []
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        resp = mgr.attach_device("COM0")
        assert resp == {
            "ok": False,
            "error_code": "DEVICE_STILL_HELD",
            "pids": [4321],
            "holder_unknown": False,
            "selector": "COM0",
        }
        assert session.state == "RELEASED"
        assert state_file.read_bytes() == state_bytes
        assert state_file.stat().st_mtime_ns == state_mtime
        assert fake_port.writes == []

    def test_holder_scan_failure_opens_with_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "probe_holders", lambda *a, **k: sp.HolderReport((), True, "permission"))
        port = mock.MagicMock()
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: port)
        rep = sp.probe_holders("/dev/null")
        assert rep.status == "permission"
        assert rep.holder_unknown is True

    def test_attach_op_invalidated_by_release_during_open(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        open_started = threading.Event()
        release_done = threading.Event()
        candidate_port = mock.MagicMock()
        candidate_port.close_calls = 0

        def fake_close():
            candidate_port.close_calls += 1

        candidate_port.close.side_effect = fake_close

        def slow_open():
            open_started.set()
            release_done.wait(timeout=2.0)

        candidate_port.open.side_effect = slow_open
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: candidate_port)

        res_holder = []

        def attach_worker():
            res_holder.append(mgr.attach_device("COM0"))

        t = threading.Thread(target=attach_worker)
        t.start()
        assert open_started.wait(timeout=1.0)
        mgr.release_device("COM0")
        release_done.set()
        t.join(timeout=2.0)

        assert res_holder
        resp = res_holder[0]
        assert resp.get("error_code") == "SESSION_RECOVERING"
        assert candidate_port.close_calls == 1
        assert by_id not in getattr(mgr, "_preopened_ports", {})

    def test_concurrent_attach_device_single_open(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        open_calls = []
        port = mock.MagicMock()

        def record_open():
            open_calls.append(1)
            time.sleep(0.1)

        port.open.side_effect = record_open
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: port)

        t1 = threading.Thread(target=lambda: mgr.attach_device("COM0"))
        t2 = threading.Thread(target=lambda: mgr.attach_device("COM0"))
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert len(open_calls) == 1

    def test_preopened_port_reused_by_probe_and_bridge(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        prof = _make_profile()
        mgr = _make_manager(tmp_path, [prof])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        session.profile_source = "fallback"
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        open_calls = []
        port = mock.MagicMock()
        port.open.side_effect = lambda: open_calls.append(1)
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: port)

        mgr.attach_device("COM0")
        assert len(open_calls) == 1

    def test_attach_device_preflight_has_holder_unknown_field(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        monkeypatch.setattr(mgr, "_probe_external_holder", lambda *a, **k: {"pids": [99], "holder": 99, "holder_unknown": False})
        resp = mgr.attach_device("COM0")
        assert resp.get("holder_unknown") is False

    def test_attach_device_not_present_on_open(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        fake_port = mock.MagicMock()
        fake_port.open.side_effect = getattr(sp, "DeviceNotPresentError", FileNotFoundError)("not present")
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        resp = mgr.attach_device("COM0")
        assert resp == {
            "ok": False,
            "error_code": "DEVICE_NOT_PRESENT",
            "selector": "COM0",
            "device_by_id": by_id,
        }

    def test_attach_device_other_error_detaches(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        fake_port = mock.MagicMock()
        fake_port.open.side_effect = OSError(5, "Input/output error")
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        resp = mgr.attach_device("COM0")
        assert resp["ok"] is True
        assert session.state == "DETACHED"
        assert session.last_error == "ATTACH_FAILED:OSError"

    def test_hotplug_attach_held_sets_last_error(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}

        fake_port = mock.MagicMock()
        fake_port.open.side_effect = sp.DeviceHeldError(by_id, holder=sp.HolderReport((), True, "unknown"))
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        mgr._attach_by_id(by_id)
        assert session.state == "DETACHED"
        assert session.last_error == "ATTACH_FAILED:DeviceHeldError"

    def test_dynamic_released_survives_two_restarts(self, tmp_path):
        from sw_core.device_watcher import DeviceInfo
        from sw_core.config import ProfileTemplate
        import sw_core.session_manager as sm_mod

        state_file = tmp_path / "state.json"
        sm_mod.STATE_PATH = str(state_file)
        mgr1 = _make_manager(tmp_path, [])
        by_id = "/dev/serial/by-id/dyn0"
        tpl = ProfileTemplate(profile_name="passthrough", platform="passthrough", uart=UartProfile())
        with mgr1._lock:
            mgr1._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyDyn0")}
            session = mgr1._session_from_template(tpl, by_id)
            mgr1._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "user"
            session.released_at = "2026-10-16"
            session.released_reason = "flash"
        mgr1._save_state()

        mgr2 = _make_manager(tmp_path, [])
        assert by_id in mgr2._released_by_ids
        data2 = json.loads(state_file.read_text(encoding="utf-8"))
        assert any(r.get("by_id") == by_id for r in data2.get("released", []))
        mgr2._save_state()

        mgr3 = _make_manager(tmp_path, [])
        assert by_id in mgr3._released_by_ids
        data3 = json.loads(state_file.read_text(encoding="utf-8"))
        assert any(r.get("by_id") == by_id for r in data3.get("released", []))

    def test_dynamic_released_attachable_after_restart(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo
        from sw_core.config import ProfileTemplate
        import sw_core.session_manager as sm_mod

        state_file = tmp_path / "state.json"
        sm_mod.STATE_PATH = str(state_file)
        mgr1 = _make_manager(tmp_path, [])
        by_id = "/dev/serial/by-id/dyn0"
        tpl = ProfileTemplate(profile_name="passthrough", platform="passthrough", uart=UartProfile())
        with mgr1._lock:
            mgr1._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyDyn0")}
            session = mgr1._session_from_template(tpl, by_id)
            com = session.profile.com
            mgr1._released_by_ids.add(by_id)
            session.state = "RELEASED"
            session.released_by = "user"
        mgr1._save_state()

        mgr2 = _make_manager(tmp_path, [])
        with mgr2._lock:
            mgr2._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyDyn0")}
        s = mgr2.get_session(com)
        assert s is not None, f"DF-04: get_session('{com}') 必須存在"
        assert s.state == "RELEASED"

        fake_port = mock.MagicMock()
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: fake_port)
        resp = mgr2.attach_device(com)
        assert resp["ok"] is True
        assert s.state == "ATTACHING"

    def test_offline_tick_keeps_released(self, tmp_path):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        mgr.update_devices({})
        assert session.state == "RELEASED"
        assert by_id in mgr._released_by_ids

    def test_unconsumed_preopened_port_closed_on_release(self, tmp_path, monkeypatch):
        from sw_core.device_watcher import DeviceInfo

        mgr = _make_manager(tmp_path, [_make_profile()])
        by_id = "/dev/serial/by-id/X"
        session = mgr.get_session("COM0")
        assert session is not None
        with mgr._lock:
            mgr._devices = {by_id: DeviceInfo(by_id=by_id, real_path="/dev/ttyX")}
            mgr._released_by_ids.add(by_id)
            session.state = "RELEASED"

        candidate_port = mock.MagicMock()
        candidate_port.close_calls = 0

        def fake_close():
            candidate_port.close_calls += 1

        candidate_port.close.side_effect = fake_close
        monkeypatch.setattr(sp, "open_serial_port", lambda *a, **k: candidate_port)

        with mock.patch.object(mgr, "_spawn_attach"):
            resp = mgr.attach_device("COM0")
            assert resp["ok"] is True

        mgr.release_device("COM0")
        assert candidate_port.close_calls == 1


# ──────────────────────────────────────────────────────────────────────────────
# Windows Native COM 測試
# ──────────────────────────────────────────────────────────────────────────────


class TestWindowsNativeCom:
    pytestmark = [pytest.mark.windows_native, pytest.mark.windows_com]

    def test_native_second_open_access_denied(self):
        com_pair = os.environ.get("SW_TEST_COM_PAIR")
        if not com_pair:
            pytest.skip("EVIDENCE_MISSING: SW_TEST_COM_PAIR not configured")
        com_a = com_pair.split(",")[0].strip()
        p1 = sp._PySerialPort(com_a, UartProfile())
        p1.open()
        try:
            p2 = sp._PySerialPort(com_a, UartProfile())
            with pytest.raises(sp.DeviceHeldError) as ei:
                p2.open()
            assert ei.value.holder.holder_unknown is True
        finally:
            p1.close()
