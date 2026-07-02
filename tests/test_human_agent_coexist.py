"""S6 整合測試：human console (tmux/minicom) + agent 共存驗證。

使用 FakeTarget (PTY) 模擬 UART target，啟動真實 daemon，
再透過 tmux 模擬 human console，驗證：
- wal.reset 不破壞 console 連線
- wal.current_seq 一致性
- session.bind 冪等不斷線
- agent 命令在 human console 開啟時正常執行且可見
"""
from __future__ import annotations

import json
import os
import pathlib
import pty
import select
import shutil
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from typing import Any

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SERIALWRAP = str(ROOT_DIR / "serialwrap")
SERIALWRAPD = str(ROOT_DIR / "serialwrapd.py")


class FakeTarget:
    """PTY 假 target，回應 prpl 風格 prompt 並 echo 命令。"""

    def __init__(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self._configure_slave(self.slave_fd)
        self.slave_path = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _configure_slave(self, fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def start(self) -> None:
        os.write(self.master_fd, b"boot done\r\nroot@prplOS:/# ")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0.2)
            except OSError:
                return
            if self.master_fd not in rlist:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except (BlockingIOError, OSError):
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.strip()
                if not cmd:
                    continue
                if cmd.startswith(b"echo __READY__"):
                    reply = cmd[5:] + b"\r\n"
                else:
                    reply = cmd + b": ok\r\n"
                try:
                    os.write(self.master_fd, reply + b"root@prplOS:/# ")
                except OSError:
                    return


class TestHumanAgentCoexist(unittest.TestCase):
    """tmux + FakeTarget + daemon 模擬完整 human+agent 共存場景。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 確認 tmux 可用
        try:
            subprocess.run(["tmux", "-V"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise unittest.SkipTest("tmux 不可用")
        # 確認 PTY 可用
        try:
            mfd, sfd = pty.openpty()
            os.close(mfd)
            os.close(sfd)
        except OSError as exc:
            raise unittest.SkipTest(f"PTY 不可用: {exc}")

    def setUp(self) -> None:
        # #120：建立一項資源、立刻 addCleanup 一項——LIFO 自然得到
        # tmux→daemon→fake→tempdir 的正確清理順序；且 setUp 中途失敗時
        # 已註冊的 cleanup 仍會執行（tearDown 則會被 unittest 跳過）。
        self._td = tempfile.mkdtemp(prefix="sw-coexist-")
        self.addCleanup(shutil.rmtree, self._td, ignore_errors=True)
        self._root = pathlib.Path(self._td)
        self._by_id_dir = self._root / "by-id"
        self._profile_dir = self._root / "profiles"
        self._by_id_dir.mkdir(parents=True)
        self._profile_dir.mkdir(parents=True)

        self._fake = FakeTarget()
        self._fake.start()
        self.addCleanup(self._fake.stop)

        self._link_path = self._by_id_dir / "fake-uart0"
        os.symlink(self._fake.slave_path, self._link_path)

        profile = f"""profiles:
  prpl-template:
    platform: prpl
    prompt_regex: "(?m)^root@prplOS:.*# "
    ready_probe: "echo __READY__${{nonce}}"
    uart:
      baud: 115200
      data_bits: 8
      parity: N
      stop_bits: 1
      flow_control: rtscts
      xonxoff: false

targets:
  - act_no: 1
    com: COM0
    alias: dut
    profile: prpl-template
    device_by_id: {self._link_path}
"""
        (self._profile_dir / "test.yaml").write_text(profile, encoding="utf-8")

        self._env = os.environ.copy()
        self._env["SERIALWRAP_STATE_DIR"] = str(self._root / "state")
        self._env["SERIALWRAP_RUN_DIR"] = str(self._root / "run")
        self._env["SERIALWRAP_BY_ID_DIR"] = str(self._by_id_dir)
        self._env["SERIALWRAP_BY_PATH_DIR"] = str(self._root / "by-path")
        self._env["SERIALWRAP_WAL_DIR"] = str(self._root / "wal")
        # #120：隔離 config 維度——否則 CLI 子行程讀 live config.yaml 誤路由到 live daemon（縱深防禦）
        self._env["SERIALWRAP_CONFIG_DIR"] = str(self._root / "config")

        self._socket = str(self._root / "run" / "serialwrapd.sock")
        self._lock = str(self._root / "run" / "serialwrapd.lock")

        self._daemon = subprocess.Popen(
            [
                os.environ.get("PYTHON", "python3"),
                SERIALWRAPD,
                "--profile-dir", str(self._profile_dir),
                "--socket", self._socket,
                "--lock", self._lock,
            ],
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_daemon)

        # tmux session 名先定、清理先掛——_wait_ready 失敗時 unittest 跳過 tearDown，
        # addCleanup 仍會執行（#120：8 個殭屍 daemon 的成因就是舊 tearDown 被跳過）
        self._tmux_session = f"sw_test_{os.getpid()}"
        self.addCleanup(self._kill_tmux)

        self._wait_ready()

    def _stop_daemon(self) -> None:
        try:
            self._sw("daemon", "stop")
        except Exception:
            pass
        if self._daemon.poll() is None:
            self._daemon.terminate()
            try:
                self._daemon.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._daemon.kill()

    def _kill_tmux(self) -> None:
        subprocess.run(
            ["tmux", "kill-session", "-t", self._tmux_session],
            capture_output=True, timeout=5,
        )

    def _sw(self, *args: str, timeout: float = 10.0) -> dict[str, Any]:
        cmd = [SERIALWRAP, "--socket", self._socket, *args]
        proc = subprocess.run(
            cmd, env=self._env,
            capture_output=True, text=True, timeout=timeout,
        )
        out = proc.stdout.strip()
        if not out:
            return {"ok": False, "_stderr": proc.stderr.strip(), "_rc": proc.returncode}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"ok": False, "_stdout": out, "_rc": proc.returncode}

    def _wait_ready(self, timeout_s: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = self._sw("session", "list")
            if resp.get("ok"):
                sessions = resp.get("sessions") or []
                if sessions and sessions[0].get("state") == "READY":
                    return
            time.sleep(0.3)
        self.fail(f"session 未在 {timeout_s}s 內達到 READY")

    def _attach_console(self, label: str = "tmux-human") -> dict[str, Any]:
        return self._sw("session", "console-attach", "--selector", "COM0", "--label", label)

    def _tmux_new(self, command: str = "bash") -> None:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", self._tmux_session, command],
            check=True, timeout=5,
        )

    def _tmux_send(self, keys: str, delay: float = 0.5) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self._tmux_session, keys, "Enter"],
            check=True, timeout=5,
        )
        time.sleep(delay)

    def _tmux_capture(self) -> str:
        proc = subprocess.run(
            ["tmux", "capture-pane", "-t", self._tmux_session, "-p"],
            capture_output=True, text=True, timeout=5,
        )
        return proc.stdout

    def _start_minicom_in_tmux(self, vtty: str) -> None:
        """在 tmux session 內用 cat 讀 vtty（模擬 minicom 的 RX 觀測）。"""
        self._tmux_new("bash")
        time.sleep(0.3)
        # 用 cat 讀 PTY slave 以觀測 RX（比 minicom 更輕量且不需安裝）
        self._tmux_send(f"cat {vtty}", delay=1.0)

    # ---------------------------------------------------------------
    # T1: wal.reset 後 console PTY 仍然有效
    # ---------------------------------------------------------------
    def test_t1_wal_reset_preserves_console(self) -> None:
        attach = self._attach_console("t1-human")
        self.assertTrue(attach.get("ok"), msg=f"console-attach 失敗: {attach}")
        vtty = attach["vtty"]
        client_id = attach["client_id"]

        self._start_minicom_in_tmux(vtty)

        # 執行 wal.reset
        reset_resp = self._sw("wal", "reset")
        self.assertTrue(reset_resp.get("ok"), msg=f"wal reset 失敗: {reset_resp}")

        # wal.reset 後送 agent 命令，驗證 console 仍可收到
        submit = self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo T1_ALIVE", "--cmd-timeout", "5")
        self.assertTrue(submit.get("ok"), msg=f"cmd submit 失敗: {submit}")
        time.sleep(2.0)

        # 驗證 console 仍在 list 中
        consoles = self._sw("session", "console-list", "--selector", "COM0")
        self.assertTrue(consoles.get("ok"))
        ids = [c["client_id"] for c in consoles.get("consoles", [])]
        self.assertIn(client_id, ids, msg="wal.reset 後 console 消失了")

        # 驗證 tmux 看到輸出
        pane = self._tmux_capture()
        self.assertIn("T1_ALIVE", pane, msg=f"console 未收到 agent 命令輸出:\n{pane}")

    # ---------------------------------------------------------------
    # T2: wal.reset 後 seq 從 0 重新計數
    # ---------------------------------------------------------------
    def test_t2_wal_reset_seq_resets_to_zero(self) -> None:
        # 先產生一些 WAL 記錄
        self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo before_reset", "--cmd-timeout", "5")
        time.sleep(1.0)

        seq_before = self._sw("wal", "current-seq")
        self.assertTrue(seq_before.get("ok"))
        self.assertGreater(seq_before["seq"], 0, "reset 前 seq 應 > 0")

        # 執行 reset
        reset_resp = self._sw("wal", "reset")
        self.assertTrue(reset_resp.get("ok"))

        seq_after = self._sw("wal", "current-seq")
        self.assertTrue(seq_after.get("ok"))
        self.assertEqual(seq_after["seq"], 0, "reset 後 seq 應 == 0")

        # 再送一條命令，seq 應從 1 開始
        self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo after_reset", "--cmd-timeout", "5")
        time.sleep(1.0)
        seq_new = self._sw("wal", "current-seq")
        self.assertTrue(seq_new.get("ok"))
        self.assertGreater(seq_new["seq"], 0, "新命令後 seq 應 > 0")

    # ---------------------------------------------------------------
    # T3: wal.current_seq RPC 與 WAL 檔案一致
    # ---------------------------------------------------------------
    def test_t3_wal_current_seq_matches_live(self) -> None:
        self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo seq_check", "--cmd-timeout", "5")
        time.sleep(1.0)

        rpc_seq = self._sw("wal", "current-seq")
        self.assertTrue(rpc_seq.get("ok"))

        # 直接讀 WAL 檔案最後一行取 seq
        wal_path = self._root / "wal" / "raw.wal.ndjson"
        if wal_path.exists():
            lines = wal_path.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                self.assertEqual(rpc_seq["seq"], last["seq"])

    # ---------------------------------------------------------------
    # T4: bind 冪等 — 已 READY 的 session 不重新 attach
    # ---------------------------------------------------------------
    def test_t4_bind_idempotent_ready_session(self) -> None:
        state_before = self._sw("session", "list")
        self.assertTrue(state_before.get("ok"))
        session = state_before["sessions"][0]
        self.assertEqual(session["state"], "READY")

        # 再次 bind 同一 device
        bind_resp = self._sw(
            "session", "bind",
            "--selector", "COM0",
            "--device-by-id", str(self._link_path),
        )
        self.assertTrue(bind_resp.get("ok"), msg=f"bind 失敗: {bind_resp}")
        self.assertTrue(bind_resp.get("already_bound"), msg="應回傳 already_bound=True")

        # session 仍然 READY
        state_after = self._sw("session", "list")
        self.assertEqual(state_after["sessions"][0]["state"], "READY")

    # ---------------------------------------------------------------
    # T5: bind 冪等後 human console 不斷線
    # ---------------------------------------------------------------
    def test_t5_bind_idempotent_preserves_console(self) -> None:
        attach = self._attach_console("t5-human")
        self.assertTrue(attach.get("ok"))
        client_id = attach["client_id"]
        vtty = attach["vtty"]

        self._start_minicom_in_tmux(vtty)

        # 冪等 bind
        self._sw(
            "session", "bind",
            "--selector", "COM0",
            "--device-by-id", str(self._link_path),
        )

        # 送 agent 命令
        self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo T5_BIND_OK", "--cmd-timeout", "5")
        time.sleep(2.0)

        consoles = self._sw("session", "console-list", "--selector", "COM0")
        ids = [c["client_id"] for c in consoles.get("consoles", [])]
        self.assertIn(client_id, ids, msg="冪等 bind 後 console 消失了")

        pane = self._tmux_capture()
        self.assertIn("T5_BIND_OK", pane, msg=f"bind 後 console 未收到輸出:\n{pane}")

    # ---------------------------------------------------------------
    # T6: human 能即時看到 agent 提交的命令及回應
    # ---------------------------------------------------------------
    def test_t6_human_sees_agent_commands(self) -> None:
        attach = self._attach_console("t6-monitor")
        self.assertTrue(attach.get("ok"))
        vtty = attach["vtty"]

        self._start_minicom_in_tmux(vtty)

        commands = ["echo T6_CMD_1", "echo T6_CMD_2", "echo T6_CMD_3"]
        for cmd in commands:
            self._sw("cmd", "submit", "--selector", "COM0", "--cmd", cmd, "--cmd-timeout", "5")
            time.sleep(0.5)

        time.sleep(2.0)
        pane = self._tmux_capture()

        for cmd in commands:
            marker = cmd.split()[-1]  # T6_CMD_1, T6_CMD_2, T6_CMD_3
            self.assertIn(marker, pane, msg=f"human 未看到 {marker}:\n{pane}")

    # ---------------------------------------------------------------
    # T7: human 打字期間 agent 命令不被阻擋
    # ---------------------------------------------------------------
    def test_t7_agent_runs_while_human_types(self) -> None:
        attach = self._attach_console("t7-typer")
        self.assertTrue(attach.get("ok"))
        vtty = attach["vtty"]

        self._start_minicom_in_tmux(vtty)

        # 模擬 human 持續打字（透過 tmux send-keys 打一些字但不按 Enter）
        subprocess.run(
            ["tmux", "send-keys", "-t", self._tmux_session, "C-c"],
            check=True, timeout=5,
        )
        time.sleep(0.3)
        # 在 human "打字" 期間提交 agent 命令
        t0 = time.monotonic()
        submit = self._sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo T7_CONCURRENT", "--cmd-timeout", "5")
        elapsed = time.monotonic() - t0

        self.assertTrue(submit.get("ok"), msg=f"agent 命令被阻擋: {submit}")
        self.assertLess(elapsed, 5.0, msg=f"agent 命令提交耗時 {elapsed:.1f}s，可能被阻擋")

        # 等命令完成
        if submit.get("cmd_id"):
            time.sleep(2.0)
            result = self._sw("cmd", "status", "--cmd-id", submit["cmd_id"])
            self.assertIn(result.get("command", {}).get("status", ""), ("done", "accepted", "running"),
                          msg=f"命令狀態異常: {result}")

    # ---------------------------------------------------------------
    # T8: 完整 full run 模擬
    # ---------------------------------------------------------------
    def test_t8_full_run_simulation(self) -> None:
        attach = self._attach_console("t8-fullrun")
        self.assertTrue(attach.get("ok"))
        vtty = attach["vtty"]
        client_id = attach["client_id"]

        self._start_minicom_in_tmux(vtty)

        # Step 1: wal.reset（模擬 testpilot run 開始）
        reset = self._sw("wal", "reset")
        self.assertTrue(reset.get("ok"))

        # Step 2: 確認 session 已 READY（模擬 setup_sessions 跳過 bind）
        state = self._sw("session", "list")
        self.assertEqual(state["sessions"][0]["state"], "READY")

        # Step 3: 模擬 3 個 test case 的 seq 追蹤
        case_seqs: list[tuple[int, int]] = []
        for i in range(1, 4):
            seq_before = self._sw("wal", "current-seq")["seq"]
            self._sw("cmd", "submit", "--selector", "COM0",
                     "--cmd", f"echo CASE_{i}_RESULT", "--cmd-timeout", "5")
            time.sleep(1.0)
            seq_after = self._sw("wal", "current-seq")["seq"]
            case_seqs.append((seq_before, seq_after))

        # Step 4: 驗證 seq 遞增
        for i, (s, e) in enumerate(case_seqs):
            self.assertGreater(e, s, msg=f"case {i+1} seq 未遞增: {s} -> {e}")

        # Step 5: wal export 驗證
        export = self._sw("wal", "export", "--from-seq", "0")
        self.assertTrue(export.get("ok"))
        self.assertGreater(len(export.get("records", [])), 0, "WAL export 無記錄")

        # Step 6: console 全程不斷線
        consoles = self._sw("session", "console-list", "--selector", "COM0")
        ids = [c["client_id"] for c in consoles.get("consoles", [])]
        self.assertIn(client_id, ids, msg="full run 後 console 斷線了")

        # Step 7: human 看到所有 case 輸出
        pane = self._tmux_capture()
        for i in range(1, 4):
            self.assertIn(f"CASE_{i}_RESULT", pane,
                          msg=f"human 未看到 CASE_{i}_RESULT:\n{pane}")


if __name__ == "__main__":
    unittest.main()
