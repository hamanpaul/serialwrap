"""DUT 控制檔通道（dut/control_file.py）失敗測試（P2-T4，TDD 紅燈）。

ControlFileChannel（DUT 端）與 DutControlClient（bridge 端）以檔案目錄往返，DUT 程序以
``--control-dir`` 啟動。產品模組尚未實作，因此相關 import 一律在函式內進行。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.process_rpc

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools" / "uart_stdio_bridge" / "src"
DEFAULT_BANNER = b"boot done\r\nroot@prplOS:/# "


def _env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), existing) if p)
    return env


class _Collector:
    """背景排空子程序 stdout，供測試以 data 屬性讀取目前累積的位元組。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            chunk = os.read(self._fd, 4096)
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)

    @property
    def data(self) -> bytes:
        with self._lock:
            return bytes(self._buf)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _spawn_dut(tmp_path: Path, control_dir: Path):
    """回傳 (proc, stdout 收集器, stderr 收集器)；兩個管道皆背景排空，避免 DUT 寫滿管道而阻塞。"""
    from uart_stdio_bridge.dut.model import TargetConfig

    target_json = tmp_path / "target.json"
    cfg = TargetConfig.from_yaml({"noise": {"enabled": False}})
    target_json.write_text(json.dumps(cfg.to_json(), ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uart_stdio_bridge.dut",
            "--target-json",
            str(target_json),
            "--control-dir",
            str(control_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    return proc, _Collector(proc.stdout.fileno()), _Collector(proc.stderr.fileno())


def _shutdown(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_roundtrip_emit_pause_reboot_ack_generation(tmp_path):
    from uart_stdio_bridge.dut.control_file import DutControlClient

    control_dir = tmp_path / "ctl"
    control_dir.mkdir()
    proc, out, _err = _spawn_dut(tmp_path, control_dir)
    try:
        assert _wait_for(lambda: out.data.startswith(DEFAULT_BANNER)), out.data
        client = DutControlClient(control_dir)

        payload = b"\x00CTRL\xff"
        ack = client.send(
            {"action": "emit", "payload_b64": base64.b64encode(payload).decode("ascii")},
            timeout_s=10,
        )
        assert ack["ok"] is True
        assert _wait_for(lambda: payload in out.data)

        ack = client.send({"action": "pause_responding", "duration_ms": 100}, timeout_s=10)
        assert ack["ok"] is True
        assert ack["generation"] == 1

        ack = client.send(
            {"action": "logical_reboot", "banner_delay_ms": 50, "pre_text": "REBOOT\r\n"},
            timeout_s=10,
        )
        assert ack["ok"] is True
        assert ack["generation"] == 2
        assert _wait_for(lambda: b"REBOOT\r\n" in out.data)
    finally:
        _shutdown(proc)


def test_send_raises_timeout_when_dut_absent(tmp_path):
    from uart_stdio_bridge.dut.control_file import DutControlClient

    client = DutControlClient(tmp_path)
    with pytest.raises(TimeoutError):
        client.send({"action": "stdin_pause"}, timeout_s=0.5)


def test_poll_skips_partial_line(tmp_path):
    from uart_stdio_bridge.dut.control_file import ControlFileChannel

    channel = ControlFileChannel(tmp_path)
    control = tmp_path / "control.ndjson"
    control.write_bytes(b'{"id":"x1","action":"stdin_pause"')
    assert channel.poll() == []
    with open(control, "ab") as handle:
        handle.write(b"}\n")
    assert channel.poll() == [{"id": "x1", "action": "stdin_pause"}]


def test_reused_control_dir_with_old_ack_and_no_dut_times_out(tmp_path):
    from uart_stdio_bridge.dut.control_file import DutControlClient

    # 模擬先前的執行留下的舊 ack.ndjson，包含 dc-1
    ack_file = tmp_path / "ack.ndjson"
    ack_file.write_text('{"id":"dc-1","ok":true,"generation":1,"t_ms":0}\n', encoding="utf-8")

    client = DutControlClient(tmp_path)
    with pytest.raises(TimeoutError):
        client.send({"action": "logical_reboot"}, timeout_s=0.3)


def test_reused_control_dir_channel_does_not_replay_old_commands(tmp_path):
    from uart_stdio_bridge.dut.control_file import ControlFileChannel

    # 模擬先前的執行留下的舊 control.ndjson
    control = tmp_path / "control.ndjson"
    control.write_bytes(b'{"id":"dc-old","action":"logical_reboot"}\n')

    channel = ControlFileChannel(tmp_path)
    assert channel.poll() == []

    with open(control, "ab") as handle:
        handle.write(b'{"id":"dc-new","action":"stdin_pause"}\n')
    assert channel.poll() == [{"id": "dc-new", "action": "stdin_pause"}]
