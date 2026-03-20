import base64
import json
import os
import pathlib
import pty
import select
import subprocess
import tempfile
import threading
import time
import termios
import unittest
from typing import Any

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SERIALWRAP = str(ROOT_DIR / "serialwrap")
SERIALWRAPD = str(ROOT_DIR / "serialwrapd.py")


class FakeTarget:
    def __init__(self) -> None:
        self.master_fd, self.slave_fd = pty.openpty()
        self._configure_slave(self.slave_fd)
        self.slave_path = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self._cmd_thread = threading.Thread(target=self._cmd_loop, daemon=True)
        self._noise_thread = threading.Thread(target=self._noise_loop, daemon=True)
        self._tick = 0

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
        self._cmd_thread.start()
        self._noise_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cmd_thread.join(timeout=2.0)
        self._noise_thread.join(timeout=2.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _noise_loop(self) -> None:
        while not self._stop.is_set():
            self._tick += 1
            msg = f"KDBG:tick:{self._tick}\r\n".encode("utf-8")
            try:
                os.write(self.master_fd, msg)
            except OSError:
                return
            time.sleep(0.05)

    def _cmd_loop(self) -> None:
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
            except BlockingIOError:
                continue
            except OSError:
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.replace(b"\r", b"").decode("utf-8", errors="replace").strip()
                if not cmd:
                    try:
                        os.write(self.master_fd, b"root@prplOS:/# ")
                    except OSError:
                        return
                    continue
                out = f"EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# ".encode("utf-8")
                try:
                    os.write(self.master_fd, out)
                except OSError:
                    return


class TestMultiAgentE2E(unittest.TestCase):
    def _run_cmd(self, argv: list[str], env: dict[str, str], timeout: float = 10.0) -> dict[str, Any]:
        proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        out = proc.stdout.strip()
        if not out:
            return {"ok": False, "error": "empty_stdout", "stderr": proc.stderr.strip(), "rc": proc.returncode}
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "error": f"bad_json:{exc}",
                "stdout": out,
                "stderr": proc.stderr.strip(),
                "rc": proc.returncode,
            }
        obj["_rc"] = proc.returncode
        return obj

    def _wait_ready(self, env: dict[str, str], socket_path: str, timeout_s: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._run_cmd([SERIALWRAP, "--socket", socket_path, "session", "list"], env=env, timeout=5.0)
            if last.get("ok"):
                sessions = last.get("sessions") or []
                if sessions and sessions[0].get("state") == "READY":
                    return {"ok": True, "payload": last}
            time.sleep(0.2)
        return {"ok": False, "payload": last}

    def test_five_agents_three_rounds_no_conflict(self) -> None:
        try:
            mfd, sfd = pty.openpty()
            os.close(mfd)
            os.close(sfd)
        except OSError as exc:
            self.skipTest(f"pty not available in current environment: {exc}")

        with tempfile.TemporaryDirectory(prefix="serialwrap-e2e-") as td:
            root = pathlib.Path(td)
            by_id_dir = root / "by-id"
            profile_dir = root / "profiles"
            by_id_dir.mkdir(parents=True, exist_ok=True)
            profile_dir.mkdir(parents=True, exist_ok=True)

            fake = FakeTarget()
            fake.start()
            self.addCleanup(fake.stop)

            link_path = by_id_dir / "fake-uart0"
            os.symlink(fake.slave_path, link_path)

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
    alias: e2e+1
    profile: prpl-template
    device_by_id: {link_path}
"""
            (profile_dir / "e2e.yaml").write_text(profile, encoding="utf-8")

            env = os.environ.copy()
            env["SERIALWRAP_STATE_DIR"] = str(root / "state")
            env["SERIALWRAP_RUN_DIR"] = str(root / "run")
            env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
            env["SERIALWRAP_BY_PATH_DIR"] = str(root / "by-path")

            socket_path = str(root / "run" / "serialwrapd.sock")
            lock_path = str(root / "run" / "serialwrapd.lock")
            daemon = subprocess.Popen(
                [
                    os.environ.get("PYTHON", "python3"),
                    SERIALWRAPD,
                    "--profile-dir",
                    str(profile_dir),
                    "--socket",
                    socket_path,
                    "--lock",
                    lock_path,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            def _cleanup_daemon() -> None:
                try:
                    self._run_cmd([SERIALWRAP, "--socket", socket_path, "daemon", "stop"], env=env, timeout=3.0)
                except Exception:
                    pass
                if daemon.poll() is None:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        daemon.kill()

            self.addCleanup(_cleanup_daemon)

            ready = self._wait_ready(env, socket_path)
            self.assertTrue(ready["ok"], msg=f"session not ready: {ready['payload']}")

            base_cmd_by_agent = {
                1: "ifconfig",
                2: "wifi restart",
                3: "iw dev wlan0 scan",
                4: "iw dev wlan0 link",
                5: "logread -f",
            }

            submit_rows: list[dict[str, Any]] = []
            submit_lock = threading.Lock()

            def _submit(agent: int, round_no: int) -> None:
                cmd = f"{base_cmd_by_agent[agent]} __a{agent}_r{round_no}"
                resp = self._run_cmd(
                    [
                        SERIALWRAP,
                        "--socket",
                        socket_path,
                        "cmd",
                        "submit",
                        "--selector",
                        "COM0",
                        "--cmd",
                        cmd,
                        "--source",
                        f"agent:{agent}",
                    ],
                    env=env,
                    timeout=8.0,
                )
                with submit_lock:
                    submit_rows.append({"agent": agent, "round": round_no, "cmd": cmd, "resp": resp})

            for round_no in (1, 2, 3):
                threads = [threading.Thread(target=_submit, args=(agent, round_no), daemon=True) for agent in (1, 2, 3, 4, 5)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=15.0)
                    self.assertFalse(th.is_alive(), "submit thread did not finish in time")

            self.assertEqual(len(submit_rows), 15)
            bad = [row for row in submit_rows if not row["resp"].get("ok")]
            self.assertFalse(bad, msg=f"submit failed: {bad}")

            cmd_ids = [row["resp"]["cmd_id"] for row in submit_rows]
            pending = set(cmd_ids)
            deadline = time.monotonic() + 20.0
            while pending and time.monotonic() < deadline:
                done_now: list[str] = []
                for cmd_id in list(pending):
                    st = self._run_cmd([SERIALWRAP, "--socket", socket_path, "cmd", "status", "--cmd-id", cmd_id], env=env, timeout=5.0)
                    command = st.get("command") or {}
                    if command.get("status") in {"done", "error", "canceled"}:
                        done_now.append(cmd_id)
                for cmd_id in done_now:
                    pending.discard(cmd_id)
                if pending:
                    time.sleep(0.2)

            self.assertFalse(pending, msg=f"pending cmd_ids: {sorted(pending)}")

            for row in submit_rows:
                st = self._run_cmd([SERIALWRAP, "--socket", socket_path, "cmd", "status", "--cmd-id", row["resp"]["cmd_id"]], env=env, timeout=5.0)
                command = st.get("command") or {}
                self.assertEqual(command.get("status"), "done", msg=f"unexpected command status: {st}")
                self.assertIn(f"RESULT:{row['cmd']}:OK", command.get("stdout") or "")

            raw = self._run_cmd(
                [SERIALWRAP, "--socket", socket_path, "log", "tail-raw", "--selector", "COM0", "--from-seq", "0", "--limit", "10000"],
                env=env,
                timeout=10.0,
            )
            self.assertTrue(raw.get("ok"), msg=f"tail raw failed: {raw}")
            records = raw.get("records") or []

            agent_tx = [
                rec
                for rec in records
                if rec.get("dir") == "TX" and isinstance(rec.get("source"), str) and rec["source"].startswith("agent:")
            ]
            self.assertEqual(len(agent_tx), 15, msg=f"agent tx count mismatch: {len(agent_tx)}")

            tx_count_by_cmd_id: dict[str, int] = {}
            for rec in agent_tx:
                cmd_id = str(rec.get("cmd_id") or "")
                tx_count_by_cmd_id[cmd_id] = tx_count_by_cmd_id.get(cmd_id, 0) + 1
            self.assertEqual(len(tx_count_by_cmd_id), 15)
            self.assertTrue(all(v == 1 for v in tx_count_by_cmd_id.values()))

            rx_text_parts: list[str] = []
            for rec in records:
                if rec.get("dir") != "RX":
                    continue
                payload_b64 = rec.get("payload_b64")
                if not isinstance(payload_b64, str):
                    continue
                try:
                    payload = base64.b64decode(payload_b64)
                except Exception:
                    continue
                rx_text_parts.append(payload.decode("utf-8", errors="replace"))
            rx_text = "".join(rx_text_parts)

            missing: list[str] = []
            for row in submit_rows:
                expect = f"RESULT:{row['cmd']}:OK"
                if expect not in rx_text:
                    missing.append(expect)
            self.assertFalse(missing, msg=f"missing result markers: {missing}")

            per_agent: dict[str, int] = {}
            for rec in agent_tx:
                src = str(rec.get("source"))
                per_agent[src] = per_agent.get(src, 0) + 1
            for agent in (1, 2, 3, 4, 5):
                self.assertEqual(per_agent.get(f"agent:{agent}", 0), 3)


if __name__ == "__main__":
    unittest.main()
