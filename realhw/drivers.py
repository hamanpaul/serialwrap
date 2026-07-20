"""subprocess 薄包裝＋純解析函式。禁 import sw_core（測部署後系統）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|\([A-Za-z]|[=>])")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def find_marker(pane_text: str, marker: str) -> bool:
    return marker in strip_ansi(pane_text)


def parse_usbipd_list(output: str) -> list[str]:
    """回傳 Connected 段的 BUSID 清單（序列裝置歸屬由 config 的 serial↔busid 對照另行判定）。"""
    busids: list[str] = []
    in_connected = False
    for line in output.splitlines():
        if line.startswith("Connected:"):
            in_connected = True
            continue
        if line.startswith("Persisted:"):
            break
        if in_connected:
            m = re.match(r"^(\d+-\d+)\s", line)
            if m:
                busids.append(m.group(1))
    return busids


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class SwCli:
    """已安裝 serialwrap CLI 的薄包裝；stdout 嘗試 JSON 解析。"""

    def run(self, *args: str, timeout: float = 30.0) -> dict:
        cp = _run(["serialwrap", *args], timeout=timeout)
        out = cp.stdout.strip()
        try:
            data = json.loads(out) if out else {}
        except json.JSONDecodeError:
            data = {"_raw": out}
        data["_rc"] = cp.returncode
        data["_stderr"] = cp.stderr.strip()
        return data

    def sessions(self) -> list[dict]:
        return self.run("session", "list").get("sessions") or []

    def session(self, com: str) -> dict:
        for s in self.sessions():
            if s.get("com") == com:
                return s
        return {}

    def submit_and_wait(self, com: str, cmd: str, *, cmd_timeout: float = 12.0,
                        settle_s: float = 1.5, poll_s: float = 0.5) -> dict:
        """cmd submit → 輪詢 cmd status 到 done（坑：submit 後立刻讀有 line race）。"""
        sub = self.run("cmd", "submit", "--selector", com, "--cmd", cmd,
                       "--cmd-timeout", str(cmd_timeout))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            return {"_error": "submit 未回 cmd_id", **sub}
        deadline = time.monotonic() + cmd_timeout + 10
        time.sleep(settle_s)
        while time.monotonic() < deadline:
            st = self.run("cmd", "status", "--cmd-id", str(cmd_id))
            command = st.get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                return command
            time.sleep(poll_s)
        return {"_error": "cmd status 輪詢逾時", "cmd_id": cmd_id}

    def wait_state(self, com: str, want: str, *, timeout_s: float, poll_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.session(com).get("state") == want:
                return True
            time.sleep(poll_s)
        return False


class TmuxCtl:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def name(self, tag: str) -> str:
        return f"{self._prefix}-{tag}-{os.getpid()}"

    def new(self, session: str, command: str) -> None:
        _run(["tmux", "new-session", "-d", "-s", session, command])

    def send(self, session: str, text: str, *, enter: bool = True) -> None:
        _run(["tmux", "send-keys", "-t", session, "-l", "--", text])
        if enter:
            _run(["tmux", "send-keys", "-t", session, "Enter"])

    def send_key(self, session: str, key: str) -> None:  # 例："Tab"、"Enter"
        _run(["tmux", "send-keys", "-t", session, key])

    def capture(self, session: str) -> str:
        return _run(["tmux", "capture-pane", "-p", "-t", session]).stdout

    def kill(self, session: str) -> None:
        _run(["tmux", "kill-session", "-t", session])


class Usbipd:
    def __init__(self, exe: str) -> None:
        self._exe = exe

    def list_busids(self) -> list[str]:
        return parse_usbipd_list(_run([self._exe, "list"]).stdout)

    def detach(self, busid: str) -> None:
        _run([self._exe, "detach", "-b", busid])

    def attach(self, busid: str) -> None:
        _run([self._exe, "attach", "-w", "-b", busid], timeout=60)


class Systemd:
    UNIT = "serialwrap"

    def restart(self) -> int:
        return _run(["sudo", "-n", "systemctl", "restart", self.UNIT]).returncode

    def main_pid(self) -> int:
        out = _run(["systemctl", "show", "-p", "MainPID", self.UNIT]).stdout
        try:
            return int(out.strip().split("=", 1)[1])
        except (IndexError, ValueError):
            return 0
