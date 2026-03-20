"""可組態的 PTY fake target。

根據 YAML test case 的 ``target`` 區塊產生行為，
包括開機 banner、指令回應、背景雜訊以及自訂覆寫。
"""
from __future__ import annotations

import os
import pty
import re
import select
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CommandOverride:
    """單一指令的自訂回應。"""
    response: str = ""
    delay_ms: int = 0
    interactive: bool = False


@dataclass
class LoginStep:
    """登入序列中的一個步驟。"""
    expect: str      # 等待從 host 端收到的字串（使用者名稱或密碼）
    respond: str     # 收到後回應的字串（下一個提示或 shell prompt）


@dataclass
class TargetConfig:
    """Fake target 的全部組態。"""
    platform: str = "prpl"
    boot_banner: str = "boot done\r\nroot@prplOS:/# "
    noise_enabled: bool = True
    noise_interval_ms: int = 50
    noise_pattern: str = "KDBG:tick:{tick}\r\n"
    default_response: str = "EXEC:{cmd}\r\nRESULT:{cmd}:OK\r\nroot@prplOS:/# "
    overrides: dict[str, CommandOverride] = field(default_factory=dict)
    login_steps: list[LoginStep] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, data: dict[str, Any] | None) -> TargetConfig:
        if data is None:
            return cls()
        noise = data.get("noise") or {}
        raw_overrides = (data.get("commands") or {}).get("overrides") or {}
        overrides: dict[str, CommandOverride] = {}
        for cmd_str, ov in raw_overrides.items():
            if isinstance(ov, dict):
                overrides[cmd_str] = CommandOverride(
                    response=ov.get("response", ""),
                    delay_ms=ov.get("delay_ms", 0),
                    interactive=ov.get("interactive", False),
                )
        raw_login = data.get("login_steps") or []
        login_steps = [
            LoginStep(expect=s["expect"], respond=s["respond"])
            for s in raw_login if isinstance(s, dict)
        ]
        return cls(
            platform=data.get("platform", "prpl"),
            boot_banner=data.get("boot_banner", cls.boot_banner),
            noise_enabled=noise.get("enabled", True),
            noise_interval_ms=noise.get("interval_ms", 50),
            noise_pattern=noise.get("pattern", cls.noise_pattern),
            default_response=(data.get("commands") or {}).get("default", cls.default_response),
            overrides=overrides,
            login_steps=login_steps,
        )


class FakeTarget:
    """PTY 假裝置，模擬 UART target 的行為。

    使用方式::

        cfg = TargetConfig.from_yaml(yaml_target_section)
        target = FakeTarget(cfg)
        target.start()
        # ... 使用 target.slave_path 給 daemon ...
        target.stop()
    """

    def __init__(self, config: TargetConfig | None = None) -> None:
        self.config = config or TargetConfig()
        self.master_fd, self.slave_fd = pty.openpty()
        self._configure_raw(self.slave_fd)
        self.slave_path: str = os.ttyname(self.slave_fd)
        self._stop = threading.Event()
        self._pause = threading.Event()  # 模擬 target 停止回應
        self._pause.set()  # 預設為「正在回應」
        self._tick = 0
        self._current_prompt: str | None = None  # 動態 prompt，None 時使用預設
        self._cmd_thread = threading.Thread(target=self._cmd_loop, daemon=True, name="fake-cmd")
        self._noise_thread = threading.Thread(target=self._noise_loop, daemon=True, name="fake-noise")

    @staticmethod
    def _configure_raw(fd: int) -> None:
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0           # iflag
        attrs[1] = 0           # oflag
        attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8  # cflag
        attrs[3] = 0           # lflag
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    # -- 公開 API --

    def start(self) -> None:
        self._write(self.config.boot_banner.encode("utf-8"))
        self._cmd_thread.start()
        if self.config.noise_enabled:
            self._noise_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()  # 解除任何 pause
        for th in (self._cmd_thread, self._noise_thread):
            if th.is_alive():
                th.join(timeout=2.0)
        for fd in (self.master_fd, self.slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def pause_responding(self, duration_s: float) -> None:
        """暫停回應（模擬 target hang）。"""
        self._pause.clear()

        def _resume() -> None:
            time.sleep(duration_s)
            self._pause.set()

        threading.Thread(target=_resume, daemon=True).start()

    # -- 內部迴圈 --

    def _write(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def _noise_loop(self) -> None:
        interval = self.config.noise_interval_ms / 1000.0
        while not self._stop.is_set():
            if self._pause.is_set() is False and not self._pause.wait(timeout=0.1):
                continue
            self._tick += 1
            msg = self.config.noise_pattern.replace("{tick}", str(self._tick))
            self._write(msg.encode("utf-8"))
            self._stop.wait(timeout=interval)

    def _cmd_loop(self) -> None:
        buf = b""
        # 登入序列（如果有設定的話）
        login_idx = 0
        in_login = len(self.config.login_steps) > 0

        while not self._stop.is_set():
            try:
                rlist, _, _ = select.select([self.master_fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if self.master_fd not in rlist:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except (BlockingIOError, OSError):
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                cmd = line_bytes.replace(b"\r", b"").decode("utf-8", errors="replace").strip()

                # 登入階段：比對 expect，回應 respond
                if in_login and login_idx < len(self.config.login_steps):
                    step = self.config.login_steps[login_idx]
                    if step.expect in cmd:
                        self._write(step.respond.encode("utf-8"))
                        login_idx += 1
                        if login_idx >= len(self.config.login_steps):
                            in_login = False
                    else:
                        # 未知輸入：重新發送目前的登入提示
                        if login_idx == 0:
                            # 還在等 username，重送 boot_banner 尾端
                            banner = self.config.boot_banner
                            last = banner.split("\r\n")[-1] if banner else "Login: "
                            self._write(f"\r\n{last}".encode("utf-8"))
                        else:
                            # 已過第一步但當前步驟未通過，重送前一步的 respond 尾端
                            prev = self.config.login_steps[login_idx - 1].respond
                            last = prev.split("\r\n")[-1] if prev else ""
                            if last:
                                self._write(f"\r\n{last}".encode("utf-8"))
                    continue

                if not cmd:
                    self._write(self._prompt_bytes())
                    continue
                self._handle_command(cmd)

    def _prompt_bytes(self) -> bytes:
        if self._current_prompt is not None:
            return self._current_prompt.encode("utf-8")
        # 若有 login_steps，取最後一步回應的最後一行作為 prompt
        if self.config.login_steps:
            last = self.config.login_steps[-1].respond
            lines = last.split("\r\n")
            prompt = lines[-1] if lines else "# "
            return prompt.encode("utf-8")
        # 從 boot_banner 取最後一行作為 prompt
        lines = self.config.boot_banner.split("\r\n")
        prompt = lines[-1] if lines else "# "
        return prompt.encode("utf-8")

    def _handle_command(self, cmd: str) -> None:
        # 檢查是否在 pause 中
        if not self._pause.is_set():
            self._pause.wait(timeout=30.0)

        override = self.config.overrides.get(cmd)
        if override is not None:
            if override.delay_ms > 0:
                time.sleep(override.delay_ms / 1000.0)
            if override.interactive:
                return  # interactive 命令不回應 prompt
            resp = override.response
        else:
            resp = self.config.default_response.replace("{cmd}", cmd)

        self._write(resp.encode("utf-8"))

        # 更新動態 prompt（取回應最後一行）
        lines = resp.split("\r\n")
        last_line = lines[-1] if lines else ""
        if last_line.strip():
            self._current_prompt = last_line
