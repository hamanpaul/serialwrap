"""stdin/stdout DUT 模擬器 CLI（python -m uart_stdio_bridge.dut）失敗測試（P2-T4，TDD 紅燈）。

以子程序執行 DUT CLI。產品模組尚未實作，因此 DUT 套件相關 import 一律在函式內進行。
"""
from __future__ import annotations

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


def _cli_args(*args: str) -> list[str]:
    return [sys.executable, "-m", "uart_stdio_bridge.dut", *args]


def _cli(args: list[str], *, stdin_data: bytes = b"", timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        _cli_args(*args),
        input=stdin_data,
        capture_output=True,
        timeout=timeout,
        env=_env(),
    )


def _target_json(tmp_path: Path, yaml_block: dict | None = None) -> Path:
    from uart_stdio_bridge.dut.model import TargetConfig

    path = tmp_path / "target.json"
    path.write_text(json.dumps(TargetConfig.from_yaml(yaml_block).to_json(), ensure_ascii=False), encoding="utf-8")
    return path


def test_stdout_only_dut_bytes(tmp_path):
    target_json = _target_json(tmp_path)
    proc = _cli(["--target-json", str(target_json)], stdin_data=b"")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(DEFAULT_BANNER)
    for line in proc.stderr.splitlines():
        json.loads(line)


def test_binary_stdin_not_translated():
    data = bytes(range(256)) * 64
    proc = _cli(["--mode", "echo"], stdin_data=data)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == data


def test_echo_stderr_flood_does_not_block_stdout():
    data = bytes(range(256)) * 4096  # 1 MiB
    proc = subprocess.Popen(
        _cli_args("--mode", "echo", "--stderr-flood-bytes", "20000000"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    err = bytearray()

    def drain_stderr() -> None:
        for chunk in iter(lambda: proc.stderr.read(65536), b""):
            err.extend(chunk)

    def feed_stdin() -> None:
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except BrokenPipeError:  # 子程序提前結束時由主斷言回報，不在此重複
            pass

    drain = threading.Thread(target=drain_stderr, daemon=True)
    writer = threading.Thread(target=feed_stdin, daemon=True)
    start = time.monotonic()
    drain.start()
    writer.start()
    out = proc.stdout.read()
    rc = proc.wait(timeout=60)
    elapsed = time.monotonic() - start
    writer.join(timeout=30)
    drain.join(timeout=30)

    assert rc == 0
    assert out == data
    assert elapsed < 30
    assert len(err) >= 20_000_000


def test_echo_exit_after_bytes():
    proc = _cli(["--mode", "echo", "--exit-after-bytes", "1000", "--exit-code", "7"], stdin_data=b"x" * 4096)
    assert proc.returncode == 7
    assert proc.stdout == b"x" * 1000


def test_echo_close_stdout_after_bytes():
    proc = subprocess.Popen(
        _cli_args("--mode", "echo", "--close-stdout-after-bytes", "10"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    got: list[bytes] = []
    try:
        reader = threading.Thread(target=lambda: got.append(proc.stdout.read()), daemon=True)
        reader.start()
        proc.stdin.write(b"A" * 100)
        proc.stdin.flush()
        reader.join(timeout=30)
        assert not reader.is_alive(), "stdout 在讀到 10 bytes 後應 EOF"
        assert got == [b"A" * 10]
        assert proc.poll() is None, "stdout 關閉後程序應仍存活，直到 stdin 關閉"
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def test_bad_config_exit2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    proc = _cli(["--target-json", str(bad)], stdin_data=b"")
    assert proc.returncode == 2
    assert proc.stdout == b""


def test_stdin_eof_exit0(tmp_path):
    target_json = _target_json(tmp_path)
    proc = _cli(["--target-json", str(target_json)], stdin_data=b"")
    assert proc.returncode == 0, proc.stderr


def test_cli_via_uart_stdio_bridge_dut_subcommand(tmp_path):
    target_json = _target_json(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "uart_stdio_bridge", "dut", "--target-json", str(target_json)],
        input=b"",
        capture_output=True,
        timeout=15,
        env=_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(DEFAULT_BANNER)


def test_cli_rejects_unknown_arguments(tmp_path):
    target_json = _target_json(tmp_path)
    proc = _cli(["--target-json", str(target_json), "--contorl-dir", "/tmp"], stdin_data=b"")
    assert proc.returncode == 2
    assert proc.stdout == b""
    line = proc.stderr.splitlines()[0]
    data = json.loads(line)
    assert "error" in data


def test_cli_rejects_incompatible_mode_arguments(tmp_path):
    target_json = _target_json(tmp_path)
    # dut 模式帶 echo 參數
    proc = _cli(["--target-json", str(target_json), "--stderr-flood-bytes", "10"], stdin_data=b"")
    assert proc.returncode == 2
    # echo 模式帶 dut 參數
    proc2 = _cli(["--mode", "echo", "--target-json", str(target_json)], stdin_data=b"")
    assert proc2.returncode == 2


def test_cli_stdin_pause_backpressure(tmp_path):
    from uart_stdio_bridge.dut.plan import ScenarioPlan, Trigger

    target_json = _target_json(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan = ScenarioPlan(
        seed=1,
        triggers=(
            Trigger(
                id="stop",
                line_contains="STOP",
                action={"kind": "stop_stdin", "resume_after_ms": None},
            ),
        ),
    )
    plan_path.write_text(json.dumps(plan.to_json(), ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen(
        _cli_args("--target-json", str(target_json), "--plan-json", str(plan_path)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    out_buf = bytearray()

    def reader():
        while True:
            chunk = proc.stdout.read(1024)
            if not chunk:
                break
            out_buf.extend(chunk)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        # 等待開機 banner
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and DEFAULT_BANNER not in out_buf:
            time.sleep(0.05)
        assert DEFAULT_BANNER in out_buf

        # 送出觸發 stop_stdin 的命令
        proc.stdin.write(b"STOP\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and b"EXEC:STOP" not in out_buf:
            time.sleep(0.05)
        assert b"EXEC:STOP" in out_buf

        # 暫停期間送入後續命令 AFTER\n
        proc.stdin.write(b"AFTER\n")
        proc.stdin.flush()
        time.sleep(0.5)

        # AFTER 不得在暫停期間被回應
        assert b"EXEC:AFTER" not in out_buf
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
