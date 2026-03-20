"""Daemon 生命週期管理。

負責啟動 serialwrapd、建立暫時目錄、產生 profile YAML、
以及清理所有資源。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from .fake_target import FakeTarget, TargetConfig

ROOT_DIR = pathlib.Path(__file__).resolve().parents[2]
SERIALWRAP = str(ROOT_DIR / "serialwrap")
SERIALWRAPD = str(ROOT_DIR / "serialwrapd.py")


@dataclass
class HarnessConfig:
    """Harness 組態，從 YAML test case 解析。"""
    target_config: TargetConfig = field(default_factory=TargetConfig)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    com: str = "COM0"
    alias: str = "func-test"
    env_files: dict[str, str] = field(default_factory=dict)  # filename -> content


class DaemonHarness:
    """管理一次功能測試的完整 daemon 環境。

    自動建立：
    - 暫時目錄（profile、state、run、by-id）
    - FakeTarget PTY
    - serialwrapd daemon 程序

    使用方式::

        harness = DaemonHarness(config)
        harness.start()
        # ... 執行測試 ...
        harness.stop()
    """

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._root: pathlib.Path | None = None
        self._fake_target: FakeTarget | None = None
        self._daemon: subprocess.Popen[str] | None = None
        self._env: dict[str, str] = {}
        self._socket_path: str = ""

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def env(self) -> dict[str, str]:
        return dict(self._env)

    @property
    def fake_target(self) -> FakeTarget | None:
        return self._fake_target

    @property
    def root(self) -> pathlib.Path:
        assert self._root is not None
        return self._root

    def start(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="sw-func-")
        self._root = pathlib.Path(self._tmpdir.name)

        by_id_dir = self._root / "by-id"
        profile_dir = self._root / "profiles"
        by_id_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # 啟動 FakeTarget
        self._fake_target = FakeTarget(self.config.target_config)
        self._fake_target.start()

        # 建立 symlink（模擬 /dev/serial/by-id/）
        link_path = by_id_dir / "fake-uart0"
        os.symlink(self._fake_target.slave_path, link_path)

        # 產生 profile YAML
        profile_yaml = self._build_profile_yaml(str(link_path))
        (profile_dir / "func-test.yaml").write_text(profile_yaml, encoding="utf-8")

        # 寫入 env 檔（放在 profile 目錄，讓 env_file 相對路徑解析正確）
        for fname, content in self.config.env_files.items():
            (profile_dir / fname).write_text(content, encoding="utf-8")

        # 準備環境變數
        self._env = os.environ.copy()
        self._env["SERIALWRAP_STATE_DIR"] = str(self._root / "state")
        self._env["SERIALWRAP_RUN_DIR"] = str(self._root / "run")
        self._env["SERIALWRAP_BY_ID_DIR"] = str(by_id_dir)
        self._env["SERIALWRAP_BY_PATH_DIR"] = str(self._root / "by-path")

        self._socket_path = str(self._root / "run" / "serialwrapd.sock")
        lock_path = str(self._root / "run" / "serialwrapd.lock")

        # 啟動 daemon
        self._daemon = subprocess.Popen(
            [
                os.environ.get("PYTHON", "python3"),
                SERIALWRAPD,
                "--profile-dir", str(profile_dir),
                "--socket", self._socket_path,
                "--lock", lock_path,
            ],
            env=self._env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def stop(self) -> None:
        if self._daemon is not None:
            # 嘗試優雅停止
            try:
                subprocess.run(
                    [SERIALWRAP, "--socket", self._socket_path, "daemon", "stop"],
                    env=self._env, timeout=3.0,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            if self._daemon.poll() is None:
                self._daemon.terminate()
                try:
                    self._daemon.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self._daemon.kill()
            self._daemon = None

        if self._fake_target is not None:
            self._fake_target.stop()
            self._fake_target = None

        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def _build_profile_yaml(self, link_path: str) -> str:
        p = self.config.profile_overrides
        prompt_regex = p.get("prompt_regex", '(?m)^root@prplOS:.*# ')
        ready_probe = p.get("ready_probe", 'echo __READY__${nonce}')
        timeout_s = p.get("timeout_s", 10)
        platform = self.config.target_config.platform

        extra_lines: list[str] = []

        login_regex = p.get("login_regex")
        if login_regex:
            extra_lines.append(f"    login_regex: '{login_regex}'")
            extra_lines.append(f"    password_regex: '{p.get('password_regex', '(?i)password')}'")
            extra_lines.append(f"    user_env: {p.get('user_env', 'USER')}")
            extra_lines.append(f"    pass_env: {p.get('pass_env', 'PASS')}")
            env_file = p.get("env_file")
            if env_file:
                extra_lines.append(f"    env_file: '{env_file}'")

        post_login_cmd = p.get("post_login_cmd")
        if post_login_cmd:
            extra_lines.append(f"    post_login_cmd: '{post_login_cmd}'")

        extra_block = "\n".join(extra_lines)
        if extra_block:
            extra_block = "\n" + extra_block

        return f"""profiles:
  func-test-template:
    platform: {platform}
    prompt_regex: '{prompt_regex}'
    ready_probe: '{ready_probe}'
    timeout_s: {timeout_s}{extra_block}
    uart:
      baud: 115200
      data_bits: 8
      parity: N
      stop_bits: 1
      flow_control: rtscts
      xonxoff: false

targets:
  - act_no: 1
    com: {self.config.com}
    alias: {self.config.alias}
    profile: func-test-template
    device_by_id: {link_path}
"""
