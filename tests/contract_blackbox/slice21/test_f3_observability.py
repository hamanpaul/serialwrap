"""F3 可觀測性（Slice 2.1 黑箱，承接 f3-* 案例）：錯誤碼、selector 回報與日誌尾端。"""
import secrets

import pytest

pytestmark = pytest.mark.cap_posix_pty


def test_fail_error_code(shell_daemon):
    r = shell_daemon.sw("session", "attach", "--selector", "NOSUCH")
    assert r["_rc"] == 2
    assert r["error_code"] == "SESSION_NOT_FOUND"
    assert r["_stderr"]


def test_device_error_names_selector(shell_daemon):
    r = shell_daemon.sw("session", "attach", "--selector", "NOSUCH")
    assert "NOSUCH" in r["_stderr"]


def test_cmd_fail_observable(shell_daemon):
    r = shell_daemon.sw("cmd", "submit", "--selector", "NOSUCH", "--cmd", "echo x")
    assert r["error_code"] == "SESSION_NOT_FOUND"
    assert r["selector"] == "NOSUCH"


def test_log_tail_latest(shell_daemon):
    marker = f"M_{secrets.token_hex(4)}"
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", f"echo {marker}")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
    r = shell_daemon.sw("log", "tail-text", "--selector", "COM0")
    assert any(marker in line for line in r["lines"]), f"log tail-text 最新行應含 {marker}"
